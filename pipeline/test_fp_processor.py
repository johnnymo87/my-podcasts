from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


def test_process_fp_digest_with_show_notes(tmp_path, monkeypatch) -> None:
    """When work_dir has plan.json and articles, show notes are stored."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    # Insert a pending fp_digest job
    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_fp_digest
           (id, date_str, status, process_after)
           VALUES (?, ?, ?, ?)""",
        ("fp-job-sn", "2026-03-10", "pending", past),
    )
    store._conn.commit()

    # Set up work_dir with plan.json and article files
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = {
        "themes": ["Middle East"],
        "directives": [
            {
                "headline": "Gaza Ceasefire Talks",
                "source": "semafor",
                "priority": 1,
                "theme": "Middle East",
                "needs_exa": False,
                "exa_query": "",
                "include_in_episode": True,
            }
        ],
    }
    (work_dir / "plan.json").write_text(json.dumps(plan))
    articles_dir = work_dir / "articles" / "semafor"
    articles_dir.mkdir(parents=True)
    (articles_dir / "gaza-ceasefire-talks.md").write_text(
        "# Gaza Ceasefire Talks\n\nURL: https://example.com/gaza\n\nText."
    )

    script_file = tmp_path / "fp_script_sn.txt"
    script_file.write_text("The FP briefing.", encoding="utf-8")

    # Mock subprocess and feed regen
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="120.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.fp_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = store.list_due_fp_digest()[0]
    process_fp_digest_job(
        job,
        store,
        r2_client,
        script_path=script_file,
        work_dir=work_dir,
        summary="Today's FP summary.",
    )

    episodes = store.list_episodes(feed_slug="fp-digest")
    assert len(episodes) == 1
    assert episodes[0].summary == "Today's FP summary."
    articles = json.loads(episodes[0].articles_json)
    assert len(articles) == 1
    assert articles[0]["url"] == "https://example.com/gaza"

    store.close()


def test_process_fp_filters_by_covered_headlines(tmp_path, monkeypatch) -> None:
    """When covered.json exists, FP show notes only include covered stories."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_fp_digest
           (id, date_str, process_after, status)
           VALUES (?, ?, ?, ?)""",
        ("fp-cov", "2026-03-10", past, "pending"),
    )
    store._conn.commit()

    work_dir = tmp_path / "fp_work"
    work_dir.mkdir()
    plan = {
        "themes": ["Middle East", "Africa"],
        "directives": [
            {
                "headline": "Gaza Ceasefire Talks",
                "source": "semafor",
                "priority": 1,
                "theme": "Middle East",
                "needs_exa": False,
                "exa_query": "",
                "include_in_episode": True,
            },
            {
                "headline": "Mali Conflict Escalates",
                "source": "homepage/africa",
                "priority": 1,
                "theme": "Africa",
                "needs_exa": False,
                "exa_query": "",
                "include_in_episode": True,
            },
        ],
    }
    (work_dir / "plan.json").write_text(json.dumps(plan))
    semafor_dir = work_dir / "articles" / "semafor"
    semafor_dir.mkdir(parents=True)
    (semafor_dir / "gaza-ceasefire-talks.md").write_text(
        "# Gaza Ceasefire Talks\n\nURL: https://example.com/gaza\n\nText."
    )
    homepage_dir = work_dir / "articles" / "homepage" / "africa"
    homepage_dir.mkdir(parents=True)
    (homepage_dir / "mali-conflict-escalates.md").write_text(
        "# Mali Conflict Escalates\n\nURL: https://example.com/mali\n\nText."
    )

    # Writer only covered Gaza
    (work_dir / "covered.json").write_text(json.dumps(["Gaza Ceasefire Talks"]))

    script_file = tmp_path / "fp_cov_script.txt"
    script_file.write_text("The FP briefing.", encoding="utf-8")

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="120.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.fp_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = store.list_due_fp_digest()[0]
    process_fp_digest_job(
        job,
        store,
        r2_client,
        script_path=script_file,
        work_dir=work_dir,
        summary="FP summary.",
    )

    episodes = store.list_episodes(feed_slug="fp-digest")
    assert len(episodes) == 1
    articles = json.loads(episodes[0].articles_json)
    # Only Gaza should appear, not Mali
    assert len(articles) == 1
    assert articles[0]["title"] == "Gaza Ceasefire Talks"

    store.close()


def test_process_fp_digest_job_rejects_empty_script(tmp_path, monkeypatch) -> None:
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_fp_digest
           (id, date_str, status, process_after)
           VALUES (?, ?, ?, ?)""",
        ("fp-job-empty", "2026-03-17", "pending", past),
    )
    store._conn.commit()

    script_file = tmp_path / "fp_empty_script.txt"
    script_file.write_text("   \n", encoding="utf-8")

    def fail_if_called(cmd, **kwargs):
        raise AssertionError(f"subprocess.run should not be called: {cmd}")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    job = store.list_due_fp_digest()[0]
    with pytest.raises(RuntimeError, match="empty script"):
        process_fp_digest_job(job, store, r2_client, script_path=script_file)

    r2_client.upload_file.assert_not_called()
    assert len(store.list_due_fp_digest()) == 1

    store.close()
