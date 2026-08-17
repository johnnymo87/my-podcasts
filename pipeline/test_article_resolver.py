from pipeline.article_resolver import extract_url, resolve_headline, slugify


def test_slugify_matches_article_family_behavior():
    assert (
        slugify("US Set to  Pay Most for 30-Year Debt")
        == "us-set-to-pay-most-for-30-year-debt"
    )
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("---") == ""


def test_slugify_truncates_to_50_chars():
    assert len(slugify("a" * 100)) == 50


def test_slugify_keeps_non_ascii_alphanumerics():
    # str.isalnum() is True for accented letters; the article family keeps them.
    # The R2-key family (script_processor/blog_poller) strips them. They are
    # deliberately NOT unified, so pin this difference.
    assert slugify("Beyoncé") == "beyoncé"


def test_extract_url_reads_header():
    assert extract_url("# H\n\nURL: https://x.com/a\n\nbody") == "https://x.com/a"


def test_extract_url_returns_none_when_absent():
    assert extract_url("# H\n\nbody") is None


def test_extract_url_ignores_url_beyond_the_header_block():
    body = (
        "# H\n\n"
        + "\n".join(f"line {i}" for i in range(20))
        + "\nURL: https://late.com/x"
    )
    assert extract_url(body) is None


def test_exact_match_wins():
    index = {"A Headline": "articles/00-a.md"}
    assert resolve_headline("A Headline", index) == ("articles/00-a.md", None)


def test_slug_match_rescues_whitespace_variation():
    # The real failure: Levine headlines come from sentence extraction and can
    # carry a double space that Gemini normalizes when echoing it back.
    index = {"US Set to  Pay Most": "articles/00-us.md"}
    assert resolve_headline("US Set to Pay Most", index) == ("articles/00-us.md", None)


def test_ambiguous_slug_is_a_miss_not_a_coin_flip():
    # Two headlines sharing a >50-char prefix collapse to one slug. Picking the
    # first would be arbitrary (dict order), so refuse and say why.
    long = "A" * 60
    index = {long + " one": "articles/00-one.md", long + " two": "articles/01-two.md"}
    assert resolve_headline(long + " three", index) == (None, "slug_ambiguous")


def test_two_index_keys_pointing_at_one_file_are_not_ambiguous():
    long = "A" * 60
    index = {long + " one": "articles/00-one.md", long + " two": "articles/00-one.md"}
    assert resolve_headline(long + " three", index) == ("articles/00-one.md", None)


def test_no_match_reports_index_no_match():
    index = {"Something Else": "articles/00-x.md"}
    assert resolve_headline("Totally Unrelated", index) == (None, "index_no_match")


def test_empty_index_reports_index_no_match():
    assert resolve_headline("Anything", {}) == (None, "index_no_match")


def test_empty_slug_headline_does_not_match_anything():
    index = {"!!!": "articles/00-x.md"}
    # Both slugify to "". Matching on an empty slug would pair arbitrary
    # punctuation-only headlines with each other.
    assert resolve_headline("???", index) == (None, "index_no_match")
