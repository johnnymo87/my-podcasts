from __future__ import annotations

import pytest

from pipeline.fp_writer import build_fp_prompt, generate_fp_script


def test_build_fp_prompt_includes_themes() -> None:
    themes = ["Iran Nuclear Deal", "Russia-Ukraine"]
    articles_by_theme = {
        "Iran Nuclear Deal": ["Article about Iran talks."],
        "Russia-Ukraine": ["Article about Ukraine ceasefire."],
    }
    prompt = build_fp_prompt(themes, articles_by_theme, date_str="2026-03-06")

    assert "2026-03-06" in prompt
    assert "Iran Nuclear Deal" in prompt
    assert "Russia-Ukraine" in prompt
    assert "Article about Iran talks." in prompt
    assert "Article about Ukraine ceasefire." in prompt


def test_build_fp_prompt_includes_context_scripts() -> None:
    themes = ["Trade War"]
    articles_by_theme = {"Trade War": ["US tariffs on China increased."]}
    context_scripts = ["Yesterday we covered the initial tariff announcement."]

    prompt = build_fp_prompt(
        themes,
        articles_by_theme,
        date_str="2026-03-06",
        context_scripts=context_scripts,
    )

    assert "Yesterday we covered the initial tariff announcement." in prompt


def test_build_fp_prompt_no_context_scripts() -> None:
    """Without context_scripts, no context block should appear."""
    themes = ["Trade War"]
    articles_by_theme = {"Trade War": ["US tariffs on China increased."]}

    prompt = build_fp_prompt(themes, articles_by_theme, date_str="2026-03-06")

    # Prompt should still contain essentials
    assert "Trade War" in prompt
    assert "2026-03-06" in prompt


def test_generate_fp_script(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session",
        lambda directory=None: "sess-fp",
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.wait_for_idle",
        lambda session_id, timeout=900: True,
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.get_messages",
        lambda session_id: [
            {"role": "user", "parts": [{"type": "text", "text": "prompt"}]},
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": "Welcome to today's foreign policy briefing.",
                    },
                ],
            },
        ],
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.delete_session",
        lambda session_id: None,
    )

    themes = ["Iran Nuclear Deal"]
    articles_by_theme = {"Iran Nuclear Deal": ["Iran talks resumed in Vienna."]}
    result = generate_fp_script(
        themes, articles_by_theme, date_str="2026-03-06", work_dir=tmp_path
    )

    assert "foreign policy briefing" in result.script
    assert result.summary == ""


def test_generate_fp_returns_writer_output_with_summary(monkeypatch, tmp_path) -> None:
    """generate_fp_script returns WriterOutput with summary when tags present."""
    from pipeline.rundown_writer import WriterOutput

    monkeypatch.setattr(
        "pipeline.fp_writer.create_session", lambda directory=None: "sess-fp-sum"
    )
    monkeypatch.setattr("pipeline.fp_writer.send_prompt_async", lambda sid, text: None)
    monkeypatch.setattr(
        "pipeline.fp_writer.wait_for_idle", lambda sid, timeout=900: True
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.get_messages",
        lambda sid: [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": "<summary>FP summary.</summary>\n\nThe FP script.",
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr("pipeline.fp_writer.delete_session", lambda sid: None)

    result = generate_fp_script(
        themes=["Iran"],
        articles_by_theme={"Iran": ["Article"]},
        date_str="2026-03-06",
        work_dir=tmp_path,
    )
    assert isinstance(result, WriterOutput)
    assert result.summary == "FP summary."
    assert result.script == "The FP script."


def test_generate_fp_script_timeout(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session",
        lambda directory=None: "sess-fp-timeout",
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.wait_for_idle",
        lambda session_id, timeout=900: False,
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        "pipeline.fp_writer.delete_session",
        lambda session_id: deleted.append(session_id),
    )

    themes = ["Iran Nuclear Deal"]
    articles_by_theme = {"Iran Nuclear Deal": ["Iran talks resumed."]}

    with pytest.raises(RuntimeError, match="900 seconds"):
        generate_fp_script(
            themes, articles_by_theme, date_str="2026-03-06", work_dir=tmp_path
        )

    # delete_session must be called in finally block even on error
    assert "sess-fp-timeout" in deleted


def test_generate_fp_script_rejects_empty_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session",
        lambda directory=None: "sess-fp-empty",
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.wait_for_idle",
        lambda session_id, timeout=900: True,
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.get_messages",
        lambda session_id: [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": "<summary>FP summary.</summary>\n\n<script>   </script>",
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.delete_session",
        lambda session_id: None,
    )

    with pytest.raises(RuntimeError, match="empty script"):
        generate_fp_script(
            themes=["Iran Nuclear Deal"],
            articles_by_theme={"Iran Nuclear Deal": ["Iran talks resumed in Vienna."]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )
