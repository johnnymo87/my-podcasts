"""Publish one podcast-report episode, safely re-runnable.

``publish_script`` is not idempotent: it uploads audio, inserts the ``episodes``
row, then regenerates and uploads every feed. The audio was rendered earlier,
inside the killable child (``work_dir/episode.mp3``): the parent never waits on
a TTS provider. A failure in the middle leaves some of that done. A retry must
therefore reconcile, not repeat: the episode's ``r2_key`` (frozen on the job row
at admission) says whether the row is already committed, in which case only the
feeds are (re)uploaded.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.feed import regenerate_and_upload_feed
from pipeline.podcast_jobs import item_from_row, podcast_identity
from pipeline.podcast_report import EPISODE_AUDIO_NAME, SUMMARY_NAME
from pipeline.podcast_sources import get_podcast_source
from pipeline.report_engine import persist_raw_output
from pipeline.script_processor import publish_script


if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.podcast_feed import PodcastItem
    from pipeline.podcast_sources import PodcastSource
    from pipeline.r2 import R2Client


SHOW_NOTES_NAME = "show_notes.md"
CATEGORY = "News"
_SUMMARY_FALLBACK = "An AI-generated briefing of this episode."


_RULE_LINE = re.compile(r"\s*([-*_])(?:\s*\1){2,}\s*")


def _clean_summary(text: str) -> str:
    """Drop lines ``extract_summary`` would treat as the end of the section
    (headings and horizontal rules), so a model-written summary cannot
    truncate itself in the stored episode."""
    kept = [
        line
        for line in text.splitlines()
        if not _RULE_LINE.fullmatch(line) and not line.lstrip().startswith("#")
    ]
    return "\n".join(kept).strip()


def _show_notes(source: PodcastSource, item: PodcastItem, work_dir: Path) -> str:
    summary = ""
    summary_path = work_dir / SUMMARY_NAME
    if summary_path.exists():
        summary = _clean_summary(summary_path.read_text(encoding="utf-8"))
    disclosure = (
        f"Unofficial AI-generated briefing of this episode of {source.name}, "
        "produced from the public audio."
    )
    if item.link:
        disclosure += f" [Original episode]({item.link})"
    # The rule ends the summary section: ``extract_summary`` reads up to the
    # next heading or ``---``, and the disclosure is not part of the summary.
    return (
        f"## Episode Summary\n\n{summary or _SUMMARY_FALLBACK}\n\n---\n\n{disclosure}\n"
    )


def publish_podcast_episode(
    row: dict, work_dir: Path, *, store: StateStore, r2_client: R2Client
) -> str:
    """Publish the episode a job row describes. Returns ``"published"`` or
    ``"reconciled"`` (the episode was already committed; only feeds re-sent).

    Raises ``RuntimeError`` on identity drift, before any TTS.

    Note: after a ``reset`` of an already-published episode, the next run
    reconciles and keeps the existing audio (by design); it never re-renders.
    """
    source = get_podcast_source(row["source"])
    item = item_from_row(row)

    ident = podcast_identity(source, item)
    row_date = datetime.fromisoformat(row["pub_date"]).astimezone(UTC)
    row_date_str = row_date.strftime("%Y-%m-%d")
    drift = [
        name
        for name, computed, frozen in (
            ("display_title", ident.display_title, row["display_title"]),
            ("date", ident.date_str, row_date_str),
            ("episode_slug", ident.slug, row["episode_slug"]),
            ("r2_key", ident.r2_key, row["r2_key"]),
        )
        if computed != frozen
    ]
    if drift:
        raise RuntimeError(
            f"identity drift for {row['source']}/{row['guid']}: "
            f"{', '.join(drift)} differ between the admitted row and a "
            "recomputation; refusing to publish"
        )

    if store.get_episode_by_r2_key(row["r2_key"]) is not None:
        regenerate_and_upload_feed(store, r2_client)
        return "reconciled"

    audio = work_dir / EPISODE_AUDIO_NAME
    try:
        audio_size = audio.stat().st_size
    except OSError as exc:
        raise RuntimeError(
            f"no rendered audio at {audio} ({exc!r}); the child renders it, the "
            "parent never does"
        ) from exc
    if audio_size == 0:
        raise RuntimeError(f"rendered audio {audio} is empty")

    show_notes_file = work_dir / SHOW_NOTES_NAME
    persist_raw_output(show_notes_file, _show_notes(source, item, work_dir))
    result = publish_script(
        script_file=work_dir / "script.txt",
        title=row["display_title"],
        feed_slug=source.feed_slug,
        store=store,
        r2_client=r2_client,
        show_notes_file=show_notes_file,
        category=CATEGORY,
        date_str=ident.date_str,
        source_url=item.link or None,
        prerendered_audio=audio,
    )
    if result.r2_key != row["r2_key"]:
        raise RuntimeError(
            f"published r2_key {result.r2_key!r} != frozen {row['r2_key']!r}"
        )
    return "published"
