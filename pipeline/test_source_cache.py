from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from pipeline.__main__ import cli
from pipeline.fp_homepage_scraper import HomepageLink
from pipeline.source_cache import (
    classify_semafor_articles,
    sync_antiwar_homepage_cache,
    sync_antiwar_rss_cache,
    sync_semafor_cache,
)


@pytest.fixture(autouse=True)
def _no_gemini_semafor_routing(monkeypatch):
    """``sync_semafor_cache`` routes articles with Gemini; stub it to the empty
    answer a host with no API key gives (files stay pending), so no test reaches
    Google. Tests of the classifier itself call ``classify_semafor_articles``
    directly (their own reference, unaffected) and stub ``genai.Client``."""
    monkeypatch.setattr(
        "pipeline.source_cache.classify_semafor_articles",
        lambda articles: {},
    )


def _make_semafor_entry(
    title: str, category: str, published_str: str = "2026-03-07"
) -> dict:
    """Create a fake feedparser entry for Semafor."""
    import feedparser

    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return feedparser.FeedParserDict(
        {
            "title": title,
            "link": f"https://semafor.com/{title.lower().replace(' ', '-')[:30]}",
            "published_parsed": st,
            "summary": f"Summary of {title}",
            "tags": [feedparser.FeedParserDict({"term": category})],
            "content": [{"value": f"<p>Full content about {title}.</p>"}],
        }
    )


def test_sync_semafor_creates_files(tmp_path):
    """Semafor articles are cached as markdown files with category metadata."""
    entry = _make_semafor_entry("Tech Company IPO", "Technology")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        new_files = sync_semafor_cache(tmp_path)

    assert len(new_files) == 1
    content = new_files[0].read_text()
    assert "# Tech Company IPO" in content
    assert "Category: Technology" in content
    assert "Source: semafor" in content


def test_sync_semafor_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    entry = _make_semafor_entry("Tech Story", "Business")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        first = sync_semafor_cache(tmp_path)
        second = sync_semafor_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_semafor_handles_fetch_failure(tmp_path):
    """If RSS fetch fails, return empty list."""
    with patch("pipeline.source_cache.fetch_feed", side_effect=Exception("timeout")):
        result = sync_semafor_cache(tmp_path)
    assert result == []


def _make_rss_entry(
    title: str, source_url: str, published_str: str = "2026-03-07"
) -> dict:
    """Create a fake feedparser entry for antiwar/CJ RSS."""
    dt = datetime.strptime(published_str, "%Y-%m-%d").replace(tzinfo=UTC)
    st = dt.timetuple()
    return {
        "title": title,
        "link": source_url,
        "published_parsed": st,
        "summary": f"Summary of {title}",
        "content": [{"value": f"<p>Full content about {title}.</p>"}],
    }


def test_sync_antiwar_rss_creates_files(tmp_path):
    """Antiwar RSS articles cached with source metadata."""
    entry = _make_rss_entry("US Troops Deploy", "https://news.antiwar.com/story1")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        new_files = sync_antiwar_rss_cache(tmp_path)

    assert len(new_files) == 4  # one file per source in FP_DIGEST_RSS_SOURCES
    content = new_files[0].read_text()
    assert "# US Troops Deploy" in content
    assert "Source: antiwar_news" in content


def test_sync_antiwar_rss_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    entry = _make_rss_entry("Troops Story", "https://news.antiwar.com/story1")
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        first = sync_antiwar_rss_cache(tmp_path)
        second = sync_antiwar_rss_cache(tmp_path)

    assert len(first) == 4
    assert len(second) == 0


def test_sync_antiwar_rss_handles_partial_failure(tmp_path):
    """A failing source is skipped; other sources still produce files."""
    good_entry = _make_rss_entry("Good Story", "https://news.antiwar.com/s1")
    good_feed = MagicMock()
    good_feed.entries = [good_entry]
    call_count = 0

    def _side_effect(url):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return good_feed
        raise Exception("timeout")

    with patch("pipeline.source_cache.fetch_feed", side_effect=_side_effect):
        result = sync_antiwar_rss_cache(tmp_path)

    assert len(result) == 1  # first source succeeded, others failed


