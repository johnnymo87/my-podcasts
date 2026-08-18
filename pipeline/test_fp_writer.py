from __future__ import annotations

import pytest

from pipeline.fp_writer import build_fp_prompt, generate_fp_script


# generate_fp_script now enforces min_chars=500 via report_engine.parse_report
# (this migration composes fetch_report_text + parse_report, mirroring the
# rundown_writer migration). Fixtures that exercise the happy path need a
# script body past that floor; short strings like "The FP script." would trip
# the plausibility guard for reasons unrelated to what the test is actually
# checking.
_LONG_SCRIPT = ("Welcome to today's foreign policy briefing. " * 20).strip()


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
        "pipeline.report_engine.create_session",
        lambda directory=None: "sess-fp",
    )
    monkeypatch.setattr(
        "pipeline.report_engine.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle",
        lambda session_id, timeout=900: True,
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_messages",
        lambda session_id: [
            {"role": "user", "parts": [{"type": "text", "text": "prompt"}]},
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": f"<script>{_LONG_SCRIPT}</script>",
                    },
                ],
            },
        ],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_last_assistant_text",
        lambda messages: messages[-1]["parts"][0]["text"],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.delete_session",
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
        "pipeline.report_engine.create_session", lambda directory=None: "sess-fp-sum"
    )
    monkeypatch.setattr(
        "pipeline.report_engine.send_prompt_async", lambda sid, text: None
    )
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle", lambda sid, timeout=900: True
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_messages",
        lambda sid: [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": (
                            f"<summary>FP summary.</summary>\n\n<script>{_LONG_SCRIPT}"
                            "</script>"
                        ),
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_last_assistant_text",
        lambda messages: messages[-1]["parts"][0]["text"],
    )
    monkeypatch.setattr("pipeline.report_engine.delete_session", lambda sid: None)

    result = generate_fp_script(
        themes=["Iran"],
        articles_by_theme={"Iran": ["Article"]},
        date_str="2026-03-06",
        work_dir=tmp_path,
    )
    assert isinstance(result, WriterOutput)
    assert result.summary == "FP summary."
    assert result.script == _LONG_SCRIPT


def test_generate_fp_script_timeout(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pipeline.report_engine.create_session",
        lambda directory=None: "sess-fp-timeout",
    )
    monkeypatch.setattr(
        "pipeline.report_engine.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle",
        lambda session_id, timeout=900: False,
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        "pipeline.report_engine.delete_session",
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
        "pipeline.report_engine.create_session",
        lambda directory=None: "sess-fp-empty",
    )
    monkeypatch.setattr(
        "pipeline.report_engine.send_prompt_async",
        lambda session_id, text: None,
    )
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle",
        lambda session_id, timeout=900: True,
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_messages",
        lambda session_id: [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": (
                            "<summary>FP summary.</summary>\n\n<script>   </script>"
                        ),
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_last_assistant_text",
        lambda messages: messages[-1]["parts"][0]["text"],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.delete_session",
        lambda session_id: None,
    )

    with pytest.raises(RuntimeError, match="empty script"):
        generate_fp_script(
            themes=["Iran Nuclear Deal"],
            articles_by_theme={"Iran Nuclear Deal": ["Iran talks resumed in Vienna."]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )
    assert not (tmp_path / "raw_writer_output.txt").exists()


def test_persists_fp_raw_output_before_parsing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pipeline.report_engine.create_session", lambda directory=None: "ses_persist"
    )
    monkeypatch.setattr("pipeline.report_engine.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle", lambda sid, timeout=900: True
    )
    raw_text = f"<summary>FP summary.</summary>\n\n<script>{_LONG_SCRIPT}</script>"
    monkeypatch.setattr(
        "pipeline.report_engine.get_messages",
        lambda sid: [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": raw_text,
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_last_assistant_text",
        lambda messages: messages[-1]["parts"][0]["text"],
    )
    monkeypatch.setattr("pipeline.report_engine.delete_session", lambda sid: None)

    result = generate_fp_script(
        themes=["Iran"],
        articles_by_theme={"Iran": ["Article"]},
        date_str="2026-03-06",
        work_dir=tmp_path,
    )

    raw_path = tmp_path / "raw_writer_output.txt"
    assert raw_path.exists()
    assert raw_path.read_text(encoding="utf-8") == raw_text
    assert result.script == _LONG_SCRIPT
    assert result.summary == "FP summary."


def test_reuses_fp_persisted_output_when_present(monkeypatch, tmp_path) -> None:
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        f"<summary>Cached.</summary>\n\n<script>{_LONG_SCRIPT}</script>",
        encoding="utf-8",
    )

    called = {"create": 0}
    monkeypatch.setattr(
        "pipeline.report_engine.create_session",
        lambda directory=None: (
            called.__setitem__("create", called["create"] + 1) or "ses"
        ),
    )
    monkeypatch.setattr("pipeline.report_engine.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle", lambda sid, timeout=900: True
    )
    monkeypatch.setattr("pipeline.report_engine.get_messages", lambda sid: [])
    monkeypatch.setattr("pipeline.report_engine.delete_session", lambda sid: None)

    result = generate_fp_script(
        themes=["Iran"],
        articles_by_theme={"Iran": ["Article"]},
        date_str="2026-03-06",
        work_dir=tmp_path,
    )

    assert result.script == _LONG_SCRIPT
    assert result.summary == "Cached."
    assert called["create"] == 0


