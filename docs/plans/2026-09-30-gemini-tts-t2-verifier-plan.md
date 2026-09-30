# T2 Verifier (large-omission detector + `tts-verify`) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a large-omission detector for rendered TTS audio (ASR the audio, then align the
transcript against the script) plus an offline `python -m pipeline tts-verify` CLI. Nothing is
wired into `render_episode`; T3 does that.

**Bead:** `my-podcasts-9p3.2` (epic `my-podcasts-9p3`). **Design:**
`docs/plans/2026-09-30-gemini-tts-design.md`, "Verification (large-omission detector)".

**Architecture:** Pure core, thin I/O.
- `pipeline/tts/normalize.py` turns script text and ASR transcripts into comparable word tokens,
  using a small, explicit number grammar.
- `pipeline/tts/verify.py` aligns token lists with `difflib` and reports unmatched spans between
  anchors, plus recall. `verify_audio()` is the per-chunk entry point T3 will call.
- `pipeline/tts/asr.py` holds the Gemini transcriber (audio only, SDK retries off, explicit
  timeout).
- `pipeline/tts/segment.py` decodes an mp3 and splits long audio at quiet points, for the CLI only.
- The CLI aligns a whole episode once and *projects* per-chunk recall. That projection is
  diagnostic and not identical to what T3 sees per chunk; the production-equivalent check is T5's.

**Tech stack:** Python 3.14, stdlib `difflib`/`wave`/`array`, `google-genai` 1.65 (already a
dependency), `click`, ffmpeg (already on PATH in the unit).

**Oracle-reviewed decisions (astra, 2026-09-30):**
1. **Spans use anchors; recall uses all matches.** A matching block of at least `anchor_min` (3)
   tokens counts as an anchor and delimits candidate spans, so a stray "the" matched inside a
   skipped passage cannot split a 40-word omission into two sub-threshold halves. Recall counts
   *all* matched tokens.
2. **Span test:** flag when the span has at least `min_span_words` script tokens *and*
   `transcript_words <= max_span_ratio * script_words`. Record the net deficit too, for T5.
3. **"Unavailable" needs explicit evidence**, never a heuristic: an exception or timeout, no
   candidates, a finish reason other than STOP (or none), an empty normalized transcript, or any
   CLI segment failing. There is **no** words-per-second gate and **no** transcript-vs-script
   length gate, because a short transcript is exactly what an omission looks like. Silent ASR
   truncation that still reports STOP cannot be told apart and reads as "suspected omission". This
   is a documented limitation; T3 re-renders once and then falls back, so it stays safe.
4. **The number grammar is bounded and explicit.** Unsupported forms stay as digits, visible
   rather than guessed. Dotted initialisms are collapsed *before* lowercasing. Negative signs are
   kept.
5. **Thresholds** are a frozen, validated `VerifyThresholds` that callers pass in and reports
   echo back. `VERIFIER_VERSION` is bumped on any change to normalization, alignment or default
   thresholds, and T3 folds it into the render cache key.
6. **The saved transcript is replayable evidence:** audio sha256, model, prompt version, and per
   segment its bounds, finish reason and raw text. A plain `.txt` transcript is accepted but
   marked `external`.
7. **Cut from scope:** Needleman-Wunsch, general English number parsing, word timestamps, and
   wiring into render.

**Conventions:**
- Every test runs offline. Add the autouse guard `_block_real_gemini_asr` in Task 3.
- Run single test files with `uv run pytest <path> -q`. The full suite takes about 8 minutes, so
  use a 900000 ms timeout for it. Run `uv run ruff check . && uv run ruff format --check .`
  before each commit.
- Commit with bare `git commit` (the configured identity). Use messages like
  `feat(tts): ...` / `test(tts): ...`.
- The fixture `pipeline/tts/fixtures/rundown_2026-09-30_chunks01.txt` holds chunks 0 and 1 of the
  real 2026-09-30 Rundown script, joined by a blank line (869 words). It is our own writer's
  output; do not add third-party newsletter text as fixtures.

---

### Task 1: `normalize.py`, a number grammar and tokenizer

**Files:**
- Create: `pipeline/tts/normalize.py`
- Test: `pipeline/tts/test_normalize.py`

**Step 1: Write the failing tests** (`pipeline/tts/test_normalize.py`)

```python
import pytest

from pipeline.tts.normalize import cardinal, normalize_tokens, ordinal, year_words


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (0, "zero"),
        (7, "seven"),
        (19, "nineteen"),
        (20, "twenty"),
        (42, "forty two"),
        (100, "one hundred"),
        (580, "five hundred eighty"),
        (1000, "one thousand"),
        (1_800_000, "one million eight hundred thousand"),
        (12_000_000_000, "twelve billion"),
    ],
)
def test_cardinal(n, words):
    assert cardinal(n) == words


def test_cardinal_rejects_out_of_range():
    with pytest.raises(ValueError):
        cardinal(10**15)


@pytest.mark.parametrize(
    ("n", "words"),
    [(1, "first"), (2, "second"), (3, "third"), (5, "fifth"), (8, "eighth"),
     (9, "ninth"), (12, "twelfth"), (20, "twentieth"), (30, "thirtieth"),
     (21, "twenty first"), (100, "one hundredth")],
)
def test_ordinal(n, words):
    assert ordinal(n) == words


@pytest.mark.parametrize(
    ("n", "words"),
    [(2026, "twenty twenty six"), (2010, "twenty ten"), (2000, "two thousand"),
     (2005, "two thousand five"), (1984, "nineteen eighty four"),
     (1905, "nineteen oh five"), (1900, "nineteen hundred")],
)
def test_year_words(n, words):
    assert year_words(n) == words


def toks(s: str) -> str:
    return " ".join(normalize_tokens(s))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Currency: magnitude moves before "dollars"; cents; singular.
        ("$580", "five hundred eighty dollars"),
        ("$750 million", "seven hundred fifty million dollars"),
        ("$1.8 billion", "one point eight billion dollars"),
        ("$2.45", "two dollars and forty five cents"),
        ("$1", "one dollar"),
        ("$1,000", "one thousand dollars"),
        # Percent, decimals, grouped integers.
        ("7%", "seven percent"),
        ("5.5 %", "five point five percent"),
        ("2.45", "two point four five"),
        ("1,800,000 people", "one million eight hundred thousand people"),
        # Years vs quantities.
        ("in 2030.", "in twenty thirty"),
        ("2026", "twenty twenty six"),
        ("2030 million", "two thousand thirty million"),
        ("12345", "twelve thousand three hundred forty five"),
        # Ordinals and decades.
        ("September 30th", "september thirtieth"),
        ("the 1990s", "the nineteen nineties"),
        ("the 90s", "the nineties"),
        # Times.
        ("4:30 AM", "four thirty am"),
        ("9:05", "nine oh five"),
        ("10:00", "ten"),
        # Negative sign is kept; a hyphen inside a word is not a sign.
        ("fell -3%", "fell minus three percent"),
        ("GPT-6.1 Astra", "gpt six point one astra"),
        # Spelled-out and digit forms converge.
        ("twenty twenty-six", "twenty twenty six"),
    ],
)
def test_numbers(text, expected):
    assert toks(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The U.S. and A.I.", "the us and ai"),
        ("AT&T", "at and t"),
        ("it’s OpenAI’s", "its openais"),
        ("“quoted” — dash–dash", "quoted dash dash"),
        ("didn't", "didnt"),
        ("", ""),
        ("... !!", ""),
    ],
)
def test_text_normalization(text, expected):
    assert toks(text) == expected


def test_out_of_range_integer_stays_digits():
    assert toks("1234567890123456789") == "1234567890123456789"
```

