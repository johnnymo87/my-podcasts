from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

import markdown as md_lib

from pipeline.feed import regenerate_and_upload_feed

if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client


def strip_markdown_for_tts(text: str) -> str:
    """Strip markdown formatting from script text for TTS input.

    Removes headings, horizontal rules, bold/italic markers, and end markers.
    Preserves the actual content text.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Skip horizontal rules
        if re.match(r"^-{3,}$", stripped):
            continue
        # Skip end-of-script marker
        if re.match(r"^\*?\[END OF SCRIPT\]\*?$", stripped):
            continue
        # Strip heading markers
        line = re.sub(r"^#{1,6}\s+", "", line)
        # Strip bold/italic markers (*** first, then **, then *)
        line = re.sub(r"\*{3}(.+?)\*{3}", r"\1", line)
        line = re.sub(r"\*{2}(.+?)\*{2}", r"\1", line)
        line = re.sub(r"\*(.+?)\*", r"\1", line)
        result_lines.append(line)
    return "\n".join(result_lines)


def extract_summary(show_notes_md: str) -> str | None:
    """Extract the Episode Summary section from show notes markdown.

    Looks for a '## Episode Summary' heading and returns the text
    between it and the next heading or horizontal rule.
    """
    lines = show_notes_md.split("\n")
    in_summary = False
    summary_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if re.match(r"^##\s+Episode Summary", stripped, re.IGNORECASE):
            in_summary = True
            continue
        if in_summary:
            # Stop at next heading or horizontal rule
            if re.match(r"^#{1,6}\s+", stripped) or re.match(r"^-{3,}$", stripped):
                break
            summary_lines.append(line)

    text = "\n".join(summary_lines).strip()
    return text if text else None


def render_show_notes_html(show_notes_md: str) -> str:
    """Convert markdown show notes to HTML for feed content:encoded."""
    return md_lib.markdown(
        show_notes_md,
        extensions=["tables", "fenced_code"],
    )


TTS_MODEL = "tts-1-hd"
DEFAULT_VOICE = "nova"
DEFAULT_CATEGORY = "Technology"


@dataclass(frozen=True)
class PublishResult:
    """Result of publishing a script episode."""

    title: str
    r2_key: str
    feed_slug: str
    size_bytes: int
    duration_seconds: int | None


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


def _slugify(title: str) -> str:
    """Convert a title to a URL-safe slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def publish_script(
    *,
    script_file: Path,
    title: str,
    feed_slug: str,
    store: StateStore,
    r2_client: R2Client,
    show_notes_file: Path | None = None,
    voice: str = DEFAULT_VOICE,
    category: str = DEFAULT_CATEGORY,
    date_str: str | None = None,
) -> PublishResult:
    """Publish a podcast episode from a pre-written script file."""
    from pipeline.db import Episode

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    # Read and prepare script text for TTS
    raw_script = script_file.read_text(encoding="utf-8")
    tts_text = strip_markdown_for_tts(raw_script)

    # Process show notes if provided
    summary: str | None = None
    show_notes_html: str | None = None
    if show_notes_file is not None:
        notes_md = show_notes_file.read_text(encoding="utf-8")
        summary = extract_summary(notes_md)
        show_notes_html = render_show_notes_html(notes_md)

    # TTS
    title_slug = _slugify(title)
    episode_slug = f"{date_str}-{title_slug}"
    episode_r2_key = f"episodes/{feed_slug}/{episode_slug}.mp3"

    with tempfile.TemporaryDirectory(prefix="publish-script-") as tmp_dir:
        tmp = Path(tmp_dir)
        input_txt = tmp / f"{episode_slug}.txt"
        output_mp3 = tmp / f"{episode_slug}.mp3"
        input_txt.write_text(tts_text, encoding="utf-8")

        cmd = [
            "ttsjoin",
            "--input-file",
            str(input_txt),
            "--output-file",
            str(output_mp3),
            "--model",
            TTS_MODEL,
            "--voice",
            voice,
        ]
        subprocess.run(cmd, check=True)

        r2_client.upload_file(output_mp3, episode_r2_key, content_type="audio/mpeg")
        size_bytes = output_mp3.stat().st_size
        duration_seconds = _parse_duration_seconds(output_mp3)

    # Insert episode
    episode = Episode(
        id=str(uuid.uuid4()),
        title=title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=feed_slug,
        category=category,
        source_tag=None,
        preset_name="Script",
        source_url=None,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        summary=summary,
        show_notes_html=show_notes_html,
    )
    store.insert_episode(episode)
    regenerate_and_upload_feed(store, r2_client)

    return PublishResult(
        title=title,
        r2_key=episode_r2_key,
        feed_slug=feed_slug,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
    )
