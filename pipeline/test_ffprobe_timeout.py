"""A hung ffprobe means "duration unknown", never a failed publish.

The episode audio is already rendered (and uploaded) by the time the duration
is probed; a probe timeout must not turn that into a job failure.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pipeline import (
    fp_processor,
    processor,
    script_processor,
    things_happen_processor,
)
from pipeline.blog_poller import BlogPost, process_blog_post
from pipeline.blog_sources import BLOG_SOURCES
from pipeline.db import StateStore


def _timeout(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))


@pytest.mark.parametrize(
    "module",
    [processor, things_happen_processor, script_processor, fp_processor],
    ids=lambda m: m.__name__,
)
def test_parse_duration_seconds_treats_probe_timeout_as_unknown(
    module, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(subprocess, "run", _timeout)
    assert module._parse_duration_seconds(tmp_path / "x.mp3") is None


def test_blog_poller_probe_timeout_publishes_with_unknown_duration(
    tmp_path, monkeypatch, fake_tts_render
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("PODCAST_BASE_URL", "https://test.example.com")
    monkeypatch.setattr(subprocess, "run", _timeout)

    store = StateStore(tmp_path / "test.db")
    post = BlogPost(
        title="Some post",
        url="https://example.com/post1",
        pub_date="Sat, 22 Aug 2026 05:17:35 +0000",
        html_content="<p>Body.</p>",
        guid="https://example.com/?p=1",
    )
    gemini = MagicMock()
    gemini.models.generate_content.return_value = MagicMock(text="Body.")
    with (
        patch("pipeline.blog_poller.genai") as mock_genai,
        patch("pipeline.feed.regenerate_and_upload_feed"),
    ):
        mock_genai.Client.return_value = gemini
        process_blog_post(post, BLOG_SOURCES[0], store, MagicMock())

    [episode] = store.list_episodes(feed_slug=BLOG_SOURCES[0].feed_slug)
    assert episode.duration_seconds is None
    store.close()