**Step 2: Run it and confirm it fails**

`uv run pytest pipeline/tts/test_normalize.py -q`. Expect an ImportError.

**Step 3: Implement** `pipeline/tts/normalize.py`

```python
"""Normalize script text and ASR transcripts into comparable word tokens.

Changing output here changes verifier policy: bump ``verify.VERIFIER_VERSION``.

Both sides pass through the same function. Numbers are expanded to their usual
spoken English reading by the bounded grammar below. That matters because the
ASR writes digits ("2026", "30th", "$580") while our writers often spell them
out ("twenty twenty-six", "thirtieth"), and number formatting was the dominant
source of alignment noise in the 2026-09-28 evaluation.

Supported, in the order applied:
  times            4:30 -> four thirty, 9:05 -> nine oh five, 10:00 -> ten
  currency         $580, $1,000, $2.45 (dollars and cents), $1.8 billion
  percent          7%, 5.5 %
  ordinals         1st, 30th, 21st
  decades          1990s, 90s
  signed numbers   -3 -> minus three (only when "-" follows start/space/"(")
  numbers          grouped (1,800,000) or plain integers, decimals (2.45);
                   a bare 4-digit 1100-2099 not followed by a magnitude word is
                   read as a year (2026 -> twenty twenty six)
Anything else numeric (above 999 trillion, version strings like 3.5.1) is left
as digits: visible noise, never a guess.
"""

from __future__ import annotations

import re
import unicodedata


_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = (
    (10**12, "trillion"),
    (10**9, "billion"),
    (10**6, "million"),
    (10**3, "thousand"),
)
MAX_CARDINAL = 10**15 - 1
_ORDINAL_IRREGULAR = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_MAGNITUDE = r"(?:thousand|million|billion|trillion)"
_INT = r"(?:\d{1,3}(?:,\d{3})+|\d+)"


def cardinal(n: int) -> str:
    if n < 0:
        return "minus " + cardinal(-n)
    if n > MAX_CARDINAL:
        raise ValueError(f"{n} is outside the supported range")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else " " + _ONES[ones])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ONES[hundreds] + " hundred" + ("" if rest == 0 else " " + cardinal(rest))
    for value, name in _SCALES:
        if n >= value:
            head, rest = divmod(n, value)
            return cardinal(head) + " " + name + ("" if rest == 0 else " " + cardinal(rest))
    raise AssertionError("unreachable")


def _ordinalize_word(word: str) -> str:
    if word in _ORDINAL_IRREGULAR:
        return _ORDINAL_IRREGULAR[word]
    if word.endswith("y"):
        return word[:-1] + "ieth"
    return word + "th"


def ordinal(n: int) -> str:
    *head, last = cardinal(n).split()
    return " ".join([*head, _ordinalize_word(last)])


def _pluralize_last(words: str) -> str:
    *head, last = words.split()
    last = last[:-1] + "ies" if last.endswith("y") else last + "s"
    return " ".join([*head, last])


def year_words(n: int) -> str:
    if not 1100 <= n <= 2099:
        raise ValueError(f"{n} is not a supported year")
    if 2000 <= n <= 2009:
        return "two thousand" + ("" if n == 2000 else " " + _ONES[n - 2000])
    high, low = divmod(n, 100)
    if low == 0:
        return cardinal(high) + " hundred"
    if low < 10:
        return cardinal(high) + " oh " + _ONES[low]
    return cardinal(high) + " " + cardinal(low)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _number(int_part: str, frac: str | None) -> str:
    words = cardinal(_int(int_part))
    if frac:
        words += " point " + " ".join(_ONES[int(d)] for d in frac)
    return words


def _safe(fn):
    """Apply ``fn`` to a match; on an unsupported value keep the original text."""

    def sub(m: re.Match) -> str:
        try:
            return " " + fn(m) + " "
        except ValueError:
            return m.group(0)

    return sub


def _time(m: re.Match) -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute == 0:
        return cardinal(hour)
    if minute < 10:
        return cardinal(hour) + " oh " + _ONES[minute]
    return cardinal(hour) + " " + cardinal(minute)


def _currency(m: re.Match) -> str:
    sign, int_part, frac, magnitude = m.group(1), m.group(2), m.group(3), m.group(4)
    prefix = "minus " if sign else ""
    if magnitude:
        return prefix + _number(int_part, frac) + " " + magnitude.lower() + " dollars"
    dollars = _int(int_part)
    unit = "dollar" if dollars == 1 else "dollars"
    if frac and len(frac) == 2:
        cents = int(frac)
        words = cardinal(dollars) + " " + unit
        if cents:
            words += " and " + cardinal(cents) + (" cent" if cents == 1 else " cents")
        return prefix + words
    if frac:
        return prefix + _number(int_part, frac) + " dollars"
    return prefix + cardinal(dollars) + " " + unit


def _percent(m: re.Match) -> str:
    prefix = "minus " if m.group(1) else ""
    return prefix + _number(m.group(2), m.group(3)) + " percent"


def _ordinal(m: re.Match) -> str:
    return ordinal(_int(m.group(1)))


def _decade(m: re.Match) -> str:
    digits = m.group(1)
    n = int(digits)
    if len(digits) == 4:
        return _pluralize_last(year_words(n))
    return _pluralize_last(cardinal(n))


def _plain(m: re.Match) -> str:
    sign, int_part, frac, following = m.group(1), m.group(2), m.group(3), m.group(4)
    prefix = "minus " if sign else ""
    is_year = (
        not sign
        and frac is None
        and "," not in int_part
        and len(int_part) == 4
        and 1100 <= int(int_part) <= 2099
        and not following
    )
    if is_year:
        return year_words(int(int_part))
    return prefix + _number(int_part, frac)


_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_SIGN = r"(?:(?<=^)|(?<=[\s(]))"
_CURRENCY_RE = re.compile(
    rf"{_SIGN}(-)?\$\s?({_INT})(?:\.(\d+))?(?:\s?({_MAGNITUDE})\b)?", re.IGNORECASE
)
_CURRENCY_NOSIGN_RE = re.compile(
    rf"()\$\s?({_INT})(?:\.(\d+))?(?:\s?({_MAGNITUDE})\b)?", re.IGNORECASE
)
_PERCENT_RE = re.compile(rf"(?:{_SIGN}(-))?(?<![\d.])({_INT})(?:\.(\d+))?\s?%")
_ORDINAL_RE = re.compile(rf"(?<![\d.])({_INT})(?:st|nd|rd|th)\b", re.IGNORECASE)
_DECADE_RE = re.compile(r"(?<![\d.])(\d0|\d{3}0)s\b")
_PLAIN_RE = re.compile(
    rf"(?:{_SIGN}(-))?(?<!\d)(?<!\d\.)({_INT})(?:\.(\d+))?(?!\d)(?=(\s+{_MAGNITUDE}\b)?)",
    re.IGNORECASE,
)
_INITIALISM_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}")
_INNER_APOSTROPHE_RE = re.compile(r"(?<=\w)'(?=\w)")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(
        {0x2018: "'", 0x2019: "'", 0x02BC: "'", 0x201C: '"', 0x201D: '"',
         0x2013: " ", 0x2014: " ", 0x2212: "-"}
    )
    text = _INITIALISM_RE.sub(lambda m: m.group(0).replace(".", ""), text)
    text = text.replace("&", " and ")
    text = _INNER_APOSTROPHE_RE.sub("", text)
    text = _TIME_RE.sub(_safe(_time), text)
    text = _CURRENCY_RE.sub(_safe(_currency), text)
    text = _CURRENCY_NOSIGN_RE.sub(_safe(_currency), text)
    text = _PERCENT_RE.sub(_safe(_percent), text)
    text = _ORDINAL_RE.sub(_safe(_ordinal), text)
    text = _DECADE_RE.sub(_safe(_decade), text)
    text = _PLAIN_RE.sub(_safe(_plain), text)
    return text.lower()


def normalize_tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_text(text))
```

