from pipeline.blog_sources import BLOG_SOURCES, BlogSource


def test_blog_sources_has_aaronson() -> None:
    assert len(BLOG_SOURCES) >= 1
    aaronson = [s for s in BLOG_SOURCES if s.feed_slug == "aaronson"]
    assert len(aaronson) == 1
    src = aaronson[0]
    assert src.feed_url == "https://scottaaronson.blog/?feed=rss2"
    assert src.tts_voice == "fable"
    assert src.category == "Technology"
