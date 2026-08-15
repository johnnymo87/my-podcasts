from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.exa_client import (
    ExaResult,
    exa_file_path,
    exa_text_if_hit,
    search_related,
    search_related_status,
)


def _make_mock_result(title: str, url: str, text: str) -> MagicMock:
    r = MagicMock()
    r.title = title
    r.url = url
    r.text = text
    return r


def test_search_related_returns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Exa, verify results are returned as ExaResult dataclasses."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    mock_response = MagicMock()
    mock_response.results = [
        _make_mock_result("Article One", "https://example.com/1", "Some text here"),
        _make_mock_result("Article Two", "https://example.com/2", "More text here"),
    ]

    with patch("pipeline.exa_client.Exa") as MockExa:
        mock_exa_instance = MockExa.return_value
        mock_exa_instance.search.return_value = mock_response

        results = search_related("test headline")

    assert len(results) == 2
    assert isinstance(results[0], ExaResult)
    assert results[0].title == "Article One"
    assert results[0].url == "https://example.com/1"
    assert results[0].text == "Some text here"
    assert isinstance(results[1], ExaResult)
    assert results[1].title == "Article Two"


def test_search_related_with_include_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Exa, verify include_domains is passed through."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    mock_response = MagicMock()
    mock_response.results = [
        _make_mock_result("Domain Article", "https://reuters.com/1", "Reuters text"),
    ]

    with patch("pipeline.exa_client.Exa") as MockExa:
        mock_exa_instance = MockExa.return_value
        mock_exa_instance.search.return_value = mock_response

        results = search_related(
            "test headline",
            include_domains=["reuters.com", "apnews.com"],
        )

        call_kwargs = mock_exa_instance.search.call_args
        assert call_kwargs.kwargs.get("include_domains") == [
            "reuters.com",
            "apnews.com",
        ]

    assert len(results) == 1
    assert results[0].title == "Domain Article"


def test_search_related_returns_empty_on_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No EXA_API_KEY env var → returns []."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    results = search_related("some headline")

    assert results == []


def test_search_related_returns_empty_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exa constructor raises → returns []."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    with patch("pipeline.exa_client.Exa") as MockExa:
        MockExa.side_effect = RuntimeError("connection failed")

        results = search_related("some headline")

    assert results == []


def test_search_related_status_returns_no_key_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No EXA_API_KEY env var → ([], "no_key")."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    results, status = search_related_status("some headline")

    assert results == []
    assert status == "no_key"


def test_search_related_status_returns_empty_when_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key set, search returns response with empty results → ([], "empty")."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    mock_response = MagicMock()
    mock_response.results = []

    with patch("pipeline.exa_client.Exa") as MockExa:
        mock_exa_instance = MockExa.return_value
        mock_exa_instance.search.return_value = mock_response

        results, status = search_related_status("some headline")

    assert results == []
    assert status == "empty"


def test_search_related_status_returns_error_with_exception_class_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key set, search raises TimeoutError → ([], "error:TimeoutError")."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    with patch("pipeline.exa_client.Exa") as MockExa:
        mock_exa_instance = MockExa.return_value
        mock_exa_instance.search.side_effect = TimeoutError("timed out")

        results, status = search_related_status("some headline")

    assert results == []
    assert status == "error:TimeoutError"


def test_search_related_still_swallows_errors_and_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_related (FP contract) keeps returning [] on error, no status."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    with patch("pipeline.exa_client.Exa") as MockExa:
        mock_exa_instance = MockExa.return_value
        mock_exa_instance.search.side_effect = RuntimeError("connection failed")

        results = search_related("some headline")

    assert results == []


# --- exa_text_if_hit / exa_file_path ---


def test_exa_file_path_builds_expected_layout(tmp_path) -> None:
    """exa_file_path constructs work_dir/enrichment/exa/{slug}.md."""
    path = exa_file_path(tmp_path, "some-slug")

    assert path == tmp_path / "enrichment" / "exa" / "some-slug.md"


def test_exa_text_if_hit_returns_text_on_hit(tmp_path) -> None:
    """Result: hit -> file contents are returned."""
    exa_file_path(tmp_path, "story").parent.mkdir(parents=True)
    exa_file_path(tmp_path, "story").write_text(
        "# Story\nResult: hit\nQuery: story\n\nFull text.", encoding="utf-8"
    )

    assert "Full text." in exa_text_if_hit(tmp_path, "story")


def test_exa_text_if_hit_gated_on_empty(tmp_path) -> None:
    """Result: empty -> gated out, empty string returned."""
    exa_file_path(tmp_path, "story").parent.mkdir(parents=True)
    exa_file_path(tmp_path, "story").write_text(
        "# Story\nResult: empty\nQuery: story\n\n", encoding="utf-8"
    )

    assert exa_text_if_hit(tmp_path, "story") == ""


def test_exa_text_if_hit_gated_on_no_key_or_error_status(tmp_path) -> None:
    """Result: no_key and Result: error:* also gate out (same != 'hit' branch)."""
    for status in ("no_key", "error:TimeoutError"):
        exa_file_path(tmp_path, "story").parent.mkdir(parents=True, exist_ok=True)
        exa_file_path(tmp_path, "story").write_text(
            f"# Story\nResult: {status}\nQuery: story\n\n", encoding="utf-8"
        )

        assert exa_text_if_hit(tmp_path, "story") == "", status


def test_exa_text_if_hit_trusts_headerless_fp_file(tmp_path) -> None:
    """No Result: header (FP format) -> trusted as-is. Permanent, not legacy."""
    exa_file_path(tmp_path, "story").parent.mkdir(parents=True)
    exa_file_path(tmp_path, "story").write_text(
        "# Story\n\nURL: https://example.com\n\nArticle text.", encoding="utf-8"
    )

    assert "Article text." in exa_text_if_hit(tmp_path, "story")


def test_exa_text_if_hit_missing_file_returns_empty(tmp_path) -> None:
    """No file at all -> empty string, not an error."""
    assert exa_text_if_hit(tmp_path, "nonexistent") == ""