The regexes are a starting point. **The tests are the spec.** If a regex fails a test, fix the
regex, not the test, and report every deviation. Two known traps:
- Python lookbehinds must be fixed-width. `(?<=^)` is legal but zero-width; if it misbehaves,
  replace the `_SIGN` alternation with an explicit `(?:^|(?<=[\s(]))`.
- `_CURRENCY_NOSIGN_RE` exists so that `"x$5"` (a dollar sign not preceded by space/start) still
  converts. Drop it if the signed regex already covers every test.

**Step 4: Run the tests** and confirm they pass: `uv run pytest pipeline/tts/test_normalize.py -q`.

**Step 5: Commit** with the message `feat(tts): token normalizer with a bounded number grammar`.

---

### Task 2: `verify.py`, the pure analysis (alignment, spans, recall, projection)

**Files:**
- Create: `pipeline/tts/verify.py`
- Test: `pipeline/tts/test_verify.py`

**Step 1: Write the failing tests** (`pipeline/tts/test_verify.py`)

```python
import re
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.tts.verify import (
    DEFAULT_THRESHOLDS,
    VerifyThresholds,
    analyze,
    project_chunks,
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
    for start in (700, 550, 400, 250, 100):  # 5 cuts x 10 words = ~6% of tokens
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
    [{"anchor_min": 0}, {"min_span_words": 0}, {"max_span_ratio": -0.1},
     {"max_span_ratio": 1.5}, {"recall_floor": 1.1}, {"recall_floor": -0.1}],
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
    assert per_chunk[1].script_tokens + per_chunk[0].script_tokens == whole.script_tokens


def test_analysis_serializes():
    a = analyze(SCRIPT, as_asr(SCRIPT))
    d = a.to_dict()
    assert d["status"] == "pass" and isinstance(d["spans"], list)
```

**Step 2: Run it and confirm it fails.**

**Step 3: Implement** `pipeline/tts/verify.py`, analysis part only (Task 4 adds `verify_audio`):

