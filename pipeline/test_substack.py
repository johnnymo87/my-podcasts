from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from pipeline.substack import SubstackPost, _api_url, html_to_clean_text, resolve_post


def test_api_url_numeric_id():
    assert _api_url("196892360") == (
        "https://substack.com/api/v1/posts/by-id/196892360"
    )


def test_api_url_short_link():
    assert _api_url("https://substack.com/@dwarkesh/p-196892360") == (
        "https://substack.com/api/v1/posts/by-id/196892360"
    )


def test_api_url_canonical_slug():
    assert _api_url("https://www.dwarkesh.com/p/david-reich-2") == (
        "https://www.dwarkesh.com/api/v1/posts/david-reich-2"
    )


def test_api_url_short_link_without_at_author():
    assert _api_url("https://substack.com/p-196892360") == (
        "https://substack.com/api/v1/posts/by-id/196892360"
    )


def test_api_url_rejects_garbage():
    with pytest.raises(ValueError, match="Unrecognized"):
        _api_url("https://www.dwarkesh.com/about")


def _fake_response(post: dict):
    resp = MagicMock()
    resp.json.return_value = {"post": post}
    resp.raise_for_status.return_value = None
    return resp


@patch("pipeline.substack.requests.get")
def test_resolve_post_happy_path(mock_get):
    mock_get.return_value = _fake_response(
        {
            "title": "David Reich – Bronze Age",
            "subtitle": "A subtitle",
            "description": "desc",
            "canonical_url": "https://www.dwarkesh.com/p/david-reich-2",
            "slug": "david-reich-2",
            "audience": "everyone",
            "wordcount": 21163,
            "body_html": "<p>Hello world.</p>",
        }
    )

    post = resolve_post("https://www.dwarkesh.com/p/david-reich-2")

    assert isinstance(post, SubstackPost)
    assert post.title == "David Reich – Bronze Age"
    assert post.canonical_url == "https://www.dwarkesh.com/p/david-reich-2"
    assert post.body_html == "<p>Hello world.</p>"
    assert post.host == "www.dwarkesh.com"
    assert post.wordcount == 21163
    mock_get.assert_called_once()


@patch("pipeline.substack.requests.get")
def test_resolve_post_rejects_paywalled(mock_get):
    mock_get.return_value = _fake_response(
        {
            "title": "Paid Post",
            "audience": "only_paid",
            "should_send_free_preview": True,
            "truncated_body_text": "preview...",
            "body_html": "<p>preview only</p>",
            "canonical_url": "https://x.substack.com/p/paid",
            "slug": "paid",
        }
    )
    with pytest.raises(ValueError, match="paywall"):
        resolve_post("https://x.substack.com/p/paid")


@patch("pipeline.substack.requests.get")
def test_resolve_post_rejects_empty_body(mock_get):
    mock_get.return_value = _fake_response(
        {
            "title": "Empty",
            "audience": "everyone",
            "body_html": "",
            "canonical_url": "https://x.substack.com/p/empty",
            "slug": "empty",
        }
    )
    with pytest.raises(ValueError, match="no body"):
        resolve_post("https://x.substack.com/p/empty")


@patch("pipeline.substack.requests.get")
def test_resolve_post_handles_unwrapped_response(mock_get):
    # The custom-domain by-slug endpoint returns the post at the TOP LEVEL,
    # with no {"post": {...}} wrapper (unlike the by-id endpoint).
    resp = MagicMock()
    resp.json.return_value = {
        "title": "Unwrapped",
        "subtitle": "sub",
        "description": "desc",
        "audience": "everyone",
        "body_html": "<p>Body.</p>",
        "canonical_url": "https://www.dwarkesh.com/p/x",
        "slug": "x",
        "wordcount": 10,
    }
    resp.raise_for_status.return_value = None
    mock_get.return_value = resp

    post = resolve_post("https://www.dwarkesh.com/p/x")

    assert post.title == "Unwrapped"
    assert post.body_html == "<p>Body.</p>"
    assert post.host == "www.dwarkesh.com"
    assert post.wordcount == 10


_SAMPLE_HTML = (
    '<p>I had no idea how wild human history was, says the <a href="x">host</a>.</p>'
    "<h3>Sponsors</h3>"
    "<p>This episode is brought to you by Sponsor X.</p>"
    "<h3>Timestamps</h3>"
    '<p><a href="#a">(00:00:00) – Topic one</a></p>'
    '<p><a href="#b">(00:15:45) – Topic two</a></p>'
    "<h3>Transcript</h3>"
    "<h3>00:00:00 – Topic one</h3>"
    "<p><strong>Dwarkesh Patel</strong> <em>00:00:00</em></p>"
    "<p>Welcome to the show.</p>"
    "<p><strong>David Reich</strong></p>"
    "<p>Glad to be here at 3:00 PM.</p>"
)


def test_html_to_clean_text_strips_boilerplate_and_timestamps():
    out = html_to_clean_text(_SAMPLE_HTML)

    # Intro prose kept (link text kept, URL dropped)
    assert "I had no idea how wild human history was" in out
    assert "host" in out
    assert "href" not in out and "http" not in out

    # Sponsors and Timestamps sections dropped
    assert "Sponsor X" not in out
    assert "Sponsors" not in out
    assert "Timestamps" not in out
    assert "(00:00:00)" not in out

    # Section header kept but timestamp prefix stripped
    assert "Topic one" in out

    # Speaker labels and content kept
    assert "Dwarkesh Patel" in out
    assert "Welcome to the show." in out
    assert "David Reich" in out
    assert "Glad to be here" in out

    # The structural word "Transcript" is dropped
    assert "Transcript" not in out

    # No leftover HH:MM:SS timestamp tokens (inline em + header prefix removed).
    assert not re.search(r"\b\d{1,2}:\d{2}:\d{2}\b", out)


def test_html_to_clean_text_no_leak_from_sub_header_in_sponsors():
    html = (
        "<p>Intro.</p>"
        "<h3>Sponsors</h3>"
        "<h4>Advertiser Inc</h4>"
        "<p>Buy our product.</p>"
        "<h3>Transcript</h3>"
        "<p>Real transcript.</p>"
    )
    out = html_to_clean_text(html)
    assert "Advertiser Inc" not in out
    assert "Buy our product" not in out
    assert "Real transcript" in out
    assert "Intro." in out
