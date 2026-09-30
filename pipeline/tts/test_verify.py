import re
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.tts.asr import Transcription, TranscriptionUnavailable
from pipeline.tts.verify import (
    DEFAULT_THRESHOLDS,
    VERIFIER_VERSION,
    VerifyThresholds,
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
    a = analyze(SCRIPT, t, replace(DEFAULT_THRESHOLDS, recall_floor=0.97))
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
    th = replace(DEFAULT_THRESHOLDS, recall_floor=0.85, min_span_words=50)
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
