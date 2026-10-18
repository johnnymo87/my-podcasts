"""Publish one podcast-report episode, safely re-runnable.

``publish_script`` is not idempotent: it renders, uploads audio, inserts the
``episodes`` row, then regenerates and uploads every feed. A failure in the
middle leaves some of that done. A retry must therefore reconcile, not repeat:
the episode's ``r2_key`` (frozen on the job row at admission) says whether the
row is already committed, in which case only the feeds are (re)uploaded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.feed import regenerate_and_upload_feed
from pipeline.podcast_jobs import item_from_row, podcast_identity
from pipeline.podcast_report import SUMMARY_NAME
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


def _show_notes(source: PodcastSource, item: PodcastItem, work_dir: Path) -> str:
    summary = ""
    summary_path = work_dir / SUMMARY_NAME
    if summary_path.exists():
        summary = summary_path.read_text(encoding="utf-8").strip()
    disclosure = (
        f"Unofficial AI-generated briefing of this episode of {source.name}, "
        "produced from the public audio."
    )
    if item.link:
        disclosure += f" Original episode: {item.link}"
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
    )
    if result.r2_key != row["r2_key"]:
        raise RuntimeError(
            f"published r2_key {result.r2_key!r} != frozen {row['r2_key']!r}"
        )
    return "published"
