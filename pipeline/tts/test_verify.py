import re
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.tts.asr import Transcription, TranscriptionUnavailable
from pipeline.tts.normalize import normalize_tokens
from pipeline.tts.verify import (
    DEFAULT_THRESHOLDS,
    VERIFIER_POLICY,
    VERIFIER_VERSION,
    VerifyThresholds,
    _recall_fails,
    analyze,
    project_chunks,
    verify_audio,
)


FIXTURE = Path(__file__).parent / "fixtures" / "rundown_2026-09-30_chunks01.txt"
SCRIPT = FIXTURE.read_text()
PARAS = SCRIPT.split("\n\n")


def as_asr(text: str) -> str:
    """What a clean ASR pass looks like: no punctuation, digits for spelled numbers."""
    text = text.replace("September thirtieth, twenty twenty-six", "September 30th 2026")
    text = text.replace("twelve point nine billion dollars", "$12.9 billion")
    return re.sub(r"[^\w\s$.%-]", "", text)


def drop_words(text: str, start: int, count: int) -> str:
    words = text.split()
    return " ".join(words[:start] + words[start + count :])


def test_clean_transcript_passes():
    a = analyze(SCRIPT, as_asr(SCRIPT))
    assert a.status == "pass"
    assert a.reasons == ("ok",)
    assert a.recall is not None and a.recall >= 0.99
    assert not any(s.flagged for s in a.spans)


def test_sparse_asr_noise_passes():
    # One substituted word in every 20: recall ~0.95, spans stay short.
    words = as_asr(SCRIPT).split()
    noisy = " ".join("xyzzy" if i % 20 == 10 else w for i, w in enumerate(words))
    a = analyze(SCRIPT, noisy)
    assert a.status == "pass", a
    assert a.longest_span_words < DEFAULT_THRESHOLDS.min_span_words


