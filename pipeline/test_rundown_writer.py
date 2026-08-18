from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.rundown_writer import (
    WriterOutput,
    _extract_script,
    build_rundown_prompt,
    generate_rundown_script,
    parse_covered,
    parse_summary,
)


# generate_rundown_script now enforces min_chars=500 via report_engine.parse_report
# (task 2 of the report_engine migration). Fixtures that exercise the happy path
# need a script body past that floor; short strings like "Hey, welcome to The
# Rundown." (30 chars) would trip the new plausibility guard for reasons unrelated
# to what the test is actually checking.
_LONG_SCRIPT = ("Hey, welcome to The Rundown. " * 20).strip()


def test_build_prompt_basic():
    prompt = build_rundown_prompt(
        sections=[
            ("Tech", ["Article about tech"]),
            ("Finance", ["Article about finance"]),
        ],
        date_str="2026-03-10",
    )
    assert "2026-03-10" in prompt
    assert "Tech" in prompt
    assert "Finance" in prompt
    assert "Article about tech" in prompt
    assert "Article about finance" in prompt
    assert "The Rundown" in prompt


def test_build_prompt_instructs_outlet_attribution_for_open_access():
    prompt = build_rundown_prompt(
        sections=[("Tech", ["Article about tech"])],
        date_str="2026-03-10",
    )
    assert "Related coverage from other outlets" in prompt
    assert "name the outlet" in prompt


def test_build_prompt_with_context():
    prompt = build_rundown_prompt(
        sections=[("Tech", ["New article"])],
        date_str="2026-03-10",
        context_scripts=["Yesterday's script content"],
    )
    assert "PRIOR EPISODES" in prompt
    assert "Yesterday's script content" in prompt


def test_build_prompt_without_context():
    prompt = build_rundown_prompt(
        sections=[("Tech", ["Article"])],
        date_str="2026-03-10",
        context_scripts=None,
    )
    assert "PRIOR EPISODES" not in prompt


def test_prompt_themes_list_matches_sections_exactly():
    """No theme is announced that has no material beneath it."""
    prompt = build_rundown_prompt(
        sections=[("Tech", ["Article about tech"])], date_str="2026-03-10"
    )
    assert "- Tech" in prompt and "## Tech" in prompt


def test_prompt_renders_orphan_section_under_its_own_name():
    prompt = build_rundown_prompt(
        sections=[("Alpha", ["a"]), ("Invented Name", ["b"])], date_str="2026-03-10"
    )
    assert "## Invented Name" in prompt and "b" in prompt


def test_prompt_never_renders_a_theme_header_with_no_articles():
    """Boundary hardening: the renderer itself must never emit a bare header.

    Task 1's assembler guarantees this today by construction, but the FP
    Digest port (my-podcasts-tj9) will call this same renderer with sections
    built by different code. A section with an empty article list must not
    appear anywhere in the prompt -- not as a rendered '## Empty' header, and
    not in the derived TODAY'S THEMES list either, so the two stay
    consistent with each other.
    """
    prompt = build_rundown_prompt(
        sections=[("Alpha", ["real"]), ("Empty", [])], date_str="2026-03-10"
    )
    assert "Empty" not in prompt
    assert "## Alpha" in prompt


