from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from pipeline import arxiv


_FIX = Path(__file__).parent / "fixtures"


def _resp(text: str, status: int = 200):
    r = MagicMock()
    r.text = text
    r.status_code = status
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(response=r)
    else:
        r.raise_for_status = MagicMock()
    return r


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("https://arxiv.org/html/2407.16314v1", "2407.16314v1"),
        ("https://arxiv.org/abs/2407.16314", "2407.16314"),
        ("https://arxiv.org/pdf/2407.16314v2", "2407.16314v2"),
        ("2407.16314", "2407.16314"),
        ("arXiv:2407.16314v1", "2407.16314v1"),
        ("ARXIV:2407.16314", "2407.16314"),
    ],
)
def test_parse_id(ref, expected):
    assert arxiv.parse_arxiv_id(ref) == expected


def test_parse_id_rejects_garbage():
    with pytest.raises(ValueError):
        arxiv.parse_arxiv_id("https://example.com/not-a-paper")


def test_matches_host_and_bare_id():
    assert arxiv.matches("https://arxiv.org/html/2407.16314v1")
    assert arxiv.matches("2407.16314")
    assert arxiv.matches("arXiv:2407.16314v1")
    assert not arxiv.matches("https://www.dwarkesh.com/p/x")
    assert not arxiv.matches("https://notarxiv.org/abs/2407.16314")


def test_fixture_sanity():
    # Guard against future fixture replacement silently weakening coverage.
    from bs4 import BeautifulSoup

    soup = BeautifulSoup((_FIX / "arxiv_paper.html").read_text("utf-8"), "html.parser")
    assert len(soup.find_all("section", class_="ltx_section")) == 6
    assert soup.select("section.ltx_bibliography, ul.ltx_biblist")


@patch("pipeline.arxiv.requests.get")
def test_resolve_builds_paper_document(mock_get):
    api_xml = (_FIX / "arxiv_api.xml").read_text("utf-8")
    paper_html = (_FIX / "arxiv_paper.html").read_text("utf-8")
    calls = []

    def _by_url(url, *a, **k):
        calls.append(url)
        return _resp(api_xml if "export.arxiv.org" in url else paper_html)

    mock_get.side_effect = _by_url
    doc = arxiv.resolve("https://arxiv.org/abs/2407.16314")  # unversioned input

    assert doc.title == "Capital as Artificial Intelligence"
    assert "Carissimo" in doc.byline and "Korecki" in doc.byline
    assert doc.canonical_url == "https://arxiv.org/abs/2407.16314v1"
    assert doc.style == "paper"
    assert doc.read_html is None
    assert doc.default_category == "Science"
    assert doc.slug == "2407-16314v1"
    assert "Capital" in doc.description
    # HTML fetched with the *versioned* id derived from the Atom entry.
    html_calls = [c for c in calls if "/html/" in c]
    assert html_calls == ["https://arxiv.org/html/2407.16314v1"]


@patch("pipeline.arxiv.requests.get")
def test_report_text_quality(mock_get):
    api_xml = (_FIX / "arxiv_api.xml").read_text("utf-8")
    paper_html = (_FIX / "arxiv_paper.html").read_text("utf-8")
    mock_get.side_effect = lambda url, *a, **k: _resp(
        api_xml if "export.arxiv.org" in url else paper_html
    )
    doc = arxiv.resolve("https://arxiv.org/html/2407.16314v1")
    t = doc.report_text
    for heading in [
        "Capital as a Historical Agential System",
        "The Agents of Capital",
        "The Entropy of Capital",
        "The Artificial Intelligence in Capital",
        "A Pragmatic Perspective",
        "Conclusion",
    ]:
        assert heading in t
    assert "Definition 1" in t                       # theorem/definition kept
    assert "The relationship between generating processes" in t  # figure caption kept
    assert "Barreto" not in t  # the actual bibliography-exclusion guard
    # tightened so a bib/footnote leak causes a failure
    assert 5000 < len(t.split()) < 7000


@patch("pipeline.arxiv.requests.get")
def test_resolve_no_html_rendering_raises(mock_get):
    api_xml = (_FIX / "arxiv_api.xml").read_text("utf-8")

    def _by_url(url, *a, **k):
        if "export.arxiv.org" in url:
            return _resp(api_xml)
        return _resp("", status=404)

    mock_get.side_effect = _by_url
    with pytest.raises(ValueError, match="(?i)html"):
        arxiv.resolve("https://arxiv.org/abs/2407.16314")


@patch("pipeline.arxiv.requests.get")
def test_api_server_error_propagates(mock_get):
    mock_get.return_value = _resp("", status=500)
    with pytest.raises(requests.HTTPError):
        arxiv._fetch_metadata("2407.16314", timeout=5)