def test_sync_homepage_creates_files(tmp_path):
    """Homepage links cached with region metadata."""
    links = [
        HomepageLink(
            region="Middle East",
            headline="Iran Tensions Rise",
            url="https://example.com/iran",
        ),
        HomepageLink(
            region="Europe", headline="NATO Expands", url="https://example.com/nato"
        ),
    ]

    with (
        patch("pipeline.source_cache.scrape_homepage", return_value=links),
        patch(
            "pipeline.source_cache._extract_homepage_text",
            return_value="Article body text.",
        ),
    ):
        new_files = sync_antiwar_homepage_cache(tmp_path)

    assert len(new_files) == 2
    content = new_files[0].read_text()
    assert "Region:" in content
    assert "Source: antiwar-homepage" in content
    assert "Article body text." in content


def test_sync_homepage_is_idempotent(tmp_path):
    """Running sync twice does not create duplicates."""
    links = [
        HomepageLink(region="Africa", headline="Story One", url="https://example.com/1")
    ]

    with (
        patch("pipeline.source_cache.scrape_homepage", return_value=links),
        patch("pipeline.source_cache._extract_homepage_text", return_value="Text."),
    ):
        first = sync_antiwar_homepage_cache(tmp_path)
        second = sync_antiwar_homepage_cache(tmp_path)

    assert len(first) == 1
    assert len(second) == 0


def test_sync_homepage_handles_scrape_failure(tmp_path):
    """If homepage scrape fails, return empty list."""
    with patch(
        "pipeline.source_cache.scrape_homepage", side_effect=Exception("timeout")
    ):
        result = sync_antiwar_homepage_cache(tmp_path)
    assert result == []


def test_sync_sources_cli():
    """The sync-sources command calls all four sync functions."""
    with (
        patch("pipeline.__main__.sync_zvi_cache") as mock_zvi,
        patch("pipeline.__main__.sync_semafor_cache") as mock_semafor,
        patch("pipeline.__main__.sync_antiwar_rss_cache") as mock_rss,
        patch("pipeline.__main__.sync_antiwar_homepage_cache") as mock_homepage,
    ):
        mock_zvi.return_value = []
        mock_semafor.return_value = []
        mock_rss.return_value = []
        mock_homepage.return_value = []

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-sources"])

    assert result.exit_code == 0
    mock_zvi.assert_called_once()
    mock_semafor.assert_called_once()
    mock_rss.assert_called_once()
    mock_homepage.assert_called_once()


def test_classify_semafor_articles_returns_routing(monkeypatch):
    """LLM classifier returns routing for each article."""
    articles = [
        {
            "title": "Hungary blocks EU loan to Ukraine",
            "description": "Orban demands Russian oil access.",
        },
        {
            "title": "Nvidia's Claw Bar was a pricey wakeup call",
            "description": "A demo at GTC was really a sales pitch.",
        },
        {"title": "Iran war reshapes Gulf diplomacy", "description": ""},
    ]

    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="fp"),
        MagicMock(index=1, routing="th"),
        MagicMock(index=2, routing="fp"),
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles(articles)

    assert result == {0: "fp", 1: "th", 2: "fp"}
    call_args = mock_client.models.generate_content.call_args
    assert "gemini-3.8-flash" in str(call_args)


def test_classify_semafor_articles_no_api_key_gives_no_answers(monkeypatch):
    """Without GEMINI_API_KEY nothing is classified (the files stay pending)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    articles = [
        {"title": "Some Article", "description": "Desc."},
        {"title": "Another Article", "description": ""},
    ]
    result = classify_semafor_articles(articles)
    assert result == {}


def test_classify_semafor_articles_error_gives_no_answers(monkeypatch):
    """On a Gemini API error nothing is classified; no 'both' is invented."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception(
            "API down"
        )
        result = classify_semafor_articles([{"title": "Test", "description": ""}])

    assert result == {}


def test_classify_semafor_articles_unparsed_reply_gives_no_answers(monkeypatch):
    mock_response = MagicMock()
    mock_response.parsed = None
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles([{"title": "Test", "description": ""}])

    assert result == {}


def test_classify_semafor_articles_empty_list():
    """Empty input returns empty dict."""
    result = classify_semafor_articles([])
    assert result == {}


def test_classify_semafor_articles_omits_unknown_routing(monkeypatch):
    """An invalid routing value is omitted, not turned into 'both'."""
    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="UNKNOWN_VALUE"),
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles([{"title": "Test", "description": ""}])

    assert result == {}


def test_sync_semafor_writes_routing_header(tmp_path, monkeypatch):
    """sync_semafor_cache writes a Routing: header from LLM classification."""
    entry = _make_semafor_entry("Iran War Update", "", "2026-03-20")
    feed = MagicMock()
    feed.entries = [entry]

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            return_value={0: "fp"},
        ),
    ):
        new_files = sync_semafor_cache(tmp_path)

    assert len(new_files) == 1
    content = new_files[0].read_text()
    assert "Routing: fp" in content
    assert "Category:" in content  # Category still present