def test_prompt_matches_legacy_rendering_when_there_is_nothing_to_fix():
    """Permanent guard: on a normal day the prompt is byte-identical to the old one.

    Carries the OLD rendering logic inline as a reference implementation. For input
    where every plan theme has >=1 article and there are no orphans -- i.e. the
    overwhelmingly common case -- the new builder must produce exactly what the old
    one did. This pins 'identical except the two intended changes' in CI, which the
    one-time manual diff in Task 6 cannot do.
    """
    themes = ["Alpha", "Beta"]
    articles = {"Alpha": ["a1", "a2"], "Beta": ["b1"]}
    legacy_sections = []
    for theme in themes:  # old logic, verbatim
        lines = [f"## {theme}"]
        for j, art in enumerate(articles[theme], 1):
            lines.append(f"### Source {j}")
            lines.append(art)
        legacy_sections.append("\n".join(lines))
    legacy_block = "\n\n".join(legacy_sections)

    prompt = build_rundown_prompt(
        sections=[(t, articles[t]) for t in themes], date_str="2026-03-10"
    )
    assert legacy_block in prompt


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_script(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    mock_create.return_value = "ses_123"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    # No <script> tags here would now trip require_tags=True (a separate test
    # pins that refusal below); this test is about the basic happy path.
    mock_text.return_value = f"<script>{_LONG_SCRIPT}</script>"

    result = generate_rundown_script(
        sections=[("Tech", ["Article"])],
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    assert result.script == _LONG_SCRIPT
    assert result.summary == ""
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_wait.assert_called_once_with("ses_123", timeout=900)
    mock_delete.assert_called_once_with("ses_123")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_script_timeout_raises(
    mock_create, mock_send, mock_wait, mock_delete, tmp_path
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            sections=[("Tech", ["Article"])],
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "900 seconds" in str(e)

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_script_extracts_script_tags(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    mock_create.return_value = "ses_456"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = f"Let me think.\n\n<script>{_LONG_SCRIPT}</script>"

    result = generate_rundown_script(
        sections=[("Tech", ["Article"])],
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    assert result.script == _LONG_SCRIPT
    assert result.summary == ""


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_rundown_script_rejects_empty_output(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Today.</summary>\n\n<script>   </script>"

    try:
        generate_rundown_script(
            sections=[("Tech", ["Article"])],
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "empty script" in str(e)

    assert not (tmp_path / "raw_writer_output.txt").exists()


def test_extract_script_with_tags():
    """<script> tags extract the spoken script."""
    raw = (
        "Let me analyze what's new.\n\n"
        "<summary>Today covers markets and AI.</summary>\n\n"
        "<script>Hey, welcome to The Rundown for Thursday.</script>"
    )
    assert _extract_script(raw) == "Hey, welcome to The Rundown for Thursday."


def test_extract_script_tags_with_reasoning():
    """Reasoning before <script> tags is stripped."""
    raw = (
        "I see 8 stories. Let me figure out what's new vs repeated.\n"
        "Stories 1-3 are new, 4-8 were covered yesterday.\n\n"
        "<script>\nHey, welcome. Three stories today.\n\n"
        "First up, markets moved.\n</script>"
    )
    assert _extract_script(raw) == (
        "Hey, welcome. Three stories today.\n\nFirst up, markets moved."
    )


def test_extract_script_no_tags_returns_raw():
    """Without <script> tags, the full text is returned as-is."""
    raw = "Hey, welcome to The Rundown for Monday."
    assert _extract_script(raw) == raw


def test_parse_summary_extracts_tags():
    text = "<summary>A brief summary.</summary>\n\nHey, welcome to The Rundown."
    result = parse_summary(text)
    assert result.summary == "A brief summary."
    assert result.script == "Hey, welcome to The Rundown."


def test_parse_summary_no_tags():
    text = "Hey, welcome to The Rundown."
    result = parse_summary(text)
    assert result.summary == ""
    assert result.script == "Hey, welcome to The Rundown."


def test_parse_summary_multiline():
    text = "<summary>\nLine one.\nLine two.\n</summary>\n\nThe script."
    result = parse_summary(text)
    assert result.summary == "Line one.\nLine two."
    assert result.script == "The script."


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_rundown_returns_writer_output_with_summary(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    """generate_rundown_script returns WriterOutput with summary when tags present."""
    mock_create.return_value = "ses_wout"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    # An untagged remainder used to become the script via the old
    # parse_summary + _extract_script(remainder) fallback. require_tags=True
    # now refuses that shape (a separate test pins the refusal), so this
    # fixture needs a real <script> block to exercise the intended path:
    # summary extraction alongside script extraction.
    mock_text.return_value = (
        f"<summary>Today's summary.</summary>\n\n<script>{_LONG_SCRIPT}</script>"
    )

    result = generate_rundown_script(
        sections=[("Tech", ["Article"])],
        date_str="2026-03-10",
        work_dir=tmp_path,
    )
    assert isinstance(result, WriterOutput)
    assert result.summary == "Today's summary."
    assert result.script == _LONG_SCRIPT


def test_parse_covered_extracts_headlines():
    """<covered> tags extract a list of headlines."""
    text = (
        "<covered>\n"
        "- Deutsche Bank Flags $30 Billion Exposure to Private Credit\n"
        "- Sunday Robotics Dishwashing Robot\n"
        "- ChatGPT Practiced Law Badly\n"
        "</covered>"
    )
    result = parse_covered(text)
    assert result == [
        "Deutsche Bank Flags $30 Billion Exposure to Private Credit",
        "Sunday Robotics Dishwashing Robot",
        "ChatGPT Practiced Law Badly",
    ]


def test_parse_covered_no_tags_returns_empty():
    """Without <covered> tags, returns empty list."""
    text = "Hey, welcome to The Rundown."
    result = parse_covered(text)
    assert result == []


def test_parse_covered_strips_whitespace():
    """Headlines are stripped of leading/trailing whitespace and dashes."""
    text = "<covered>\n  - Some Headline  \n  Another Headline\n- Third One\n</covered>"
    result = parse_covered(text)
    assert result == ["Some Headline", "Another Headline", "Third One"]


def test_parse_covered_skips_empty_lines():
    """Empty lines inside <covered> are ignored."""
    text = "<covered>\n- Headline One\n\n- Headline Two\n\n</covered>"
    result = parse_covered(text)
    assert result == ["Headline One", "Headline Two"]


def test_writer_output_has_covered_headlines():
    """WriterOutput includes covered_headlines field."""
    wo = WriterOutput(
        script="The script.",
        summary="A summary.",
        covered_headlines=["Story A", "Story B"],
    )
    assert wo.covered_headlines == ["Story A", "Story B"]


def test_writer_output_covered_defaults_empty():
    """WriterOutput.covered_headlines defaults to empty list."""
    wo = WriterOutput(script="The script.", summary="A summary.")
    assert wo.covered_headlines == []


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_script_parses_covered_tags(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    """generate_rundown_script populates covered_headlines from <covered> tags."""
    mock_create.return_value = "ses_cov"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Markets and AI today.</summary>\n\n"
        "<covered>\n"
        "- Deutsche Bank Exposure\n"
        "- ChatGPT Lawsuit\n"
        "</covered>\n\n"
        f"<script>{_LONG_SCRIPT}</script>"
    )

    result = generate_rundown_script(
        sections=[("Finance", ["Article"])],
        date_str="2026-03-12",
        work_dir=tmp_path,
    )

    assert result.script == _LONG_SCRIPT
    assert result.summary == "Markets and AI today."
    assert result.covered_headlines == ["Deutsche Bank Exposure", "ChatGPT Lawsuit"]


def test_generate_script_prompt_asks_for_covered_tags():
    """The Rundown prompt instructs the writer to emit <covered> tags."""
    # The instruction is prepended in generate_rundown_script, not in the prompt
    # itself. Check the instruction text instead.
    import inspect

    from pipeline.rundown_writer import generate_rundown_script

    source = inspect.getsource(generate_rundown_script)
    assert "<covered>" in source


def test_rundown_editor_uses_coverage_ledger_over_scripts(monkeypatch):
    """When coverage_ledger is provided, scripts are not included in prompt."""
    from unittest.mock import patch

    from pipeline.things_happen_editor import (
        RundownResearchPlan,
        generate_rundown_research_plan,
    )

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    mock_plan = RundownResearchPlan(
        themes=["Theme A"], directives=[], rotation_override=None
    )
    mock_response = MagicMock()
    mock_response.parsed = mock_plan

    with patch("pipeline.things_happen_editor.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        generate_rundown_research_plan(
            ["Test headline\nContext: ..."],
            context_scripts=["Old script text here"],
            coverage_ledger="## COVERAGE LEDGER\n| Theme | Days |",
        )

        prompt_used = mock_client.models.generate_content.call_args[1]["contents"]
        assert "COVERAGE LEDGER" in prompt_used
        assert "Old script text here" not in prompt_used


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_persists_raw_output_before_parsing(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    """The raw assistant text is written to raw_writer_output.txt the moment
    it's available, before any parsing happens."""
    mock_create.return_value = "ses_persist"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    raw_text = f"<summary>Today's summary.</summary>\n\n<script>{_LONG_SCRIPT}</script>"
    mock_text.return_value = raw_text

    result = generate_rundown_script(
        sections=[("Tech", ["Article"])],
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    raw_path = tmp_path / "raw_writer_output.txt"
    assert raw_path.exists()
    assert raw_path.read_text(encoding="utf-8") == raw_text
    assert result.script == _LONG_SCRIPT
    assert result.summary == "Today's summary."


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_reuses_persisted_output_when_present(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    """If raw_writer_output.txt already exists, the model is not called."""
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        f"<summary>Cached summary.</summary>\n\n<script>{_LONG_SCRIPT}</script>",
        encoding="utf-8",
    )

    result = generate_rundown_script(
        sections=[("Tech", ["Article"])],
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    assert result.script == _LONG_SCRIPT
    assert result.summary == "Cached summary."
    mock_create.assert_not_called()
    mock_send.assert_not_called()
    mock_wait.assert_not_called()
    mock_messages.assert_not_called()
    mock_delete.assert_not_called()


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_deletes_persisted_output_on_parse_failure(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    """If a persisted file parses to an empty script, the file is deleted
    so the next retry regenerates."""
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Empty body.</summary>\n\n<script>   </script>",
        encoding="utf-8",
    )

    try:
        generate_rundown_script(
            sections=[("Tech", ["Article"])],
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "empty script" in str(e)

    assert not raw_path.exists()
    mock_create.assert_not_called()


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_does_not_persist_when_wait_for_idle_times_out(
    mock_create, mock_send, mock_wait, mock_delete, tmp_path
):
    """If wait_for_idle returns False, no raw_writer_output.txt is written."""
    mock_create.return_value = "ses_timeout_persist"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            sections=[("Tech", ["Article"])],
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "900 seconds" in str(e)

    raw_path = tmp_path / "raw_writer_output.txt"
    assert not raw_path.exists()
    mock_delete.assert_called_once_with("ses_timeout_persist")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_script_requires_a_script_tag(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    """require_tags=True is in force: no <script> tag at all is a loud refusal.

    Before this migration, raw model output with no <script> tags at all
    passed straight through (via parse_summary's remainder + _extract_script's
    permissive fallback) and got narrated to subscribers as the episode. This
    is an automated, no-human-in-the-loop publish path, so that fallback must
    now be closed.
    """
    mock_create.return_value = "ses_notags"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "Let me think about today's stories and how to frame them for the "
        "listener before I write anything down. Still thinking it over."
    )

    with pytest.raises(RuntimeError, match="no <script> tag"):
        generate_rundown_script(
            sections=[("Tech", ["Article"])],
            date_str="2026-03-10",
            work_dir=tmp_path,
        )

    # A require_tags refusal is a parse failure like any other -- the raw
    # file must not survive to loop the next retry on the same broken output.
    assert not (tmp_path / "raw_writer_output.txt").exists()


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_script_refuses_leaked_covered_tag(
    mock_create,
    mock_send,
    mock_wait,
    mock_messages,
    mock_text,
    mock_delete,
    tmp_path,
):
    """The <covered> leak guard is in force through this writer.

    Only the daily writers emit <covered>, so report_engine's guard for it
    (added in task 1) was previously untested through a real caller. A
    well-formed <script> block whose content happens to still carry a
    literal <covered> tag (e.g. truncated/mangled tag structure) must be
    refused, not narrated aloud with the tag read as text.
    """
    mock_create.return_value = "ses_leak"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<script>Hey listeners, <covered>oops</covered> here is today's "
        "briefing, padded out well past the minimum plausible length so "
        "only the leaked markup guard -- not the length floor -- is what "
        "trips this refusal.</script>"
    )

    with pytest.raises(RuntimeError, match="leaked"):
        generate_rundown_script(
            sections=[("Tech", ["Article"])],
            date_str="2026-03-10",
            work_dir=tmp_path,
        )

    assert not (tmp_path / "raw_writer_output.txt").exists()


def test_generate_refuses_before_reading_persisted_raw_file(tmp_path):
    """The no-sections refusal fires before the reuse path, even with a file present.

    A retry after an all-empty collection run must not be able to launder a
    stale-but-valid script left on disk from an earlier, unrelated successful
    run -- the guard has to run first regardless of what raw_writer_output.txt
    contains.
    """
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(f"<script>{_LONG_SCRIPT}</script>", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no section has any article text"):
        generate_rundown_script(
            sections=[("Alpha", [])],
            date_str="2026-03-10",
            work_dir=tmp_path,
        )

    # The refusal must not have touched the persisted file either way.
    assert raw_path.exists()


def test_only_runtimeerror_triggers_raw_file_cleanup(tmp_path):
    """The except clause catches RuntimeError only -- pin that deliberately.

    Every refusal report_engine.parse_report raises today is a RuntimeError,
    so this passes. A future engine exception of a different type must NOT be
    silently swallowed into a delete-and-retry loop; it should propagate, and
    the persisted raw file should be left untouched rather than deleted for an
    error the except clause was never written to handle.
    """
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(f"<script>{_LONG_SCRIPT}</script>", encoding="utf-8")

    with patch(
        "pipeline.rundown_writer.parse_report",
        side_effect=ValueError("not a RuntimeError"),
    ):
        with pytest.raises(ValueError):
            generate_rundown_script(
                sections=[("Tech", ["Article"])],
                date_str="2026-03-10",
                work_dir=tmp_path,
            )

    assert raw_path.exists()


def test_rundown_editor_falls_back_to_scripts(monkeypatch):
    """When no coverage_ledger, context_scripts are used."""
    from unittest.mock import patch

    from pipeline.things_happen_editor import (
        RundownResearchPlan,
        generate_rundown_research_plan,
    )

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    mock_plan = RundownResearchPlan(
        themes=["Theme A"], directives=[], rotation_override=None
    )
    mock_response = MagicMock()
    mock_response.parsed = mock_plan

    with patch("pipeline.things_happen_editor.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = mock_response

        generate_rundown_research_plan(
            ["Test headline\nContext: ..."],
            context_scripts=["Script from yesterday"],
            coverage_ledger=None,
        )

        prompt_used = mock_client.models.generate_content.call_args[1]["contents"]
        assert "Script from yesterday" in prompt_used
        assert "Previous episodes" in prompt_used


def test_generate_refuses_to_write_from_zero_sections():
    """A total resolution failure must not yield a fabricated episode.

    If every directive fails to resolve, the prompt would carry an empty
    STORIES BY THEME block while still instructing the model to produce a
    briefing -- and it would, entirely from parametric memory, published
    unread. The writer must refuse so the consumer backs off and retries.
    """
    import pytest

    with pytest.raises(RuntimeError, match="no section has any article text"):
        generate_rundown_script(
            sections=[],
            date_str="2026-03-10",
        )


def test_generate_refuses_when_sections_exist_but_all_are_empty():
    """Same guard, but for sections present with no article text in them."""
    import pytest

    with pytest.raises(RuntimeError, match="no section has any article text"):
        generate_rundown_script(
            sections=[("Alpha", []), ("Beta", [])],
            date_str="2026-03-10",
        )


def test_prompt_forbids_a_closing_recap():
    """The writer must not re-narrate the episode at the end.

    Measured on the 2026-08-17 episode: the closing recap ran 182 words of
    1907 (10% of runtime) and restated all six stories the listener had just
    heard. The prompt had only asked for "a brief sign-off" -- the recap was
    the model's own addition, so forbidding it has to be explicit.
    """
    from pipeline.rundown_writer import PROMPT_TEMPLATE

    assert "Do NOT end with a recap" in PROMPT_TEMPLATE
    assert "naming no stories" in PROMPT_TEMPLATE
    # The old wording invited the behavior; make sure it is gone.
    assert "a brief sign-off are\nuseful" not in PROMPT_TEMPLATE


def test_extract_script_picks_the_longest_block_not_the_first():
    """A placeholder <script>...</script> before the real one must not win.

    Real incident, 2026-08-18 FP Digest: the model emitted a literal
    `<script>...</script>` sketch at offset 647 before writing the real script
    at 2523. The non-greedy regex matched the placeholder, so a 3-byte script
    was TTS'd and a 2636-byte mp3 shipped to subscribers (a normal episode is
    ~3 MB). The full correct script was sitting in raw_writer_output.txt.
    """
    from pipeline.rundown_writer import _extract_script

    raw = (
        "planning notes\n<script>...</script>\nmore notes\n"
        "<script>\nThe real briefing text, which is much longer.\n</script>\n"
    )
    assert _extract_script(raw) == "The real briefing text, which is much longer."


def test_extract_script_returns_text_when_no_tags():
    from pipeline.rundown_writer import _extract_script

    assert _extract_script("no tags here") == "no tags here"


def test_writer_rejects_an_implausibly_short_script():
    """`...` is not empty, so the empty-check let it through to TTS.

    The guard must be a plausibility floor, not an emptiness check.
    """
    from pipeline.rundown_writer import _validate_script_length

    with pytest.raises(RuntimeError, match="too short"):
        _validate_script_length("...", "Rundown")
    # A real script passes untouched.
    _validate_script_length("word " * 200, "Rundown")
