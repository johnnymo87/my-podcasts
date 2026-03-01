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
    episodes = store.list_episodes(feed_slug="things-happen")
    assert len(episodes) == 1
    assert "Things Happen" in episodes[0].title
    assert "2026-02-26" in episodes[0].title

    # MP3 should be uploaded to R2.
    r2_client.upload_file.assert_called_once()
    upload_args = r2_client.upload_file.call_args
    # upload_file(local_path, r2_key, content_type=...)
    upload_key = upload_args[0][1]
    assert upload_key.startswith("episodes/things-happen/")

    store.close()


def test_levine_email_creates_pending_things_happen_job(tmp_path) -> None:
    """Processing a Levine email with Things Happen should create a pending job."""
    import json

    from pipeline.db import StateStore
    from pipeline.processor import _maybe_queue_things_happen

    store = StateStore(tmp_path / "test.sqlite3")

    raw_html = """\
<html><body>
<h2>Things happen</h2>
<p>How <a href="https://links.message.bloomberg.com/s/c/FAKE1">Blue Owl</a> sparked a chill. <a href="https://links.message.bloomberg.com/s/c/FAKE2">Janus Bidding War</a> begins.</p>
</body></html>
"""

    _maybe_queue_things_happen(
        raw_email_html=raw_html,
        source_r2_key="inbox/raw/test.eml",
        date_str="2026-02-26",
        feed_slug="levine",
        store=store,
    )

    # A pending job should exist (not yet due).
    rows = store._conn.execute(
        "SELECT * FROM pending_things_happen WHERE status = 'pending'"
    ).fetchall()
    assert len(rows) == 1
    links = json.loads(rows[0]["links_json"])
    assert len(links) == 2
    assert links[0]["link_text"] == "Blue Owl"
    store.close()
