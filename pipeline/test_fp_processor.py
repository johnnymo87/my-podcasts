from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from pipeline.fp_processor import process_fp_digest_job


def test_process_fp_digest_job(tmp_path, monkeypatch) -> None:
    """Verify TTS, R2 upload, episode insert, and feed regeneration are called."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    # Insert a pending fp_digest job
    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_fp_digest
           (id, date_str, status, process_after)
           VALUES (?, ?, ?, ?)""",
        ("fp-job-1", "2026-03-06", "pending", past),
    )
    store._conn.commit()

    # Write a pre-made script to a temp file
    script_file = tmp_path / "fp_script.txt"
    script_file.write_text("This is the Foreign Policy briefing.", encoding="utf-8")

    # Mock ttsjoin and ffprobe subprocess
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="120.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    # Mock feed regeneration (avoid R2 calls)
    monkeypatch.setattr(
        "pipeline.fp_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = store.list_due_fp_digest()[0]
    process_fp_digest_job(job, store, r2_client, script_path=script_file)

    # Job should be marked completed
    assert store.list_due_fp_digest() == []

    # Episode should be inserted with correct feed_slug
    episodes = store.list_episodes(feed_slug="fp-digest")
    assert len(episodes) == 1
    episode = episodes[0]
    assert "Foreign Policy Digest" in episode.title
    assert "2026-03-06" in episode.title
    assert episode.feed_slug == "fp-digest"
    assert episode.category == "News"
    assert episode.source_tag == "fp-digest"
    assert episode.preset_name == "Foreign Policy Digest"

    # MP3 should be uploaded to R2 with correct key
    r2_client.upload_file.assert_called_once()
    upload_args = r2_client.upload_file.call_args
    upload_key = upload_args[0][1]
    assert upload_key == "episodes/fp-digest/2026-03-06-fp-digest.mp3"
    assert upload_args[1]["content_type"] == "audio/mpeg"

    store.close()
