"""Integration test: the email path speaks the episode title before TTS.

No prior test drove ``process_email_bytes`` end-to-end -- this builds the
harness (fake email bytes, real StateStore, stubbed subprocess/R2/feed
regen) and asserts on the captured TTS input text, not on source strings.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from email_processor.api import EmailProcessor
from pipeline.db import StateStore
from pipeline.processor import process_email_bytes


# ``captured_tts_input`` is a shared fixture defined in conftest.py.


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


def test_tts_input_opens_with_episode_title(
    tmp_path: Path, captured_tts_input: list[str], monkeypatch
) -> None:
    """The audio states the title; the DB row and artifacts are unaffected."""
    monkeypatch.setattr(
        "pipeline.processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    # The cleaned body process_email_bytes will see, independent of any
    # prelude logic -- LevineAdapter.clean_body and maybe_rewrite_transcript
    # are both no-ops for this feed/body, so this is exactly what reaches
    # the tempdir before the prelude is prepended.
    original_body = EmailProcessor(_LEVINE_HTML_EMAIL).parse()["body"]

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

    # Full-string equality, not just startswith/substring: catches a stray
    # truncation or double-insert that a substring check would miss.
    assert tts_input == f"Money Stuff: Goat Hedge.\n\n{original_body}"

    episodes = store.list_episodes(feed_slug="levine")
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.title == "2026-08-17 - Money Stuff - Goat Hedge"
    assert result.title == "2026-08-17 - Money Stuff - Goat Hedge"

    store.close()


def test_tts_input_unchanged_when_body_already_states_title(
    tmp_path: Path, captured_tts_input: list[str], monkeypatch
) -> None:
    """No double-statement when the body already opens with the title."""
    monkeypatch.setattr(
        "pipeline.processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )
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
