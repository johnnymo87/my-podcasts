from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.report_engine import (
    ReportOutput,
    extract_script,
    extract_summary,
    fetch_report_text,
    parse_report,
    run_report_prompt,
)


# --- extraction ---


def test_extract_script_with_tags():
    raw = "Reasoning here.\n\n<script>The briefing text.</script>"
    assert extract_script(raw) == "The briefing text."


def test_extract_script_picks_longest_block():
    """Regression for the 2026-08-18 incident (commit 39589e3)."""
    raw = "<script>...</script>\n\n<script>" + "The real script. " * 50 + "</script>"
    assert extract_script(raw).startswith("The real script.")
    assert len(extract_script(raw)) > 500


def test_extract_script_unclosed_final_block_is_recovered():
    """my-podcasts-ne0, composed with the longest-block case.

    NOTE: the original spec for this test asserted ``"scrip" not in
    extract_script(raw)``. That is unsatisfiable by construction -- the
    fixture body is "The real script. " repeated, and the English word
    "script" itself contains "scrip" as a substring, independent of any
    implementation. The real regression concern is the mangled closing-tag
    artifact (``</scrip>``) leaking into the narrated output, so that is what
    this asserts instead.
    """
    real = "The real script. " * 50
    raw = f"<script>...</script>\n\n<script>{real}</scrip>"
    assert extract_script(raw).startswith("The real script.")
    assert "</scrip>" not in extract_script(raw)


def test_extract_script_trailing_chatter_does_not_outcompete_real_script():
    """Adversarial-review find: a well-formed, properly-closed real script
    followed by unrelated model chatter after a STRAY literal '<script>'
    mention must not lose to that chatter on length.

    Before this fix, ANY trailing '<script>' with no matching close was
    treated as an unclosed block and appended as a length-competing
    candidate -- even when a perfectly good, properly-closed script already
    existed. That is the same improbability class as the placeholder bug
    that fired in production on 2026-08-18: models chattering around tags is
    proven behavior, not a hypothetical.
    """
    real = "Real script sentence. " * 40
    chatter = "Model chatter about what it did. " * 60
    raw = f"<script>{real}</script>\nNote: I wrapped it in <script>\n{chatter}"
    assert extract_script(raw).strip() == real.strip()


def test_extract_script_no_tags_falls_back_without_summary_prose():
    raw = "<summary>Brief.</summary>\n\nThe briefing without script tags."
    assert extract_script(raw) == "The briefing without script tags."


def test_extract_script_case_insensitive_tags():
    """Regression: uppercase tags used to be narrated verbatim, unextracted."""
    raw = "<SCRIPT>Real script here.</SCRIPT>"
    assert extract_script(raw) == "Real script here."


def test_extract_summary_with_tags():
    raw = "<summary>Brief.</summary>\n\nThe rest."
    assert extract_summary(raw) == "Brief."


def test_extract_summary_no_tags_returns_empty():
    assert extract_summary("No summary tags here.") == ""


def test_extract_summary_picks_longest_block():
    """The same placeholder-while-planning behavior that motivated
    extract_script's longest-block fix can hit <summary> too; apply the same
    defense for symmetry (see extract_summary's docstring for why).
    """
    raw = (
        "<summary>...</summary>\n\n<summary>"
        + "Real summary sentence. " * 10
        + "</summary>"
    )
    assert extract_summary(raw).startswith("Real summary sentence.")


