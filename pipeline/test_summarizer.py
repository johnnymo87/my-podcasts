from __future__ import annotations

from pipeline.article_fetcher import FetchedArticle
from pipeline.summarizer import build_prompt, generate_briefing_script


def test_build_prompt_includes_all_articles() -> None:
    articles = [
        FetchedArticle(
            url="https://example.com/1",
            content="Article about banks.",
            source_tier="live",
        ),
        FetchedArticle(
            url="https://example.com/2",
            content="Tech layoffs headline",
            source_tier="headline_only",
        ),
    ]
    prompt = build_prompt(articles, date_str="2026-02-26")
    assert "Article about banks" in prompt
    assert "Tech layoffs headline" in prompt
    assert "2026-02-26" in prompt
    assert "publicly available" in prompt.lower()
    assert "headline alone" in prompt.lower()


def test_generate_briefing_calls_opencode(monkeypatch) -> None:
    """Verify that generate_briefing_script invokes opencode run."""
    import subprocess

    captured_args = {}

    def fake_run(cmd, **kwargs):
        captured_args["cmd"] = cmd

        class FakeResult:
            returncode = 0
            stdout = "Here is your briefing script about today's news."

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    articles = [
        FetchedArticle(
            url="https://example.com/1", content="Test content.", source_tier="archive"
        ),
    ]
    result = generate_briefing_script(articles, date_str="2026-02-26")
    assert "briefing script" in result
    assert captured_args["cmd"][0] == "opencode"
    assert "run" in captured_args["cmd"]
