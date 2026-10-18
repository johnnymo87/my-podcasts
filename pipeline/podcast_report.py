"""Run one podcast episode (download -> transcribe -> write) into a work dir.

Publishes nothing and touches no DB, R2 or feed: the ``podcast-report dry-run``
CLI is a thin wrapper, and a later phase reuses ``run_episode_to_script`` as
its "one episode into a work dir" step.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import requests

from pipeline import opencode_client
from pipeline.podcast_download import download_enclosure
from pipeline.podcast_feed import PodcastItem, parse_podcast_feed
from pipeline.podcast_transcribe import (
    MANIFEST_NAME,
    GeminiPieceTranscriber,
    transcribe_episode,
)
from pipeline.podcast_writer import write_report
from pipeline.report_engine import persist_raw_output


if TYPE_CHECKING:
    from pipeline.podcast_sources import PodcastSource


DRY_RUN_ROOT = Path("/persist/my-podcasts/podcast-work/dry-run")
AUDIO_NAME = "audio.mp3"
ITEM_NAME = "item.json"
SCRIPT_NAME = "script.txt"
SUMMARY_NAME = "summary.txt"

_RSS_TIMEOUT_S = 30.0


_SERVE_HEALTH_TIMEOUT_S = 5.0


class ItemNotFoundError(LookupError):
    pass


class OpencodeUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class DryRunResult:
    work_dir: Path
    transcript_path: Path
    script_path: Path
    summary_path: Path | None
    script_words: int
    piece_count: int
    # Pieces whose text came from a checkpoint instead of a fresh Gemini call
    # in this run. Zero when the whole transcript was reused.
    pieces_from_checkpoint: int
    # transcript.txt/.json were already complete and matched: no ASR at all.
    transcript_reused: bool
    # Summed from the manifest, so they include work done by an earlier run
    # whenever a checkpoint or the whole transcript was reused.
    input_tokens: int
    output_tokens: int
    download_s: float
    transcribe_s: float
    write_s: float


def default_work_dir(source_key: str, guid: str) -> Path:
    digest = hashlib.sha256(guid.encode("utf-8")).hexdigest()[:16]
    return DRY_RUN_ROOT / source_key / digest


def check_opencode_serve() -> None:
    """Fail fast if the opencode serve the writer needs is not answering.

    The writer is the last, and (up to 900 s) slowest, stage; discovering a dead
    serve only after the paid transcription would waste it.
    """
    base = opencode_client.OPENCODE_URL.rstrip("/")
    try:
        resp = requests.get(f"{base}/global/health", timeout=_SERVE_HEALTH_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OpencodeUnavailableError(
            f"opencode serve at {base} is not answering ({type(exc).__name__}: "
            f"{exc}); the report writer needs it"
        ) from exc


def fetch_feed_xml(url: str) -> str:
    resp = httpx.get(
        url,
        timeout=_RSS_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": "my-podcasts/1.0"},
    )
    resp.raise_for_status()
    return resp.text


def find_item(rss_xml: str, guid: str) -> PodcastItem:
    items = parse_podcast_feed(rss_xml)
    for item in items:
        if item.guid == guid:
            return item
    raise ItemNotFoundError(f"guid {guid!r} not found in feed ({len(items)} items)")


def item_metadata(source: PodcastSource, item: PodcastItem) -> dict[str, Any]:
    return {
        "source": source.key,
        "guid": item.guid,
        "title": item.title,
        "link": item.link,
        "pub_date": item.pub_date.isoformat(),
        "duration_s": item.duration_s,
        "enclosure_url": item.enclosure_url,
        "description": item.description,
    }


def _manifest_stamp(work_dir: Path) -> tuple[int, int] | None:
    """Identity of the manifest file: a fresh transcription replaces it (new
    inode and mtime); a reused transcript leaves it untouched."""
    try:
        st = (work_dir / MANIFEST_NAME).stat()
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_mtime_ns


def _manifest_totals(work_dir: Path) -> tuple[int, int, int, int]:
    manifest = json.loads((work_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    pieces = manifest["pieces"]

    def total(key: str) -> int:
        return sum(p[key] for p in pieces if isinstance(p.get(key), int))

    from_cp = sum(1 for p in pieces if p.get("from_checkpoint") is True)
    return len(pieces), from_cp, total("input_tokens"), total("output_tokens")


# Anything a previous run leaves behind that a different episode must not reuse.
_WORK_ARTIFACTS = (AUDIO_NAME, "transcript.txt", MANIFEST_NAME, "pieces")


def _claim_work_dir(source: PodcastSource, item: PodcastItem, work_dir: Path) -> None:
    """Bind ``work_dir`` to this (source, guid), or refuse. Writes nothing on a
    refusal.

    ``download_enclosure`` and the transcript/checkpoint reuse all trust whatever
    is already on disk, so a work dir reused for another episode would be
    narrated under the wrong title. ``item.json`` is the owner record. Once it
    exists its fields are kept as written: the audio is pinned to the
    ``enclosure_url`` recorded there, and a feed that later hands out different
    tracking params must not make the record disagree with the audio.
    """
    item_path = work_dir / ITEM_NAME
    owner: dict[str, Any] | None = None
    if item_path.exists():
        try:
            loaded = json.loads(item_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"work dir {work_dir} has an unreadable {ITEM_NAME} ({exc!r}); "
                "refusing to reuse it"
            ) from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(
                f"work dir {work_dir} has a malformed {ITEM_NAME}; refusing to reuse it"
            )
        if (loaded.get("source"), loaded.get("guid")) != (source.key, item.guid):
            raise RuntimeError(
                f"work dir belongs to {loaded.get('source')}/{loaded.get('guid')}, "
                f"not {source.key}/{item.guid}; use a different --work-dir"
            )
        owner = loaded
    else:
        leftovers = [n for n in _WORK_ARTIFACTS if (work_dir / n).exists()]
        if leftovers:
            raise RuntimeError(
                f"work dir {work_dir} has no {ITEM_NAME} but contains "
                f"{', '.join(leftovers)}; it is not known to belong to "
                f"{source.key}/{item.guid}. Refusing to reuse it"
            )
    record = owner if owner is not None else item_metadata(source, item)
    record["last_requested_at"] = datetime.now(UTC).isoformat()
    work_dir.mkdir(parents=True, exist_ok=True)
    persist_raw_output(item_path, json.dumps(record, indent=2, ensure_ascii=False))


def run_episode_to_script(
    source: PodcastSource,
    item: PodcastItem,
    work_dir: Path,
    *,
    carry_context: bool = True,
    transcriber_factory: Callable[[], Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> DryRunResult:
    """Download, transcribe and write one episode into ``work_dir``.

    Pure of DB/R2/feed. ``transcriber_factory`` defaults to
    ``GeminiPieceTranscriber``; the instance is closed afterwards (``close()``
    when it has one). ``progress`` receives one line before each stage.
    """
    say = progress or (lambda _msg: None)
    _claim_work_dir(source, item, work_dir)

    say(f"Downloading {item.enclosure_url} ...")
    t0 = time.monotonic()
    audio_path = work_dir / AUDIO_NAME
    download_enclosure(item.enclosure_url, audio_path)
    t1 = time.monotonic()

    say("Transcribing (Gemini, in ~10-minute pieces) ...")
    stamp_before = _manifest_stamp(work_dir)
    transcriber = (transcriber_factory or GeminiPieceTranscriber)()
    try:
        transcript = transcribe_episode(
            audio_path,
            work_dir,
            rss_duration_s=item.duration_s,
            description=item.description,
            hosts=source.hosts,
            transcriber=transcriber,
            carry_context=carry_context,
        )
    finally:
        close = getattr(transcriber, "close", None)
        if callable(close):
            close()
    t2 = time.monotonic()
    transcript_reused = (
        stamp_before is not None and _manifest_stamp(work_dir) == stamp_before
    )

    # A stale script from an earlier run must not survive a failed write stage.
    (work_dir / SCRIPT_NAME).unlink(missing_ok=True)
    (work_dir / SUMMARY_NAME).unlink(missing_ok=True)
    say("Writing report (opencode-serve) ...")
    report = write_report(
        transcript=transcript,
        title=item.title,
        description=item.description,
        hosts=source.hosts,
        work_dir=work_dir,
    )
    t3 = time.monotonic()

    script_path = work_dir / SCRIPT_NAME
    persist_raw_output(script_path, report.script)
    summary_path: Path | None = None
    if report.summary:
        summary_path = work_dir / SUMMARY_NAME
        persist_raw_output(summary_path, report.summary)

    piece_count, from_cp, in_tok, out_tok = _manifest_totals(work_dir)
    return DryRunResult(
        work_dir=work_dir,
        transcript_path=work_dir / "transcript.txt",
        script_path=script_path,
        summary_path=summary_path,
        script_words=len(report.script.split()),
        piece_count=piece_count,
        pieces_from_checkpoint=0 if transcript_reused else from_cp,
        transcript_reused=transcript_reused,
        input_tokens=in_tok,
        output_tokens=out_tok,
        download_s=t1 - t0,
        transcribe_s=t2 - t1,
        write_s=t3 - t2,
    )
