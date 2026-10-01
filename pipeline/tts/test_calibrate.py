import ast
import json
import random
import struct
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.tts import calibrate
from pipeline.tts.asr import pcm_to_wav
from pipeline.tts.calibrate import (
    EDGE_TOKENS,
    FAMILIES,
    MULTI_MIN_GAP_TOKENS,
    MULTI_TOTAL,
    BaseLabel,
    CutSpec,
    EvalRecord,
    SimSpec,
    choose_cuts,
    cut_pcm,
    default_grid,
    evaluate,
    map_words_to_script,
    read_wav,
    reconstruction,
    removed_sanity,
    replay,
    screen_base,
    simulate,
)
from pipeline.tts.config import PCM_SAMPLE_RATE
from pipeline.tts.normalize import normalize_tokens
from pipeline.tts.verify import DEFAULT_THRESHOLDS, VerifyThresholds, analyze


FIXTURE = Path(__file__).parent / "fixtures" / "rundown_2026-09-30_chunks01.txt"
SCRIPT = FIXTURE.read_text()
TOKENS = normalize_tokens(SCRIPT)
RATE = PCM_SAMPLE_RATE


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def fake_whisper(text: str, *, drop=(), substitute=()) -> dict:
    """What whisper-1 would return for a faithful read of ``text``.

    Whitespace words with edge punctuation stripped (whisper keeps the inner
    "." of "$1.5"). ``drop`` / ``substitute`` are word indices to omit / garble.
    """
    words, t = [], 0.0
    for i, raw in enumerate(text.split()):
        w = raw.strip('.,;:!?"“”()')
        dur = 0.18 + 0.03 * len(w)
        if i in drop or not w:
            t += dur + 0.12
            continue
        if i in substitute:
            w = "zzq"
        words.append({"word": w, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur + 0.12
    return {"words": words, "duration": round(t + 0.5, 3), "text": text}


def ramp_pcm(n_samples: int) -> bytes:
    return b"".join(struct.pack("<h", i % 30000) for i in range(n_samples))


def faithful(text=SCRIPT, base_id="b0", **kw) -> BaseLabel:
    lab = screen_base(text, fake_whisper(text, **kw), base_id=base_id)
    assert lab.label == "faithful", lab.reasons
    return lab


@pytest.fixture(scope="module")
def base() -> BaseLabel:
    return faithful()


def rng(seed=0) -> random.Random:
    return random.Random(seed)


# --------------------------------------------------------------------------- #
# cut_pcm / wav
# --------------------------------------------------------------------------- #


def test_cut_pcm_removes_exact_samples_and_returns_clips():
    pcm = ramp_pcm(100)
    remaining, clips = cut_pcm(pcm, [(10, 20), (50, 55)])
    assert len(remaining) == 2 * (100 - 15)
    assert clips == [pcm[20:40], pcm[100:110]]
    assert remaining == pcm[:20] + pcm[40:100] + pcm[110:]
    # 16-bit aligned: every remaining sample is one of the originals, intact
    assert struct.unpack("<85h", remaining) == tuple(
        v
        for i, v in enumerate(struct.unpack("<100h", pcm))
        if i not in (*range(10, 20), *range(50, 55))
    )


def test_cut_pcm_edges_touching_and_no_intervals():
    pcm = ramp_pcm(40)
    assert cut_pcm(pcm, []) == (pcm, [])
    remaining, clips = cut_pcm(pcm, [(0, 10), (10, 20), (30, 40)])
    assert remaining == pcm[40:60]
    assert [len(c) for c in clips] == [20, 20, 20]
    remaining, clips = cut_pcm(pcm, [(0, 40)])
    assert remaining == b"" and clips == [pcm]


@pytest.mark.parametrize(
    "intervals",
    [
        [(10, 20), (15, 25)],  # overlap
        [(30, 40), (10, 20)],  # unsorted
        [(-1, 5)],
        [(0, 41)],  # past the end
        [(5, 5)],  # empty
        [(8, 3)],  # reversed
    ],
)
def test_cut_pcm_rejects_bad_intervals(intervals):
    with pytest.raises(ValueError):
        cut_pcm(ramp_pcm(40), intervals)


def test_cut_pcm_rejects_odd_length_and_non_integers():
    with pytest.raises(ValueError):
        cut_pcm(b"\x00\x00\x00", [(0, 1)])
    with pytest.raises(TypeError):
        cut_pcm(ramp_pcm(10), [(0.5, 3)])


def test_wav_roundtrip_and_rejects_non_mono_16bit():
    pcm = ramp_pcm(50)
    assert read_wav(calibrate.wav_bytes(pcm)) == (pcm, RATE)
    assert calibrate.wav_bytes(pcm) == pcm_to_wav(pcm)
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00\x00" * 8)
    with pytest.raises(ValueError):
        read_wav(buf.getvalue())


# --------------------------------------------------------------------------- #
# word <-> token mapping
# --------------------------------------------------------------------------- #


def test_map_words_handles_multiword_numbers():
    script = normalize_tokens("It cost $1.5 billion in total.")
    words = [
        {"word": "It", "start": 0.0, "end": 0.2},
        {"word": "cost", "start": 0.3, "end": 0.6},
        {"word": "$1.5", "start": 0.7, "end": 1.4},
        {"word": "billion", "start": 1.4, "end": 1.9},
        {"word": "in", "start": 2.0, "end": 2.1},
        {"word": "total", "start": 2.2, "end": 2.7},
    ]
    wm = map_words_to_script(script, words)
    assert [m.token for m in wm] == script
    by_token = {m.token: m for m in wm}
    # tokens of the number group carry the whole group's time span, not exact
    for tok in ("one", "point", "five", "billion", "dollars"):
        m = by_token[tok]
        assert (m.word_index, m.start, m.end, m.exact) == (2, 0.7, 1.9, False)
    # the words around it map 1:1 and are exact boundary candidates
    assert (by_token["in"].word_index, by_token["in"].exact) == (4, True)
    assert (by_token["total"].word_index, by_token["total"].end) == (5, 2.7)
    assert by_token["it"].exact and by_token["cost"].exact


def test_map_words_zero_token_word_and_unmatched_script_token():
    script = normalize_tokens("alpha beta gamma delta epsilon")
    words = [
        {"word": "alpha", "start": 0.0, "end": 0.5},
        {"word": "—", "start": 0.5, "end": 0.6},  # normalizes to no tokens
        {"word": "beta", "start": 0.6, "end": 1.0},
        {"word": "gamma", "start": 1.0, "end": 1.4},
        {"word": "epsilon", "start": 1.5, "end": 2.0},  # "delta" never spoken
    ]
    wm = map_words_to_script(script, words)
    assert [m.word_index for m in wm] == [0, 2, 3, None, 4]
    assert (wm[3].start, wm[3].end, wm[3].exact) == (None, None, False)
    # alpha beta gamma form a 3-block: exact; epsilon's block is size 1: not
    assert [m.exact for m in wm] == [True, True, True, False, False]


def test_stray_single_token_match_is_never_exact():
    wm = map_words_to_script(
        ["the", "cat", "sat"],
        [
            {"word": "a", "start": 0, "end": 1},
            {"word": "the", "start": 1, "end": 2},
            {"word": "dog", "start": 2, "end": 3},
        ],
    )
    assert wm[0].word_index == 1 and not wm[0].exact


def test_multitoken_word_is_matched_but_not_exact():
    wm = map_words_to_script(
        normalize_tokens("see you in 2026 again soon"),
        [
            {"word": w, "start": float(i), "end": i + 0.5}
            for i, w in enumerate(["see", "you", "in", "2026", "again", "soon"])
        ],
    )
    year = [m for m in wm if m.token in ("twenty", "six")]
    assert all(m.word_index == 3 and not m.exact for m in year)
    assert wm[-1].exact and wm[0].exact


# --------------------------------------------------------------------------- #
# screen_base
# --------------------------------------------------------------------------- #


def test_faithful_base(base):
    assert base.label == "faithful" and base.reasons == ()
    assert base.recall is not None and base.recall >= 0.99
    assert base.suspect_spans == ()
    assert base.script_tokens == tuple(TOKENS)
    assert sum(m.exact for m in base.word_map) > 0.8 * len(TOKENS)
    assert base.total_samples == round(fake_whisper(SCRIPT)["duration"] * RATE)


def test_dropped_stretch_is_suspect_with_a_clip_window():
    drop = range(300, 330)
    lab = screen_base(SCRIPT, fake_whisper(SCRIPT, drop=drop), base_id="x")
    assert lab.label == "suspect"
    assert "net_missing_span" in lab.reasons
    (span,) = lab.suspect_spans
    assert span.net_missing >= 6
    whisper = fake_whisper(SCRIPT, drop=drop)
    dropped_start = whisper["words"][299]["end"]
    assert span.clip_start_s <= dropped_start
    assert 0 <= span.clip_start_s < span.clip_end_s <= whisper["duration"]
    # the owner clip is the span +/- 3 s, not the whole base
    assert span.clip_end_s - span.clip_start_s < whisper["duration"] / 3


def test_net_missing_5_is_faithful_6_is_suspect():
    five = screen_base(SCRIPT, fake_whisper(SCRIPT, drop=range(300, 305)))
    six = screen_base(SCRIPT, fake_whisper(SCRIPT, drop=range(300, 306)))
    assert five.label == "faithful"
    assert six.label == "suspect"


def test_low_recall_alone_is_suspect():
    # one garbled word in 12: spans stay short but recall falls under 0.95
    sub = range(5, 800, 12)
    lab = screen_base(SCRIPT, fake_whisper(SCRIPT, substitute=sub))
    assert lab.label == "suspect"
    assert lab.reasons == ("recall_below_0.95",)
    assert lab.suspect_spans == ()


def test_empty_script_and_empty_whisper_are_suspect():
    assert screen_base("... --", fake_whisper("hello there")).reasons == (
        "empty_script",
    )
    lab = screen_base("hello there my friend", {"words": [], "duration": 1.0})
    assert lab.label == "suspect" and lab.recall == 0.0


def test_total_samples_default_and_override():
    w = fake_whisper("alpha beta gamma delta")
    assert screen_base("alpha beta gamma delta", w).total_samples == round(
        w["duration"] * RATE
    )
    lab = screen_base("alpha beta gamma delta", w, total_samples=12345)
    assert lab.total_samples == 12345


def test_base_label_roundtrips_through_json(base):
    again = BaseLabel.from_dict(json.loads(json.dumps(base.to_dict())))
    assert again == base


# --------------------------------------------------------------------------- #
# choose_cuts
# --------------------------------------------------------------------------- #

ST = calibrate._structure(SCRIPT)


def _check_invariants(spec: CutSpec, base: BaseLabel):
    assert spec.base_id == base.base_id and spec.base_samples == base.total_samples
    last_end = 0
    for iv in spec.intervals:
        # boundary tokens are exactly matched
        assert base.word_map[iv.script_start].exact
        assert base.word_map[iv.script_end - 1].exact
        # label text is the literal script text and its normalized length
        assert iv.literal
        assert iv.removed_text in SCRIPT
        assert (
            normalize_tokens(iv.removed_text) == TOKENS[iv.script_start : iv.script_end]
        )
        assert iv.normalized_tokens == iv.script_end - iv.script_start
        # cut points sit in the gaps next to the first/last word, never inside one
        w0, w1 = base.words[iv.first_word], base.words[iv.last_word]
        assert iv.sample_start <= round(w0.start * RATE)
        assert iv.sample_end >= round(w1.end * RATE)
        prev_end = base.words[iv.first_word - 1].end if iv.first_word else 0.0
        assert iv.sample_start >= round(prev_end * RATE)
        assert iv.sample_start < iv.sample_end <= base.total_samples
        assert iv.sample_start >= last_end
        last_end = iv.sample_end
    assert spec.total_tokens == sum(i.normalized_tokens for i in spec.intervals)


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_yields_a_valid_cut(base, family):
    size = MULTI_TOTAL if family == "multi" else 20
    if family == "paragraph":
        size = 80
    spec = choose_cuts(base, family, size, rng(1), seed=1)
    assert spec is not None, family
    _check_invariants(spec, base)
    assert spec.family == family and spec.size_bin == size and spec.seed == 1
    assert spec.cut_id == f"b0--{family}-{size}"


def test_start_and_end_sit_at_the_edges(base):
    for seed in range(15):
        a = choose_cuts(base, "start", 20, rng(seed))
        assert a.intervals[0].script_start < EDGE_TOKENS
        assert a.total_tokens == 20
        b = choose_cuts(base, "end", 20, rng(seed))
        assert b.intervals[0].script_end > len(TOKENS) - EDGE_TOKENS
        assert b.total_tokens == 20


def test_mid_fluent_never_touches_a_sentence_boundary(base):
    sent = set(ST.sentence_starts)
    bounds = sent | {len(TOKENS)}

    def sentence_id(i):
        return sum(1 for x in sent if x <= i)

    within_by_size = {}
    for size in (10, 20, 40, 80):
        for seed in range(25):
            spec = choose_cuts(base, "mid_fluent", size, rng(seed))
            s, e = spec.token_intervals[0]
            assert s not in sent and e not in bounds
            within = sentence_id(s) == sentence_id(e - 1)
            assert spec.notes["within_sentence"] == within
            within_by_size.setdefault(size, set()).add(within)
    # short cuts fit inside one sentence (and that is preferred); an 80-token
    # cut cannot, so it begins and ends mid-sentence across a boundary
    assert within_by_size[10] == {True}
    assert within_by_size[80] == {False}


def test_sentence_cut_snaps_to_sentence_boundaries(base):
    sent = set(ST.sentence_starts)
    bounds = sent | {len(TOKENS)}
    for size in (20, 40, 80):
        for seed in range(10):
            spec = choose_cuts(base, "sentence", size, rng(seed))
            assert spec is not None
            s, e = spec.token_intervals[0]
            assert s in sent and e in bounds
            assert 0.6 * size <= e - s <= 1.4 * size
            assert (
                spec.intervals[0].removed_text.rstrip().endswith((".", "?", "!", '"'))
            )


def test_paragraph_cut_snaps_to_paragraph_boundaries(base):
    paras = set(ST.paragraph_starts)
    bounds = paras | {len(TOKENS)}
    lengths = [b - a for a, b in zip(sorted(bounds), sorted(bounds)[1:], strict=False)]
    size = lengths[1]  # a paragraph that exists
    spec = choose_cuts(base, "paragraph", size, rng(3))
    s, e = spec.token_intervals[0]
    assert s in paras and e in bounds
    assert abs((e - s) - size) <= 0.4 * size
    # the removed text starts at a paragraph start in the script
    assert SCRIPT.count(spec.intervals[0].removed_text) == 1


def test_predictable_contains_a_quote_or_a_repeated_trigram(base):
    kinds = set()
    for seed in range(40):
        spec = choose_cuts(base, "predictable", 10, rng(seed))
        assert spec is not None
        s, e = spec.token_intervals[0]
        why = spec.notes["predictable"]
        kinds.add(why["kind"])
        if why["kind"] == "quote":
            assert any(
                (s <= qs and qe <= e) or (qs <= s and e <= qe) for qs, qe in ST.quotes
            )
        else:
            gram = why["text"]
            assert gram in " ".join(TOKENS[s:e])
            # ...and the same 3-gram also occurs entirely outside the cut
            outside = " ".join(TOKENS[:s]) + " | " + " ".join(TOKENS[e:])
            assert gram in outside
    assert kinds == {"quote", "repeat_3gram"}


def test_predictable_quote_candidate_exists():
    text = (
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima. "
        'Then she told reporters "this was never going to be easy for anyone" and '
        "walked away from the podium without taking questions from the press. "
        "Mike november oscar papa quebec romeo sierra tango uniform victor whiskey."
    )
    lab = faithful(text, base_id="q")
    spec = choose_cuts(lab, "predictable", 12, rng(0))
    assert spec is not None and spec.notes["predictable"]["kind"] == "quote"
    s, e = spec.token_intervals[0]
    quote = tuple(normalize_tokens("this was never going to be easy for anyone"))
    window = lab.script_tokens[s:e]
    assert any(window[i : i + len(quote)] == quote for i in range(len(window)))


def test_predictable_without_quotes_or_repeats_is_none():
    text = " ".join(
        f"word{chr(97 + i % 26)}{chr(97 + i // 26)}unique" for i in range(120)
    )
    lab = faithful(text, base_id="u")
    assert choose_cuts(lab, "predictable", 10, rng(0)) is None


def test_multi_is_three_separated_cuts(base):
    for seed in range(15):
        spec = choose_cuts(base, "multi", MULTI_TOTAL, rng(seed))
        _check_invariants(spec, base)
        assert len(spec.intervals) == 3
        assert [i.normalized_tokens for i in spec.intervals] == [8, 8, 8]
        for a, b in zip(spec.intervals, spec.intervals[1:], strict=False):
            assert b.script_start - a.script_end >= MULTI_MIN_GAP_TOKENS
            assert b.sample_start >= a.sample_end


def test_multi_splits_an_uneven_total(base):
    spec = choose_cuts(base, "multi", 26, rng(2))
    assert sorted(i.normalized_tokens for i in spec.intervals) == [8, 9, 9]


def test_choose_cuts_is_deterministic_and_seed_sensitive(base):
    for family in FAMILIES:
        size = MULTI_TOTAL if family == "multi" else 20
        a = choose_cuts(base, family, size, rng(7), seed=7)
        b = choose_cuts(base, family, size, rng(7), seed=7)
        assert a == b
    starts = {
        choose_cuts(base, "mid_fluent", 20, rng(s)).token_intervals[0]
        for s in range(12)
    }
    assert len(starts) > 3


def test_boundaries_avoid_inexact_tokens():
    # whisper garbles every other word in 100..299: nothing there may bound a cut
    bad = range(100, 300, 2)
    lab = replace(
        screen_base(SCRIPT, fake_whisper(SCRIPT, substitute=bad), base_id="g"),
        label="faithful",
    )
    for seed in range(40):
        spec = choose_cuts(lab, "mid_fluent", 10, rng(seed))
        for iv in spec.intervals:
            assert lab.word_map[iv.script_start].exact
            assert lab.word_map[iv.script_end - 1].exact


def test_choose_cuts_rejects_bad_requests_and_reports_impossible(base):
    suspect = screen_base(SCRIPT, fake_whisper(SCRIPT, drop=range(300, 330)))
    with pytest.raises(ValueError, match="faithful"):
        choose_cuts(suspect, "start", 10, rng())
    with pytest.raises(ValueError, match="family"):
        choose_cuts(base, "nope", 10, rng())
    with pytest.raises(ValueError):
        choose_cuts(base, "start", 0, rng())
    with pytest.raises(ValueError):
        choose_cuts(base, "multi", 2, rng())
    assert choose_cuts(base, "start", 5000, rng()) is None
    assert choose_cuts(base, "sentence", 1, rng()) is None  # no 1-token sentence


def test_number_tokens_never_bound_a_cut():
    text = (
        "Alpha bravo charlie delta echo foxtrot golf hotel. Shares rose to "
        "$1.5 billion in 2026 after the deal closed on Friday. India juliet kilo "
        "lima mike november oscar papa quebec romeo sierra tango uniform victor."
    )
    lab = faithful(text, base_id="n")
    wm = lab.word_map
    for seed in range(40):
        spec = choose_cuts(lab, "mid_fluent", 5, rng(seed))
        if spec is None:
            continue
        s, e = spec.token_intervals[0]
        assert wm[s].exact and wm[e - 1].exact
        for i in (s, e - 1):
            assert lab.script_tokens[i] not in ("point", "dollars", "twenty")


def test_cut_spec_apply_matches_its_labels(base):
    spec = choose_cuts(base, "sentence", 20, rng(5))
    pcm = ramp_pcm(base.total_samples)
    remaining, clips = spec.apply(pcm)
    assert len(remaining) == 2 * (base.total_samples - spec.removed_samples)
    assert len(clips) == len(spec.intervals)
    s0, s1 = spec.sample_intervals[0]
    assert clips[0] == pcm[2 * s0 : 2 * s1]
    with pytest.raises(ValueError):
        spec.apply(pcm[:-2])


def test_cut_spec_roundtrips_through_json(base):
    for family in FAMILIES:
        size = {"multi": MULTI_TOTAL, "paragraph": 80}.get(family, 20)
        spec = choose_cuts(base, family, size, rng(2), seed="s-2")
        again = CutSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
        assert again == spec


def test_removed_sanity_within_25_percent():
    words = {
        "words": [
            {"word": f"word{chr(97 + i)}", "start": i, "end": i + 0.5}
            for i in range(20)
        ]
    }
    assert removed_sanity(20, words)["ok"]
    assert removed_sanity(25, words)["ok"]  # 20 vs 25 = 20%
    bad = removed_sanity(30, words)
    assert not bad["ok"] and bad["observed_tokens"] == 20


# --------------------------------------------------------------------------- #
# simulate
# --------------------------------------------------------------------------- #


def test_simulate_reproduces_the_t2_blind_spot_exactly():
    spec = SimSpec(
        "delete_noise", size=40, position="mid", noise_span=45, noise_every=3
    )
    res = simulate(TOKENS, TOKENS, spec, rng(0))
    mid = (len(TOKENS) - 40) // 2
    assert res.deleted == ((mid, mid + 40),)
    assert len(res.tokens) == len(TOKENS) - 40
    assert len(res.noise_positions) == 15
    # the ratio rule alone misses it (85 script vs 45 transcript tokens)...
    a = analyze(SCRIPT, res.text)
    assert a.status == "pass"
    blind = [s for s in a.spans if s.script_words > 40]
    assert [(s.script_words, s.transcript_words, s.net_missing) for s in blind] == [
        (85, 45, 40)
    ]
    # ...and the additive net-deficit rule catches it
    th = replace(DEFAULT_THRESHOLDS, net_deficit_min=40)
    assert analyze(SCRIPT, res.text, th).status == "omission"
    assert analyze(SCRIPT, res.text, replace(th, net_deficit_min=41)).status == "pass"


@pytest.mark.parametrize("position", ["start", "mid", "end"])
def test_simulate_plain_deletion_is_flagged_by_default(position):
    res = simulate(TOKENS, TOKENS, SimSpec("delete", size=40, position=position), rng())
    assert len(res.tokens) == len(TOKENS) - 40
    assert res.noise_positions == ()
    a = analyze(SCRIPT, res.text)
    assert a.status == "omission" and "long_unmatched_span" in a.reasons
    ((s, e),) = res.deleted
    flagged = [sp for sp in a.spans if sp.flagged]
    assert any(sp.script_start <= s + 2 and sp.script_end >= e - 2 for sp in flagged)


def test_simulate_deletes_against_a_noisy_real_transcript():
    # a transcript with ASR substitutions: the deleted script interval still maps
    transcript = [("xyzzy" if i % 25 == 3 else t) for i, t in enumerate(TOKENS)]
    res = simulate(
        transcript, TOKENS, SimSpec("delete", size=40, position="mid"), rng()
    )
    ((bs, be),) = res.removed_transcript
    assert 36 <= be - bs <= 44
    assert len(res.tokens) == len(transcript) - (be - bs)


def test_simulate_multi_is_separated_and_exact():
    res = simulate(TOKENS, TOKENS, SimSpec("multi", size=8, count=3), rng(4))
    assert len(res.deleted) == 3
    for (_a0, a1), (b0, _b1) in zip(res.deleted, res.deleted[1:], strict=False):
        assert b0 - a1 >= MULTI_MIN_GAP_TOKENS
    assert len(res.tokens) == len(TOKENS) - 24
    assert analyze(SCRIPT, res.text).status in ("pass", "omission")


def test_simulate_repeated_phrase_deletion_keeps_a_twin():
    tri = calibrate._trigram_positions(TOKENS)
    for seed in range(10):
        res = simulate(TOKENS, TOKENS, SimSpec("repeat_delete", size=20), rng(seed))
        ((s, e),) = res.deleted
        gram = calibrate._repeated_gram(tri, TOKENS, s, e)
        assert gram is not None
        # the phrase survives elsewhere in the synthetic transcript
        assert " ".join(gram) in " ".join(res.tokens)


def test_simulate_is_deterministic_and_validates():
    spec = SimSpec("delete", size=20, position="random")
    assert simulate(TOKENS, TOKENS, spec, rng(3)) == simulate(
        TOKENS, TOKENS, spec, rng(3)
    )
    assert simulate(TOKENS, TOKENS, spec, rng(3)) != simulate(
        TOKENS, TOKENS, spec, rng(4)
    )
    with pytest.raises(ValueError):
        simulate(TOKENS, TOKENS, SimSpec("delete", size=5000), rng())
    with pytest.raises(ValueError):
        simulate(TOKENS, TOKENS, SimSpec("delete", position="sideways"), rng())
    unique = [f"t{i}x" for i in range(200)]
    with pytest.raises(ValueError, match="repeated"):
        simulate(unique, unique, SimSpec("repeat_delete", size=20), rng())
    res = simulate(TOKENS, TOKENS, SimSpec("delete", size=10), rng())
    assert json.loads(json.dumps(res.to_dict()))["spec"]["kind"] == "delete"


# --------------------------------------------------------------------------- #
# reconstruction
# --------------------------------------------------------------------------- #

R_TEXT = " ".join(f"w{chr(97 + i // 26)}{chr(97 + i % 26)}x" for i in range(60))
R_TOKENS = normalize_tokens(R_TEXT)


def _label(intervals):
    from pipeline.tts.calibrate import CutInterval

    ivs = tuple(
        CutInterval(s, e, e - s, 0, 0, 0, 1, " ".join(R_TOKENS[s:e]), True)
        for s, e in intervals
    )
    return CutSpec(
        "c", "b", "mid_fluent", 20, ivs, sum(e - s for s, e in intervals), 100, 0
    )


def test_reconstruction_detects_a_confirmed_reconstruction():
    label = _label([(20, 40)])
    expected = R_TOKENS[:20] + R_TOKENS[40:]
    reconstructed = " ".join(R_TOKENS)  # the ASR "heard" the removed text anyway
    out = reconstruction(label, R_TEXT, R_TEXT, reconstructed)
    assert out["level"] == "confirmed"
    assert out["cut_hits"] == 20 and out["clean_hits"] == 20
    assert out["fraction"] == 1.0 and out["fraction_of_clean"] == 1.0
    assert out["matched_text"] == [" ".join(R_TOKENS[20:40])]
    faithful_cut = " ".join(expected)
    clean = reconstruction(label, R_TEXT, R_TEXT, faithful_cut)
    assert clean["level"] == "none" and clean["cut_hits"] == 0
    assert clean["clean_hits"] == 20  # the uncut base does contain it: the ceiling


def test_reconstruction_partial_and_short_matches():
    label = _label([(20, 40)])
    expected = R_TOKENS[:20] + R_TOKENS[40:]
    # 5 of 20 removed tokens reappear: partial, not confirmed
    partial = expected[:20] + R_TOKENS[25:30] + expected[20:]
    out = reconstruction(label, R_TEXT, R_TEXT, " ".join(partial))
    assert out["level"] == "partial" and out["cut_hits"] == 5
    # scattered single/double token matches are coincidence, not reconstruction
    scatter = (
        expected[:20]
        + [R_TOKENS[22], "qq1", R_TOKENS[30], R_TOKENS[31], "qq2"]
        + expected[20:]
    )
    out = reconstruction(label, R_TEXT, None, " ".join(scatter))
    assert out["level"] == "none" and out["cut_hits"] == 0
    assert out["clean_hits"] is None and out["fraction_of_clean"] is None


def test_reconstruction_multi_interval_and_validation():
    label = _label([(5, 15), (30, 40)])
    expected = [t for i, t in enumerate(R_TOKENS) if not (5 <= i < 15 or 30 <= i < 40)]
    half = expected[:5] + R_TOKENS[5:15] + expected[5:]  # only the first interval back
    out = reconstruction(label, R_TEXT, R_TEXT, " ".join(half))
    assert [i["cut_hits"] for i in out["intervals"]] == [10, 0]
    assert out["level"] == "confirmed"  # 10 of 20 removed tokens
    with pytest.raises(ValueError):
        reconstruction(_label([(50, 70)]), R_TEXT, R_TEXT, R_TEXT)


# --------------------------------------------------------------------------- #
# evaluate
# --------------------------------------------------------------------------- #

_SIM_BLIND = simulate(
    TOKENS, TOKENS, SimSpec("delete_noise", size=40, position="mid"), rng(0)
)
_SIM_PLAIN = simulate(
    TOKENS, TOKENS, SimSpec("delete", size=40, position="mid"), rng(0)
)


def _rec(rid, kind, transcript, **kw):
    return EvalRecord(
        record_id=rid,
        kind=kind,
        base_id=kw.pop("base_id", rid.split(":")[0]),
        script_text=SCRIPT,
        transcript=transcript,
        **kw,
    )


def _records():
    mid = (len(TOKENS) - 40) // 2
    cut = {"family": "mid_fluent", "size_bin": 40, "removed": ((mid, mid + 40),)}
    return [
        _rec(
            "b1:r0",
            "base",
            " ".join(TOKENS),
            split="dev",
            feed="rundown",
            model="flash",
        ),
        _rec(
            "b1:r1",
            "base",
            " ".join(TOKENS),
            split="dev",
            feed="rundown",
            model="flash",
            repeat=1,
        ),
        _rec("b2:r0", "base", None, unavailable="asr_timeout", split="dev", feed="fp"),
        _rec(
            "b3:r0", "base", " ".join(TOKENS[100:400]), split="holdout", feed="fp"
        ),  # false alarm
        _rec(
            "c1",
            "cut",
            _SIM_PLAIN.text,
            split="dev",
            feed="rundown",
            model="flash",
            **cut,
        ),
        _rec(
            "c2",
            "cut",
            _SIM_BLIND.text,
            split="dev",
            feed="rundown",
            model="lite",
            **cut,
        ),
        _rec(
            "c3",
            "cut",
            None,
            unavailable="asr_error",
            split="holdout",
            feed="fp",
            **cut,
        ),
    ]


def test_replay_matches_verify_analyze_and_localizes():
    recs = {r.record_id: r for r in _records()}
    o = replay(recs["c1"], DEFAULT_THRESHOLDS)
    assert o.status == "omission" and o.localized and o.flagged_spans == 1
    o = replay(recs["c2"], DEFAULT_THRESHOLDS)
    assert o.status == "pass" and not o.localized
    o = replay(recs["c2"], replace(DEFAULT_THRESHOLDS, net_deficit_min=40))
    assert o.status == "omission" and o.localized
    assert replay(recs["c3"], DEFAULT_THRESHOLDS).status == "unavailable"
    assert replay(_rec("e", "base", "... ---"), DEFAULT_THRESHOLDS).reasons == (
        "asr_empty",
    )


def test_evaluate_counts_detection_false_alarms_and_availability_separately():
    off, on = (replace(DEFAULT_THRESHOLDS, net_deficit_min=m) for m in (None, 40))
    r_off, r_on = evaluate(_records(), [off, on])

    cuts = r_off["cuts"]
    assert cuts["overall"] == {
        "n": 3,
        "caught": 1,
        "missed": 1,
        "unavailable": 1,
        "localized": 1,
    }
    assert cuts["missed"] == ["c2"]
    assert cuts["by_split"]["dev"]["caught"] == 1
    assert cuts["by_split"]["holdout"] == {
        "n": 1,
        "caught": 0,
        "missed": 0,
        "unavailable": 1,
        "localized": 0,
    }
    assert cuts["by_family"]["mid_fluent"]["n"] == 3
    assert cuts["by_family_size"]["mid_fluent/40"]["n"] == 3
    assert cuts["by_model"]["flash"]["caught"] == 1
    assert cuts["by_model"]["lite"]["missed"] == 1

    bases = r_off["bases"]
    assert bases["overall"] == {
        "n": 4,
        "passed": 2,
        "false_alarms": 1,
        "unavailable": 1,
        "bases": 3,
        "bases_with_false_alarm": 1,
    }
    assert bases["false_alarms"] == ["b3:r0"] and bases["false_alarm_bases"] == ["b3"]
    assert bases["by_feed"]["rundown"]["passed"] == 2
    assert bases["by_feed"]["fp"] == {
        "n": 2,
        "passed": 0,
        "false_alarms": 1,
        "unavailable": 1,
    }
    assert "by_family" not in bases

    # the rule catches c2; availability is untouched by the thresholds
    assert r_on["cuts"]["overall"]["caught"] == 2 and r_on["cuts"]["missed"] == []
    assert r_on["cuts"]["overall"]["unavailable"] == 1
    assert r_on["thresholds"]["net_deficit_min"] == 40
    json.dumps([r_off, r_on])  # fully JSON-serializable


def test_unavailable_is_never_counted_as_a_catch():
    only = [
        _rec("c", "cut", None, unavailable="asr_incomplete", family="end", size_bin=10)
    ]
    (res,) = evaluate(only, [DEFAULT_THRESHOLDS])
    assert res["cuts"]["overall"] == {
        "n": 1,
        "caught": 0,
        "missed": 0,
        "unavailable": 1,
        "localized": 0,
    }


def test_evaluate_with_no_records_is_all_zero():
    (res,) = evaluate([], [DEFAULT_THRESHOLDS])
    assert res["cuts"]["overall"]["n"] == 0 and res["bases"]["overall"]["bases"] == 0


def test_default_grid_is_the_declared_rule_space():
    grid = default_grid()
    assert len(grid) == 15
    assert {t.net_deficit_min for t in grid} == {None, 12, 16, 20, 24}
    assert {t.recall_floor for t in grid} == {0.85, 0.90, 0.93}
    assert all(t.anchor_min == 3 for t in grid)
    assert all(isinstance(t, VerifyThresholds) for t in grid)


def test_eval_record_roundtrips_through_json():
    for r in _records():
        again = EvalRecord.from_dict(json.loads(json.dumps(r.to_dict())))
        assert again == r


# --------------------------------------------------------------------------- #
# hygiene
# --------------------------------------------------------------------------- #


def test_module_imports_only_stdlib_and_pipeline_tts():
    tree = ast.parse(Path(calibrate.__file__).read_text())
    stdlib = sys.stdlib_module_names
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            assert top in stdlib or name.startswith("pipeline.tts"), name
