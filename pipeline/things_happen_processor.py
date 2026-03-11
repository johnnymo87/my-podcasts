from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.article_fetcher import fetch_all_articles
from pipeline.db import Episode
from pipeline.feed import regenerate_and_upload_feed
from pipeline.show_notes import extract_show_notes_articles
from pipeline.summarizer import generate_briefing_script
from pipeline.things_happen_extractor import resolve_redirect_url


if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client


TTS_MODEL = "tts-1-hd"
TTS_VOICE = "nova"
FEED_SLUG = "the-rundown"
CATEGORY = "News"


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


def process_things_happen_job(
    job: dict,
    store: StateStore,
    r2_client: R2Client,
    script_path: Path | None = None,
    work_dir: Path | None = None,
    summary: str | None = None,
) -> None:
    """Process a single pending Things Happen job."""
    job_id = job["id"]
    date_str = job["date_str"]

    if script_path is not None:
        # Use the pre-written script; skip steps 1-3.
        script = script_path.read_text(encoding="utf-8")
    else:
        links_raw = json.loads(job["links_json"])

        # Step 1: Resolve redirect URLs.
        for link in links_raw:
            link["resolved_url"] = resolve_redirect_url(link["raw_url"])

        # Step 2: Fetch articles with fallback chain.
        articles = fetch_all_articles(links_raw, delay_between=3.0)

        # Step 3: Generate briefing script via LLM.
        script = generate_briefing_script(articles, date_str=date_str)

    # Step 4: TTS.
    episode_slug = f"{date_str}-the-rundown"
    episode_r2_key = f"episodes/{FEED_SLUG}/{episode_slug}.mp3"

    with tempfile.TemporaryDirectory(prefix="the-rundown-") as tmp_dir:
        tmp = Path(tmp_dir)
        input_txt = tmp / f"{episode_slug}.txt"
        output_mp3 = tmp / f"{episode_slug}.mp3"
        input_txt.write_text(script, encoding="utf-8")

        cmd = [
            "ttsjoin",
            "--input-file",
            str(input_txt),
            "--output-file",
            str(output_mp3),
            "--model",
            TTS_MODEL,
            "--voice",
            TTS_VOICE,
        ]
        subprocess.run(cmd, check=True)

        r2_client.upload_file(output_mp3, episode_r2_key, content_type="audio/mpeg")
        size_bytes = output_mp3.stat().st_size
        duration_seconds = _parse_duration_seconds(output_mp3)

    # Step 5: Insert episode and mark job complete.
    articles_json_str: str | None = None
    if work_dir is not None:
        articles = extract_show_notes_articles(work_dir)
        if articles:
            articles_json_str = json.dumps(articles)

    episode_title = f"{date_str} - The Rundown"
    episode = Episode(
        id=str(uuid.uuid4()),
        title=episode_title,
        slug=episode_slug,
        pub_date=format_datetime(datetime.now(tz=UTC)),
        r2_key=episode_r2_key,
        feed_slug=FEED_SLUG,
        category=CATEGORY,
        source_tag="the-rundown",
        preset_name="The Rundown",
        source_url=None,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        summary=summary,
        articles_json=articles_json_str,
    )
    store.insert_episode(episode)
    store.mark_things_happen_completed(job_id)
    regenerate_and_upload_feed(store, r2_client)
