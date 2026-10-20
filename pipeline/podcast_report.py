"""Run one podcast episode (download -> transcribe -> write) into a work dir.

Publishes nothing and touches no DB, R2 or feed: the ``podcast-report dry-run``
CLI is a thin wrapper, and a later phase reuses ``run_episode_to_script`` as
its "one episode into a work dir" step.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import requests

from pipeline import opencode_client, tts
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
ACQUISITION_NAME = "acquisition.json"
EPISODE_AUDIO_NAME = "episode.mp3"

_RSS_TIMEOUT_S = 30.0  # per read; see FEED_FETCH_DEADLINE_S for the total
FEED_FETCH_DEADLINE_S = 60  # total wall clock for one feed fetch
FEED_MAX_BYTES = 10 * 1024 * 1024  # the real feed is ~175 KB
FEED_MAX_REDIRECTS = 5


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


class FeedFetchError(httpx.HTTPError):
    """The feed fetch exceeded its total deadline or its size cap.

    An ``httpx.HTTPError``, so every caller that already handles a failed fetch
    (the CLI, the discovery poller) handles this. The message never names the
    URL: it may be signed.
    """


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Module state for the single-flight rule: the one abandoned (timed out, still
# running) fetch thread, if any. Guarded by ``_fetch_lock``.
_fetch_lock = threading.Lock()
_abandoned_fetch: threading.Thread | None = None


def fetch_feed_xml(
    url: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    transport: httpx.BaseTransport | None = None,
    deadline_s: float | None = None,
) -> str:
    """The feed body as text, within one total deadline and ``FEED_MAX_BYTES``.

    The whole exchange is bounded, not just the body loop: connect and header
    waits and redirects count too. Two layers do it:

    * The fetch runs on a daemon worker thread that the caller joins for at most
      ``deadline_s`` (default ``FEED_FETCH_DEADLINE_S``). On timeout the thread
      is abandoned (a GET has no side effects) and ``FeedFetchError`` is raised.
      **Single-flight**: while an abandoned thread is still alive a new fetch
      fails at once with ``FeedFetchError``, so at most one thread ever leaks.
    * Inside the worker redirects are followed by hand (``FEED_MAX_REDIRECTS`` hops,
      the redirect response is closed and its body never read), and the body is
      read through a deadline + size-cap loop. One deadline spans all hops.

    Errors raised in the worker (``httpx.HTTPError`` and friends) propagate to
    the caller unchanged. ``clock``, ``transport`` and ``deadline_s`` are test
    seams. No message names the URL: it may be signed.
    """
    global _abandoned_fetch
    if deadline_s is None:
        deadline_s = FEED_FETCH_DEADLINE_S
    with _fetch_lock:
        if _abandoned_fetch is not None:
            if _abandoned_fetch.is_alive():
                raise FeedFetchError("previous feed fetch still running")
            _abandoned_fetch = None

    cancelled = threading.Event()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["text"] = _fetch_feed_xml(
                url,
                clock=clock,
                transport=transport,
                deadline_s=deadline_s,
                cancelled=cancelled,
            )
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=work, name="podcast-feed-fetch", daemon=True)
    thread.start()
    thread.join(deadline_s)
    if thread.is_alive():
        cancelled.set()  # if it ever wakes, it stops instead of downloading
        with _fetch_lock:
            _abandoned_fetch = thread
        raise FeedFetchError(f"feed fetch exceeded the {deadline_s:g}s deadline")
    if "error" in box:
        raise box["error"]
    return box["text"]


def _fetch_feed_xml(
    url: str,
    *,
    clock: Callable[[], float],
    transport: httpx.BaseTransport | None,
    deadline_s: float,
    cancelled: threading.Event,
) -> str:
    deadline = clock() + deadline_s

    def check() -> None:
        if cancelled.is_set():
            raise FeedFetchError("feed fetch abandoned")
        if clock() > deadline:
            raise FeedFetchError(f"feed fetch exceeded the {deadline_s:g}s deadline")

    with httpx.Client(
        timeout=_RSS_TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": "my-podcasts/1.0"},
        transport=transport,
    ) as client:
        current = httpx.URL(url)
        for hop in range(FEED_MAX_REDIRECTS + 1):
            check()
            resp = client.send(client.build_request("GET", current), stream=True)
            try:
                check()
                if resp.status_code in _REDIRECT_STATUSES:
                    # Never read a redirect body: it is attacker-sized.
                    location = resp.headers.get("location")
                    if not location:
                        raise FeedFetchError("feed redirect without a Location")
                    if hop == FEED_MAX_REDIRECTS:
                        raise FeedFetchError(
                            f"feed followed more than {FEED_MAX_REDIRECTS} redirects"
                        )
                    try:
                        current = resp.url.join(location)
                    except httpx.InvalidURL as exc:
                        raise FeedFetchError("feed redirect to an invalid URL") from exc
                    if current.scheme not in ("http", "https"):
                        raise FeedFetchError(
                            f"feed redirect to a non-http scheme ({current.scheme!r})"
                        )
                    continue
                return _read_body(resp, check)
            finally:
                resp.close()
    raise FeedFetchError("feed redirect loop")  # unreachable; keeps types honest


def _read_body(resp: httpx.Response, check: Callable[[], None]) -> str:
    resp.raise_for_status()
    declared = resp.headers.get("content-length")
    if declared is not None and declared.isdigit():
        if int(declared) > FEED_MAX_BYTES:
            raise FeedFetchError(
                f"feed declares {declared} bytes, over the cap of "
                f"{FEED_MAX_BYTES} bytes"
            )
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > FEED_MAX_BYTES:
            raise FeedFetchError(
                f"feed body exceeded the cap of {FEED_MAX_BYTES} bytes"
            )
        chunks.append(chunk)
        check()
    # Same decoding as ``Response.text``: declared charset, else UTF-8.
    return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


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
    narrated under the wrong title. ``item.json`` is the owner record, written
    once and never touched again: the audio is pinned to the ``enclosure_url``
    recorded there, and a feed that later hands out different tracking params
    must not make the record disagree with the audio. (The URL actually fetched
    is recorded separately in ``acquisition.json``.)
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
    if owner is not None:
        return  # written once, never rewritten
    work_dir.mkdir(parents=True, exist_ok=True)
    persist_raw_output(
        item_path,
        json.dumps(item_metadata(source, item), indent=2, ensure_ascii=False),
    )


def run_episode_to_script(
    source: PodcastSource,
    item: PodcastItem,
    work_dir: Path,
    *,
    carry_context: bool = True,
    transcriber_factory: Callable[[], Any] | None = None,
    progress: Callable[[str], None] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> DryRunResult:
    """Download, transcribe and write one episode into ``work_dir``.

    Pure of DB/R2/feed. ``transcriber_factory`` defaults to
    ``GeminiPieceTranscriber``; the instance is closed afterwards (``close()``
    when it has one). ``progress`` receives one line before each stage;
    ``on_stage`` receives the stage name (``claim``, ``download``,
    ``transcribe``, ``write``) as each begins, so a caller that kills or loses
    this run can say where it was.
    """
    say = progress or (lambda _msg: None)
    stage = on_stage or (lambda _name: None)
    stage("claim")
    _claim_work_dir(source, item, work_dir)

    stage("download")
    # Names the episode, not the URL: audioboom's enclosure links are signed and
    # this line reaches a terminal and any captured log.
    say(f"Downloading {source.key}/{item.guid} ...")
    t0 = time.monotonic()
    audio_path = work_dir / AUDIO_NAME
    fresh = not audio_path.exists() or audio_path.stat().st_size == 0
    size = download_enclosure(item.enclosure_url, audio_path)
    if fresh:
        # The URL actually fetched (it may differ from item.json's by tracking
        # params) and what came back. Written only for a real download.
        persist_raw_output(
            work_dir / ACQUISITION_NAME,
            json.dumps(
                {
                    "url": item.enclosure_url,
                    "downloaded_at": datetime.now(UTC).isoformat(),
                    "bytes": size,
                    "sha256": sha256_file(audio_path),
                },
                indent=2,
            ),
        )
    t1 = time.monotonic()

    stage("transcribe")
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
    stage("write")
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


@dataclass(frozen=True)
class EpisodeAudioResult:
    """What the killable child reports after script *and* audio are done."""

    script_path: Path
    piece_count: int
    input_tokens: int
    output_tokens: int
    audio_path: Path
    audio_sha256: str
    audio_bytes: int
    tts_text_sha256: str


def clear_episode_audio(work_dir: Path) -> None:
    """Remove ``episode.mp3`` and any partial sibling of it.

    Called at the start of every child run, so a stage that fails can never
    leave an earlier attempt's audio lying where a publish would pick it up.
    """
    for path in work_dir.glob(EPISODE_AUDIO_NAME + "*"):
        with contextlib.suppress(OSError):
            path.unlink()


def run_episode_to_audio(
    source: PodcastSource,
    item: PodcastItem,
    work_dir: Path,
    *,
    display_title: str,
    feed_slug: str,
    on_stage: Callable[[str], None] | None = None,
    carry_context: bool = True,
    transcriber_factory: Callable[[], Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> EpisodeAudioResult:
    """The production child's job: ``run_episode_to_script``, then render the
    script to ``work_dir/episode.mp3``.

    Rendering happens here, inside the killable child, because the single-
    threaded consumer must never wait on a TTS provider that can trickle. The
    TTS text is built by the same ``build_tts_text`` ``publish_script`` uses,
    and ``render_episode`` gets the config, feed and episode id it would get
    there, so the audio is what a publish would have rendered (and a retry
    replays the render cache).

    ``episode.mp3`` is written via a tmp file and ``os.replace``, so it exists
    only when complete. It is deleted first thing (see ``clear_episode_audio``)
    and again if the render fails.
    """
    # Before anything else: whatever happens next, old audio must not survive.
    work_dir.mkdir(parents=True, exist_ok=True)
    clear_episode_audio(work_dir)
    stage = on_stage or (lambda _name: None)

    script = run_episode_to_script(
        source,
        item,
        work_dir,
        carry_context=carry_context,
        transcriber_factory=transcriber_factory,
        progress=progress,
        on_stage=on_stage,
    )

    # Imported here: script_processor pulls in the daily processors, which the
    # dry-run CLI path never needs.
    from pipeline.podcast_jobs import podcast_identity
    from pipeline.script_processor import build_tts_text

    stage("render")
    tts_text = build_tts_text(
        script.script_path.read_text(encoding="utf-8"),
        feed_slug=feed_slug,
        title=display_title,
    )
    episode_id = podcast_identity(source, item).slug
    final = work_dir / EPISODE_AUDIO_NAME
    tmp = work_dir / (EPISODE_AUDIO_NAME + ".tmp")
    try:
        tts.render_episode(
            tts_text,
            tts.resolve_render_config(feed_slug),
            tmp,
            feed_slug=feed_slug,
            episode_id=episode_id,
        )
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("TTS render produced an empty or missing mp3")
        os.replace(tmp, final)
    except BaseException:
        clear_episode_audio(work_dir)
        raise
    return EpisodeAudioResult(
        script_path=script.script_path,
        piece_count=script.piece_count,
        input_tokens=script.input_tokens,
        output_tokens=script.output_tokens,
        audio_path=final,
        audio_sha256=sha256_file(final),
        audio_bytes=final.stat().st_size,
        tts_text_sha256=hashlib.sha256(tts_text.encode("utf-8")).hexdigest(),
    )
