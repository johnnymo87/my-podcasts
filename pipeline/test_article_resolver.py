from pipeline.article_resolver import slugify, extract_url


def test_slugify_matches_article_family_behavior():
    assert slugify("US Set to  Pay Most for 30-Year Debt") == "us-set-to-pay-most-for-30-year-debt"
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
    body = "# H\n\n" + "\n".join(f"line {i}" for i in range(20)) + "\nURL: https://late.com/x"
    assert extract_url(body) is None
