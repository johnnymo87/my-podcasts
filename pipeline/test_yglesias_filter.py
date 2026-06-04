from __future__ import annotations

import inspect

from pipeline import processor
from pipeline.yglesias_filter import is_argument_transcript


# A real Argument transcript: 2 named speakers, each with 1 seed + 6 loop
# = 7 turns, comfortably above _MIN_TURNS_PER_SPEAKER (5).
_TRANSCRIPT_BODY = "\n".join(
    [
        "Jerusalem Demsas: Welcome to the show.",
        "Kelsey Piper: Glad to be here.",
    ]
    + [
        line
        for i in range(6)
        for line in (
            f"Jerusalem Demsas: Point number {i}.",
            f"Kelsey Piper: My response {i}.",
        )
    ]
)


_ESSAY_BODY = """
Today I want to talk about housing policy and why the same problems
keep cropping up. Note: this is just an aside. Update: more later.
Subscribe to Slow Boring for more posts like this one.
"""


def test_transcript_with_two_speakers_is_detected():
    assert is_argument_transcript(_TRANSCRIPT_BODY) is True


def test_normal_essay_is_not_a_transcript():
    assert is_argument_transcript(_ESSAY_BODY) is False


def test_single_speaker_many_turns_is_not_enough():
    body = "\n".join(f"Host: line {i}" for i in range(20))
    assert is_argument_transcript(body) is False


def test_two_speakers_below_turn_threshold_not_detected():
    # Two distinct speakers but only 4 turns each -> below the 5-turn floor.
    body = "\n".join(
        line
        for i in range(4)
        for line in (f"Alice: {i}", f"Bob: {i}")
    )
    assert is_argument_transcript(body) is False


def test_empty_body_is_not_a_transcript():
    assert is_argument_transcript("") is False


def test_timestamp_and_label_lines_do_not_trigger():
    # Digit-led timestamps and one-off labels never reach the threshold.
    body = (
        "Time stamps:\n0:00-intro\n7:07-the point\n"
        "Show Notes:\nCoverage of the topic: an article\n"
    )
    assert is_argument_transcript(body) is False


def test_repeated_structural_labels_still_below_threshold():
    # 'Note:' matches the speaker-label regex but appears only 4 times,
    # so the >=5-turns-per-speaker threshold blocks it. This makes the
    # threshold (not the regex) the explicit line of defense.
    body = "\n".join(f"Note: aside number {i}." for i in range(4))
    assert is_argument_transcript(body) is False


# --- Wiring check ---


def test_processor_calls_maybe_rewrite_yglesias():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_yglesias" in source
    # The old skip machinery is gone.
    assert "should_skip" not in source