@pytest.mark.parametrize("where", ["start", "middle", "end"])
def test_forty_word_skip_is_flagged(where):
    words = as_asr(SCRIPT).split()
    start = {"start": 0, "middle": len(words) // 2, "end": len(words) - 40}[where]
    a = analyze(SCRIPT, drop_words(as_asr(SCRIPT), start, 40))
    assert a.status == "omission"
    assert "long_unmatched_span" in a.reasons
    flagged = [s for s in a.spans if s.flagged]
    assert len(flagged) == 1
    assert 38 <= flagged[0].script_words <= 44
    assert flagged[0].transcript_words == 0


def test_stray_short_match_inside_skip_does_not_split_it():
    # The ASR keeps one common word ("the") from the middle of a 40-word skip;
    # a 1-token match is not an anchor, so the span stays whole.
    words = as_asr(SCRIPT).split()
    mid = len(words) // 2
    kept = words[:mid] + ["the"] + words[mid + 40 :]
    a = analyze(SCRIPT, " ".join(kept))
    flagged = [s for s in a.spans if s.flagged]
    assert len(flagged) == 1 and flagged[0].script_words >= 38


def test_replacement_by_much_shorter_speech_is_flagged():
    words = as_asr(SCRIPT).split()
    mid = len(words) // 2
    garbled = words[:mid] + ["um", "so", "anyway"] + words[mid + 40 :]
    a = analyze(SCRIPT, " ".join(garbled))
    assert "long_unmatched_span" in a.reasons


def test_several_short_cuts_trip_the_recall_floor():
    t = as_asr(SCRIPT)
    for start in (700, 550, 400, 250, 120):  # 5 cuts x 10 words = ~6% of tokens
        t = drop_words(t, start, 10)
    # the span rule off: each 10-word cut is its own span and would flag
    th = replace(DEFAULT_THRESHOLDS, net_deficit_min=None, recall_floor=0.97)
    a = analyze(SCRIPT, t, th)
    assert a.status == "omission"
    assert a.reasons == ("recall_below_floor",)


def test_empty_script_passes_trivially():
    a = analyze("... --", "anything")
    assert a.status == "pass" and a.reasons == ("empty_script",) and a.recall is None


def test_empty_transcript_is_an_omission_of_everything():
    # verify_audio() turns an empty *ASR* result into "unavailable" before
    # analysis; analyze() itself just reports what it sees.
    a = analyze(SCRIPT, "")
    assert a.status == "omission" and a.recall == 0.0


def test_span_coordinates_are_normalized_token_indices():
    a = analyze(SCRIPT, drop_words(as_asr(SCRIPT), 300, 40))
    s = next(s for s in a.spans if s.flagged)
    assert s.script_end - s.script_start == s.script_words
    assert s.transcript_end - s.transcript_start == s.transcript_words
    assert s.net_missing == s.script_words - s.transcript_words
    assert len(s.excerpt.split()) <= 30


@pytest.mark.parametrize(
    "kwargs",
    [
        {"net_deficit_min": 0},
        {"net_deficit_min": -5},
        {"anchor_min": 0},
        {"min_span_words": 0},
        {"max_span_ratio": -0.1},
        {"max_span_ratio": 1.5},
        {"recall_floor": 1.1},
        {"recall_floor": -0.1},
    ],
)
def test_thresholds_validate(kwargs):
    with pytest.raises(ValueError):
        VerifyThresholds(**kwargs)


TAIL = (
    "Thanks for listening, everyone. This show is produced by Margaret Whitfield "
    "and edited by Tobias Okonkwo. We will be back tomorrow with more news, so "
    "please subscribe and tell a friend about it today."
)
V2 = replace(DEFAULT_THRESHOLDS, recall_min_tokens=1)


def _misheard(text: str) -> str:
    # two non-adjacent substituted words, nothing dropped
    return text.replace("Whitfield", "Whitfeld").replace("Okonkwo", "Okonko")


def test_recall_min_tokens_defaults_to_300_and_is_validated():
    assert DEFAULT_THRESHOLDS.recall_min_tokens == 300
    for bad in (0, -1):
        with pytest.raises(
            ValueError, match=f"recall_min_tokens must be >= 1, got {bad}"
        ):
            VerifyThresholds(recall_min_tokens=bad)


def test_long_chunks_keep_the_v2_rule_exactly():
    for n in range(300, 1201):
        for m in range(n + 1):
            assert _recall_fails(m, n, DEFAULT_THRESHOLDS) == (m / n < 0.95), (m, n)


def test_recall_min_tokens_1_is_the_v2_rule_for_every_length():
    for n in range(1, 401):
        for m in range(n + 1):
            assert _recall_fails(m, n, V2) == (m / n < 0.95), (m, n)


@pytest.mark.parametrize("total", [11, 34, 70, 150, 299])
def test_short_chunk_bound_is_15_unmatched_tokens(total):
    # 15 unmatched passes (a wholly missing 11-token chunk is the span rule's job)
    assert not _recall_fails(total - min(15, total), total, DEFAULT_THRESHOLDS)
    if total >= 16:
        assert _recall_fails(total - 16, total, DEFAULT_THRESHOLDS)


def test_recall_fails_is_false_for_an_empty_chunk():
    assert not _recall_fails(0, 0, DEFAULT_THRESHOLDS)


def test_short_signoff_with_two_misheard_names_passes():
    n = len(normalize_tokens(TAIL))
    assert n == 34
    heard = _misheard(TAIL)
    a = analyze(TAIL, heard)
    assert a.status == "pass" and a.reasons == ("ok",)
    # recall is still reported truthfully
    assert a.recall == a.matched_tokens / a.script_tokens
    assert a.matched_tokens == n - 2 and a.recall < 0.95
    old = analyze(TAIL, heard, V2)
    assert old.status == "omission" and old.reasons == ("recall_below_floor",)


def test_a_wholly_dropped_5_token_chunk_is_not_flagged_by_v3():
    # A documented limit, not desired behavior: below 16 tokens the padded
    # recall rule cannot fire, and 5 tokens is under the span rule's floor.
    script = "Thanks again, see you soon."
    assert len(normalize_tokens(script)) == 5
    a = analyze(script, "unrelated")
    assert a.status == "pass" and a.reasons == ("ok",)
    old = analyze(script, "unrelated", V2)
    assert old.status == "omission" and old.reasons == ("recall_below_floor",)


def test_short_chunk_contiguous_drop_is_still_flagged_by_the_span_rule():
    script = " ".join(SCRIPT.split()[:70])
    assert len(normalize_tokens(script)) >= 60
    a = analyze(script, drop_words(script, 30, 8))
    assert a.status == "omission" and "long_unmatched_span" in a.reasons
    short = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo."
    assert len(normalize_tokens(short)) == 11
    for heard in ("", "unrelated"):
        b = analyze(short, heard)
        assert b.status == "omission" and "long_unmatched_span" in b.reasons
        assert "recall_below_floor" not in b.reasons


def test_project_chunks_uses_padded_recall_per_chunk():
    chunks = [SCRIPT, TAIL]
    transcript = as_asr(SCRIPT) + " " + _misheard(TAIL)
    whole, per_chunk = project_chunks(chunks, transcript)
    assert whole.status == "pass" and whole.reasons == ("ok",)
    tail = per_chunk[1]
    assert tail.status == "pass"
    assert tail.recall == tail.matched_tokens / tail.script_tokens
    assert tail.matched_tokens == tail.script_tokens - 2
    whole_v2, per_v2 = project_chunks(chunks, transcript, V2)
    assert per_v2[1].status == "omission"
    assert whole_v2.status == "omission"
    assert whole_v2.reasons == ("chunk_recall_below_floor",)
    assert per_v2[1].recall == tail.recall


def test_project_chunks_localizes_the_omission():
    chunks = [PARAS[0] + "\n\n" + PARAS[1], "\n\n".join(PARAS[2:])]
    transcript = as_asr(chunks[0]) + " " + drop_words(as_asr(chunks[1]), 50, 40)
    whole, per_chunk = project_chunks(chunks, transcript)
    assert whole.status == "omission"
    assert [c.index for c in per_chunk] == [0, 1]
    assert per_chunk[0].status == "pass" and per_chunk[0].recall >= 0.99
    assert per_chunk[1].status == "omission"
    assert (
        per_chunk[1].script_tokens + per_chunk[0].script_tokens == whole.script_tokens
    )


def test_analysis_serializes():
    a = analyze(SCRIPT, as_asr(SCRIPT))
    d = a.to_dict()
    assert d["status"] == "pass" and isinstance(d["spans"], list)


def _three_chunks():
    return [
        PARAS[0] + "\n\n" + PARAS[1],
        PARAS[2] + "\n\n" + PARAS[3],
        "\n\n".join(PARAS[4:]),
    ]


def _swap_first_word(text: str) -> str:
    words = text.split()
    return " ".join(["xyzzy", *words[1:]])


def _swap_last_word(text: str) -> str:
    words = text.split()
    return " ".join([*words[:-1], "xyzzy"])


def test_dropped_middle_chunk_does_not_flag_its_neighbours():
    chunks = _three_chunks()
    transcript = (
        _swap_last_word(as_asr(chunks[0])) + " " + _swap_first_word(as_asr(chunks[2]))
    )
    whole, per_chunk = project_chunks(chunks, transcript)
    assert whole.status == "omission"
    assert [p.status for p in per_chunk] == ["pass", "omission", "pass"]
    assert per_chunk[0].flagged_spans == 0
    assert per_chunk[2].flagged_spans == 0
    assert per_chunk[1].flagged_spans == 1


def test_chunk_failure_with_passing_whole_uses_chunk_reason():
    # Whole-episode recall clears the floor, but one small chunk is mostly gone.
    chunks = _three_chunks()
    small = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet."
    chunks.insert(1, small)
    transcript = " ".join(as_asr(c) for c in chunks if c != small)
    # recall_min_tokens=1: this pins the chunk-reason plumbing, not the padding
    th = replace(
        DEFAULT_THRESHOLDS,
        recall_floor=0.85,
        min_span_words=50,
        net_deficit_min=None,
        recall_min_tokens=1,
    )
    whole, per_chunk = project_chunks(chunks, transcript, th)
    assert per_chunk[1].status == "omission"
    assert whole.status == "omission"
    assert whole.reasons == ("chunk_recall_below_floor",)


def fake_transcriber(text=None, exc=None):
    calls = []

    def t(audio, mime_type):
        calls.append((audio, mime_type))
        if exc:
            raise exc
        return Transcription(text, "gemini-3.8-flash", "1", "STOP", 1.5, 10, 20, 7)

    return t, calls


def test_verify_audio_pass_records_asr_and_policy():
    t, calls = fake_transcriber(as_asr(SCRIPT))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "pass"
    assert v.recall is not None and v.recall >= 0.99
    assert v.verifier_version == VERIFIER_VERSION
    assert v.verifier_policy == VERIFIER_POLICY
    assert v.to_dict()["verifier_policy"] == VERIFIER_POLICY
    assert v.mode == "chunk"
    assert v.asr.model == "gemini-3.8-flash" and v.asr.finish_reason == "STOP"
    assert v.asr.thinking_tokens == 7
    # The transcriber got the audio and its type, nothing else.
    assert calls == [(b"WAV", "audio/wav")]


def test_verify_audio_omission():
    t, _ = fake_transcriber(drop_words(as_asr(SCRIPT), 200, 40))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "omission" and "long_unmatched_span" in v.reasons


def test_verify_audio_unavailable_is_never_a_pass():
    t, _ = fake_transcriber(exc=TranscriptionUnavailable("asr_timeout", "slow"))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "unavailable"
    assert v.reasons == ("asr_timeout",)
    assert v.recall is None and v.analysis is None and v.asr is None


def test_verify_audio_passes_asr_blocked_through_as_the_reason():
    t, _ = fake_transcriber(
        exc=TranscriptionUnavailable(
            "asr_blocked", "no candidates (block_reason=OTHER)"
        )
    )
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "unavailable"
    assert v.reasons == ("asr_blocked",)
    assert "block_reason=OTHER" in v.detail


def test_verify_audio_punctuation_only_transcript_is_unavailable():
    t, _ = fake_transcriber("... [music] ...")  # normalizes to "music" — still content
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "omission"
    t, _ = fake_transcriber("... --- ...")
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "unavailable" and v.reasons == ("asr_empty",)


def test_verify_audio_passes_thresholds_through():
    t, _ = fake_transcriber(as_asr(SCRIPT))
    th = VerifyThresholds(min_span_words=5)
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t, thresholds=th)
    assert v.thresholds == th and v.analysis.thresholds == th


