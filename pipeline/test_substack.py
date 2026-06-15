from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.substack import SubstackPost, _api_url, resolve_post


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
