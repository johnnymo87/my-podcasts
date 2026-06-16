from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.document import Document
from pipeline.sources import resolve_document


def _doc(**kw) -> Document:
    base = {
        "title": "T", "byline": "B", "canonical_url": "https://x/y", "description": "D",
        "report_text": "R", "read_html": "<p>H</p>", "slug": "s", "style": "interview",
        "wordcount": 1, "default_category": "Technology",
    }
    base.update(kw)
    return Document(**base)


@patch("pipeline.arxiv.resolve")
def test_dispatch_routes_arxiv_url(mock_arxiv_resolve):
    mock_arxiv_resolve.return_value = _doc(style="paper")
    doc = resolve_document("https://arxiv.org/html/2407.16314v1")
    assert doc.style == "paper"
    mock_arxiv_resolve.assert_called_once()


@patch("pipeline.sources._substack_resolve")
def test_dispatch_routes_substack_url(mock_sub_resolve):
    mock_sub_resolve.return_value = _doc(style="interview")
    doc = resolve_document("https://www.dwarkesh.com/p/david-reich-2")
    assert doc.style == "interview"
    mock_sub_resolve.assert_called_once()


@patch("pipeline.sources._substack_resolve")
def test_bare_numeric_id_routes_substack(mock_sub_resolve):
    mock_sub_resolve.return_value = _doc()
    resolve_document("123456")
    mock_sub_resolve.assert_called_once()


@patch("pipeline.arxiv.resolve")
def test_explicit_source_override(mock_arxiv_resolve):
    mock_arxiv_resolve.return_value = _doc(style="paper")
    resolve_document("anything", source="arxiv")
    mock_arxiv_resolve.assert_called_once()


def test_no_match_raises():
    with pytest.raises(ValueError):
        resolve_document("https://example.com/random")


def test_unknown_source_raises():
    with pytest.raises(ValueError):
        resolve_document("https://arxiv.org/abs/1", source="bogus")
