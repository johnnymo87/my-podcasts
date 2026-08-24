"""Integration test: the email path speaks the episode title before TTS.

No prior test drove ``process_email_bytes`` end-to-end -- this builds the
harness (fake email bytes, real StateStore, stubbed subprocess/R2/feed
regen) and asserts on the captured TTS input text, not on source strings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from pipeline.db import StateStore
from pipeline.processor import process_email_bytes


# Levine-shaped body: opens with boilerplate, never states the headline.
_LEVINE_HTML_EMAIL = b"""\
Date: Mon, 17 Aug 2026 08:00:00 +0000
Subject: Money Stuff: Goat Hedge
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <body>
    <p>Programming note: normal service resumes Monday.</p>
    <p>A private equity fund bought a herd of goats to hedge against
    lawn-mowing costs, which is a sentence I did not expect to write
    today, and yet here we are, discussing the financial engineering
    of livestock.</p>
  </body>
</html>
"""


def test_tts_input_opens_with_episode_title(tmp_path: Path, monkeypatch) -> None:
    """The audio states the title; the DB row and artifacts are unaffected."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    captured_tts_input: list[str] = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            input_file = Path(cmd[cmd.index("--input-file") + 1])
            captured_tts_input.append(input_file.read_text(encoding="utf-8"))
            output_file = Path(cmd[cmd.index("--output-file") + 1])
            output_file.write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    result = process_email_bytes(
        raw_email=_LEVINE_HTML_EMAIL,
        source_r2_key="raw/test-levine.eml",
        route_tag="levine",
        store=store,
        r2_client=r2_client,
        levine_cache_dir=tmp_path / "levine-cache",
    )

    assert len(captured_tts_input) == 1
    tts_input = captured_tts_input[0]

    # Body is Levine-shaped: opens with boilerplate, headline absent.
    assert tts_input.startswith("Money Stuff: ")
    assert "Programming note" in tts_input  # original body still present, after prelude

    episodes = store.list_episodes(feed_slug="levine")
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.title == "2026-08-17 - Money Stuff - Goat Hedge"
    assert result.title == "2026-08-17 - Money Stuff - Goat Hedge"

    store.close()


def test_tts_input_unchanged_when_body_already_states_title(
    tmp_path: Path, monkeypatch
) -> None:
    """No double-statement when the body already opens with the title."""
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    email_bytes = b"""\
Date: Mon, 17 Aug 2026 08:00:00 +0000
Subject: Money Stuff: Goat Hedge
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <body>
    <p>Money Stuff: Goat Hedge. A private equity fund bought a herd of
    goats to hedge against lawn-mowing costs.</p>
  </body>
</html>
"""

    captured_tts_input: list[str] = []

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            input_file = Path(cmd[cmd.index("--input-file") + 1])
            captured_tts_input.append(input_file.read_text(encoding="utf-8"))
            output_file = Path(cmd[cmd.index("--output-file") + 1])
            output_file.write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        "pipeline.processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    process_email_bytes(
        raw_email=email_bytes,
        source_r2_key="raw/test-levine-2.eml",
        route_tag="levine",
        store=store,
        r2_client=r2_client,
        levine_cache_dir=tmp_path / "levine-cache",
    )

    assert len(captured_tts_input) == 1
    tts_input = captured_tts_input[0]
    # The prelude is skipped -- title text appears exactly once at the top.
    assert tts_input.count("Goat Hedge") == 1

    store.close()
