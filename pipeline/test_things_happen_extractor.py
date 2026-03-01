from __future__ import annotations

from pipeline.things_happen_extractor import ThingsHappenLink, extract_things_happen


# Minimal reproduction of Bloomberg's email HTML structure for Things Happen.
THINGS_HAPPEN_HTML = """\
<html><body>
<table><tr><td>
  <h2 style="font-weight: bold;">Things happen</h2>
</td></tr></table>
<p style="margin: 16px 0;">How <a href="https://links.message.bloomberg.com/s/c/FAKE1">Blue Owl's battles</a> sparked a chill for private credit.\xa0Jefferies Fund Sued by Investors Over <a href="https://links.message.bloomberg.com/s/c/FAKE2"> First Brands Collapse</a>.\xa0<a href="https://links.message.bloomberg.com/s/c/FAKE3">Janus Bidding War</a> Begins as Victory Capital Tops Trian Offer. AI <a href="https://links.message.bloomberg.com/s/c/FAKE4">dispersion trades</a>.</p>
<p style="margin: 16px 0;">If you'd like to get Money Stuff in handy email form, right in your inbox, please subscribe at this link.</p>
</body></html>
"""


def test_extracts_links_from_things_happen_section() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert len(links) == 4
    assert all(isinstance(link, ThingsHappenLink) for link in links)


def test_link_text_is_cleaned() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert links[0].link_text == "Blue Owl's battles"
    assert links[1].link_text == "First Brands Collapse"


def test_link_urls_are_captured() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert links[0].raw_url == "https://links.message.bloomberg.com/s/c/FAKE1"
    assert links[3].raw_url == "https://links.message.bloomberg.com/s/c/FAKE4"


def test_headline_context_captures_full_sentence() -> None:
    links = extract_things_happen(THINGS_HAPPEN_HTML)
    assert "Blue Owl's battles" in links[0].headline_context
    assert "sparked a chill for private credit" in links[0].headline_context


def test_returns_empty_list_when_no_things_happen_section() -> None:
    html = "<html><body><p>No Things Happen here.</p></body></html>"
    links = extract_things_happen(html)
    assert links == []