def test_verdict_serializes():
    t, _ = fake_transcriber(as_asr(SCRIPT))
    d = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t).to_dict()
    assert d["status"] == "pass" and d["asr"]["model"] == "gemini-3.8-flash"
    assert d["analysis"]["recall"] >= 0.99


def test_verifier_policy_covers_verifier_and_asr_policy():
    from pipeline.tts import asr

    assert f"verifier-v{VERIFIER_VERSION}" in VERIFIER_POLICY
    assert asr.ASR_MODEL in VERIFIER_POLICY
    assert f"prompt-v{asr.ASR_PROMPT_VERSION}" in VERIFIER_POLICY
    assert asr.ASR_POLICY in VERIFIER_POLICY


def test_unavailable_verdict_also_carries_policy():
    t, _ = fake_transcriber(exc=TranscriptionUnavailable("asr_timeout", "slow"))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.verifier_policy == VERIFIER_POLICY


def _t2_blind_spot_transcript() -> str:
    """40 tokens cut, then every 3rd of the next 45 substituted.

    No 3-token anchor survives, so the cut and the noise merge into one span of
    85 script tokens against 45 transcript tokens: net_missing 40, but the
    transcript side is over half the script side, so the ratio test misses it.
    """
    toks = normalize_tokens(SCRIPT)
    mid = len(toks) // 2
    noisy = [
        "xyzzy" if i % 3 == 2 else t for i, t in enumerate(toks[mid + 40 : mid + 85])
    ]
    return " ".join(toks[:mid] + noisy + toks[mid + 85 :])