```python
"""Large-omission detector: align an ASR transcript against the script.

Changing normalization, alignment, or DEFAULT_THRESHOLDS changes verifier
policy: bump VERIFIER_VERSION (T3 folds it into the render cache key).

Claimed scope: catches LARGE omissions. It does not detect changed numbers,
negations, repetitions or added speech. See the design doc, "Verification".

Alignment: difflib matching blocks over normalized tokens. Blocks of at least
``anchor_min`` tokens are anchors; the script-side gaps between consecutive
anchors (and before the first / after the last) are candidate spans. A span is
flagged when it is long (>= min_span_words script tokens) and the transcript
side is much shorter (<= max_span_ratio of it). Recall counts ALL matched
script tokens, not only anchored ones. Coordinates are indices into the
normalized token lists, not characters or seconds.
"""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass
from typing import Literal

from pipeline.tts.normalize import normalize_tokens


VERIFIER_VERSION = "1"
_EXCERPT_TOKENS = 30


@dataclass(frozen=True)
class VerifyThresholds:
    """Placeholders until T5 calibrates them on real audio-level cuts."""

    anchor_min: int = 3
    min_span_words: int = 12
    max_span_ratio: float = 0.5
    recall_floor: float = 0.85

    def __post_init__(self) -> None:
        if self.anchor_min < 1:
            raise ValueError(f"anchor_min must be >= 1, got {self.anchor_min}")
        if self.min_span_words < 1:
            raise ValueError(f"min_span_words must be >= 1, got {self.min_span_words}")
        if not 0.0 <= self.max_span_ratio <= 1.0:
            raise ValueError(f"max_span_ratio must be in [0, 1], got {self.max_span_ratio}")
        if not 0.0 <= self.recall_floor <= 1.0:
            raise ValueError(f"recall_floor must be in [0, 1], got {self.recall_floor}")


DEFAULT_THRESHOLDS = VerifyThresholds()

Status = Literal["pass", "omission"]


@dataclass(frozen=True)
class Span:
    """A script-side gap between anchors. ``flagged`` marks a suspected omission."""

    script_start: int
    script_end: int
    transcript_start: int
    transcript_end: int
    script_words: int
    transcript_words: int
    net_missing: int
    flagged: bool
    excerpt: str  # the first <=30 normalized script tokens of the span


@dataclass(frozen=True)
class Analysis:
    status: Status
    reasons: tuple[str, ...]  # "ok" | "empty_script" | "long_unmatched_span" | "recall_below_floor"
    recall: float | None  # None only when the script has no tokens
    script_tokens: int
    transcript_tokens: int
    matched_tokens: int
    longest_span_words: int  # max script_words over all candidate spans; 0 if none
    spans: tuple[Span, ...]  # every candidate gap with >= 1 script token, in order
    thresholds: VerifyThresholds

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChunkProjection:
    """Per-chunk view of a whole-episode alignment. Diagnostic: T3 aligns each
    chunk in isolation against its own ASR, which can differ."""

    index: int
    chars: int
    script_tokens: int
    matched_tokens: int
    recall: float | None
    flagged_spans: int
    status: Status


def _align(
    script: list[str], transcript: list[str], th: VerifyThresholds
) -> tuple[list[bool], list[Span]]:
    sm = difflib.SequenceMatcher(a=script, b=transcript, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    matched = [False] * len(script)
    for b in blocks:
        for i in range(b.a, b.a + b.size):
            matched[i] = True
    anchors = [(b.a, b.b, b.size) for b in blocks if b.size >= th.anchor_min]
    anchors.append((len(script), len(transcript), 0))  # sentinel: closes the tail gap
    spans: list[Span] = []
    prev_a = prev_b = 0
    for a, b, size in anchors:
        if a > prev_a:
            s_words, t_words = a - prev_a, max(0, b - prev_b)
            spans.append(
                Span(
                    script_start=prev_a,
                    script_end=a,
                    transcript_start=prev_b,
                    transcript_end=prev_b + t_words,
                    script_words=s_words,
                    transcript_words=t_words,
                    net_missing=s_words - t_words,
                    flagged=(
                        s_words >= th.min_span_words
                        and t_words <= th.max_span_ratio * s_words
                    ),
                    excerpt=" ".join(script[prev_a : min(a, prev_a + _EXCERPT_TOKENS)]),
                )
            )
        prev_a, prev_b = a + size, b + size
    return matched, spans


def _analysis(
    script: list[str],
    transcript: list[str],
    matched: list[bool],
    spans: list[Span],
    th: VerifyThresholds,
) -> Analysis:
    if not script:
        return Analysis("pass", ("empty_script",), None, 0, len(transcript), 0, 0, (), th)
    n_matched = sum(matched)
    recall = n_matched / len(script)
    reasons = []
    if any(s.flagged for s in spans):
        reasons.append("long_unmatched_span")
    if recall < th.recall_floor:
        reasons.append("recall_below_floor")
    return Analysis(
        status="omission" if reasons else "pass",
        reasons=tuple(reasons) or ("ok",),
        recall=recall,
        script_tokens=len(script),
        transcript_tokens=len(transcript),
        matched_tokens=n_matched,
        longest_span_words=max((s.script_words for s in spans), default=0),
        spans=tuple(spans),
        thresholds=th,
    )


def analyze(
    script_text: str, transcript_text: str, thresholds: VerifyThresholds = DEFAULT_THRESHOLDS
) -> Analysis:
    script = normalize_tokens(script_text)
    transcript = normalize_tokens(transcript_text)
    matched, spans = _align(script, transcript, thresholds)
    return _analysis(script, transcript, matched, spans, thresholds)


def project_chunks(
    chunks: list[str],
    transcript_text: str,
    thresholds: VerifyThresholds = DEFAULT_THRESHOLDS,
) -> tuple[Analysis, list[ChunkProjection]]:
    """Align a whole episode once; report it whole and projected per chunk.

    Token ranges come from normalizing each chunk separately and concatenating,
    so chunk boundaries are exact in token space. The whole-episode status is
    an omission if the whole analysis is one OR any chunk's projection is.
    """
    per_chunk = [normalize_tokens(c) for c in chunks]
    script = [t for toks in per_chunk for t in toks]
    transcript = normalize_tokens(transcript_text)
    matched, spans = _align(script, transcript, thresholds)
    whole = _analysis(script, transcript, matched, spans, thresholds)

    projections: list[ChunkProjection] = []
    start = 0
    for i, (chunk, toks) in enumerate(zip(chunks, per_chunk, strict=True)):
        end = start + len(toks)
        n = sum(matched[start:end])
        flagged = sum(
            1 for s in spans if s.flagged and s.script_start < end and s.script_end > start
        )
        recall = n / len(toks) if toks else None
        bad = flagged > 0 or (recall is not None and recall < thresholds.recall_floor)
        projections.append(
            ChunkProjection(
                index=i,
                chars=len(chunk),
                script_tokens=len(toks),
                matched_tokens=n,
                recall=recall,
                flagged_spans=flagged,
                status="omission" if bad else "pass",
            )
        )
        start = end
    if whole.status == "pass" and any(p.status == "omission" for p in projections):
        whole = _replace_status(whole, "recall_below_floor")
    return whole, projections


def _replace_status(a: Analysis, reason: str) -> Analysis:
    from dataclasses import replace

    return replace(a, status="omission", reasons=(reason,))
```

Implementation notes for the implementer:
- The design says "between alignment anchors, whether the aligner calls it delete or replace".
  The gap construction above covers both.
- Lift `replace` to a top-level import. It is inline above only to keep the snippet short.
- If `test_several_short_cuts_trip_the_recall_floor` flags a span as well, because two of the
  cuts end up adjacent, move the cut offsets further apart. The test's intent is recall *without*
  a long span.

**Step 4: Run the tests** and confirm they pass.

**Step 5: Commit** with the message `feat(tts): large-omission analysis (anchored spans + recall)`.

---

### Task 3: `asr.py`, the Gemini transcriber, `pcm_to_wav`, and the conftest guard

**Files:**
- Create: `pipeline/tts/asr.py`
- Modify: `pipeline/conftest.py` (add `_block_real_gemini_asr` next to `_block_real_openai_tts`)
- Test: `pipeline/tts/test_asr.py`

**Step 1: Write the failing tests** (`pipeline/tts/test_asr.py`)

