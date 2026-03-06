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
    """Verify that generate_briefing_script uses the opencode client."""
    monkeypatch.setattr(
        "pipeline.summarizer.create_session",
        lambda directory=None: "sess-sum",
    )
    monkeypatch.setattr(
        "pipeline.summarizer.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.summarizer.wait_for_idle",
        lambda session_id, timeout=120: True,
    )
    monkeypatch.setattr(
        "pipeline.summarizer.get_messages",
        lambda session_id: [
            {"role": "user", "parts": [{"type": "text", "text": "prompt"}]},
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": "Here is your briefing script about today's news.",
                    },
                ],
            },
        ],
    )
    monkeypatch.setattr(
        "pipeline.summarizer.delete_session",
        lambda session_id: None,
    )

    articles = [
        FetchedArticle(
            url="https://example.com/1", content="Test content.", source_tier="archive"
        ),
    ]
    result = generate_briefing_script(articles, date_str="2026-02-26")
    assert "briefing script" in result