def test_sync_semafor_leaves_file_pending_when_classification_fails(
    tmp_path, monkeypatch
):
    """No answer from the classifier leaves the file at __PENDING__ -- it is
    NOT stamped 'both' (my-podcasts-6fk). Readers fall back to the category."""
    entry = _make_semafor_entry("Some Article", "Technology", "2026-03-20")
    feed = MagicMock()
    feed.entries = [entry]

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            return_value={},
        ),
    ):
        new_files = sync_semafor_cache(tmp_path)

    content = new_files[0].read_text()
    assert "Routing: __PENDING__" in content
    assert "Routing: both" not in content


def test_sync_antiwar_rss_summary_entities_are_decoded(tmp_path):
    """Antiwar entries carry no <content>, so the summary must still be decoded.

    Measured 2026-08-18: every cached antiwar body holds literal &#8217;/&#8220;
    and a trailing [&#8230;], and those characters reach the writer prompt
    verbatim.
    """
    entry = _make_rss_entry(
        "Ansar Allah Announces Attacks", "https://news.antiwar.com/a/"
    )
    entry["summary"] = "he called it a &#8220;landing ship&#8221; [&#8230;]"
    del entry["content"]
    feed = MagicMock()
    feed.entries = [entry]

    with patch("pipeline.source_cache.fetch_feed", return_value=feed):
        new_files = sync_antiwar_rss_cache(tmp_path)

    assert len(new_files) == 4  # one file per source in FP_DIGEST_RSS_SOURCES
    body = new_files[0].read_text(encoding="utf-8").split("\n\n", 2)[2]
    assert "&#8220;" not in body
    assert "&#8230;" not in body
    assert "\u201clanding ship\u201d" in body
    assert "\u2026" in body


def test_sync_semafor_description_entities_are_decoded(tmp_path):
    """A Semafor entry with no <content> still needs its description decoded.

    The identical defect shape as the antiwar RSS summary above — Semafor
    entries normally carry <content> so this may never bite in production,
    but the fix belongs everywhere the defect lives (docs/ROADMAP.md).
    """
    entry = _make_semafor_entry("Ansar Allah Announces Attacks", "Gulf")
    entry["summary"] = "he called it a &#8220;landing ship&#8221; [&#8230;]"
    del entry["content"]
    feed = MagicMock()
    feed.entries = [entry]

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            return_value={0: "fp"},
        ),
    ):
        new_files = sync_semafor_cache(tmp_path)

    assert len(new_files) == 1
    body = new_files[0].read_text(encoding="utf-8").split("\n\n", 2)[2]
    assert "&#8220;" not in body
    assert "&#8230;" not in body
    assert "\u201clanding ship\u201d" in body
    assert "\u2026" in body


def test_classify_semafor_articles_omits_missing_indices(monkeypatch):
    """An index the model skipped is omitted (it stays pending), not 'both'."""
    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="fp"),
        # index 1 missing
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles(
            [
                {"title": "Article A", "description": ""},
                {"title": "Article B", "description": ""},
            ]
        )

    assert result == {0: "fp"}


