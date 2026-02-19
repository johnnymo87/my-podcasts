from __future__ import annotations

import email
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

import requests

from email_processor.api import EmailProcessor
from pipeline.db import Episode, StateStore
from pipeline.feed import regenerate_and_upload_feed
from pipeline.presets import resolve_preset


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


def _format_episode_title(
    *,
    date_str: str,
    subject_raw: str,
    subject_slug: str,
    feed_slug: str,
) -> str:
    subject = subject_raw.strip() or subject_slug.replace("-", " ")
    if feed_slug == "levine":
        match = re.match(r"^Money Stuff:\s*(.+)$", subject, flags=re.IGNORECASE)
        if match:
            return f"{date_str} - Money Stuff - {match.group(1).strip()}"
        return f"{date_str} - {subject}"
    return subject


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


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _slugify_for_url(text: str) -> str:
    lower = text.lower().strip()
    lower = re.sub(r"[^a-z0-9\s-]", "", lower)
    lower = re.sub(r"\s+", "-", lower)
    return re.sub(r"-+", "-", lower).strip("-")


def _extract_candidate_links(raw_email: bytes) -> list[str]:
    msg = email.message_from_bytes(raw_email)
    chunks: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    chunks.append(
                        payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="replace",
                        )
                    )
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            chunks.append(
                payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="replace",
                )
            )

    text = "\n".join(chunks)
    url_pattern = re.compile(r"https?://[^\s<>'\"]+")
    urls = [u.rstrip(").,>") for u in url_pattern.findall(text)]
    return list(dict.fromkeys(urls))


def _resolve_once(url: str) -> str | None:
    try:
        response = requests.get(url, timeout=10, allow_redirects=False)
    except requests.RequestException:
        return None
    location = response.headers.get("Location")
    if not location:
        return None
    if location.startswith("/"):
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{location}"
    return location


def _extract_levine_source_url(
    raw_email: bytes,
    date_str: str,
    subject_raw: str,
) -> str | None:
    links = _extract_candidate_links(raw_email)

    newsletter_pattern = re.compile(
        r"^https://www\.bloomberg\.com/opinion/newsletters/\d{4}-\d{2}-\d{2}/[^/?#]+",
        flags=re.IGNORECASE,
    )

    for link in links:
        if newsletter_pattern.match(link):
            return _canonicalize_url(link)

    preferred_shortlinks = [
        link
        for link in links
        if link.startswith("https://bloom.bg/")
        or link.startswith("https://links.message.bloomberg.com/")
    ]
    for shortlink in preferred_shortlinks:
        redirected = _resolve_once(shortlink)
        if redirected and newsletter_pattern.match(redirected):
            return _canonicalize_url(redirected)

    match = re.match(r"^Money Stuff:\s*(.+)$", subject_raw.strip(), flags=re.IGNORECASE)
    if match:
        inferred_slug = _slugify_for_url(match.group(1))
        if inferred_slug:
            return (
                "https://www.bloomberg.com/opinion/newsletters/"
                f"{date_str}/{inferred_slug}"
            )
    return None


def _extract_yglesias_source_url(raw_email: bytes) -> str | None:
    msg = email.message_from_bytes(raw_email)
    list_post = msg.get("List-Post", "")

    substack_post_pattern = re.compile(
        r"https://www\.slowboring\.com/p/[^\s<>?#]+",
        flags=re.IGNORECASE,
    )

    if list_post:
        header_match = substack_post_pattern.search(list_post)
        if header_match:
            return _canonicalize_url(header_match.group(0))

    links = _extract_candidate_links(raw_email)
    for link in links:
        if substack_post_pattern.match(link):
            return _canonicalize_url(link)

    redirect_links = [
        link
        for link in links
        if link.startswith("https://substack.com/redirect/")
        or link.startswith("https://www.slowboring.com/action/")
    ]
    for link in redirect_links:
        redirected = _resolve_once(link)
        if redirected and substack_post_pattern.match(redirected):
            return _canonicalize_url(redirected)

    return None


def process_email_bytes(
    *,
    raw_email: bytes,
    source_r2_key: str,
    route_tag: str | None,
    store: StateStore,
    r2_client: R2Client,
) -> ProcessResult:
    parsed = EmailProcessor(raw_email).parse()
    date_str = parsed["date"]
    subject_slug = parsed["subject"]
    subject_raw = parsed.get("subject_raw", "")
    body = parsed["body"]

    preset = resolve_preset(route_tag)
    tts_model = os.getenv("TTS_MODEL", preset.tts_model)
    tts_voice = os.getenv("TTS_VOICE", preset.tts_voice)

    episode_slug = f"{date_str}-{subject_slug}"
    episode_r2_key = f"episodes/{preset.feed_slug}/{episode_slug}.mp3"
    episode_title = _format_episode_title(
        date_str=date_str,
        subject_raw=subject_raw,
        subject_slug=subject_slug,
        feed_slug=preset.feed_slug,
    )
    source_url = None
    if preset.feed_slug == "levine":
        source_url = _extract_levine_source_url(raw_email, date_str, subject_raw)
    elif preset.feed_slug == "yglesias":
        source_url = _extract_yglesias_source_url(raw_email)

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
) -> ProcessResult:
    raw = r2_client.get_object_bytes(key)
    return process_email_bytes(
        raw_email=raw,
        source_r2_key=key,
        route_tag=route_tag,
        store=store,
        r2_client=r2_client,
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
