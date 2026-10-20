"""Identity and admission for podcast-report jobs.

``podcast_identity`` is the ONE place that computes what an episode is called
and where it lives. Admission freezes the result on the job row; publish
recomputes it from the same row and refuses to proceed on any difference.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.podcast_feed import PodcastItem
from pipeline.podcast_report import item_metadata
from pipeline.script_processor import episode_identity


if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.podcast_sources import PodcastSource


PODCAST_WORK_ROOT = Path("/persist/my-podcasts/podcast-work")


@dataclass(frozen=True)
class PodcastIdentity:
    display_title: str
    date_str: str
    slug: str
    r2_key: str


def podcast_identity(source: PodcastSource, item: PodcastItem) -> PodcastIdentity:
    """Title, UTC date, slug and R2 key for a report on ``item``."""
    date_str = item.pub_date.astimezone(UTC).strftime("%Y-%m-%d")
    display_title = f"Report: {date_str} - {source.title_label} - {item.title}"
    ident = episode_identity(display_title, source.feed_slug, date_str)
    return PodcastIdentity(
        display_title=display_title,
        date_str=date_str,
        slug=ident.slug,
        r2_key=ident.r2_key,
    )


def work_dir_for(source_key: str, guid: str, root: Path = PODCAST_WORK_ROOT) -> Path:
    digest = hashlib.sha256(guid.encode("utf-8")).hexdigest()[:16]
    return root / source_key / digest


def admit_item(
    store: StateStore,
    source: PodcastSource,
    item: PodcastItem,
    *,
    via: str,
    now: datetime,
) -> bool:
    """Freeze ``item``'s identity and metadata as a pending job.

    Returns False if (source, guid) was already admitted (nothing changes).
    Refuses an item dated after ``now``: a clock-skewed or mis-dated feed entry
    would otherwise be published under a date that has not happened yet.
    """
    if item.pub_date > now:
        raise ValueError(
            f"refusing future-dated item {item.guid!r}: "
            f"pub_date {item.pub_date.isoformat()} is after {now.isoformat()}"
        )
    ident = podcast_identity(source, item)
    return store.admit_podcast_episode(
        source=source.key,
        guid=item.guid,
        item_json=json.dumps(item_metadata(source, item)),
        pub_date=item.pub_date.isoformat(),
        display_title=ident.display_title,
        episode_slug=ident.slug,
        r2_key=ident.r2_key,
        admitted_via=via,
        now=now,
    )


def item_from_row(row: dict) -> PodcastItem:
    """Rebuild the admitted ``PodcastItem`` from a job row's ``item_json``."""
    meta = json.loads(row["item_json"])
    return PodcastItem(
        guid=meta["guid"],
        title=meta["title"],
        link=meta["link"],
        pub_date=datetime.fromisoformat(meta["pub_date"]),
        duration_s=meta["duration_s"],
        enclosure_url=meta["enclosure_url"],
        description=meta["description"],
    )