def test_the_t2_blind_spot_passes_with_the_rule_off_and_is_caught_by_default():
    assert analyze(SCRIPT, _t2_blind_spot_transcript()).status == "omission"
    off = replace(DEFAULT_THRESHOLDS, net_deficit_min=None, recall_floor=0.85)
    a = analyze(SCRIPT, _t2_blind_spot_transcript(), off)
    assert a.status == "pass"
    blind = [s for s in a.spans if s.script_words > 40]
    assert [(s.script_words, s.transcript_words, s.net_missing) for s in blind] == [
        (85, 45, 40)
    ]


@pytest.mark.parametrize("m", [6, 12, 24, 40])
def test_net_deficit_rule_flags_the_t2_blind_spot(m):
    th = replace(DEFAULT_THRESHOLDS, net_deficit_min=m, recall_floor=0.85)
    a = analyze(SCRIPT, _t2_blind_spot_transcript(), th)
    assert a.status == "omission"
    assert a.reasons == ("long_unmatched_span",)
    assert [s.net_missing for s in a.spans if s.flagged] == [40]


def test_net_deficit_above_the_deficit_does_not_flag():
    th = replace(DEFAULT_THRESHOLDS, net_deficit_min=41, recall_floor=0.85)
    assert analyze(SCRIPT, _t2_blind_spot_transcript(), th).status == "pass"


