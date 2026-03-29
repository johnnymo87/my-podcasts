from pipeline.blog_sources import BLOG_SOURCES, BlogSource
from pipeline.db import StateStore


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
