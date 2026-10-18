from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.report_engine import ReportOutput
from pipeline.transcript_report import (
    _MIN_SCRIPT_CHARS,
    TRANSCRIPT_FEEDS,
    build_report_prompt,
    generate_report,
    maybe_rewrite_transcript,
)


_BODY = "Alice: hello\nBob: hi\n"
_SUBJECT = "Some Subject"


def test_min_script_chars_is_derived_from_the_prompt_floor_not_inherited():
    """Adversarial-review find: 500 was copied from rundown_writer.py's own
    _MIN_SCRIPT_CHARS without re-deriving it for this pipeline. The prompts
    here demand 800-1500 words (~4400-9000 chars) and a rejected generation
    here means UNBOUNDED email redelivery (no backoff/errored/alert path
    like the daily jobs have), so the floor must sit well below a
    legitimately terse briefing but well above anything that could pass for
    one -- 500 (11% of the prompt's own floor) left the entire 500-4000
    range open to refusals and truncations shipping as a full episode.
    """
    assert _MIN_SCRIPT_CHARS == 2000


# The chinatalk and yglesias prompt templates in this module were checked
# byte-identical to the retired pipeline/chinatalk_writer.py and
# pipeline/yglesias_writer.py at migration time via a golden test comparing
# build_report_prompt output directly against each retired module's own
# build_report_prompt. Both modules (and that test) are now deleted -- the
# equivalence proof is recorded in the commit history, not re-checked here.


# --- gate + writer, parametrized over the registered feeds ---


_TRANSCRIPT = "".join(f"Alice: line {i}\nBob: line {i}\n" for i in range(5))
# generate_report is mocked wherever this is used, so _MIN_SCRIPT_CHARS never
# actually gates it -- kept comfortably above it anyway so this fixture stays
# a plausible stand-in for real output if that mocking ever changes.
_LONG_SCRIPT = "A spoken briefing sentence. " * 80  # > _MIN_SCRIPT_CHARS (2000)


def test_unregistered_feed_is_passthrough():
    body, title = maybe_rewrite_transcript(
        body=_TRANSCRIPT,
        title="2026-04-25 - Money Stuff - Foo",
        feed_slug="levine",
        subject_raw="Money Stuff: Foo",
    )
    assert body == _TRANSCRIPT
    assert title == "2026-04-25 - Money Stuff - Foo"


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
def test_essay_is_passthrough(slug):
    body, title = maybe_rewrite_transcript(
        body="An essay on policy. No speaker turns here.",
        title="T",
        feed_slug=slug,
        subject_raw="S",
    )
    assert body == "An essay on policy. No speaker turns here."
    assert title == "T"


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
@patch("pipeline.transcript_report.generate_report")
def test_transcript_is_rewritten(mock_writer, slug):
    mock_writer.return_value = ReportOutput(script=_LONG_SCRIPT, summary="Brief.")
    body, title = maybe_rewrite_transcript(
        body=_TRANSCRIPT, title="Ep 42", feed_slug=slug, subject_raw="Subject 42"
    )
    assert body == _LONG_SCRIPT
    assert title == "Report: Ep 42"
    mock_writer.assert_called_once_with(
        body=_TRANSCRIPT, subject="Subject 42", feed_slug=slug
    )


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
@patch("pipeline.transcript_report.generate_report", side_effect=RuntimeError("boom"))
def test_writer_failure_propagates(mock_writer, slug):
    """A confirmed transcript must never silently degrade to a literal read."""
    with pytest.raises(RuntimeError):
        maybe_rewrite_transcript(
            body=_TRANSCRIPT, title="Ep 42", feed_slug=slug, subject_raw="Subject 42"
        )


@pytest.mark.parametrize("slug", sorted(TRANSCRIPT_FEEDS))
def test_prompt_contains_subject_and_body(slug):
    prompt = build_report_prompt(
        body="BODYMARKER", subject="SUBJMARKER", feed_slug=slug
    )
    assert "BODYMARKER" in prompt
    assert "SUBJMARKER" in prompt