def test_net_deficit_is_about_missing_tokens_not_substitutions():
    # Substitutions keep net_missing near zero (a substituted word is still a
    # transcript token), so the rule leaves a noisy but complete control alone.
    words = as_asr(SCRIPT).split()
    noisy = " ".join("xyzzy" if i % 3 == 2 else w for i, w in enumerate(words))
    th = replace(DEFAULT_THRESHOLDS, net_deficit_min=12)
    a = analyze(SCRIPT, noisy, th)
    assert not any(s.flagged for s in a.spans)
    assert "long_unmatched_span" not in a.reasons


def test_net_deficit_does_not_change_the_clean_control():
    th = replace(DEFAULT_THRESHOLDS, net_deficit_min=12)
    assert analyze(SCRIPT, as_asr(SCRIPT), th).status == "pass"


def test_a_plain_skip_is_flagged_with_or_without_the_net_deficit_rule():
    skipped = drop_words(as_asr(SCRIPT), 300, 40)
    on = analyze(SCRIPT, skipped)
    off = analyze(SCRIPT, skipped, replace(DEFAULT_THRESHOLDS, net_deficit_min=None))
    assert on.status == off.status == "omission"
    assert on.spans == off.spans and on.reasons == off.reasons
    assert VERIFIER_VERSION == "4"


def test_calibrated_defaults_are_frozen():
    assert DEFAULT_THRESHOLDS == VerifyThresholds(
        anchor_min=3,
        min_span_words=12,
        max_span_ratio=0.5,
        net_deficit_min=6,
        recall_floor=0.95,
        recall_min_tokens=300,
    )


