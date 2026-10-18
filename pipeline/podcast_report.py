"""Run one podcast episode (download -> transcribe -> write) into a work dir.

Publishes nothing and touches no DB, R2 or feed: the ``podcast-report dry-run``
CLI is a thin wrapper, and a later phase reuses ``run_dry_run`` as its "one
episode into a work dir" step.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

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


class ItemNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class DryRunResult:
    work_dir: Path
    transcript_path: Path
    script_path: Path
    summary_path: Path | None
    script_words: int
    piece_count: int
    input_tokens: int
    output_tokens: int
    download_s: float
    transcribe_s: float
    write_s: float


def default_work_dir(guid: str) -> Path:
    return DRY_RUN_ROOT / hashlib.sha256(guid.encode("utf-8")).hexdigest()[:16]


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


def _manifest_totals(work_dir: Path) -> tuple[int, int, int]:
    manifest = json.loads((work_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    pieces = manifest["pieces"]

    def total(key: str) -> int:
        return sum(p[key] for p in pieces if isinstance(p.get(key), int))

    return len(pieces), total("input_tokens"), total("output_tokens")


def run_dry_run(
    source: PodcastSource,
    item: PodcastItem,
    work_dir: Path,
    *,
    carry_context: bool = True,
    transcriber_factory: Callable[[], Any] | None = None,
) -> DryRunResult:
    """Download, transcribe and write one episode into ``work_dir``.

    ``transcriber_factory`` defaults to ``GeminiPieceTranscriber``; the instance
    is closed afterwards (``close()`` when it has one).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    persist_raw_output(
        work_dir / ITEM_NAME,
        json.dumps(item_metadata(source, item), indent=2, ensure_ascii=False),
    )

    t0 = time.monotonic()
    audio_path = work_dir / AUDIO_NAME
    download_enclosure(item.enclosure_url, audio_path)
    t1 = time.monotonic()

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

    piece_count, in_tok, out_tok = _manifest_totals(work_dir)
    return DryRunResult(
        work_dir=work_dir,
        transcript_path=work_dir / "transcript.txt",
        script_path=script_path,
        summary_path=summary_path,
        script_words=len(report.script.split()),
        piece_count=piece_count,
        input_tokens=in_tok,
        output_tokens=out_tok,
        download_s=t1 - t0,
        transcribe_s=t2 - t1,
        write_s=t3 - t2,
    )