def test_build_report_prompt_rejects_unknown_feed():
    with pytest.raises(ValueError, match="levine"):
        build_report_prompt(body="b", subject="s", feed_slug="levine")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_report_enforces_the_length_floor(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """A placeholder script must not replace a real transcript body."""
    mock_create.return_value = "ses_x"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "<script>...</script>"

    with pytest.raises(RuntimeError, match="too short to be real"):
        generate_report(body="b", subject="s", feed_slug="chinatalk")

    mock_delete.assert_called_once_with("ses_x")


@patch("pipeline.report_engine.delete_session")
@patch("pipeline.report_engine.get_last_assistant_text")
@patch("pipeline.report_engine.get_messages")
@patch("pipeline.report_engine.wait_for_idle")
@patch("pipeline.report_engine.send_prompt_async")
@patch("pipeline.report_engine.create_session")
def test_generate_report_refuses_output_with_no_script_tag(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete
):
    """A reply with no <script> tag must refuse, not narrate raw reasoning.

    Without ``require_tags=True`` the engine's no-tag fallback returns
    essentially the whole reply with the <summary> block stripped -- measured
    at 6522 chars in, 6521 out. That is long, so ``_MIN_SCRIPT_CHARS`` cannot
    catch it, and the model's own reasoning would be published as the episode
    in place of the post. This module's contract is re-raise-over-degrade, so
    the correct outcome is no episode and a redelivery.
    """
    mock_create.return_value = "ses_x"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    # Long enough to clear _MIN_SCRIPT_CHARS, so only require_tags can catch it.
    mock_text.return_value = "Here is my reasoning about the transcript. " * 200

    with pytest.raises(RuntimeError, match="no trustworthy <script> markup"):
        generate_report(body="b", subject="s", feed_slug="chinatalk")

    mock_delete.assert_called_once_with("ses_x")


# --- Audio report (The Argument, from our own ASR of the public audio) -----

_HOSTS = ("Jerusalem Demsas", "Matt Yglesias")


def _audio(**kw):
    from pipeline import transcript_report as tr

    args = {
        "transcript": "Jerusalem Demsas: hi",
        "title": "T",
        "description": "D",
        "hosts": _HOSTS,
    } | kw
    return tr.build_audio_report_instruction(**args)


def test_email_yglesias_prompt_unchanged() -> None:
    p = build_report_prompt(body="B", subject="S", feed_slug="yglesias")
    assert "It ran in" in p and "Slow Boring" in p


def test_audio_instruction_is_its_own_prompt() -> None:
    ins = _audio()
    assert "Slow Boring" not in ins
    assert "[SPONSOR]" in ins
    assert "Unknown speaker" in ins
    assert "<script>" in ins and "<summary>" in ins
    assert "Episode title: T" in ins and "Episode description: D" in ins
    assert ins.rstrip().endswith("Jerusalem Demsas: hi")


def test_audio_instruction_survives_braces_in_transcript() -> None:
    ins = _audio(transcript="a {b} c", title="{t}", description="{d}")
    assert ins.rstrip().endswith("a {b} c")
    assert "Episode title: {t}" in ins


def test_audio_prompt_not_registered_for_email() -> None:
    from pipeline import transcript_report as tr

    assert tr._ARGUMENT_AUDIO_TEMPLATE not in TRANSCRIPT_FEEDS.values()


@pytest.mark.parametrize(
    ("hosts", "rendered"),
    [
        (("A",), "with A."),
        (("A", "B"), "with A and B."),
        (("A", "B", "C"), "with A, B and C."),
    ],
)
def test_audio_instruction_renders_hosts(hosts, rendered) -> None:
    ins = _audio(hosts=hosts)
    assert "The Argument" in ins
    assert rendered in ins


def test_audio_instruction_hosts_are_not_hardcoded() -> None:
    ins = _audio(hosts=("Pat Example", "Sam Sample"), transcript="x")
    assert "Pat Example and Sam Sample" in ins
    assert "Demsas" not in ins and "Yglesias" not in ins


def test_audio_instruction_requires_hosts() -> None:
    with pytest.raises(ValueError):
        _audio(hosts=())


def test_audio_prompt_unknown_speaker_labels_are_not_a_through_line() -> None:
    ins = _audio()
    assert "restart" in ins
    assert "same person" in ins
    assert "one participant argued" not in ins
    assert "the other pushed back" not in ins
    assert "a participant raised" in ins


def test_audio_prompt_sponsor_housekeeping_and_spelling_rules() -> None:
    ins = _audio()
    assert "never closed" in ins
    assert "subscribe" in ins and "premium" in ins
    assert "spelling" in ins and "description" in ins