def test_deletes_fp_persisted_output_on_parse_failure(monkeypatch, tmp_path) -> None:
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Empty.</summary>\n\n<script>   </script>",
        encoding="utf-8",
    )

    called = {"create": 0}
    monkeypatch.setattr(
        "pipeline.report_engine.create_session",
        lambda directory=None: (
            called.__setitem__("create", called["create"] + 1) or "ses"
        ),
    )

    with pytest.raises(RuntimeError, match="empty script"):
        generate_fp_script(
            themes=["Iran"],
            articles_by_theme={"Iran": ["Article"]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )

    assert not raw_path.exists()
    assert called["create"] == 0


def test_does_not_persist_fp_when_wait_for_idle_times_out(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "pipeline.report_engine.create_session", lambda directory=None: "ses_timeout"
    )
    monkeypatch.setattr("pipeline.report_engine.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle", lambda sid, timeout=900: False
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        "pipeline.report_engine.delete_session", lambda sid: deleted.append(sid)
    )

    with pytest.raises(RuntimeError, match="900 seconds"):
        generate_fp_script(
            themes=["Iran"],
            articles_by_theme={"Iran": ["Article"]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )

    assert not (tmp_path / "raw_writer_output.txt").exists()
    assert "ses_timeout" in deleted


def test_generate_fp_script_requires_a_script_tag(monkeypatch, tmp_path) -> None:
    """require_tags=True is in force through the FP writer: no <script> tag
    at all is a loud refusal, not a narration of raw model reasoning.

    Before this migration, raw model output with no <script> tags at all
    passed straight through (via parse_summary's remainder + _extract_script's
    permissive fallback) and got narrated to subscribers as the episode. FP
    Digest is an automated, no-human-in-the-loop publish path, so that
    fallback must be closed here exactly as it was for the Rundown.
    """
    monkeypatch.setattr(
        "pipeline.report_engine.create_session", lambda directory=None: "ses_notags"
    )
    monkeypatch.setattr("pipeline.report_engine.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle", lambda sid, timeout=900: True
    )
    no_tag_text = (
        "Let me think about today's stories and how to frame them for the "
        "listener before I write anything down. Still thinking it over."
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_messages",
        lambda sid: [
            {"role": "assistant", "parts": [{"type": "text", "text": no_tag_text}]}
        ],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_last_assistant_text",
        lambda messages: messages[-1]["parts"][0]["text"],
    )
    monkeypatch.setattr("pipeline.report_engine.delete_session", lambda sid: None)

    with pytest.raises(RuntimeError, match="no trustworthy <script> markup"):
        generate_fp_script(
            themes=["Iran"],
            articles_by_theme={"Iran": ["Article"]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )

    # A require_tags refusal is a parse failure like any other -- the raw
    # file must not survive to loop the next retry on the same broken output.
    assert not (tmp_path / "raw_writer_output.txt").exists()


def test_generate_fp_script_refuses_leaked_covered_tag(monkeypatch, tmp_path) -> None:
    """The <covered> leak guard is in force through the FP writer too.

    FP Digest is one of only two callers that emit <covered> (the other is
    The Rundown), so this exercises report_engine's guard for it through a
    second real caller.
    """
    monkeypatch.setattr(
        "pipeline.report_engine.create_session", lambda directory=None: "ses_leak"
    )
    monkeypatch.setattr("pipeline.report_engine.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.report_engine.wait_for_idle", lambda sid, timeout=900: True
    )
    leaked_text = (
        "<script>Hey listeners, <covered>oops</covered> here is today's "
        "briefing, padded out well past the minimum plausible length so "
        "only the leaked markup guard -- not the length floor -- is what "
        "trips this refusal.</script>"
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_messages",
        lambda sid: [
            {"role": "assistant", "parts": [{"type": "text", "text": leaked_text}]}
        ],
    )
    monkeypatch.setattr(
        "pipeline.report_engine.get_last_assistant_text",
        lambda messages: messages[-1]["parts"][0]["text"],
    )
    monkeypatch.setattr("pipeline.report_engine.delete_session", lambda sid: None)

    with pytest.raises(RuntimeError, match="leaked"):
        generate_fp_script(
            themes=["Iran"],
            articles_by_theme={"Iran": ["Article"]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )

    assert not (tmp_path / "raw_writer_output.txt").exists()


def test_only_runtimeerror_triggers_fp_raw_file_cleanup(tmp_path) -> None:
    """The except clause catches RuntimeError only -- pin that deliberately.

    Every refusal report_engine.parse_report raises today is a RuntimeError,
    so this passes. A future engine exception of a different type must NOT be
    silently swallowed into a delete-and-retry loop; it should propagate, and
    the persisted raw file should be left untouched rather than deleted for an
    error the except clause was never written to handle.
    """
    from unittest.mock import patch

    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(f"<script>{_LONG_SCRIPT}</script>", encoding="utf-8")

    with patch(
        "pipeline.fp_writer.parse_report",
        side_effect=ValueError("not a RuntimeError"),
    ):
        with pytest.raises(ValueError):
            generate_fp_script(
                themes=["Iran"],
                articles_by_theme={"Iran": ["Article"]},
                date_str="2026-03-06",
                work_dir=tmp_path,
            )

    assert raw_path.exists()


def test_prompt_forbids_a_closing_recap():
    """FP had TWO closings: a full recap paragraph and a sign-off.

    Measured on the 2026-08-17 episode: "So here is where things stand this
    Monday..." restated all five stories, then a separate sign-off followed.
    """
    from pipeline.fp_writer import PROMPT_TEMPLATE

    assert "Do NOT end with a recap" in PROMPT_TEMPLATE
    assert "naming no stories" in PROMPT_TEMPLATE
    assert "a brief sign-off are\nuseful" not in PROMPT_TEMPLATE
