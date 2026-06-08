from __future__ import annotations

from pipeline.transcript_detect import looks_like_transcript


def test_two_speakers_five_turns_each_is_transcript():
    body = "".join(f"Alice: line {i}\nBob: line {i}\n" for i in range(5))
    assert looks_like_transcript(body) is True


def test_essay_is_not_transcript():
    assert looks_like_transcript("An essay on policy. No speaker turns here.") is False


def test_single_speaker_is_not_transcript():
    body = "".join(f"Alice: line {i}\n" for i in range(10))
    assert looks_like_transcript(body) is False


def test_two_speakers_under_threshold_is_not_transcript():
    body = "".join(f"Alice: line {i}\nBob: line {i}\n" for i in range(4))
    assert looks_like_transcript(body) is False


def test_empty_body_is_not_transcript():
    assert looks_like_transcript("") is False


def test_chinatalk_shaped_transcript_is_transcript():
    body = (
        "Jordan Schneider: Welcome back.\n"
        "Rob Lee: Glad to be here.\n"
        "Jordan Schneider: Tell us about the front.\n"
        "Rob Lee: It's underground.\n"
        "Jordan Schneider: And drones?\n"
        "Rob Lee: Everywhere.\n"
        "Jordan Schneider: Logistics?\n"
        "Rob Lee: By UGV.\n"
        "Jordan Schneider: Casualties?\n"
        "Rob Lee: Mostly drones.\n"
    )
    assert looks_like_transcript(body) is True


def test_prose_essay_with_incidental_colons_is_not_transcript():
    # Headers and incidental "Word:" lines appear once and never reach the
    # per-speaker turn threshold; a real essay must not be misclassified.
    body = (
        "Note: this is an essay.\n\n"
        "The first paragraph argues a point at length without any dialogue. "
        "It cites a study and draws a conclusion.\n\n"
        "Summary: the second paragraph continues the argument in prose, "
        "again with no speaker turns whatsoever.\n"
    )
    assert looks_like_transcript(body) is False
