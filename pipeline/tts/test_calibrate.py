import ast
import json
import random
import struct
import sys
from array import array
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
    CutInterval,
    CutSpec,
    EvalRecord,
    SimSpec,
    boundary_eligibility,
    choose_cuts,
    cut_pcm,
    default_grid,
    evaluate,
    finalize_cut,
    map_words_to_script,
    read_wav,
    reconstruction,
    removed_sanity,
    replay,
    screen_base,
    simulate,
    verify_cut_audio,
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


def _word(n: int) -> str:
    return "w" + "".join(chr(97 + (n // 26**k) % 26) for k in range(3)) + "x"


def ts(whisper: dict) -> int:
    """The sample count of the audio a fake whisper response describes."""
    return round(whisper["duration"] * RATE)


def screen(text, whisper, **kw) -> BaseLabel:
    return screen_base(text, whisper, total_samples=ts(whisper), **kw)


def faithful(text=SCRIPT, base_id="b0", **kw) -> BaseLabel:
    lab = screen(text, fake_whisper(text, **kw), base_id=base_id)
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
    lab = screen(SCRIPT, fake_whisper(SCRIPT, drop=drop), base_id="x")
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
    five = screen(SCRIPT, fake_whisper(SCRIPT, drop=range(300, 305)))
    six = screen(SCRIPT, fake_whisper(SCRIPT, drop=range(300, 306)))
    assert five.label == "faithful"
    assert six.label == "suspect"


def test_low_recall_alone_is_suspect():
    # one garbled word in 12: spans stay short but recall falls under 0.95
    sub = range(5, 800, 12)
    lab = screen(SCRIPT, fake_whisper(SCRIPT, substitute=sub))
    assert lab.label == "suspect"
    assert lab.reasons == ("recall_below_0.95",)
    assert lab.suspect_spans == ()


def test_empty_script_and_empty_whisper_are_suspect():
    assert screen("... --", fake_whisper("hello there")).reasons == ("empty_script",)
    lab = screen_base("hello there my friend", {"words": []}, total_samples=RATE)
    assert lab.label == "suspect" and lab.recall == 0.0


def test_total_samples_is_required_and_comes_from_the_caller():
    w = fake_whisper("alpha beta gamma delta")
    with pytest.raises(TypeError):
        screen_base("alpha beta gamma delta", w)  # type: ignore[call-arg]
    lab = screen_base("alpha beta gamma delta", w, total_samples=ts(w) + 777)
    assert lab.total_samples == ts(w) + 777


def test_bare_words_list_works():
    w = fake_whisper("alpha beta gamma delta")
    lab = screen_base("alpha beta gamma delta", w["words"], total_samples=ts(w))
    assert lab.label == "faithful" and lab.total_samples == ts(w)


def test_whisper_that_outruns_the_pcm_is_rejected():
    w = fake_whisper("alpha beta gamma delta")
    with pytest.raises(ValueError, match="longer than"):
        screen_base("alpha beta gamma delta", w, total_samples=RATE // 4)
    with pytest.raises(ValueError):
        screen_base("alpha", w, total_samples=0)


def test_base_label_roundtrips_through_json(base):
    again = BaseLabel.from_dict(json.loads(json.dumps(base.to_dict())))
    assert again == base


# --------------------------------------------------------------------------- #
# choose_cuts
# --------------------------------------------------------------------------- #

ST = calibrate._structure(SCRIPT)


def _mid(w) -> int:
    return round((w.start + w.end) / 2 * RATE)


def _check_invariants(spec: CutSpec, base: BaseLabel, *, finalized=False):
    assert spec.base_id == base.base_id and spec.base_samples == base.total_samples
    assert spec.sample_rate == base.sample_rate
    last_end = 0
    for iv in spec.intervals:
        # boundary tokens are exactly matched
        assert base.word_map[iv.script_start].exact
        assert base.word_map[iv.script_end - 1].exact
        # label text is the literal script text and its normalized length
        assert iv.literal
        assert iv.removed_text in base.script_text
        assert (
            tuple(normalize_tokens(iv.removed_text))
            == base.script_tokens[iv.script_start : iv.script_end]
        )
        assert iv.normalized_tokens == iv.script_end - iv.script_start
        # Every cut point (nominal AND snapped) lies between the nominal
        # midpoints of the adjacent words: the cut never removes a sample from
        # inside the first half of the kept word before it, nor the second
        # half of the one after, and never keeps the boundary word's far half.
        w = base.words
        first, last = w[iv.first_word], w[iv.last_word]
        lo0 = _mid(w[iv.first_word - 1]) if iv.first_word else 0
        hi0 = _mid(first)
        lo1 = _mid(last)
        hi1 = (
            _mid(w[iv.last_word + 1])
            if iv.last_word + 1 < len(w)
            else base.total_samples
        )
        assert (iv.start_bounds, iv.end_bounds) == ((lo0, hi0), (lo1, hi1))
        for point in (iv.nominal_start, iv.sample_start):
            assert lo0 <= point <= hi0
        for point in (iv.nominal_end, iv.sample_end):
            assert lo1 <= point <= hi1
        assert iv.nominal_start <= round(first.start * RATE)
        assert iv.nominal_end >= round(last.end * RATE)
        assert iv.snapped is finalized
        if not finalized:
            assert (iv.sample_start, iv.sample_end) == (
                iv.nominal_start,
                iv.nominal_end,
            )
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
        screen(SCRIPT, fake_whisper(SCRIPT, substitute=bad), base_id="g"),
        label="faithful",
    )
    for seed in range(40):
        spec = choose_cuts(lab, "mid_fluent", 10, rng(seed))
        for iv in spec.intervals:
            assert lab.word_map[iv.script_start].exact
            assert lab.word_map[iv.script_end - 1].exact


def test_choose_cuts_rejects_bad_requests_and_reports_impossible(base):
    suspect = screen(SCRIPT, fake_whisper(SCRIPT, drop=range(300, 330)))
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


def test_a_cut_never_removes_most_of_the_chunk():
    para = " ".join(f"{_word(i)}" for i in range(80)) + "."
    lab = faithful(para, base_id="one-paragraph")
    # the only paragraph / sentence IS the chunk: removing it would empty the audio
    assert choose_cuts(lab, "paragraph", 80, rng(0)) is None
    assert choose_cuts(lab, "sentence", 80, rng(0)) is None
    spec = choose_cuts(lab, "mid_fluent", 10, rng(0))
    assert spec is not None and spec.total_tokens <= 0.5 * len(lab.script_tokens)
    assert choose_cuts(lab, "mid_fluent", 60, rng(0)) is None


def test_cut_spec_apply_matches_its_labels(base):
    spec = choose_cuts(base, "sentence", 20, rng(5))
    pcm = ramp_pcm(base.total_samples)
    with pytest.raises(ValueError, match="finalize_cut"):
        spec.apply(pcm)
    spec = finalize_cut(spec, pcm)
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
    ivs = tuple(
        CutInterval(
            script_start=s,
            script_end=e,
            normalized_tokens=e - s,
            first_word=0,
            last_word=0,
            sample_start=0,
            sample_end=1,
            removed_text=" ".join(R_TOKENS[s:e]),
            literal=True,
            nominal_start=0,
            nominal_end=1,
            start_bounds=(0, 1),
            end_bounds=(0, 1),
        )
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


def test_multi_interval_cut_is_localized_only_if_every_interval_is():
    mid = (len(TOKENS) - 40) // 2
    gone = _SIM_PLAIN  # one 40-token deletion at ``mid``
    declared = ((mid, mid + 40), (20, 40))  # ...but the cut said two intervals
    rec = _rec("m", "cut", gone.text, family="multi", size_bin=24, removed=declared)
    o = replay(rec, DEFAULT_THRESHOLDS)
    assert o.status == "omission"
    assert o.localized_intervals == (True, False)
    assert o.localized is False  # one interval was never found
    (res,) = evaluate([rec, _rec("b", "base", " ".join(TOKENS))], [DEFAULT_THRESHOLDS])
    assert res["cuts"]["overall"]["caught"] == 1
    assert res["cuts"]["overall"]["caught_localized"] == 0
    assert res["cuts"]["partially_localized"] == ["m"]
    assert res["cuts"]["unlocalized"] == ["m"]
    assert res["acceptance_ok"] is False
    # both intervals actually removed and found: localized
    two = simulate(TOKENS, TOKENS, SimSpec("multi", size=12, count=2), rng(1))
    ok = _rec("m2", "cut", two.text, family="multi", size_bin=24, removed=two.deleted)
    o2 = replay(ok, replace(DEFAULT_THRESHOLDS, min_span_words=10))
    assert o2.localized_intervals == (True, True) and o2.localized


def test_heard_tokens_falls_back_to_text_for_a_dict_without_words():
    assert calibrate._heard_tokens({"text": "Hello, there"}) == ["hello", "there"]
    assert calibrate._heard_tokens({"words": [], "text": "ignored"}) == []
    assert calibrate._heard_tokens({}) == []
    assert calibrate._heard_tokens(
        {"words": [{"word": "a", "start": 0, "end": 1}]}
    ) == ["a"]


def test_evaluate_counts_detection_false_alarms_and_availability_separately():
    off, on = (replace(DEFAULT_THRESHOLDS, net_deficit_min=m) for m in (None, 40))
    r_off, r_on = evaluate(_records(), [off, on])

    cuts = r_off["cuts"]
    assert cuts["overall"] == {
        "n": 3,
        "caught": 1,
        "missed": 1,
        "caught_localized": 1,
        "unavailable": 1,
    }
    assert cuts["missed"] == ["c2"]
    assert cuts["by_split"]["dev"]["caught"] == 1
    assert cuts["by_split"]["holdout"] == {
        "n": 1,
        "caught": 0,
        "missed": 0,
        "unavailable": 1,
        "caught_localized": 0,
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
        "caught_localized": 0,
    }


def test_evaluate_with_no_records_is_all_zero():
    (res,) = evaluate([], [DEFAULT_THRESHOLDS])
    assert res["cuts"]["overall"]["n"] == 0 and res["bases"]["overall"]["bases"] == 0


def _good_records():
    mid = (len(TOKENS) - 40) // 2
    cut = {"family": "mid_fluent", "size_bin": 40, "removed": ((mid, mid + 40),)}
    return [
        _rec("b1:r0", "base", " ".join(TOKENS)),
        _rec("b1:r1", "base", " ".join(TOKENS), repeat=1),
        _rec("c1", "cut", _SIM_PLAIN.text, **cut),
    ]


def _acceptance(records, th=DEFAULT_THRESHOLDS):
    (res,) = evaluate(records, [th])
    return res["acceptance_ok"], res["acceptance"], res


def test_acceptance_needs_every_base_to_pass_every_repeat_and_every_cut_localized():
    ok, detail, res = _acceptance(_good_records())
    assert ok is True and res["cuts"]["overall"]["caught_localized"] == 1
    assert detail == {
        "base_false_alarms": 0,
        "base_unavailable": 0,
        "cuts_not_caught_localized": 0,
        "cut_unavailable": 0,
    }
    # one repeat of one base false-alarms
    bad = [
        *_good_records(),
        _rec("b1:r2", "base", " ".join(TOKENS[100:400]), base_id="b1", repeat=2),
    ]
    ok, detail, _ = _acceptance(bad)
    assert not ok and detail["base_false_alarms"] == 1
    # an unavailable base, or an unavailable cut, is reported on its own and fails
    ok, detail, _ = _acceptance(
        [*_good_records(), _rec("b9", "base", None, unavailable="asr_timeout")]
    )
    assert (
        not ok and detail["base_unavailable"] == 1 and detail["base_false_alarms"] == 0
    )
    cut = {"family": "end", "size_bin": 10, "removed": ((0, 10),)}
    ok, detail, _ = _acceptance(
        [*_good_records(), _rec("c9", "cut", None, unavailable="asr_error", **cut)]
    )
    assert not ok and detail["cut_unavailable"] == 1
    assert detail["cuts_not_caught_localized"] == 1


def test_a_catch_by_recall_alone_is_caught_but_not_localized_and_fails_acceptance():
    # the flagged span is real but far from where the cut was declared
    elsewhere = {"family": "start", "size_bin": 40, "removed": ((0, 40),)}
    recs = [
        _rec("b", "base", " ".join(TOKENS)),
        _rec("c", "cut", _SIM_PLAIN.text, **elsewhere),
    ]
    ok, detail, res = _acceptance(recs)
    assert res["cuts"]["overall"]["caught"] == 1
    assert res["cuts"]["overall"]["caught_localized"] == 0
    assert res["cuts"]["unlocalized"] == ["c"]
    assert not ok and detail["cuts_not_caught_localized"] == 1


def test_acceptance_depends_on_the_thresholds_and_is_never_vacuous():
    blind = _rec(
        "cb",
        "cut",
        _SIM_BLIND.text,
        family="mid_fluent",
        size_bin=40,
        removed=((_SIM_BLIND.deleted[0]),),
    )
    recs = [_rec("b", "base", " ".join(TOKENS)), blind]
    off, on = (replace(DEFAULT_THRESHOLDS, net_deficit_min=m) for m in (None, 40))
    r_off, r_on = evaluate(recs, [off, on])
    assert r_off["acceptance_ok"] is False and r_on["acceptance_ok"] is True
    assert evaluate([], [DEFAULT_THRESHOLDS])[0]["acceptance_ok"] is False
    only_bases = [_rec("b", "base", " ".join(TOKENS))]
    assert evaluate(only_bases, [DEFAULT_THRESHOLDS])[0]["acceptance_ok"] is False


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
# real-shaped timings: abutting words, zero-length words, inflated neighbours
# --------------------------------------------------------------------------- #

REAL = json.loads(
    (Path(__file__).parent / "fixtures" / "whisper_real_timing.json").read_text()
)
REAL_SCRIPT = " ".join(SCRIPT.split()[:195])


@pytest.fixture(scope="module")
def real_base() -> BaseLabel:
    lab = screen(REAL_SCRIPT, REAL, base_id="real")
    assert lab.recall is not None and lab.recall > 0.95
    return replace(lab, label="faithful")  # a trimmed excerpt: faithful for cutting


def _timing_ok(words, k: int) -> bool:
    for j in (k - 1, k, k + 1):
        if 0 <= j < len(words) and words[j].end - words[j].start < 0.05:
            return False
    if k > 0 and words[k].start < words[k - 1].end - 0.001:
        return False
    return not (k + 1 < len(words) and words[k].end > words[k + 1].start + 0.001)


def test_the_fixture_really_has_zero_length_abutting_words(real_base):
    w = real_base.words
    zero = [i for i, x in enumerate(w) if x.end - x.start < 0.05]
    assert len(zero) >= 10
    abutting = sum(1 for a, b in zip(w, w[1:], strict=False) if a.end == b.start)
    assert abutting > 0.8 * len(w)


def test_boundary_eligibility_excludes_zero_length_words_and_their_neighbours(
    real_base,
):
    el = boundary_eligibility(real_base)
    assert len(el) == len(real_base.script_tokens)
    w = real_base.words
    for i, wm in enumerate(real_base.word_map):
        if el[i]:
            assert wm.exact and wm.word_index is not None
            assert _timing_ok(w, wm.word_index)
    # "failed" (word 21, zero-length) and its neighbours "it" and "safety"
    assert [w[k].word for k in (20, 21, 22)] == ["it", "failed", "safety"]
    for i, wm in enumerate(real_base.word_map):
        if wm.word_index in (20, 21, 22):
            assert not el[i]
    assert sum(el) < sum(m.exact for m in real_base.word_map)


def test_overlapping_words_are_ineligible_like_zero_length_ones():
    words = [
        {"word": f"tok{chr(97 + i)}", "start": i * 0.4, "end": i * 0.4 + 0.4}
        for i in range(20)
    ]
    words[10]["start"] -= 0.2  # overlaps word 9
    text = " ".join(w["word"] for w in words)
    lab = screen_base(text, {"words": words}, total_samples=9 * RATE)
    el = boundary_eligibility(lab)
    ineligible = [i for i, ok in enumerate(el) if not ok]
    assert ineligible == [9, 10]  # the overlapping pair; word 11 does not overlap
    assert all(el[i] for i in range(20) if i not in (9, 10))


@pytest.mark.parametrize("family", FAMILIES)
def test_real_timed_cuts_never_use_an_ineligible_boundary(real_base, family):
    size = {"multi": 24, "paragraph": 80, "sentence": 20}.get(family, 10)
    el = boundary_eligibility(real_base)
    found = 0
    for seed in range(40):
        spec = choose_cuts(real_base, family, size, rng(seed))
        if spec is None:
            continue
        found += 1
        _check_invariants(spec, real_base)
        for iv in spec.intervals:
            assert el[iv.script_start] and el[iv.script_end - 1]
            for k in (iv.first_word, iv.last_word):
                assert _timing_ok(real_base.words, k)
    assert found or family in ("paragraph", "end")  # one paragraph / ragged tail


def test_real_timed_finalized_cuts_stay_between_adjacent_word_midpoints(real_base):
    pcm = noisy_pcm(real_base.total_samples)
    for seed in range(30):
        spec = choose_cuts(real_base, "mid_fluent", 10, rng(seed))
        done = finalize_cut(spec, pcm)
        _check_invariants(done, real_base, finalized=True)


# --------------------------------------------------------------------------- #
# energy snapping
# --------------------------------------------------------------------------- #

FRAME = RATE // 100  # 10 ms


def loud_pcm(n_samples: int, silent=()) -> bytes:
    """Constant-amplitude "speech" with silent (zero) stretches at [a, b)."""
    a = array("h", [8000]) * n_samples
    for lo, hi in silent:
        a[lo:hi] = array("h", [0]) * (hi - lo)
    return a.tobytes()


def noisy_pcm(n_samples: int) -> bytes:
    r = random.Random(0)
    return array("h", (r.randint(-9000, 9000) for _ in range(n_samples))).tobytes()


def abutting(n: int, dur: float):
    """n unique abutting words of ``dur`` seconds, and the matching script text."""
    words = [
        {
            "word": f"tok{chr(97 + i // 26)}{chr(97 + i % 26)}",
            "start": i * dur,
            "end": (i + 1) * dur,
        }
        for i in range(n)
    ]
    text = " ".join(w["word"] for w in words)
    total = round((n * dur + 0.5) * RATE)
    return text, {"words": words}, total


def _mid_cut(dur: float):
    text, whisper, total = abutting(40, dur)
    lab = screen_base(text, whisper, total_samples=total, base_id="s")
    assert lab.label == "faithful"
    spec = choose_cuts(lab, "mid_fluent", 6, rng(3))
    assert spec is not None
    return lab, spec


def test_snap_moves_each_cut_point_to_the_quietest_frame_and_records_both():
    lab, spec = _mid_cut(0.3)  # words 300 ms: midpoints +/-150 ms, past the radius
    (iv,) = spec.intervals
    n0, n1 = iv.nominal_start, iv.nominal_end
    silent = [(n0 + 4 * FRAME, n0 + 7 * FRAME), (n1 - 8 * FRAME, n1 - 5 * FRAME)]
    pcm = loud_pcm(lab.total_samples, silent)
    (done_iv,) = finalize_cut(spec, pcm).intervals
    assert done_iv.snapped
    assert (done_iv.nominal_start, done_iv.nominal_end) == (n0, n1)  # recorded
    # a frame centred c spans [c - 5 ms, c + 5 ms): fully silent for 5ms inside
    (a0, b0), (a1, b1) = silent
    assert a0 + FRAME // 2 <= done_iv.sample_start <= b0 - FRAME // 2
    assert a1 + FRAME // 2 <= done_iv.sample_end <= b1 - FRAME // 2
    assert done_iv.sample_start != n0 and done_iv.sample_end != n1
    assert abs(done_iv.sample_start - n0) <= RATE // 10
    assert abs(done_iv.sample_end - n1) <= RATE // 10


def test_snap_is_clamped_to_the_adjacent_word_midpoints():
    lab, spec = _mid_cut(0.12)  # 120 ms words: midpoints only 60 ms away
    (iv,) = spec.intervals
    n0, n1 = iv.nominal_start, iv.nominal_end
    # silence reaches 5 ms past the previous word's midpoint (60 ms before)
    # and 5 ms past the following word's midpoint
    silent = [
        (n0 - 95 * RATE // 1000, n0 - 55 * RATE // 1000),
        (n1 + 55 * RATE // 1000, n1 + 95 * RATE // 1000),
    ]
    (done,) = finalize_cut(spec, loud_pcm(lab.total_samples, silent)).intervals
    assert done.sample_start == done.start_bounds[0] == n0 - 60 * RATE // 1000
    assert done.sample_end == done.end_bounds[1] == n1 + 60 * RATE // 1000
    _check_invariants(
        finalize_cut(spec, loud_pcm(lab.total_samples, silent)), lab, finalized=True
    )


def test_snap_on_flat_audio_keeps_the_nominal_point_and_is_idempotent():
    lab, spec = _mid_cut(0.3)
    pcm = loud_pcm(lab.total_samples)
    done = finalize_cut(spec, pcm)
    assert done.sample_intervals == spec.sample_intervals
    assert all(i.snapped for i in done.intervals)
    assert finalize_cut(done, pcm) == done


def test_snap_records_the_energy_of_each_snapped_frame_relative_to_the_base():
    lab, spec = _mid_cut(0.3)
    (iv,) = spec.intervals
    assert iv.start_energy_ratio is None and iv.end_energy_ratio is None  # nominal
    flat = finalize_cut(spec, loud_pcm(lab.total_samples)).intervals[0]
    assert flat.start_energy_ratio == pytest.approx(1.0)  # no quieter place existed
    assert flat.end_energy_ratio == pytest.approx(1.0)
    n0 = iv.nominal_start
    silent = [(n0 + 4 * FRAME, n0 + 7 * FRAME)]
    quiet = finalize_cut(spec, loud_pcm(lab.total_samples, silent)).intervals[0]
    assert quiet.start_energy_ratio == pytest.approx(0.0, abs=1e-6)  # a true gap
    assert quiet.end_energy_ratio == pytest.approx(1.0, rel=0.01)
    # RMS ratio against the base's global RMS: a base that is half silence is
    # louder-than-average at a speech frame
    half = loud_pcm(lab.total_samples, [(0, lab.total_samples // 2)])
    assert calibrate.global_mean_energy(half) == pytest.approx(0.5 * 8000**2, rel=0.01)
    ratio = finalize_cut(spec, half).intervals[0].end_energy_ratio
    assert ratio == pytest.approx(2**0.5, rel=0.02)
    again = CutSpec.from_dict(
        json.loads(json.dumps(finalize_cut(spec, half).to_dict()))
    )
    assert again == finalize_cut(spec, half)
    assert calibrate.global_mean_energy(b"") == 0.0


def test_snap_rejects_the_wrong_pcm_and_survives_json():
    lab, spec = _mid_cut(0.3)
    pcm = loud_pcm(
        lab.total_samples,
        [
            (
                spec.intervals[0].nominal_start + 2400,
                spec.intervals[0].nominal_start + 3000,
            )
        ],
    )
    with pytest.raises(ValueError):
        finalize_cut(spec, pcm[:-2])
    done = finalize_cut(spec, pcm)
    assert CutSpec.from_dict(json.loads(json.dumps(done.to_dict()))) == done
    remaining, clips = done.apply(pcm)
    assert len(remaining) + sum(map(len, clips)) == len(pcm)


# --------------------------------------------------------------------------- #
# verify_cut_audio: whisper on the CUT audio
# --------------------------------------------------------------------------- #


def words_of(tokens):
    return {
        "words": [
            {"word": t, "start": i * 0.3, "end": (i + 1) * 0.3}
            for i, t in enumerate(tokens)
        ]
    }


def _kept(intervals):
    return [
        t for i, t in enumerate(R_TOKENS) if not any(s <= i < e for s, e in intervals)
    ]


def test_verify_cut_audio_passes_a_clean_cut():
    label = _label([(20, 40)])
    out = verify_cut_audio(label, R_TEXT, words_of(_kept([(20, 40)])))
    assert (
        out["ok"] and out["leftover_tokens"] == 0 and out["extra_missing_tokens"] == 0
    )
    assert out["intervals"][0]["script_start"] == 20
    json.dumps(out)


@pytest.mark.parametrize("leak", [20, 21, 38, 39])
def test_verify_cut_audio_catches_a_leaked_removed_word(leak):
    kept = _kept([(20, 40)])
    heard = kept[:20] + [R_TOKENS[leak]] + kept[20:]
    out = verify_cut_audio(_label([(20, 40)]), R_TEXT, words_of(heard))
    assert (
        not out["ok"]
        and out["leftover_tokens"] == 1
        and out["extra_missing_tokens"] == 0
    )
    assert out["intervals"][0]["leftover_text"] == [R_TOKENS[leak]]


def test_verify_cut_audio_catches_removed_text_heard_elsewhere():
    heard = _kept([(20, 40)]) + R_TOKENS[22:26]
    out = verify_cut_audio(_label([(20, 40)]), R_TEXT, words_of(heard))
    assert not out["ok"] and out["leftover_tokens"] == 4


@pytest.mark.parametrize("gone", [19, 20, 15, 24])  # kept tokens within +/-5 of the cut
def test_verify_cut_audio_catches_a_kept_boundary_word_that_went_missing(gone):
    kept = _kept([(20, 40)])
    heard = kept[:gone] + kept[gone + 1 :]
    out = verify_cut_audio(_label([(20, 40)]), R_TEXT, words_of(heard))
    assert (
        not out["ok"]
        and out["extra_missing_tokens"] == 1
        and out["leftover_tokens"] == 0
    )
    assert out["intervals"][0]["missing_text"] == [kept[gone]]


def test_verify_cut_audio_ignores_noise_outside_the_boundary_window():
    kept = _kept([(20, 40)])
    for gone in (0, 5, 14, 25, 30):  # whisper noise nowhere near the cut
        out = verify_cut_audio(
            _label([(20, 40)]), R_TEXT, words_of(kept[:gone] + kept[gone + 1 :])
        )
        assert out["ok"], gone


def test_verify_cut_audio_does_not_blame_the_cut_for_what_the_base_already_missed():
    label = _label([(20, 40)])
    base = faithful(R_TEXT, base_id="r")
    kept = _kept([(20, 40)])
    heard = kept[:19] + kept[20:]  # whisper loses kept token 19 ...
    assert not verify_cut_audio(label, R_TEXT, words_of(heard))["ok"]
    missed_in_base = replace(
        base,
        word_map=tuple(
            replace(m, word_index=None, start=None, end=None, exact=False)
            if i == 19
            else m
            for i, m in enumerate(base.word_map)
        ),
    )
    # ... but it also lost it in the uncut base, so it is not the cut's fault
    assert verify_cut_audio(label, R_TEXT, words_of(heard), base=missed_in_base)["ok"]
    # a token the base DID hear is still the cut's fault
    other = kept[:18] + kept[19:]
    assert not verify_cut_audio(label, R_TEXT, words_of(other), base=missed_in_base)[
        "ok"
    ]


def test_verify_cut_audio_multi_interval_reports_each_and_dedupes_windows():
    label = _label([(10, 18), (22, 30)])  # windows overlap in expected coordinates
    kept = _kept([(10, 18), (22, 30)])
    ok = verify_cut_audio(label, R_TEXT, words_of(kept))
    assert ok["ok"] and len(ok["intervals"]) == 2
    heard = kept[:14] + [R_TOKENS[29]] + kept[14:]  # leaks the second cut's last word
    bad = verify_cut_audio(label, R_TEXT, words_of(heard))
    assert not bad["ok"]
    assert bad["intervals"][0]["leftover"] + bad["intervals"][1]["leftover"] == 1
    gone = kept[:11] + kept[12:]
    out = verify_cut_audio(label, R_TEXT, words_of(gone))
    assert out["extra_missing_tokens"] == 1  # one token, counted once


def test_verify_cut_audio_accepts_a_bare_word_list_and_text_script_edges():
    label = _label([(0, 10)])  # a cut at the very start of the chunk
    kept = _kept([(0, 10)])
    assert verify_cut_audio(label, R_TEXT, words_of(kept)["words"])["ok"]
    label = _label([(50, 60)])  # ... and at the very end
    assert verify_cut_audio(label, R_TEXT, words_of(_kept([(50, 60)])))["ok"]


# --------------------------------------------------------------------------- #
# reconstruction: audio residue, per-interval levels, repeated phrase
# --------------------------------------------------------------------------- #


def test_reconstruction_subtracts_what_whisper_hears_in_the_cut_audio():
    label = _label([(20, 40)])
    kept = _kept([(20, 40)])
    residue_audio = kept[:20] + R_TOKENS[20:25] + kept[20:]  # 5 tokens leaked
    gemini = " ".join(residue_audio)
    plain = reconstruction(label, R_TEXT, R_TEXT, gemini)
    assert plain["level"] == "partial" and plain["cut_hits"] == 5
    out = reconstruction(
        label, R_TEXT, R_TEXT, gemini, audio_residue=" ".join(residue_audio)
    )
    assert out["cut_hits"] == 0 and out["level"] == "none"
    assert out["raw_cut_hits"] == 5 and out["audio_residue_tokens"] == 5
    # a token list and a whisper dict are accepted too
    assert (
        reconstruction(label, R_TEXT, R_TEXT, gemini, audio_residue=residue_audio)[
            "cut_hits"
        ]
        == 0
    )
    assert (
        reconstruction(
            label, R_TEXT, R_TEXT, gemini, audio_residue=words_of(residue_audio)
        )["cut_hits"]
        == 0
    )
    # residue does not excuse text Gemini produced BEYOND what the audio holds
    more = kept[:20] + R_TOKENS[20:36] + kept[20:]
    out = reconstruction(
        label, R_TEXT, R_TEXT, " ".join(more), audio_residue=" ".join(residue_audio)
    )
    assert (
        out["raw_cut_hits"] == 16
        and out["cut_hits"] == 11
        and out["level"] == "confirmed"
    )
    assert plain["audio_residue_tokens"] is None


def test_reconstruction_levels_are_per_interval_and_overall_is_the_max():
    label = _label([(0, 10), (30, 50)])
    kept = _kept([(0, 10), (30, 50)])
    # 5 of 10 back in the first (>= half: confirmed), 8 of 20 in the second (partial)
    heard = R_TOKENS[0:5] + kept[:20] + R_TOKENS[30:38] + kept[20:]
    out = reconstruction(label, R_TEXT, R_TEXT, " ".join(heard))
    assert [i["level"] for i in out["intervals"]] == ["confirmed", "partial"]
    assert out["level"] == "confirmed"  # 13 of 30 overall would have read "partial"
    assert out["cut_hits"] == 13


def test_reconstruction_paraphrase_without_a_run_reads_as_none():
    label = _label([(20, 40)])
    kept = _kept([(20, 40)])
    # the ASR "reconstructs" by paraphrase: every other removed word reappears,
    # so no block reaches min_run. This is a known blind spot, by design.
    para = [t for i, t in enumerate(R_TOKENS[20:40]) if i % 2 == 0]
    out = reconstruction(label, R_TEXT, R_TEXT, " ".join(kept[:20] + para + kept[20:]))
    assert out["level"] == "none" and out["cut_hits"] == 0


A_, B_, C_ = (
    "aaaa aaab aaac aaad aaae",
    "bbba bbbb bbbc bbbd bbbe",
    "ccca cccb cccc cccd ccce",
)
P_ = "alpha bravo charlie delta"


@pytest.mark.parametrize("which", ["first", "second"])
def test_reconstruction_of_a_repeated_phrase_is_told_from_the_surviving_copy(which):
    # script A P B P C; one copy of P is cut. The other survives in the audio.
    text = f"{A_} {P_} {B_} {P_} {C_}"
    toks = normalize_tokens(text)
    p = len(normalize_tokens(A_))
    first, second = (p, p + 4), (p + 4 + 5, p + 4 + 5 + 4)
    iv = first if which == "first" else second
    label = _label_for(toks, [iv])
    kept = [t for i, t in enumerate(toks) if not iv[0] <= i < iv[1]]
    faithful_cut = " ".join(kept)
    out = reconstruction(label, text, text, faithful_cut)
    assert out["level"] == "none" and out["cut_hits"] == 0
    assert out["clean_hits"] == 4
    # the model "hears" the cut copy back: the text of the full script
    out = reconstruction(label, text, text, text)
    assert out["level"] == "confirmed" and out["cut_hits"] == 4
    assert out["matched_text"] == [P_]


def _label_for(tokens, intervals):
    base = _label(intervals)
    return replace(
        base,
        intervals=tuple(
            replace(iv, removed_text=" ".join(tokens[iv.script_start : iv.script_end]))
            for iv in base.intervals
        ),
    )


# --------------------------------------------------------------------------- #
# sentence structure and number grouping
# --------------------------------------------------------------------------- #


def test_abbreviations_and_initials_do_not_end_sentences():
    text = (
        "Dr. Jones met J. P. Morgan at Acme Inc. in the U.S. on Tuesday. "
        "Then he left! Why? Mr. Smith stayed."
    )
    st = calibrate._structure(text)
    toks = list(st.tokens)
    assert st.sentence_starts == (
        0,
        toks.index("then"),
        toks.index("why"),
        toks.index("mr"),
    )


def test_ordinary_sentence_ends_still_split():
    st = calibrate._structure("He left. She stayed. Then it rained.")
    assert len(st.sentence_starts) == 3


def test_a_dollar_sign_word_then_the_number_then_the_magnitude_group_together():
    script = normalize_tokens("It cost $1.5 billion in total.")
    words = [
        {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.5}
        for i, w in enumerate(["It", "cost", "$", "1.5", "billion", "in", "total"])
    ]
    wm = map_words_to_script(script, words)
    by_token = {m.token: m for m in wm}
    for tok in ("one", "point", "five", "billion", "dollars"):
        assert by_token[tok].word_index == 2 and not by_token[tok].exact
    assert by_token["in"].word_index == 5 and by_token["in"].exact
    assert by_token["total"].word_index == 6 and by_token["total"].exact
    assert all(m.word_index is not None for m in wm)


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


# --- approximate boundary labels ----------------------------------------------


def test_verify_cut_audio_reports_which_removed_tokens_are_still_audible():
    kept = _kept([(20, 40)])
    heard = kept[:20] + [R_TOKENS[20], R_TOKENS[21]] + kept[20:]  # first two leak
    out = verify_cut_audio(_label([(20, 40)]), R_TEXT, words_of(heard))
    assert out["leftover_tokens"] == 2
    assert out["intervals"][0]["leftover_indices"] == [20, 21]
    assert out["leftover_indices"] == [20, 21]
    clean = verify_cut_audio(_label([(20, 40)]), R_TEXT, words_of(kept))
    assert clean["leftover_indices"] == []


def test_approximate_label_gives_a_count_interval_per_interval_and_in_total():
    from pipeline.tts.calibrate import approximate_label

    label = _label([(10, 18), (30, 40)])
    kept = _kept([(10, 18), (30, 40)])
    # whisper hears one removed token of the first cut; and the kept token just
    # before the second cut is missing
    heard = kept[:10] + [R_TOKENS[10]] + kept[10:]
    heard = [t for t in heard if t != R_TOKENS[29]]
    post = verify_cut_audio(label, R_TEXT, words_of(heard))
    assert not post["ok"]
    a = approximate_label(post)
    assert a["status"] == "approx"
    assert a["nominal_tokens"] == 18
    assert (a["lower_tokens"], a["upper_tokens"]) == (17, 19)
    assert a["boundary_error"] == 2
    first, second = a["intervals"]
    assert (first["nominal"], first["lower"], first["upper"]) == (8, 7, 8)
    assert (second["nominal"], second["lower"], second["upper"]) == (10, 10, 11)
    # an exact cut: lower == upper == nominal
    exact = approximate_label(verify_cut_audio(label, R_TEXT, words_of(kept)))
    assert exact["status"] == "exact" and exact["boundary_error"] == 0
    assert (exact["lower_tokens"], exact["upper_tokens"]) == (18, 18)


def test_reconstruction_never_counts_tokens_the_cut_audio_still_contains():
    label = _label([(20, 40)])
    kept = _kept([(20, 40)])
    # the cut was a word late: removed tokens 20-21 are audible. Gemini writes
    # them plus three more it could not have heard: a contiguous run of 5.
    gemini = " ".join(kept[:20] + R_TOKENS[20:25] + kept[20:])
    plain = reconstruction(label, R_TEXT, R_TEXT, gemini)
    assert plain["cut_hits"] == 5
    out = reconstruction(label, R_TEXT, R_TEXT, gemini, residue_indices=[20, 21])
    assert out["cut_hits"] == 3 and out["raw_cut_hits"] == 5
    assert out["audio_residue_tokens"] == 2
    assert out["intervals"][0]["cut_hits"] == 3
    # an empty list changes nothing; and a 2-token leak alone is never "hits"
    assert (
        reconstruction(label, R_TEXT, R_TEXT, gemini, residue_indices=[])["cut_hits"]
        == 5
    )
    leak_only = " ".join(kept[:20] + R_TOKENS[20:22] + kept[20:])
    none = reconstruction(label, R_TEXT, R_TEXT, leak_only, residue_indices=[20, 21])
    assert none["cut_hits"] == 0 and none["level"] == "none"


def test_eval_records_carry_label_status_and_bounds_and_evaluate_bins_by_it():
    mid = (len(TOKENS) - 40) // 2
    base_kw = {"family": "mid_fluent", "size_bin": 40, "removed": ((mid, mid + 40),)}
    exact = _rec("c-exact", "cut", _SIM_PLAIN.text, **base_kw)
    approx = _rec(
        "c-approx", "cut", _SIM_PLAIN.text, label_status="approx",
        lower_tokens=38, upper_tokens=41, **base_kw,
    )  # fmt: skip
    assert exact.label_status == "exact" and exact.lower_tokens is None
    again = EvalRecord.from_dict(json.loads(json.dumps(approx.to_dict())))
    assert again == approx
    (res,) = evaluate(
        [exact, approx, _rec("b", "base", " ".join(TOKENS))], [DEFAULT_THRESHOLDS]
    )
    by = res["cuts"]["by_label_status"]
    assert by["exact"]["caught_localized"] == 1 and by["approx"]["n"] == 1
    assert "by_label_status" not in res["bases"]