def test_defaults_flag_a_6_token_contiguous_deletion_wherever_it_is():
    words = as_asr(SCRIPT).split()
    for start in (0, 100, len(words) // 2, len(words) - 6):
        a = analyze(SCRIPT, drop_words(as_asr(SCRIPT), start, 6))
        assert a.status == "omission", start
        assert "long_unmatched_span" in a.reasons
        assert any(s.flagged and s.net_missing >= 6 for s in a.spans)


def test_the_claim_stops_at_6_tokens_a_5_token_deletion_is_not_flagged():
    # scope of the calibration, kept honest: below the threshold is not claimed
    a = analyze(SCRIPT, drop_words(as_asr(SCRIPT), 300, 5))
    assert a.status == "pass"


def test_defaults_pass_a_transcript_with_two_scattered_substitutions():
    words = as_asr(SCRIPT).split()
    noisy = " ".join("xyzzy" if i in (120, 480) else w for i, w in enumerate(words))
    a = analyze(SCRIPT, noisy)
    assert a.status == "pass" and a.reasons == ("ok",)
    assert max((s.net_missing for s in a.spans), default=0) <= 2
    assert a.recall is not None and a.recall >= 0.99


def test_scattered_losses_trip_the_recall_floor_at_095_but_not_093():
    # nine separated 5-token losses (~5% of this script): no span reaches
    # net_missing 6, so only the recall floor can catch them (the calibration's
    # reason for 0.95 rather than 0.93)
    t = as_asr(SCRIPT)
    for start in (810, 710, 610, 510, 410, 310, 210, 110, 30):
        t = drop_words(t, start, 5)
    a = analyze(SCRIPT, t)
    assert a.status == "omission" and a.reasons == ("recall_below_floor",)
    assert (
        analyze(SCRIPT, t, replace(DEFAULT_THRESHOLDS, recall_floor=0.93)).status
        == "pass"
    )


def test_production_policy_strings_are_pinned():
    from pipeline.tts import asr

    assert asr.ASR_POLICY == "gemini-3.8-flash|prompt-v1|temp0|thinking-low"
    assert asr.ASR_POLICY == asr.policy_for()
    assert VERIFIER_POLICY == f"verifier-v4|{asr.ASR_POLICY}"
    assert VERIFIER_POLICY == (
        "verifier-v4|gemini-3.8-flash|prompt-v1|temp0|thinking-low"
    )


def test_verdict_records_the_policy_of_the_transcriber_actually_used():
    from pipeline.tts import asr
    from pipeline.tts.verify import verifier_policy

    low = asr.policy_for(thinking="default")

    def t(audio, mime_type):
        return Transcription(
            as_asr(SCRIPT), "gemini-3.8-flash", "1", "STOP", 1.0, 1, 2, 3, policy=low
        )

    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "pass"
    assert v.asr.policy == low
    assert v.verifier_policy == verifier_policy(low) != VERIFIER_POLICY
    assert v.to_dict()["asr"]["policy"] == low
    assert v.to_dict()["verifier_policy"] == f"verifier-v{VERIFIER_VERSION}|{low}"


def test_verdict_without_a_transcription_policy_falls_back_to_the_default():
    t, _ = fake_transcriber(as_asr(SCRIPT))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.asr.policy is None
    assert v.verifier_policy == VERIFIER_POLICY


def test_unavailable_verdict_uses_the_transcribers_policy_attribute():
    from pipeline.tts import asr
    from pipeline.tts.verify import verifier_policy

    low = asr.GeminiTranscriber(thinking="default")
    exc = TranscriptionUnavailable("asr_timeout", "slow")

    def t(audio, mime_type):
        raise exc

    t.policy = low.policy
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "unavailable"
    assert v.verifier_policy == verifier_policy(low.policy)


# --- diagnostics: what the ASR heard (never affects flagging) ----------------


def _unique_words(n: int, prefix: str = "") -> list[str]:
    """Distinct alphabetic words, so difflib anchors exactly where we intend."""
    import itertools
    import string

    gen = ("".join(c) for c in itertools.product(string.ascii_lowercase, repeat=3))
    return [prefix + w for w in itertools.islice(gen, n)]


def test_flagged_span_records_what_was_heard():
    script = _unique_words(60)
    transcript = script[:25] + ["xray", "yankee"] + script[35:]  # 10 words -> 2
    a = analyze(" ".join(script), " ".join(transcript))
    flagged = [s for s in a.spans if s.flagged]
    assert len(flagged) == 1
    assert flagged[0].script_words == 10 and flagged[0].transcript_words == 2
    assert flagged[0].heard == "xray yankee"
    assert flagged[0].excerpt == " ".join(script[25:35])


def test_heard_is_empty_when_the_gap_has_no_transcript_words():
    script = _unique_words(60)
    transcript = script[:20] + script[40:]  # 20 words vanish outright
    a = analyze(" ".join(script), " ".join(transcript))
    [flagged] = [s for s in a.spans if s.flagged]
    assert flagged.heard == ""


def test_heard_is_capped_at_the_excerpt_length():
    script = _unique_words(120)
    noise = _unique_words(45, prefix="z")  # 45 heard words in place of 60
    transcript = script[:30] + noise + script[90:]
    a = analyze(" ".join(script), " ".join(transcript))
    [flagged] = [s for s in a.spans if s.flagged]
    assert flagged.transcript_words == 45
    assert flagged.heard == " ".join(noise[:30])
    assert len(flagged.heard.split()) == 30


def test_unflagged_spans_also_record_heard():
    script = _unique_words(40)
    transcript = script[:20] + ["qq"] + script[21:]  # one substitution
    a = analyze(" ".join(script), " ".join(transcript))
    [span] = a.spans
    assert not span.flagged
    assert span.heard == "qq"


def test_verdict_carries_the_raw_asr_text():
    raw = as_asr(SCRIPT)
    t, _ = fake_transcriber(raw)
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.transcript == raw
    assert v.to_dict()["transcript"] == raw


def test_verdict_carries_the_transcript_on_omission_and_asr_empty():
    raw = drop_words(as_asr(SCRIPT), 200, 40)
    t, _ = fake_transcriber(raw)
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "omission" and v.transcript == raw
    t, _ = fake_transcriber("... --- ...")
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "unavailable" and v.reasons == ("asr_empty",)
    assert v.transcript == "... --- ..."


def test_verdict_transcript_is_none_when_the_transcriber_raised():
    t, _ = fake_transcriber(exc=TranscriptionUnavailable("asr_timeout", "slow"))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.transcript is None


# --- an unavailable verdict keeps the usage of a call that answered (9p3.18) ---


def _usage(inp=1777, out=0, think=None, elapsed=2.5):
    from pipeline.tts.asr import AsrUsage

    return AsrUsage(elapsed, inp, out, think)


def test_unavailable_verdict_carries_asr_info_when_the_exception_has_usage():
    exc = TranscriptionUnavailable(
        "asr_blocked", "no candidates (block_reason=OTHER)", _usage()
    )
    t, _ = fake_transcriber(exc=exc)
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    # Status, reasons and detail are exactly what they were without usage.
    assert v.status == "unavailable" and v.reasons == ("asr_blocked",)
    assert v.detail == str(exc) and "block_reason=OTHER" in v.detail
    assert v.analysis is None and v.recall is None and v.transcript is None
    assert v.asr is not None
    assert (v.asr.input_tokens, v.asr.output_tokens, v.asr.thinking_tokens) == (
        1777,
        0,
        None,
    )
    assert v.asr.elapsed_s == 2.5
    assert v.asr.finish_reason == "NONE" and v.asr.transcript_chars == 0
    assert v.asr.model == "gemini-3.8-flash"
    assert v.to_dict()["asr"]["input_tokens"] == 1777


def test_unavailable_asr_info_uses_the_transcribers_own_model_and_policy():
    class Declared:
        model = "gemini-x"
        prompt_version = "9"
        policy = "gemini-x|prompt-v1|temp0|thinking-default"

        def __call__(self, audio, mime_type):
            raise TranscriptionUnavailable("asr_blocked", "x", _usage())

    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=Declared())
    assert v.asr.model == "gemini-x" and v.asr.prompt_version == "9"
    assert v.asr.policy == Declared.policy
    assert v.verifier_policy.endswith(Declared.policy)


