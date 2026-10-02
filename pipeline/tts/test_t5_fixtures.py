"""Regression fixtures from the T5 calibration data (real audio, real ASR).

Each fixture in ``fixtures/t5/`` is a script, the Gemini transcript of real audio
under ASR policy ``thinking-low``, and the label that audio was given. These
tests pin that the calibrated defaults (``DEFAULT_THRESHOLDS``) still flag
every real cut and every natural omission and still pass faithful renders. They
are evidence, not mocks: a change to normalization, alignment or the thresholds
that breaks one of them needs a new look at the calibration (and a
``VERIFIER_VERSION`` bump), not a fixture edit.

The fixtures were captured as verifier v2 evidence. Verifier v3 differs from v2
only on chunks shorter than ``recall_min_tokens`` (300) tokens, and every T5
chunk had 306 or more, so v3 keeps every T5 verdict (pinned below).

The repo is public: full-chunk fixtures use our own generated Rundown/FP
scripts; the Levine (Bloomberg) fixtures are trimmed windows of at most 150
words either side of the skipped span.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.tts import calibrate as cal
from pipeline.tts.normalize import normalize_tokens
from pipeline.tts.verify import DEFAULT_THRESHOLDS, VERIFIER_VERSION, analyze


FIXTURES = Path(__file__).parent / "fixtures" / "t5"
ASR_POLICY_AT_CAPTURE = "gemini-3.8-flash|prompt-v1|temp0|thinking-low"
MAX_FIXTURE_BYTES = 10_000
LEVINE_WORDS_EACH_SIDE = 150


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


CUT_FILES = sorted(p.name for p in FIXTURES.glob("cut-*.json"))
FAITHFUL_FILES = sorted(p.name for p in FIXTURES.glob("faithful-*.json"))
LEVINE_FILES = sorted(p.name for p in FIXTURES.glob("levine-*.json"))


def flagged_spans(script: str, transcript: str):
    a = analyze(script, transcript, DEFAULT_THRESHOLDS)
    return a, [s for s in a.spans if s.flagged]


def overlaps(span, start: int, end: int) -> bool:
    return span.script_start < end and span.script_end > start


def test_the_fixture_set_is_exactly_what_was_curated():
    assert sorted(p.name for p in FIXTURES.iterdir() if p.suffix == ".json") == [
        "cut-end.json",
        "cut-mid_fluent.json",
        "cut-multi.json",
        "cut-paragraph.json",
        "cut-predictable.json",
        "cut-sentence.json",
        "cut-start.json",
        "faithful-flash.json",
        "faithful-lite.json",
        "levine-charon-2.json",
        "levine-kore-0.json",
    ]


def test_fixtures_are_small_and_v3_keeps_every_t5_verdict():
    for p in FIXTURES.glob("*.json"):
        assert p.stat().st_size <= MAX_FIXTURE_BYTES, p.name
    # Captured as v2 evidence; v3 and v4 change no verdict here. v4 (currency,
    # my-podcasts-9p3.15) moved cut-multi's token intervals down by one: its
    # script spells out "dollars", which v4 drops on both sides.
    assert VERIFIER_VERSION == "4"


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.json")))
def test_v3_and_v2_agree_on_every_fixture(name):
    fx = load(name)
    v3 = analyze(fx["script"], fx["transcript"], DEFAULT_THRESHOLDS)
    v2 = analyze(
        fx["script"],
        fx["transcript"],
        replace(DEFAULT_THRESHOLDS, recall_min_tokens=1),
    )
    assert (v3.status, v3.reasons) == (v2.status, v2.reasons)


@pytest.mark.parametrize("name", [*CUT_FILES, *FAITHFUL_FILES, *LEVINE_FILES])
def test_every_fixture_records_its_provenance(name):
    fx = load(name)
    prov = fx["provenance"]
    assert prov["source_root"] == "/persist/my-podcasts/tts-eval/t5"
    assert prov["asr_policy"] == ASR_POLICY_AT_CAPTURE
    assert re.fullmatch(r"[0-9a-f]{64}", prov["audio_sha256"])
    assert prov["audio"] and prov["transcript"]
    if "chunk_sha256" in prov:
        assert re.fullmatch(r"[0-9a-f]{64}", prov["chunk_sha256"])


# --- 1. one real dev cut per family -------------------------------------------


def test_there_is_one_cut_fixture_per_family():
    families = [load(n)["cut"]["family"] for n in CUT_FILES]
    assert sorted(families) == sorted(cal.FAMILIES)


@pytest.mark.parametrize("name", CUT_FILES)
def test_defaults_flag_a_span_over_every_removed_interval_of_a_real_cut(name):
    fx = load(name)
    cut, script = fx["cut"], fx["script"]
    a, flagged = flagged_spans(script, fx["transcript"])
    assert a.status == "omission", name
    assert "long_unmatched_span" in a.reasons
    assert cut["intervals"]
    for iv in cut["intervals"]:
        assert any(
            overlaps(s, iv["script_start"], iv["script_end"]) for s in flagged
        ), (
            name,
            iv,
        )


@pytest.mark.parametrize("name", CUT_FILES)
def test_cut_fixture_labels_are_consistent_with_the_script(name):
    fx = load(name)
    cut, script = fx["cut"], fx["script"]
    toks = normalize_tokens(script)
    assert cut["split"] == "dev"
    assert cut["cut_id"].split("-")[0] in ("rundown", "fp")  # our own text only
    assert cut["feed"] in ("rundown", "fp")
    for iv in cut["intervals"]:
        s, e = iv["script_start"], iv["script_end"]
        assert 0 <= s < e <= len(toks) and iv["tokens"] == e - s
        # the literal removed text is the script's own words for that interval
        assert normalize_tokens(iv["removed_text"]) == toks[s:e]
        assert iv["removed_text"] in script
    label = cut["label"]
    nominal = sum(iv["tokens"] for iv in cut["intervals"])
    assert label["nominal_tokens"] == nominal
    assert label["lower_tokens"] <= nominal <= label["upper_tokens"]
    assert cut["label_status"] in ("exact", "approx")
    if cut["label_status"] == "exact":
        assert label["boundary_error"] == 0
        assert label["lower_tokens"] == label["upper_tokens"] == nominal
    else:
        assert 0 < label["boundary_error"] <= 3
    if cut["family"] == "multi":
        assert len(cut["intervals"]) == 3
    else:
        assert len(cut["intervals"]) == 1


@pytest.mark.parametrize("name", CUT_FILES)
def test_the_transcript_really_lacks_the_removed_words(name):
    fx = load(name)
    toks = normalize_tokens(fx["script"])
    heard = normalize_tokens(fx["transcript"])
    removed = sum(iv["tokens"] for iv in fx["cut"]["intervals"])
    assert len(heard) <= len(toks) - 0.5 * removed  # most of the cut is missing


def test_cuts_cover_the_small_end_and_the_hard_families():
    sizes = {load(n)["cut"]["family"]: load(n)["cut"]["size_bin"] for n in CUT_FILES}
    assert min(sizes.values()) == 10
    assert sizes["multi"] == 24 and sizes["paragraph"] == 80
    statuses = {load(n)["cut"]["label_status"] for n in CUT_FILES}
    assert statuses == {"exact", "approx"}  # both label kinds are represented


# --- 2. faithful dev bases ----------------------------------------------------


def test_there_is_a_faithful_flash_and_a_faithful_lite_base():
    models = {load(n)["base"]["model"] for n in FAITHFUL_FILES}
    feeds = {load(n)["base"]["feed"] for n in FAITHFUL_FILES}
    assert models == {"flash", "lite"} and feeds <= {"rundown", "fp"}


@pytest.mark.parametrize("name", FAITHFUL_FILES)
def test_defaults_pass_a_faithful_real_render(name):
    fx = load(name)
    assert fx["base"]["split"] == "dev" and fx["base"]["whisper_label"] == "faithful"
    a, flagged = flagged_spans(fx["script"], fx["transcript"])
    assert a.status == "pass" and a.reasons == ("ok",)
    assert not flagged
    assert a.recall is not None and a.recall >= 0.97  # evidence: min dev recall 0.973
    assert max((s.net_missing for s in a.spans), default=0) <= 2  # dev maximum
    assert a.recall == pytest.approx(fx["observed"]["recall"])
    # real faithful audio sits well inside the thresholds
    assert max(s.net_missing for s in a.spans) < DEFAULT_THRESHOLDS.net_deficit_min
    assert a.recall > DEFAULT_THRESHOLDS.recall_floor


# --- 3. natural omissions (Levine), trimmed windows ----------------------------


def test_the_two_levine_reproductions_are_the_ones_the_plan_names():
    assert LEVINE_FILES == ["levine-charon-2.json", "levine-kore-0.json"]
    spans = {n: load(n)["span"]["tokens"] for n in LEVINE_FILES}
    assert spans == {"levine-charon-2.json": 24, "levine-kore-0.json": 57}


@pytest.mark.parametrize("name", LEVINE_FILES)
def test_defaults_flag_the_natural_omission(name):
    fx = load(name)
    span = fx["span"]
    a, flagged = flagged_spans(fx["script"], fx["transcript"])
    assert a.status == "omission" and "long_unmatched_span" in a.reasons
    hit = [s for s in flagged if overlaps(s, span["script_start"], span["script_end"])]
    assert hit, (name, [(s.script_start, s.script_end) for s in flagged])
    # the flagged span is the whole skipped passage, not a stray fragment
    assert max(s.net_missing for s in hit) >= span["tokens"] - 3
    # and it is the only flagged span: nothing else in the window is a false alarm
    assert flagged == hit


@pytest.mark.parametrize("name", LEVINE_FILES)
def test_levine_fixtures_are_trimmed_windows_not_whole_chunks(name):
    fx = load(name)
    win, span = fx["window"], fx["span"]
    assert win["words_before_span"] <= LEVINE_WORDS_EACH_SIDE
    assert win["words_after_span"] <= LEVINE_WORDS_EACH_SIDE
    toks = normalize_tokens(fx["script"])
    assert len(toks) == win["chunk_token_end"] - win["chunk_token_start"]
    assert len(toks) < 480  # the chunk had 480 tokens
    span_words = len(" ".join(toks[span["script_start"] : span["script_end"]]).split())
    assert len(fx["script"].split()) <= 2 * LEVINE_WORDS_EACH_SIDE + span_words + 5
    a, b = win["span_in_chunk_tokens"]
    assert (a - win["chunk_token_start"], b - win["chunk_token_start"]) == (
        span["script_start"],
        span["script_end"],
    )
    assert "trimmed" in fx["provenance"]["note"].lower()
    assert fx["provenance"]["script_sha256"] and fx["provenance"]["chunk_sha256"]


@pytest.mark.parametrize("name", LEVINE_FILES)
def test_the_independent_asr_agreed_on_the_levine_span(name):
    fx = load(name)
    a, b = fx["window"]["span_in_chunk_tokens"]
    whisper = fx["confirmed_by"]["whisper_suspect_spans_in_chunk_tokens"]
    assert any(s < b and e > a and miss >= (b - a) - 3 for s, e, miss in whisper)


def test_levine_text_is_never_stored_whole():
    # the only Levine text in the repo is these two windows
    for name in LEVINE_FILES:
        assert load(name)["provenance"]["chunk_index"] == 0
        assert len(load(name)["script"]) < 2000  # the chunk was 2831 chars
