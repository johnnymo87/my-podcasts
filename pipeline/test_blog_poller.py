from unittest.mock import patch, MagicMock

from pipeline.blog_poller import BlogPost, parse_blog_feed, adapt_for_audio
from pipeline.blog_sources import BLOG_SOURCES, BlogSource
from pipeline.db import StateStore


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<title>Test Blog</title>
<item>
  <title>Test Post</title>
  <link>https://example.com/post1</link>
  <pubDate>Sun, 29 Mar 2026 05:17:35 +0000</pubDate>
  <content:encoded><![CDATA[<p>Hello world</p>]]></content:encoded>
  <guid isPermaLink="false">https://example.com/?p=123</guid>
</item>
<item>
  <title>Second Post</title>
  <link>https://example.com/post2</link>
  <pubDate>Wed, 18 Mar 2026 16:10:13 +0000</pubDate>
  <content:encoded><![CDATA[<p>Another post</p>]]></content:encoded>
  <guid isPermaLink="false">https://example.com/?p=124</guid>
</item>
</channel>
</rss>"""


def test_parse_blog_feed_extracts_posts() -> None:
    posts = parse_blog_feed(SAMPLE_RSS)
    assert len(posts) == 2
    assert posts[0].title == "Test Post"
    assert posts[0].url == "https://example.com/post1"
    assert posts[0].html_content == "<p>Hello world</p>"
    assert posts[0].pub_date == "Sun, 29 Mar 2026 05:17:35 +0000"


def test_parse_blog_feed_handles_missing_content() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title>
    <item><title>No Content</title><link>https://example.com/x</link>
    <pubDate>Sun, 29 Mar 2026 05:17:35 +0000</pubDate></item>
    </channel></rss>"""
    posts = parse_blog_feed(rss)
    assert len(posts) == 0  # skip posts without content:encoded


def test_parse_blog_feed_empty() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title></channel></rss>"""
    posts = parse_blog_feed(rss)
    assert len(posts) == 0


def test_blog_sources_has_aaronson() -> None:
    assert len(BLOG_SOURCES) >= 1
    aaronson = [s for s in BLOG_SOURCES if s.feed_slug == "aaronson"]
    assert len(aaronson) == 1
    src = aaronson[0]
    assert src.feed_url == "https://scottaaronson.blog/?feed=rss2"
    assert src.tts_voice == "fable"
    assert src.category == "Technology"


def test_is_blog_post_processed_returns_false_for_unknown(tmp_path) -> None:
    store = StateStore(tmp_path / "test.db")
    assert store.is_blog_post_processed("https://example.com/post1") is False
    store.close()


def test_mark_blog_post_processed(tmp_path) -> None:
    store = StateStore(tmp_path / "test.db")
    store.mark_blog_post_processed("https://example.com/post1", "aaronson")
    assert store.is_blog_post_processed("https://example.com/post1") is True
    store.close()


def test_mark_blog_post_processed_idempotent(tmp_path) -> None:
    store = StateStore(tmp_path / "test.db")
    store.mark_blog_post_processed("https://example.com/post1", "aaronson")
    store.mark_blog_post_processed("https://example.com/post1", "aaronson")
    assert store.is_blog_post_processed("https://example.com/post1") is True
    store.close()


def test_adapt_for_audio_calls_gemini(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Adapted text for TTS."
    mock_client.models.generate_content.return_value = mock_response

    with patch("pipeline.blog_poller.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = adapt_for_audio("<p>Hello <b>world</b></p>", "Test Title")

    assert result == "Adapted text for TTS."
    call_args = mock_client.models.generate_content.call_args
    assert "gemini" in str(call_args)


def test_adapt_for_audio_returns_none_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = adapt_for_audio("<p>Hello</p>", "Test Title")
    assert result is None