def test_unavailable_asr_info_falls_back_to_the_defaults():
    from pipeline.tts import asr

    t, _ = fake_transcriber(exc=TranscriptionUnavailable("asr_blocked", "x", _usage()))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.asr.model == asr.ASR_MODEL
    assert v.asr.prompt_version == asr.ASR_PROMPT_VERSION
    assert v.asr.policy == asr.ASR_POLICY


@pytest.mark.parametrize(
    ("reason", "finish", "chars"),
    [
        ("asr_empty", "STOP", 3),
        ("asr_incomplete", "MAX_TOKENS", 0),
        ("asr_blocked", "NONE", 0),
    ],
)
def test_every_answered_unavailable_reason_keeps_its_usage(reason, finish, chars):
    usage = replace(_usage(100, 5, 3), finish_reason=finish, transcript_chars=chars)
    t, _ = fake_transcriber(exc=TranscriptionUnavailable(reason, "x", usage))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.reasons == (reason,)
    assert v.asr.finish_reason == finish and v.asr.transcript_chars == chars
    assert (v.asr.input_tokens, v.asr.output_tokens, v.asr.thinking_tokens) == (
        100,
        5,
        3,
    )


def test_unavailable_verdict_has_no_asr_info_when_the_exception_has_no_usage():
    t, _ = fake_transcriber(exc=TranscriptionUnavailable("asr_timeout", "slow"))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.asr is None
    assert v.to_dict()["asr"] is None
