from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from email_processor.api import EmailProcessor
from pipeline.chinatalk_report import maybe_rewrite_chinatalk
from pipeline.db import Episode, StateStore
from pipeline.feed import regenerate_and_upload_feed
from pipeline.presets import resolve_preset
from pipeline.source_adapters import get_source_adapter
from pipeline.things_happen_extractor import extract_things_happen
from pipeline.yglesias_filter import should_skip


if TYPE_CHECKING:
    from pipeline.r2 import R2Client


@dataclass(frozen=True)
class ProcessResult:
    r2_key: str
    title: str
    route_tag: str | None
    feed_slug: str
    category: str
    preset_name: str
    source_url: str | None
    size_bytes: int
    duration_seconds: int | None
    # True when the email was intentionally dropped (e.g. a Yglesias podcast
    # show-notes post). All other fields hold placeholder/blank values in
    # that case; callers should branch on this rather than reading them.
    skipped: bool = False


def _parse_duration_seconds(mp3_path: Path) -> int | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(mp3_path),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return int(round(float(result.stdout.strip())))
    except ValueError:
        return None


def _cache_levine_links(
    *,
    raw_email_html: str,
    date_str: str,
    feed_slug: str,
    levine_cache_dir: Path,
) -> None:
    """If this is a Levine email, extract Things Happen links and cache to disk."""
    if feed_slug != "levine":
        return
    links = extract_things_happen(raw_email_html)
    if not links:
        return
    links_data = [
        {
            "link_text": link.link_text,
            "raw_url": link.raw_url,
            "headline_context": link.headline_context,
        }
        for link in links
    ]
    levine_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = levine_cache_dir / f"{date_str}.json"
    cache_file.write_text(json.dumps(links_data), encoding="utf-8")


def process_email_bytes(
    *,
    raw_email: bytes,
    source_r2_key: str,
    route_tag: str | None,
    store: StateStore,
    r2_client: R2Client,
    levine_cache_dir: Path = Path("/persist/my-podcasts/levine-cache"),
) -> ProcessResult:
    parsed = EmailProcessor(raw_email).parse()
    date_str = parsed["date"]
    subject_slug = parsed["subject"]
    subject_raw = parsed.get("subject_raw", "")
    body = parsed["body"]

    preset = resolve_preset(route_tag)
    adapter = get_source_adapter(preset.feed_slug)
    tts_model = os.getenv("TTS_MODEL", preset.tts_model)
    tts_voice = os.getenv("TTS_VOICE", preset.tts_voice)

    episode_slug = f"{date_str}-{subject_slug}"
    episode_r2_key = f"episodes/{preset.feed_slug}/{episode_slug}.mp3"
    episode_title = adapter.format_title(
        date_str=date_str,
        subject_raw=subject_raw,
        subject_slug=subject_slug,
    )
    source_url = adapter.extract_source_url(
        raw_email=raw_email,
        date_str=date_str,
        subject_raw=subject_raw,
    )
    body = adapter.clean_body(raw_email=raw_email, body=body)

    # Some Yglesias newsletters are really the show notes + transcript for
    # his Argument podcast with Jerusalem Demsas. We can't usefully turn
    # those into audio (they're already audio), so drop them entirely.
    # Mark the source email as processed so the queue does not retry it.
    if should_skip(body=body, feed_slug=preset.feed_slug):
        print(f"Skipping Yglesias podcast post: {episode_title}")
        store.mark_processed(source_r2_key)
        return ProcessResult(
            r2_key="",
            title=episode_title,
            route_tag=route_tag,
            feed_slug=preset.feed_slug,
            category=preset.category,
            preset_name=preset.name,
            source_url=None,
            size_bytes=0,
            duration_seconds=None,
            skipped=True,
        )

    body, episode_title = maybe_rewrite_chinatalk(
        body=body,
        title=episode_title,
        feed_slug=preset.feed_slug,
        subject_raw=subject_raw,
    )

    with tempfile.TemporaryDirectory(prefix="my-podcasts-") as tmp_dir:
        tmp = Path(tmp_dir)
        input_txt = tmp / f"{episode_slug}.txt"
        output_mp3 = tmp / f"{episode_slug}.mp3"
        input_txt.write_text(body, encoding="utf-8")

        cmd = [
            "ttsjoin",
            "--input-file",
            str(input_txt),
            "--output-file",
            str(output_mp3),
            "--model",
            tts_model,
            "--voice",
            tts_voice,
        ]
        subprocess.run(cmd, check=True)

        r2_client.upload_file(output_mp3, episode_r2_key, content_type="audio/mpeg")
        size_bytes = output_mp3.stat().st_size
        duration_seconds = _parse_duration_seconds(output_mp3)

    episode = Episode(
        id=str(uuid.uuid4()),
        title=episode_title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=preset.feed_slug,
        category=preset.category,
        source_tag=route_tag,
        preset_name=preset.name,
        source_url=source_url,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
    )
    store.insert_episode(episode)
    # Cache Things Happen links if this is a Levine email.
    try:
        raw_html = EmailProcessor(raw_email)._extract_html_part()
        _cache_levine_links(
            raw_email_html=raw_html,
            date_str=date_str,
            feed_slug=preset.feed_slug,
            levine_cache_dir=levine_cache_dir,
        )
    except Exception:
        pass  # Don't let Things Happen extraction failure block the main pipeline.
    store.mark_processed(source_r2_key)
    regenerate_and_upload_feed(store, r2_client)

    return ProcessResult(
        r2_key=episode_r2_key,
        title=episode.title,
        route_tag=route_tag,
        feed_slug=preset.feed_slug,
        category=preset.category,
        preset_name=preset.name,
        source_url=source_url,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
    )


def process_r2_email_key(
    key: str,
    route_tag: str | None,
    store: StateStore,
    r2_client: R2Client,
    levine_cache_dir: Path = Path("/persist/my-podcasts/levine-cache"),
) -> ProcessResult:
    raw = r2_client.get_object_bytes(key)
    return process_email_bytes(
        raw_email=raw,
        source_r2_key=key,
        route_tag=route_tag,
        store=store,
        r2_client=r2_client,
        levine_cache_dir=levine_cache_dir,
    )


def process_local_eml_file(
    path: Path,
    route_tag: str | None,
    store: StateStore,
    r2_client: R2Client,
) -> ProcessResult:
    raw = path.read_bytes()
    source_key = f"local/{path.name}"
    return process_email_bytes(
        raw_email=raw,
        source_r2_key=source_key,
        route_tag=route_tag,
        store=store,
        r2_client=r2_client,
    )
