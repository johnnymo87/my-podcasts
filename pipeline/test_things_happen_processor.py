from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from pipeline.things_happen_processor import process_things_happen_job


def test_process_things_happen_job_end_to_end(tmp_path, monkeypatch) -> None:
    """Verify the full orchestration: resolve links, fetch, summarize, TTS, upload."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    links = [
        {
            "link_text": "Blue Owl's battles",
            "raw_url": "https://links.message.bloomberg.com/s/c/FAKE1",
            "headline_context": "How Blue Owl's battles sparked a chill",
        },
    ]

    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "job-1",
            "inbox/raw/abc.eml",
            "2026-02-26",
            json.dumps(links),
            past,
            "pending",
        ),
    )
    store._conn.commit()

    # Mock redirect resolution
    monkeypatch.setattr(
        "requests.head",
        lambda url, **kw: type(
            "R", (), {"url": "https://bloomberg.com/article", "status_code": 200}
        )(),
    )

    # Mock article fetching (skip network)
    from pipeline.article_fetcher import FetchedArticle

    monkeypatch.setattr(
        "pipeline.things_happen_processor.fetch_all_articles",
        lambda links, **kw: [
            FetchedArticle(
                url="https://bloomberg.com/article",
                content="Full article text.",
                source_tier="archive",
            ),
        ],
    )

    # Mock opencode run (LLM)
    monkeypatch.setattr(
        "pipeline.things_happen_processor.generate_briefing_script",
        lambda articles, date_str: "Here is the briefing script for today.",
    )

    # Mock ttsjoin and ffprobe subprocess
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    # Mock feed regeneration (avoid R2 calls)
    monkeypatch.setattr(
        "pipeline.things_happen_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = store.list_due_things_happen()[0]
    process_things_happen_job(job, store, r2_client)

    # Job should be marked completed.
    assert store.list_due_things_happen() == []

    # Episode should be inserted.
    episodes = store.list_episodes(feed_slug="the-rundown")
    assert len(episodes) == 1
    assert "The Rundown" in episodes[0].title
    assert "2026-02-26" in episodes[0].title

    # MP3 should be uploaded to R2.
    r2_client.upload_file.assert_called_once()
    upload_args = r2_client.upload_file.call_args
    # upload_file(local_path, r2_key, content_type=...)
    upload_key = upload_args[0][1]
    assert upload_key.startswith("episodes/the-rundown/")

    store.close()


def test_levine_email_caches_links(tmp_path) -> None:
    """Processing a Levine email with Things Happen should cache links to disk."""
    import json

    from pipeline.processor import _cache_levine_links

    levine_cache = tmp_path / "levine-cache"

    raw_html = """\
<html><body>
<h2>Things happen</h2>
<p>How <a href="https://links.message.bloomberg.com/s/c/FAKE1">Blue Owl</a> sparked a chill. <a href="https://links.message.bloomberg.com/s/c/FAKE2">Janus Bidding War</a> begins.</p>
</body></html>
"""

    _cache_levine_links(
        raw_email_html=raw_html,
        date_str="2026-02-26",
        feed_slug="levine",
        levine_cache_dir=levine_cache,
    )

    # A JSON file should be written to the cache dir.
    cache_file = levine_cache / "2026-02-26.json"
    assert cache_file.exists(), f"Expected cache file at {cache_file}"
    links = json.loads(cache_file.read_text())
    assert len(links) == 2
    assert links[0]["link_text"] == "Blue Owl"


def test_process_things_happen_job_with_script_file(tmp_path, monkeypatch) -> None:
    """When script_path is provided, skip steps 1-3 and go straight to TTS."""
    import subprocess

    from pipeline.db import StateStore
    from pipeline.things_happen_processor import process_things_happen_job

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    # Insert a pending job with empty links (they won't be used).
    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("job-script", "inbox/raw/abc.eml", "2026-02-26", "[]", past, "pending"),
    )
    store._conn.commit()

    # Write a pre-made script to a temp file.
    script_file = tmp_path / "my_script.txt"
    script_file.write_text("This is a pre-written briefing script.", encoding="utf-8")

    # Ensure steps 1-3 are NOT called by making them raise if invoked.
    monkeypatch.setattr(
        "pipeline.things_happen_processor.resolve_redirect_url",
        lambda url: (_ for _ in ()).throw(
            AssertionError("resolve_redirect_url should not be called")
        ),
    )
    monkeypatch.setattr(
        "pipeline.things_happen_processor.fetch_all_articles",
        lambda links, **kw: (_ for _ in ()).throw(
            AssertionError("fetch_all_articles should not be called")
        ),
    )
    monkeypatch.setattr(
        "pipeline.things_happen_processor.generate_briefing_script",
        lambda articles, date_str: (_ for _ in ()).throw(
            AssertionError("generate_briefing_script should not be called")
        ),
    )

    # Mock ttsjoin and ffprobe subprocess.
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="45.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    # Mock feed regeneration.
    monkeypatch.setattr(
        "pipeline.things_happen_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = store.list_due_things_happen()[0]
    process_things_happen_job(job, store, r2_client, script_path=script_file)

    # Job should be marked completed.
    assert store.list_due_things_happen() == []

    # Episode should be inserted.
    episodes = store.list_episodes(feed_slug="the-rundown")
    assert len(episodes) == 1
    assert "The Rundown" in episodes[0].title
    assert "2026-02-26" in episodes[0].title

    # R2 upload should have been called.
    r2_client.upload_file.assert_called_once()
    upload_args = r2_client.upload_file.call_args
    upload_key = upload_args[0][1]
    assert upload_key.startswith("episodes/the-rundown/")

    store.close()