```python
import io
import wave
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from google.genai import types

from pipeline.tts import asr
from pipeline.tts.asr import (
    ASR_MODEL,
    ASR_PROMPT,
    GeminiTranscriber,
    TranscriptionUnavailable,
    pcm_to_wav,
)


def response(text="hello world", finish="STOP", candidates=True):
    cands = []
    if candidates:
        parts = [types.Part(text=text)] if text is not None else []
        cands = [
            types.Candidate(
                finish_reason=getattr(types.FinishReason, finish) if finish else None,
                content=types.Content(parts=parts),
            )
        ]
    return types.GenerateContentResponse(
        candidates=cands,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=5
        ),
    )


class FakeModels:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result):
        self.models = FakeModels(result)
        self.closed = False

    def close(self):
        self.closed = True


def transcriber_with(result):
    client = FakeClient(result)
    t = GeminiTranscriber(timeout_s=30)
    patcher = patch.object(asr, "_make_genai_client", lambda timeout_s: client)
    return t, client, patcher


def test_success_returns_text_and_metadata():
    t, client, p = transcriber_with(response("hello world"))
    with p:
        out = t(b"RIFF...", "audio/wav")
    assert out.text == "hello world"
    assert out.finish_reason == "STOP"
    assert out.model == ASR_MODEL
    assert out.input_tokens == 100 and out.output_tokens == 5
    call = client.models.calls[0]
    assert call["model"] == ASR_MODEL
    # Audio only: exactly one audio part plus the fixed instruction, nothing else.
    contents = call["contents"]
    assert len(contents) == 2 and contents[1] == ASR_PROMPT
    assert contents[0].inline_data.mime_type == "audio/wav"
    assert call["config"].temperature == 0


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (response(finish="MAX_TOKENS"), "asr_incomplete"),
        (response(finish="SAFETY"), "asr_incomplete"),
        (response(finish="OTHER", text=None), "asr_incomplete"),
        (response(finish=None), "asr_incomplete"),
        (response(text="   "), "asr_empty"),
        (response(candidates=False), "asr_empty"),
        (RuntimeError("boom"), "asr_error"),
        (httpx.ReadTimeout("slow"), "asr_timeout"),
    ],
)
def test_unavailable_cases(result, reason):
    t, _, p = transcriber_with(result)
    with p, pytest.raises(TranscriptionUnavailable) as exc_info:
        t(b"x", "audio/wav")
    assert exc_info.value.reason == reason


def test_client_is_lazy_reused_and_closed():
    made = []

    def make(timeout_s):
        made.append(timeout_s)
        return FakeClient(response())

    t = GeminiTranscriber(timeout_s=42)
    with patch.object(asr, "_make_genai_client", make):
        assert made == []
        t(b"x", "audio/wav")
        t(b"x", "audio/wav")
        client = t._client
        t.close()
    assert made == [42]
    assert client.closed


def test_real_client_has_sdk_retries_off_and_timeout(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    with patch.object(asr.genai, "Client", fake_client):
        asr._make_genai_client_unguarded(timeout_s=90)
    opts = captured["http_options"]
    assert opts.timeout == 90_000
    assert opts.retry_options.attempts == 1
    assert captured["api_key"] == "k"


def test_guard_blocks_real_client_in_tests():
    with pytest.raises(AssertionError, match="real Gemini"):
        asr._make_genai_client(timeout_s=1)


def test_pcm_to_wav_roundtrip():
    pcm = b"\x01\x00\xff\x7f" * 100
    with wave.open(io.BytesIO(pcm_to_wav(pcm))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24_000)
        assert w.readframes(w.getnframes()) == pcm


def test_pcm_to_wav_rejects_odd_length():
    with pytest.raises(ValueError):
        pcm_to_wav(b"\x00\x00\x00")
```

**Step 2: Run it and confirm it fails.**

**Step 3: Implement** `pipeline/tts/asr.py`

```python
"""Transcribe rendered audio with Gemini, for the omission detector.

The model gets AUDIO ONLY, never the script: a transcriber that can see the
script can "hear" what it expects. Callers align afterwards (verify.py).

The SDK's own retries are OFF (``HttpRetryOptions(attempts=1)``) and the
timeout is explicit; the caller owns retry policy and, in T3, the hard
deadline (a child process). ``timeout`` is per HTTP request, not a wall clock.
"""

from __future__ import annotations

import io
import os
import time
import wave
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import types

from pipeline.tts.config import PCM_SAMPLE_RATE


ASR_MODEL = "gemini-3.8-flash"
ASR_PROMPT_VERSION = "1"
ASR_PROMPT = (
    "Transcribe the speech in this audio verbatim. Output only the spoken words "
    "as plain text: no timestamps, no speaker labels, no headings, no commentary. "
    "Write numbers as digits."
)
DEFAULT_ASR_TIMEOUT_SECONDS = 90.0


class TranscriptionUnavailable(Exception):
    """Explicit evidence the transcript cannot be trusted: never a pass.

    ``reason`` is one of ``asr_error``, ``asr_timeout``, ``asr_empty``,
    ``asr_incomplete`` (finish reason other than STOP, or none).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


@dataclass(frozen=True)
class Transcription:
    text: str
    model: str
    prompt_version: str
    finish_reason: str
    elapsed_s: float
    input_tokens: int | None
    output_tokens: int | None


def _make_genai_client_unguarded(*, timeout_s: float) -> genai.Client:
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(
            timeout=int(timeout_s * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def _make_genai_client(timeout_s: float) -> genai.Client:
    return _make_genai_client_unguarded(timeout_s=timeout_s)


class GeminiTranscriber:
    """Callable ``(audio_bytes, mime_type) -> Transcription``. Client is lazy."""

    def __init__(
        self, *, model: str = ASR_MODEL, timeout_s: float = DEFAULT_ASR_TIMEOUT_SECONDS
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._client: genai.Client | None = None

    def __call__(self, audio: bytes, mime_type: str) -> Transcription:
        if self._client is None:
            self._client = _make_genai_client(self.timeout_s)
        started = time.monotonic()
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_bytes(data=audio, mime_type=mime_type), ASR_PROMPT],
                config=types.GenerateContentConfig(temperature=0),
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise TranscriptionUnavailable("asr_timeout", repr(exc)) from exc
        except Exception as exc:  # any SDK/transport failure = no transcript
            raise TranscriptionUnavailable("asr_error", repr(exc)) from exc
        elapsed = time.monotonic() - started

        if not resp.candidates:
            raise TranscriptionUnavailable("asr_empty", "no candidates")
        finish = resp.candidates[0].finish_reason
        finish_name = finish.name if finish is not None else "NONE"
        if finish_name != "STOP":
            raise TranscriptionUnavailable("asr_incomplete", f"finish_reason={finish_name}")
        text = resp.text or ""
        if not text.strip():
            raise TranscriptionUnavailable("asr_empty", "blank transcript")
        usage = resp.usage_metadata
        return Transcription(
            text=text,
            model=self.model,
            prompt_version=ASR_PROMPT_VERSION,
            finish_reason=finish_name,
            elapsed_s=elapsed,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def pcm_to_wav(pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE) -> bytes:
    """Wrap 16-bit mono PCM (what providers return) in a WAV container."""
    if len(pcm) % 2:
        raise ValueError(f"PCM length {len(pcm)} is not a whole number of 16-bit samples")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()
```

