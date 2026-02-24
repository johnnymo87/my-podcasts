from __future__ import annotations

from pipeline import source_adapters as adapters


def _sample_email(body: str) -> bytes:
    return (
        "Subject: Money Stuff: Insider Trading on War\n"
        "Date: Thu, 12 Feb 2026 18:27:14 +0000\n"
        "Content-Type: text/plain; charset=utf-8\n\n"
        f"{body}\n"
    ).encode()


def test_extract_levine_source_from_direct_newsletter_url() -> None:
    raw = _sample_email(
        "View in browser "
        "<https://www.bloomberg.com/opinion/newsletters/2026-02-12/"
        "insider-trading-on-war?utm_source=newsletter>"
    )
    url = adapters._extract_levine_source_url(
        raw,
        date_str="2026-02-12",
        subject_raw="Money Stuff: Insider Trading on War",
    )
    assert (
        url
        == "https://www.bloomberg.com/opinion/newsletters/2026-02-12/insider-trading-on-war"
    )


def test_extract_levine_source_resolves_shortlink(monkeypatch) -> None:
    raw = _sample_email("View in browser <https://bloom.bg/4aeB1mX>")

    monkeypatch.setattr(
        adapters,
        "_resolve_once",
        lambda _url: "https://www.bloomberg.com/opinion/newsletters/2026-02-12/insider-trading-on-war?cmpid=foo",
    )
    url = adapters._extract_levine_source_url(
        raw,
        date_str="2026-02-12",
        subject_raw="Money Stuff: Insider Trading on War",
    )
    assert (
        url
        == "https://www.bloomberg.com/opinion/newsletters/2026-02-12/insider-trading-on-war"
    )


def test_extract_levine_source_falls_back_to_inferred_url() -> None:
    raw = _sample_email("No links here")
    url = adapters._extract_levine_source_url(
        raw,
        date_str="2026-02-12",
        subject_raw="Money Stuff: Insider Trading on War",
    )
    assert (
        url
        == "https://www.bloomberg.com/opinion/newsletters/2026-02-12/insider-trading-on-war"
    )


def test_extract_yglesias_source_from_list_post_header() -> None:
    raw = (
        b"Subject: A.I. progress is giving me writer's block\n"
        b"Date: Wed, 18 Feb 2026 11:03:05 +0000\n"
        b"List-Post: <https://www.slowboring.com/p/ai-progress-is-giving-me-writers>\n"
        b"Content-Type: text/plain; charset=utf-8\n\n"
        b"Text body\n"
    )
    url = adapters._extract_yglesias_source_url(raw)
    assert url == "https://www.slowboring.com/p/ai-progress-is-giving-me-writers"


def test_extract_yglesias_source_from_body_link() -> None:
    raw = (
        b"Subject: A.I. progress is giving me writer's block\n"
        b"Date: Wed, 18 Feb 2026 11:03:05 +0000\n"
        b"Content-Type: text/plain; charset=utf-8\n\n"
        b"View this post on the web at "
        b"https://www.slowboring.com/p/ai-progress-is-giving-me-writers?utm_source=email\n"
    )
    url = adapters._extract_yglesias_source_url(raw)
    assert url == "https://www.slowboring.com/p/ai-progress-is-giving-me-writers"