def test_classify_semafor_articles_ignores_out_of_range_index(monkeypatch):
    mock_parsed = MagicMock()
    mock_parsed.articles = [
        MagicMock(index=0, routing="th"),
        MagicMock(index=7, routing="fp"),
        MagicMock(index=-1, routing="fp"),
    ]
    mock_response = MagicMock()
    mock_response.parsed = mock_parsed
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with patch("pipeline.source_cache.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        result = classify_semafor_articles([{"title": "Only", "description": ""}])

    assert result == {0: "th"}


# --- pending reclassification (my-podcasts-6fk) ---------------------------------


def _pending_file(
    cache_dir,
    date_prefix: str,
    title: str,
    category: str = "Politics",
    body: str = "Body text of the article.",
    routing: str = "__PENDING__",
):
    path = cache_dir / f"{date_prefix}-{title.lower().replace(' ', '-')}.md"
    path.write_text(
        f"# {title}\n\nURL: https://semafor.com/{title.lower().replace(' ', '-')}\n"
        f"Published: {date_prefix}\nSource: semafor\nCategory: {category}\n"
        f"Routing: {routing}\nType: article\n\n{body}",
        encoding="utf-8",
    )
    return path


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%d")


def _empty_feed():
    feed = MagicMock()
    feed.entries = []
    return feed


def test_next_sync_reclassifies_pending_files_inside_the_window(tmp_path):
    recent = _pending_file(tmp_path, _days_ago(3), "Recent Story", body="R" * 500)
    edge = _pending_file(tmp_path, _days_ago(14), "Edge Story")
    seen: list[list[dict]] = []

    def classify(articles):
        seen.append(articles)
        return {0: "fp", 1: "th"}

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=_empty_feed()),
        patch("pipeline.source_cache.classify_semafor_articles", classify),
    ):
        new_files = sync_semafor_cache(tmp_path)

    assert new_files == []  # return value is newly written files only
    assert len(seen) == 1
    titles = {a["title"] for a in seen[0]}
    assert titles == {"Recent Story", "Edge Story"}
    by_title = {a["title"]: a for a in seen[0]}
    # The description of an existing file is the first 300 chars of its body.
    assert by_title["Recent Story"]["description"] == "R" * 300
    routings = {
        "Recent Story": recent.read_text(),
        "Edge Story": edge.read_text(),
    }
    assert sum("Routing: fp" in t for t in routings.values()) == 1
    assert sum("Routing: th" in t for t in routings.values()) == 1
    assert all("__PENDING__" not in t for t in routings.values())


def test_reclassification_leaves_old_unknown_and_already_routed_files_alone(tmp_path):
    old = _pending_file(tmp_path, _days_ago(15), "Old Story")
    unknown = tmp_path / "unknown-mystery-story.md"
    unknown.write_text(
        "# Mystery Story\n\nURL: u\nPublished: unknown\nSource: semafor\n"
        "Category: Politics\nRouting: __PENDING__\nType: article\n\nBody",
        encoding="utf-8",
    )
    routed = _pending_file(tmp_path, _days_ago(1), "Routed Story", routing="th")
    originals = {p: p.read_text() for p in (old, unknown, routed)}
    calls: list[list[dict]] = []

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=_empty_feed()),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            lambda articles: calls.append(articles) or {},
        ),
    ):
        sync_semafor_cache(tmp_path)

    assert calls == []  # nothing eligible: the classifier is not even asked
    assert {p: p.read_text() for p in originals} == originals


def test_new_and_pending_articles_are_classified_in_one_call(tmp_path):
    pending = _pending_file(tmp_path, _days_ago(2), "Pending Story")
    feed = MagicMock()
    feed.entries = [_make_semafor_entry("Brand New Story", "Gulf", _days_ago(0))]
    calls: list[list[dict]] = []

    def classify(articles):
        calls.append(articles)
        return dict.fromkeys(range(len(articles)), "fp")

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch("pipeline.source_cache.classify_semafor_articles", classify),
    ):
        new_files = sync_semafor_cache(tmp_path)

    assert len(calls) == 1
    assert {a["title"] for a in calls[0]} == {"Brand New Story", "Pending Story"}
    assert len(new_files) == 1
    assert "Routing: fp" in new_files[0].read_text()
    assert "Routing: fp" in pending.read_text()


def test_partial_reply_patches_only_covered_indices(tmp_path):
    feed = MagicMock()
    feed.entries = [
        _make_semafor_entry("Story A", "Gulf", _days_ago(0)),
        _make_semafor_entry("Story B", "Gulf", _days_ago(0)),
    ]

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            return_value={0: "th"},  # index 1 unanswered
        ),
    ):
        new_files = sync_semafor_cache(tmp_path)

    texts = [f.read_text() for f in new_files]
    assert sum("Routing: th" in t for t in texts) == 1
    assert sum("Routing: __PENDING__" in t for t in texts) == 1


def test_failed_classification_then_recovery_across_syncs(tmp_path):
    """The 2026-10-20 shape: a 402 at sync time must not stick for 180 days."""
    feed = MagicMock()
    feed.entries = [_make_semafor_entry("Story A", "Gulf", _days_ago(0))]

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch("pipeline.source_cache.classify_semafor_articles", return_value={}),
    ):
        (path,) = sync_semafor_cache(tmp_path)
    assert "Routing: __PENDING__" in path.read_text()

    with (
        patch("pipeline.source_cache.fetch_feed", return_value=feed),
        patch(
            "pipeline.source_cache.classify_semafor_articles",
            return_value={0: "fp"},
        ),
    ):
        assert sync_semafor_cache(tmp_path) == []  # nothing new written
    assert "Routing: fp" in path.read_text()
    assert "__PENDING__" not in path.read_text()
