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

    This is the actual near-miss in this feed's fixture set (not the mailbag
    -- see that test's docstring): the fixture measurably exercises the
    detector's discrimination, with three distinct one-shot name-shaped
    labels ("Texas:", "Ohio:", "Iowa:", each occurring exactly once) --
    label-shaped lines that a cruder detector could plausibly count. Each
    stays far below the five-turn-per-speaker floor here.

    Caveat: this fixture is a truncated PREFIX of the real body (see the
    fixture-generation note in the design doc), and truncation can only
    ever REMOVE labels, never add them -- so a False result here is weaker
    evidence than a False on the full body would be. The full-archive
    57-email replay (cited above) is what actually establishes the
    negative for this post; this fixture exists to catch a regression in
    that established result, not to independently prove it.
    """
    assert looks_like_transcript((_FIX / "silver_essay.txt").read_text()) is False


def test_timestamp_and_label_lines_do_not_trigger():
    """Digit-led timestamps and one-off labels never reach the threshold.

    Rescued from the retired test_yglesias_filter.py: pins that a digit-led
    line start (e.g. "0:00-intro") never matches the speaker-label regex at
    all (see transcript_detect._SPEAKER_TURN_RE's docstring), distinct from
    the word-label cases below which do match but stay under the turn floor.
    """
    body = (
        "Time stamps:\n0:00-intro\n7:07-the point\n"
        "Show Notes:\nCoverage of the topic: an article\n"
    )
    assert looks_like_transcript(body) is False


def test_repeated_structural_labels_still_below_threshold():
    """Rescued from the retired test_yglesias_filter.py.

    'Note:' matches the speaker-label regex (the regex deliberately accepts
    single-word labels like 'Announcer:' / 'Q:'), so the per-speaker turn
    threshold -- not the regex -- is the primary line of defense. Four
    'Note:' lines stay under the >=5 floor.
    """
    body = "\n".join(f"Note: aside number {i}." for i in range(4))
    assert looks_like_transcript(body) is False


def test_two_single_word_labels_each_below_threshold_are_not_a_transcript():
    """Rescued from the retired test_yglesias_filter.py.

    The threshold needs >=2 distinct labels EACH at >=5 turns. Four 'Note:'
    and four 'Update:' lines are two distinct labels but both under the
    floor, so this essay-shaped body is not treated as a transcript.

    Known benign limitation: if a single post somehow had >=5 'Note:' AND
    >=5 'Update:' line-starts, the detector would fire. That is extremely
    unlikely in a real essay, and the consequence under the report path is a
    spoken briefing instead of a long reading -- never a dropped episode --
    so we accept it rather than tighten the regex and risk missing real
    single-word speaker labels.
    """
    body = "\n".join(
        [f"Note: aside {i}." for i in range(4)]
        + [f"Update: item {i}." for i in range(4)]
    )
    assert looks_like_transcript(body) is False


def test_real_silver_mailbag_is_not_detected():
    """Real email-cleaned body from the 2026-04-21 SBSQ #31 mailbag post.

    "Silver Bulletin Subscriber Questions" was the leading false-positive
    suspect for this feed. What this fixture actually proves, verified
    directly against the detector's own regex: this particular post carries
    ZERO speaker-turn-shaped line starts at all -- not even a bare "Q:" --
    so it passes trivially and pins only "a real mailbag post from this
    series does not look like a transcript," nothing about *why*.

    It does NOT exercise the "one recurring label can't clear the
    two-speaker floor" mechanism this series was suspected of triggering --
    see test_repeated_q_label_with_unlabeled_answers_is_not_transcript below
    for a synthetic case that actually does exercise that mechanism.
    """
    assert looks_like_transcript((_FIX / "silver_mailbag.txt").read_text()) is False


def test_repeated_q_label_with_unlabeled_answers_is_not_transcript():
    """Synthetic case for the mechanism the real mailbag fixture above
    cannot exercise (it has no speaker labels at all): a single recurring
    "Q:" label whose answers are unlabeled prose. The speaker-turn regex
    does match a bare "Q:", so this pins that one recurring label -- no
    matter how many times it recurs -- can never clear the two-DISTINCT-
    speaker floor on its own.
    """
    body = "\n".join(
        f"Q: Question number {i}?\nSome unlabeled prose answering it at length.\n"
        for i in range(6)
    )
    assert looks_like_transcript(body) is False
