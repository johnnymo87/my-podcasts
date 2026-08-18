from __future__ import annotations

from pathlib import Path

from pipeline.transcript_detect import looks_like_transcript


_FIX = Path(__file__).parent / "fixtures"


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


def test_real_silver_transcript_is_detected():
    """Real email-cleaned body (16000-char prefix) from the 2026-08-17 Silver
    Bulletin post "Why does everyone hate data centers?".

    Not a synthetic case: this is externally-validated safety data, not a
    test driving new code. All 57 archived Silver Bulletin emails were
    replayed through the real production email path (EmailProcessor ->
    SubstackAdapter.clean_body -> this detector), giving 4 transcripts, 53
    essays, and zero false positives. See
    docs/plans/2026-08-18-silver-transcript-report-design.md.
    """
    assert looks_like_transcript((_FIX / "silver_transcript.txt").read_text()) is True


def test_real_silver_essay_is_not_detected():
    """Real email-cleaned body from the 2026-08-15 Senate-races roundup.

    Poll and ranking tables produce name-shaped line-start labels (e.g.
    "Texas:", "Ohio:", "Maine:") but each occurs only once, far below the
    five-turn-per-speaker floor. Pins externally-validated safety data (see
    the 57-email replay cited above), not new detector behavior.
    """
    assert looks_like_transcript((_FIX / "silver_essay.txt").read_text()) is False


def test_real_silver_mailbag_is_not_detected():
    """Real email-cleaned body from the 2026-04-21 SBSQ #31 mailbag post.

    "Silver Bulletin Subscriber Questions" was the leading false-positive
    suspect for this feed: the speaker-turn regex does match a bare "Q:".
    But this series does not label its answers, so only one label can recur
    and the two-speaker floor holds. Pins externally-validated safety data
    (see the 57-email replay cited above), not new detector behavior.
    """
    assert looks_like_transcript((_FIX / "silver_mailbag.txt").read_text()) is False