Add this to `pipeline/conftest.py`, directly after `_block_real_openai_tts` and in the same
style:

```python
@pytest.fixture(autouse=True)
def _block_real_gemini_asr(request):
    """No test may build a real Gemini client for TTS verification (costs money).

    Tests patch ``pipeline.tts.asr._make_genai_client`` themselves; their patch
    nests inside this one and wins.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    def _refuse(timeout_s: float):
        raise AssertionError(
            "A test tried to build a real Gemini ASR client. Patch "
            "pipeline.tts.asr._make_genai_client or pass a fake transcriber."
        )

    with patch("pipeline.tts.asr._make_genai_client", _refuse):
        yield
```

`test_guard_blocks_real_client_in_tests` checks for "real Gemini", so keep that phrase in the
message.

**Step 4: Run** `uv run pytest pipeline/tts/test_asr.py -q` and confirm it passes.

**Step 5: Commit** with the message `feat(tts): Gemini ASR transcriber (audio only, SDK retries off) + test guard`.

---

### Task 4: `verify_audio`, the per-chunk entry point T3 will call

**Files:**
- Modify: `pipeline/tts/verify.py`
- Test: `pipeline/tts/test_verify.py` (append)

**Step 1: Append the failing tests**

```python
from pipeline.tts.asr import Transcription, TranscriptionUnavailable
from pipeline.tts.verify import VERIFIER_VERSION, verify_audio


def fake_transcriber(text=None, exc=None):
    calls = []

    def t(audio, mime_type):
        calls.append((audio, mime_type))
        if exc:
            raise exc
        return Transcription(text, "gemini-3.8-flash", "1", "STOP", 1.5, 10, 20)

    return t, calls


def test_verify_audio_pass_records_asr_and_policy():
    t, calls = fake_transcriber(as_asr(SCRIPT))
    v = verify_audio(b"WAV", "audio/wav", SCRIPT, transcriber=t)
    assert v.status == "pass"
    assert v.recall is not None and v.recall >= 0.99
    assert v.verifier_version == VERIFIER_VERSION
    assert v.mode == "chunk"
    assert v.asr.model == "gemini-3.8-flash" and v.asr.finish_reason == "STOP"
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
```

**Step 2: Run them and confirm they fail.**

**Step 3: Implement.** Append to `pipeline/tts/verify.py`, and move the imports to the top of
the module:

```python
from collections.abc import Callable

from pipeline.tts.asr import Transcription, TranscriptionUnavailable

Transcriber = Callable[[bytes, str], Transcription]


@dataclass(frozen=True)
class AsrInfo:
    model: str
    prompt_version: str
    finish_reason: str
    elapsed_s: float
    input_tokens: int | None
    output_tokens: int | None
    transcript_chars: int


@dataclass(frozen=True)
class Verdict:
    status: Literal["pass", "omission", "unavailable"]
    reasons: tuple[str, ...]
    detail: str  # human-readable; "" when nothing to add
    analysis: Analysis | None
    asr: AsrInfo | None
    thresholds: VerifyThresholds
    verifier_version: str
    mode: Literal["chunk"]

    @property
    def recall(self) -> float | None:
        return self.analysis.recall if self.analysis else None

    def to_dict(self) -> dict:
        return asdict(self)


def _unavailable(reason: str, detail: str, th: VerifyThresholds, asr: AsrInfo | None) -> Verdict:
    return Verdict("unavailable", (reason,), detail, None, asr, th, VERIFIER_VERSION, "chunk")


def verify_audio(
    audio: bytes,
    mime_type: str,
    script_text: str,
    *,
    transcriber: Transcriber,
    thresholds: VerifyThresholds = DEFAULT_THRESHOLDS,
) -> Verdict:
    """Verify one rendered chunk. The transcriber never sees ``script_text``.

    Only ``TranscriptionUnavailable`` becomes "unavailable"; any other
    exception from the transcriber is a bug and propagates.
    """
    try:
        tr = transcriber(audio, mime_type)
    except TranscriptionUnavailable as exc:
        return _unavailable(exc.reason, str(exc), thresholds, None)
    info = AsrInfo(
        model=tr.model,
        prompt_version=tr.prompt_version,
        finish_reason=tr.finish_reason,
        elapsed_s=tr.elapsed_s,
        input_tokens=tr.input_tokens,
        output_tokens=tr.output_tokens,
        transcript_chars=len(tr.text),
    )
    if not normalize_tokens(tr.text):
        return _unavailable("asr_empty", "transcript has no word tokens", thresholds, info)
    a = analyze(script_text, tr.text, thresholds)
    return Verdict(a.status, a.reasons, "", a, info, thresholds, VERIFIER_VERSION, "chunk")
```

`verify.py` now imports `asr.py`. `asr.py` must not import `verify.py`, so the dependency runs in
one direction.

**Step 4: Run** `uv run pytest pipeline/tts/ -q` and confirm everything passes.

**Step 5: Commit** with the message `feat(tts): verify_audio per-chunk verdict (unavailable is never a pass)`.

---

### Task 5: `segment.py`, decoding the mp3 and splitting it at quiet points (CLI only)

**Files:**
- Create: `pipeline/tts/segment.py`
- Test: `pipeline/tts/test_segment.py`

**Step 1: Write the failing tests**

```python
import subprocess
from array import array
from unittest.mock import patch

import pytest

from pipeline.tts import segment
from pipeline.tts.segment import DECODE_TIMEOUT_SECONDS, decode_to_pcm, split_pcm

RATE = 24_000


def tone(seconds: float, amp: int = 8000) -> bytes:
    n = int(seconds * RATE)
    return array("h", [amp if i % 2 else -amp for i in range(n)]).tobytes()


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * RATE) * 2)


def test_short_audio_is_one_segment():
    pcm = tone(100)
    assert split_pcm(pcm) == [(0, len(pcm))]


def test_long_audio_cuts_in_the_quiet_gap_near_target():
    # loud 0-293 s, silent 293-294 s, loud to 700 s
    pcm = tone(293) + silence(1) + tone(406)
    segs = split_pcm(pcm, target_s=300, search_s=15)
    assert segs[0][0] == 0 and segs[-1][1] == len(pcm)
    for (_, end), (start, _) in zip(segs, segs[1:]):
        assert end == start  # contiguous, no overlap, nothing dropped
    first_cut_s = segs[0][1] / (RATE * 2)
    assert 293 <= first_cut_s <= 294
    assert all(a % 2 == 0 and b % 2 == 0 for a, b in segs)


def test_no_quiet_point_still_cuts_within_window():
    pcm = tone(650)
    segs = split_pcm(pcm, target_s=300, search_s=15)
    assert len(segs) == 3
    assert 285 <= segs[0][1] / (RATE * 2) <= 315


def test_split_rejects_bad_args():
    with pytest.raises(ValueError):
        split_pcm(tone(1), target_s=10, search_s=10)


def test_decode_invokes_bounded_ffmpeg(tmp_path):
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    with patch.object(segment.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"\x00\x00", stderr=b"")
        assert decode_to_pcm(mp3) == b"\x00\x00"
    args, kwargs = run.call_args
    cmd = args[0]
    assert cmd[0] == "ffmpeg" and str(mp3) in cmd
    assert cmd[cmd.index("-ar") + 1] == "24000" and cmd[cmd.index("-ac") + 1] == "1"
    assert kwargs["timeout"] == DECODE_TIMEOUT_SECONDS


def test_decode_failure_raises(tmp_path):
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    with patch.object(segment.subprocess, "run") as run:
        run.side_effect = subprocess.CalledProcessError(1, "ffmpeg", stderr=b"bad")
        with pytest.raises(RuntimeError, match="bad"):
            decode_to_pcm(mp3)
```