# --- session mechanics ---


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_happy_path(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_1"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Brief.</summary>\n<script>Spoken words.</script>"

    result = run_report_prompt("INSTRUCTION", label="chinatalk")

    assert result == ReportOutput(script="Spoken words.", summary="Brief.")
    mock_send.assert_called_once_with("ses_1", "INSTRUCTION")
    mock_wait.assert_called_once_with("ses_1", timeout=900)
    mock_delete.assert_called_once_with("ses_1")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_timeout_raises_and_cleans_up(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_timeout"
    mock_wait.return_value = False

    with pytest.raises(RuntimeError, match="silver .*900 seconds"):
        run_report_prompt("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_timeout")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_empty_script_raises(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_empty"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<summary>Anything.</summary>\n\n<script>   </script>"

    with pytest.raises(RuntimeError, match="empty script"):
        run_report_prompt("INSTRUCTION", label="yglesias")

    mock_delete.assert_called_once_with("ses_empty")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_min_chars_rejects_short_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """min_chars is opt-in: emptiness is the wrong test on a publish path."""
    mock_create.return_value = "ses_short"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>Too short to be an episode.</script>"

    with pytest.raises(RuntimeError, match="too short to be real"):
        run_report_prompt("INSTRUCTION", label="silver", min_chars=500)

    mock_delete.assert_called_once_with("ses_short")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_default_has_no_length_floor(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """Default min_chars=0 preserves report_writer's existing behavior."""
    mock_create.return_value = "ses_default"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>Short but allowed.</script>"

    result = run_report_prompt("INSTRUCTION", label="paper")

    assert result.script == "Short but allowed."


# --- leaked-markup guard (review-found: silent-wrong class) ---


@pytest.mark.parametrize(
    "raw_text",
    [
        pytest.param(
            "<summary>Long prose. " + "padding word " * 80 + "</summarywhoops",
            id="dangling-unclosed-summary-no-script-tags",
        ),
        pytest.param(
            "<script>outer <script>inner</script> tail</script>",
            id="nested-script-orphan-tag",
        ),
    ],
)
@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_rejects_leaked_markup(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete, raw_text
):
    """Reproduces the review's confirmed cases: malformed model output that
    extract_script cannot cleanly resolve must fail loudly, not ship to TTS.
    """
    mock_create.return_value = "ses_leak"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = raw_text

    with pytest.raises(RuntimeError, match="leaked markup"):
        run_report_prompt("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_leak")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_leaked_markup_checked_before_min_chars(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """A leaked-markup script that would also clear min_chars must still be
    rejected for the markup, not silently accepted because it's long enough.
    """
    mock_create.return_value = "ses_leak_long"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Long prose. " + "padding word " * 80 + "</summarywhoops"
    )

    with pytest.raises(RuntimeError, match="leaked markup"):
        run_report_prompt("INSTRUCTION", label="silver", min_chars=500)

    mock_delete.assert_called_once_with("ses_leak_long")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_uppercase_tags_do_not_trip_the_leak_guard(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """Fix 2 (case-insensitivity) and fix 1 (leak guard) must not interact
    badly: a cleanly-extracted uppercase-tagged script must NOT be rejected.
    """
    mock_create.return_value = "ses_upper"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<SCRIPT>Real script here.</SCRIPT>"

    result = run_report_prompt("INSTRUCTION", label="silver")

    assert result.script == "Real script here."


# --- cleanup guarantee under mid-try exceptions ---


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_cleans_up_when_send_prompt_raises(
    mock_create, mock_send, mock_delete
):
    mock_create.return_value = "ses_send_fail"
    mock_send.side_effect = RuntimeError("network boom")

    with pytest.raises(RuntimeError, match="network boom"):
        run_report_prompt("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_send_fail")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_run_report_prompt_cleans_up_when_get_messages_raises(
    mock_create, mock_send, mock_wait, mock_messages, mock_delete
):
    mock_create.return_value = "ses_messages_fail"
    mock_wait.return_value = True
    mock_messages.side_effect = RuntimeError("api boom")

    with pytest.raises(RuntimeError, match="api boom"):
        run_report_prompt("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_messages_fail")


# --- fetch_report_text: session lifecycle in isolation ---


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_fetch_report_text_returns_stripped_text_and_deletes_session(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    mock_create.return_value = "ses_fetch"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "  raw model output  "

    result = fetch_report_text("INSTRUCTION", label="chinatalk")

    assert result == "raw model output"
    mock_send.assert_called_once_with("ses_fetch", "INSTRUCTION")
    mock_wait.assert_called_once_with("ses_fetch", timeout=900)
    mock_delete.assert_called_once_with("ses_fetch")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_fetch_report_text_deletes_session_on_timeout(
    mock_create, mock_send, mock_wait, mock_delete
):
    mock_create.return_value = "ses_fetch_timeout"
    mock_wait.return_value = False

    with pytest.raises(RuntimeError, match="silver .*900 seconds"):
        fetch_report_text("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_fetch_timeout")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_fetch_report_text_deletes_session_when_get_messages_raises(
    mock_create, mock_send, mock_wait, mock_messages, mock_delete
):
    mock_create.return_value = "ses_fetch_boom"
    mock_wait.return_value = True
    mock_messages.side_effect = RuntimeError("api boom")

    with pytest.raises(RuntimeError, match="api boom"):
        fetch_report_text("INSTRUCTION", label="silver")

    mock_delete.assert_called_once_with("ses_fetch_boom")


# --- parse_report: evidence-table shapes (plan's measured A-E) ---


def test_parse_report_shape_a_mangled_close_recovers_clean_script():
    """Shape A: '<script>' + long + '</scrip>' -> clean recovery, no literal
    tag in the result. This was 6516 chars WITH a literal tag under the old
    rundown-local extractor; the engine's recovery fixes it.
    """
    long = "The real script. " * 60
    raw = f"<script>{long}</scrip>"

    result = parse_report(raw, label="rundown")

    assert result.script.strip() == long.strip()
    assert "<script>" not in result.script
    assert "</scrip>" not in result.script


def test_parse_report_shape_b_uppercase_tags_recover_clean_script():
    """Shape B: '<SCRIPT>' + long + '</SCRIPT>' -> clean recovery."""
    long = "The real script. " * 60
    raw = f"<SCRIPT>{long}</SCRIPT>"

    result = parse_report(raw, label="rundown")

    assert result.script.strip() == long.strip()


def test_parse_report_shape_c_no_tags_returns_text_by_default():
    """Shape C: no tags at all -> with require_tags=False, falls through to
    the (cosmetic) no-tag fallback and returns text, matching today's
    already-migrated behavior.
    """
    long = "Just raw model prose with no tags at all. " * 40

    result = parse_report(long, label="rundown")

    assert result.script.strip() == long.strip()


def test_parse_report_shape_c_no_tags_raises_when_require_tags():
    """Shape C, with require_tags=True: refuse instead of narrating raw
    model reasoning (the new guard this task adds).
    """
    long = "Just raw model prose with no tags at all. " * 40

    with pytest.raises(RuntimeError, match="rundown.*no <script> tag"):
        parse_report(long, label="rundown", require_tags=True)


def test_parse_report_shape_d_placeholder_then_mangled_real_script():
    """Shape D: a placeholder '<script>...</script>' followed by the real
    '<script>' + long + '</scrip>' -> returns the long script, not the
    3-char placeholder (the 2026-08-18 incident shape).
    """
    long = "The real script. " * 60
    raw = f"<script>...</script> planning <script>{long}</scrip>"

    result = parse_report(raw, label="fp-digest")

    assert result.script.strip() == long.strip()
    assert result.script.strip() != "..."


def test_parse_report_shape_e_covered_leak_now_raises():
    """Shape E: '<summary>s</summary><covered>- h1</covered>' + long, no
    '<script>' tags at all -> now raises on the leaked <covered> block.
    Before this task's guard addition, this returned 6523 chars including
    the literal <covered> block, narrated aloud (Finding 2 in the plan).
    """
    long = "Real briefing prose with no script tags. " * 40
    raw = "<summary>s</summary><covered>- h1</covered> " + long

    with pytest.raises(RuntimeError, match="leaked markup"):
        parse_report(raw, label="rundown")


def test_parse_report_stray_script_mention_in_summary_is_a_deliberate_refusal():
    """Pinned as a DELIBERATE refusal, not a bug to fix.

    Per the plan's "Summary-remainder vs full-text" tradeoff: the engine
    extracts from the FULL text (not the post-<summary>-strip remainder), so
    a real, well-formed <script> block that happens to be preceded by
    summary prose which itself *mentions* "<script>" produces a candidate
    that spans from that stray mention through the real closing </script>,
    swallowing "</summary><covered>" along the way -- which then trips the
    leaked-markup guard. The plan measured the alternative (remainder-first)
    and found it fails a DIFFERENT shape instead (a real <script> nested
    inside <summary> extracts to 0 chars -> empty -> refusal). Neither
    ordering is strictly better, so full-text was adopted because it keeps
    the blast radius off already-migrated callers and the cost here is one
    retry with a fresh model call, not a wrong episode. Do not "fix" this by
    switching to remainder-first without re-reading that tradeoff.
    """
    long = "The real script text. " * 60
    raw = (
        "<summary>I wrapped it in <script> tags.</summary>"
        "<covered>- h</covered>"
        f"<script>{long}</script>"
    )

    with pytest.raises(RuntimeError, match="leaked markup"):
        parse_report(raw, label="rundown")


def test_parse_report_require_tags_still_accepts_well_formed_input():
    """require_tags=True must not reject normal, well-formed output."""
    raw = "<summary>Brief.</summary>\n<script>Spoken words here.</script>"

    result = parse_report(raw, label="silver", require_tags=True)

    assert result.script == "Spoken words here."
    assert result.summary == "Brief."