**Step 2: Run them and confirm they fail.**

**Step 3: Implement** `pipeline/tts/segment.py`

```python
"""Decode an episode mp3 and split long audio for transcription (tts-verify only).

Whole episodes run 12-23 minutes; ASR runs on ~5-minute pieces. Cuts land on
the quietest 100 ms window within +-search_s of each target, so a word is
rarely split. Segments are contiguous and non-overlapping: nothing dropped,
nothing duplicated. A word cut at a boundary is a documented limitation.
"""

from __future__ import annotations

import subprocess
from array import array
from pathlib import Path

from pipeline.tts.config import PCM_SAMPLE_RATE


DECODE_TIMEOUT_SECONDS = 300
_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2
_WINDOW_S = 0.1


def decode_to_pcm(path: Path) -> bytes:
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-f", "s16le", "-ac", "1", "-ar", str(PCM_SAMPLE_RATE), "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd, check=True, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS
        )
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode(errors="replace")[-2000:]
        raise RuntimeError(f"ffmpeg decode of {path} failed: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg decode of {path} timed out") from exc
    return proc.stdout


def _quietest_cut(pcm: bytes, lo_s: float, hi_s: float) -> int:
    samples = array("h")
    lo = int(lo_s * PCM_SAMPLE_RATE)
    hi = int(hi_s * PCM_SAMPLE_RATE)
    samples.frombytes(pcm[lo * 2 : hi * 2])
    win = int(_WINDOW_S * PCM_SAMPLE_RATE)
    best_i, best_energy = 0, None
    for i in range(0, max(1, len(samples) - win + 1), win):
        energy = sum(abs(s) for s in samples[i : i + win])
        if best_energy is None or energy < best_energy:
            best_i, best_energy = i, energy
    return (lo + best_i + win // 2) * 2


def split_pcm(
    pcm: bytes, *, target_s: float = 300.0, search_s: float = 15.0
) -> list[tuple[int, int]]:
    """Return contiguous ``(start, end)`` byte ranges covering ``pcm``."""
    if search_s <= 0 or search_s >= target_s:
        raise ValueError("need 0 < search_s < target_s")
    total_s = len(pcm) / _BYTES_PER_SECOND
    segments: list[tuple[int, int]] = []
    start = 0
    while (total_s - start / _BYTES_PER_SECOND) > target_s + search_s:
        base = start / _BYTES_PER_SECOND + target_s
        cut = _quietest_cut(pcm, base - search_s, base + search_s)
        segments.append((start, cut))
        start = cut
    segments.append((start, len(pcm)))
    return segments
```

For 30 s of candidate window, `sum(abs(...))` over 720k samples takes well under a second in
CPython. If the test run is slow (above ~5 s), step the windows by `win` over a pre-summed prefix
array instead.

**Step 4: Run the tests** and confirm they pass. **Step 5: Commit** with the message
`feat(tts): mp3 decode + quiet-point splitting for tts-verify`.

---

### Task 6: the `tts-verify` CLI

**Files:**
- Modify: `pipeline/__main__.py` (new `@cli.command("tts-verify")`, placed after `run-stats`)
- Test: `pipeline/test_tts_verify_cli.py`

**Behavior (this is the spec):**
- `--audio PATH` (mp3 or wav; required) and `--script PATH` (the exact TTS input text; required).
- `--transcript PATH` reuses a transcript and makes no ASR call:
  - A `.json` file must be one written by `--save-transcript`. If its `audio_sha256` differs from
    the `--audio` file, fail with a `click.UsageError` (exit 2 from click, with a message). This
    stops calibration from ever running against the wrong transcript.
  - Any other extension is read as plain text, with `asr.source = "external"`.
- `--save-transcript PATH` writes JSON after a *complete* ASR run:
  `{"audio_sha256", "model", "prompt_version", "segments": [{"start_s", "end_s", "finish_reason", "elapsed_s", "input_tokens", "output_tokens", "text"}]}`.
  Writing it together with `--transcript` is a usage error.
- `--json PATH` writes the report there; otherwise the report JSON goes to stdout. A one-line
  human summary always goes to stderr.
- `--timeout` (seconds per ASR request, default 120), plus `--anchor-min`, `--min-span-words`,
  `--max-span-ratio` and `--recall-floor`. Each defaults to `DEFAULT_THRESHOLDS`; an invalid
  value is a `click.BadParameter`.
- Without `--transcript`: `decode_to_pcm`, then `split_pcm`, then for each segment
  `GeminiTranscriber(timeout_s=...)` on `pcm_to_wav(segment)` with `"audio/wav"`. The transcriber
  is closed in `finally`. **If any segment raises `TranscriptionUnavailable`, the whole report is
  `unavailable`**, with `reasons=[exc.reason]` and a detail naming the segment index. Never
  concatenate only the segments that succeeded.
- The transcript text is `"\n".join(segment texts)`. Then `chunks = chunk_text(script)` and
  `whole, per_chunk = project_chunks(chunks, transcript, thresholds)`.
- Report JSON:
  `{"verifier_version", "mode": "projected", "status", "reasons", "detail", "thresholds", "audio_sha256", "script_sha256", "asr": {"source": "asr"|"saved"|"external", "model", "prompt_version", "segments": [... without text ...]}, "analysis": whole.to_dict() or null, "chunks": [asdict(p) ...]}`.
  `mode: "projected"` is required, because these per-chunk numbers are diagnostic and not what T3
  computes.
- Exit code: 0 pass, 1 omission, 3 unavailable. Use 3, not 2, because click already uses 2 for
  usage errors.
- The command's help text must say that per-chunk numbers are projected from a whole-episode
  alignment, and that production-equivalent checks align each synthesized chunk on its own (T5).

**Step 1: Write the failing tests** (`pipeline/test_tts_verify_cli.py`). Use `CliRunner` from
`click.testing` and `from pipeline.__main__ import cli`. Copy the `as_asr`/`drop_words` helpers
from `test_verify.py`; `SCRIPT` is the fixture file. Cover:

1. `--transcript clean.txt`: exit 0, `status == "pass"`, `mode == "projected"`,
   `asr.source == "external"`, and `chunks` non-empty with each `recall >= 0.99`. Patch
   `pipeline.tts.asr._make_genai_client` to raise if called, proving the command made no ASR
   call.
2. `--transcript skipped.txt` (40 words dropped): exit 1 and `status == "omission"`.
3. The ASR path with `decode_to_pcm` patched (`pipeline.tts.segment.decode_to_pcm`, patched where
   `__main__` looks it up) to return 700 s of `bytes`, and `GeminiTranscriber` patched with a
   fake class. The fake's `__call__` returns the next of three prepared `Transcription`s (the
   script's thirds, run through `as_asr`) and records the mime type; its `close()` records that
   it was called. With `--save-transcript out.json`: exit 0; `out.json` has 3 segments whose
   `text` concatenates to the transcript and whose `audio_sha256` matches the file; `close()`
   was called; every mime was `audio/wav`.
4. The same, but the fake raises `TranscriptionUnavailable("asr_incomplete", ...)` on segment 2:
   exit 3, `status == "unavailable"`, `reasons == ["asr_incomplete"]`, and `--save-transcript`
   was NOT written.
5. Replay: feed test 3's `out.json` back via `--transcript out.json` for the same audio: exit 0,
   `asr.source == "saved"`. With a different audio file: nonzero exit and a message mentioning
   `audio_sha256`.
6. `--recall-floor 1.5`: nonzero exit (a usage error).
7. `--transcript x.txt --save-transcript y.json`: a usage error.

**Step 2: Run** `uv run pytest pipeline/test_tts_verify_cli.py -q` and confirm it fails.

**Step 3: Implement** the command in `pipeline/__main__.py`. Keep the heavy imports inside the
function, as the other commands do (`from pipeline.tts import asr, segment, verify`, `hashlib`,
`json`). Structure:

```python
@cli.command("tts-verify")
@click.option("--audio", "audio_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--script", "script_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--transcript", "transcript_path", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Reuse a transcript instead of calling ASR (.json from --save-transcript, or plain text).")
@click.option("--save-transcript", "save_transcript", default=None,
              type=click.Path(dir_okay=False, path_type=Path))
@click.option("--json", "json_out", default=None, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--timeout", "timeout_s", default=120.0, show_default=True, type=float,
              help="Seconds per ASR request.")
@click.option("--anchor-min", default=None, type=int)
@click.option("--min-span-words", default=None, type=int)
@click.option("--max-span-ratio", default=None, type=float)
@click.option("--recall-floor", default=None, type=float)
def tts_verify_command(...) -> None:
    """Check rendered audio for large omissions against its script (offline).

    Transcribes the audio with Gemini (audio only) in ~5-minute pieces, aligns
    the transcript against the whole script once, and reports spans plus
    per-chunk recall PROJECTED from that whole-episode alignment. Projected
    numbers are diagnostic; production-equivalent checks verify each
    synthesized chunk on its own (see T5 / my-podcasts-9p3.5).

    Exit status: 0 pass, 1 omission, 3 verification unavailable.
    """
```

Build `VerifyThresholds` with `dataclasses.replace(DEFAULT_THRESHOLDS, **{k: v for k, v in overrides.items() if v is not None})`
inside `try`, and turn a `ValueError` into a `click.BadParameter`. Hash with
`hashlib.sha256(path.read_bytes()).hexdigest()`. Write the `--json` report with `Path.write_text`
(no atomicity needed for an operator tool). Finish with `raise SystemExit(code)` only when the
code is nonzero, as the other commands do.

**Step 4: Run** the new test file, then all of `pipeline/tts/`, and confirm both pass.

**Step 5: Commit** with the message `feat(cli): tts-verify (offline large-omission check with replayable transcripts)`.

---

### Task 7: docs

**Files:**
- Modify: `pipeline/AGENTS.md`. Under the "TTS Renderer" section, add a short "Verifier (T2)"
  subsection covering:
  - what it catches and what it doesn't (the claimed scope)
  - the modules
  - the `tts-verify` usage example:
    `uv run python -m pipeline tts-verify --audio X.mp3 --script S.txt --save-transcript T.json --json R.json`,
    then re-tune with `--transcript T.json --min-span-words ...`
  - the exit codes
  - that thresholds are placeholders until T5 (`my-podcasts-9p3.5`)
  - that per-chunk numbers are projected
  - that ASR costs money (Gemini Flash audio input), and that the tests are guarded by
    `_block_real_gemini_asr`
  - that `VERIFIER_VERSION` bumps on any policy change
- Modify: `AGENTS.md`, the "Core Paths" TTS renderer bullet. Append:
  `verifier: pipeline/tts/verify.py (+ normalize.py, asr.py; CLI tts-verify)`.

Commit with the message `docs: tts verifier and tts-verify`.

---

### Task 8 (controller, not a subagent): real-audio smoke test

Paid, a few cents. Keep every artifact under `/persist/my-podcasts/tts-eval/t2/`, never in
`/tmp`.

1. **Clean control:**
   `uv run python -m pipeline tts-verify --audio /persist/my-podcasts/tts-eval/pub-levine.mp3 --script /persist/my-podcasts/tts-eval/in-levine.txt --save-transcript .../t2/pub-levine.transcript.json --json .../t2/pub-levine.report.json`.
   Expect `pass`. Record the recall, the longest span, and the minimum chunk recall. Do the same
   for `pub-rundown` and `pub-fp`.
2. **Audio-level cut:** remove about 15 s of speech from the middle of `pub-rundown.mp3` with
   ffmpeg (`-filter_complex "[0]atrim=0:300,asetpts=PTS-STARTPTS[a];[0]atrim=start=315,asetpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=0:a=1"`;
   the `aselect` form silently produced an uncut file, so always confirm the duration dropped with
   ffprobe), then run `tts-verify` on the cut file. Expect `omission`, with the flagged span in the chunk covering 300 s.
3. If a clean control fails, **do not tune thresholds to pass it.** Inspect the flagged span,
   decide whether it is a normalization gap or a real ASR miss, fix normalization only when it is
   a genuine systematic mismatch, and record the finding for T5.
4. Record the results in a `bd note` on `my-podcasts-9p3.2` and in the PR body.
