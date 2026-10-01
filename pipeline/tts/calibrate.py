"""Pure core of the T5 omission-detector calibration (no network, no CLI).

Design: ``docs/plans/2026-09-30-gemini-tts-t5-calibration-plan.md``. This module
holds the deterministic, offline pieces; the paid collection steps (Task 3) are
built on top of it. Imports only the stdlib and ``pipeline.tts.*``.

The idea in one paragraph. A *base* is a Gemini render of one script chunk. An
independent ASR (OpenAI whisper-1, word timestamps) screens it (``screen_base``).
A faithful base is cut *by construction*: ``choose_cuts`` picks a script-token
interval whose boundary tokens whisper matched exactly, and ``cut_pcm`` removes
the matching audio, so the label is the audio intervention, never the verifier's
opinion of it. ``evaluate`` replays ``verify.analyze`` over saved transcripts for
a grid of thresholds; ``simulate`` does the same job in text space for free;
``reconstruction`` asks whether an ASR "heard" removed text that is not there.

Everything is deterministic given a seeded ``random.Random``. Token indices are
indices into ``normalize_tokens(script_text)`` (end exclusive); sample indices
are 24 kHz mono 16-bit samples (end exclusive).
"""

from __future__ import annotations

import bisect
import difflib
import io
import math
import operator
import random
import re
import struct
import wave
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from typing import Any, Literal

from pipeline.tts.asr import pcm_to_wav
from pipeline.tts.config import PCM_SAMPLE_RATE
from pipeline.tts.normalize import normalize_tokens
from pipeline.tts.verify import (
    DEFAULT_THRESHOLDS,
    VerifyThresholds,
    _align,
    analyze,
)


FAMILIES = (
    "start",
    "end",
    "mid_fluent",
    "sentence",
    "paragraph",
    "predictable",
    "multi",
)
SIZE_BINS = (10, 20, 40, 80)
# ``multi`` takes its size as the TOTAL removed, split over MULTI_PARTS cuts
# (24 -> 3 x 8, the plan's "3 separated cuts of ~8 tokens each").
MULTI_TOTAL = 24
MULTI_PARTS = 3
MULTI_MIN_GAP_TOKENS = 20

# Decision 2 (base labeling).
FAITHFUL_NET_MISSING = 6  # a whisper span missing this many tokens -> suspect
FAITHFUL_RECALL = 0.95
CLIP_PAD_S = 3.0  # owner clips: the suspect span +/- this many seconds

# A script token is "exact" only inside a matching block at least this long, so
# one stray coincidental "the" can never become a cut boundary.
EXACT_MIN_BLOCK = 3
# ``start``/``end`` cuts must begin/finish within this many tokens of the chunk edge.
EDGE_TOKENS = 6
# ``sentence``/``paragraph`` windows must land within +/- this share of the size.
SIZE_TOLERANCE = 0.4
# A repeated 3-gram only counts as "predictable" with at least this many letters.
MIN_GRAM_CHARS = 12
# Real whisper-1 word timestamps are not boundaries: ~90% of inter-word gaps are
# exactly 0, and ~4% of words are zero-length with the previous word inflated
# over them. A token may bound a cut only when its word and both neighbours last
# at least this long and nothing overlaps.
MIN_BOUNDARY_WORD_S = 0.05
OVERLAP_TOLERANCE_S = 0.001
# Each cut point moves to the lowest-energy 10 ms frame within this radius of the
# nominal point, but never past the midpoint of either adjacent word.
SNAP_RADIUS_S = 0.1
SNAP_FRAME_S = 0.01
# verify_cut_audio looks at this many tokens either side of each cut.
BOUNDARY_WINDOW = 5
# whisper may report words slightly past the end of the PCM; more means the wrong audio.
MAX_WHISPER_OVERRUN_S = 1.0
# A cut may remove at most this share of the chunk: a "paragraph" cut of a chunk
# that is one paragraph would otherwise delete the whole audio.
MAX_REMOVED_FRACTION = 0.5
# decision 3: removed-clip whisper token count must be within 25 percent.
REMOVED_SANITY_TOLERANCE = 0.25
RECON_MIN_RUN = 3


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# PCM / WAV
# --------------------------------------------------------------------------- #


def wav_bytes(pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE) -> bytes:
    """16-bit mono PCM in a WAV container (``asr.pcm_to_wav``)."""
    return pcm_to_wav(pcm, sample_rate)


def read_wav(data: bytes) -> tuple[bytes, int]:
    """``(pcm, sample_rate)`` of a mono 16-bit WAV; anything else is an error."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(
                f"expected mono 16-bit WAV, got {w.getnchannels()} channel(s) "
                f"x {8 * w.getsampwidth()} bit"
            )
        return w.readframes(w.getnframes()), w.getframerate()


def cut_pcm(
    pcm: bytes, intervals: Sequence[tuple[int, int]]
) -> tuple[bytes, list[bytes]]:
    """Remove sample ``intervals`` from 16-bit mono ``pcm``.

    Intervals are ``[start, end)`` in SAMPLES, non-empty, in range, sorted and
    non-overlapping (touching is fine). Returns ``(remaining, removed_clips)``
    with the clips in interval order. Slicing is whole-sample, so the result is
    always 16-bit aligned; the byte arithmetic is checked, not assumed.
    """
    if len(pcm) % 2:
        raise ValueError(f"PCM length {len(pcm)} is not whole 16-bit samples")
    total = len(pcm) // 2
    prev_end = 0
    clean: list[tuple[int, int]] = []
    for raw_start, raw_end in intervals:
        start, end = operator.index(raw_start), operator.index(raw_end)
        if not 0 <= start < end <= total:
            raise ValueError(
                f"interval ({start}, {end}) is empty or outside 0..{total} samples"
            )
        if start < prev_end:
            raise ValueError(
                f"interval ({start}, {end}) overlaps or precedes the previous one "
                f"(ends at {prev_end}); intervals must be sorted and disjoint"
            )
        prev_end = end
        clean.append((start, end))
    remaining = bytearray()
    removed: list[bytes] = []
    pos = 0
    for start, end in clean:
        remaining += pcm[2 * pos : 2 * start]
        removed.append(pcm[2 * start : 2 * end])
        pos = end
    remaining += pcm[2 * pos :]
    expected = 2 * (total - sum(e - s for s, e in clean))
    if len(remaining) != expected or len(remaining) + sum(map(len, removed)) != len(
        pcm
    ):
        raise RuntimeError("cut_pcm byte arithmetic does not add up")  # not -O safe
    return bytes(remaining), removed


# --------------------------------------------------------------------------- #
# Word <-> token mapping
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Word:
    """One whisper word: ``{"word": "Hey", "start": 0.0, "end": 0.4}``."""

    word: str
    start: float
    end: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Word:
        return cls(str(d["word"]), float(d["start"]), float(d["end"]))


def parse_words(whisper: dict | Sequence[dict]) -> list[Word]:
    """Words from a whisper ``verbose_json`` dict (or from the bare words list)."""
    raw = whisper["words"] if isinstance(whisper, dict) else whisper
    return [w if isinstance(w, Word) else Word.from_dict(w) for w in raw]


@dataclass(frozen=True)
class WordMap:
    """Where one script token landed in the whisper transcript.

    ``word_index``/``start``/``end`` are the matched whisper word (or, for a
    token that came out of a multi-word group such as ``"$1.5" "billion"``, the
    group's first word and its whole time span). ``exact`` means: matched inside
    a block of >= EXACT_MIN_BLOCK tokens AND produced by exactly one whisper word
    that normalizes to exactly one token. Only exact tokens may bound a cut.
    """

    token_index: int
    token: str
    word_index: int | None
    start: float | None
    end: float | None
    exact: bool

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> WordMap:
        return cls(**d)


@dataclass(frozen=True)
class _Tokenized:
    tokens: list[str]
    owner: list[int]  # group id of each token
    groups: list[tuple[int, int]]  # (first_unit, last_unit) inclusive
    group_tokens: list[int]  # tokens per group


def _differs(units: Sequence[str], i: int, size: int) -> bool:
    window = units[i : i + size]
    joined = normalize_tokens(" ".join(window))
    parts = [t for u in window for t in normalize_tokens(u)]
    return joined != parts


def _group_len(units: Sequence[str], i: int) -> int:
    """How many units starting at ``i`` normalize only TOGETHER ("$" "1.5" "billion").

    Normalizing words one at a time loses constructs that span words. The group
    is the smallest window whose joined normalization differs from the per-unit
    one (a 3-window counts only when the difference is not just units ``i+1..``),
    extended by one more unit when that unit changes the group's reading rather
    than merely following it: "$" "1.5" groups with "billion" (one point five
    billion dollars), "$1.5" "billion" does not swallow the "in" after it.
    """
    n = len(units)
    if i + 2 <= n and _differs(units, i, 2):
        k = 2
    elif i + 3 <= n and _differs(units, i, 3) and not _differs(units, i + 1, 2):
        k = 3
    else:
        return 1
    if k == 2 and i + 3 <= n:
        grown = normalize_tokens(" ".join(units[i : i + 3]))
        alone = normalize_tokens(" ".join(units[i : i + 2])) + normalize_tokens(
            units[i + 2]
        )
        if grown != alone:
            k = 3
    return k


def _tokenize_units(units: Sequence[str]) -> _Tokenized:
    tokens: list[str] = []
    owner: list[int] = []
    groups: list[tuple[int, int]] = []
    counts: list[int] = []
    i = 0
    while i < len(units):
        k = _group_len(units, i)
        toks = normalize_tokens(" ".join(units[i : i + k]))
        g = len(groups)
        groups.append((i, i + k - 1))
        counts.append(len(toks))
        tokens.extend(toks)
        owner.extend([g] * len(toks))
        i += k
    return _Tokenized(tokens, owner, groups, counts)


def _match(
    script: Sequence[str], transcript: Sequence[str]
) -> tuple[list[int | None], list[int]]:
    """Per script token: matched transcript index (or None) and its block size."""
    sm = difflib.SequenceMatcher(a=script, b=transcript, autojunk=False)
    match_b: list[int | None] = [None] * len(script)
    block = [0] * len(script)
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            match_b[b.a + k] = b.b + k
            block[b.a + k] = b.size
    return match_b, block


def map_words_to_script(
    script_tokens: Sequence[str], whisper_words: Sequence[dict | Word]
) -> list[WordMap]:
    """One ``WordMap`` per script token.

    Each whisper word is normalized with the production normalizer (a word can
    yield 0..n tokens; a number spanning words is normalized as a group), the two
    token streams are aligned with ``difflib`` exactly as the verifier does, and
    every matched script token gets its whisper word's index and times.
    """
    words = [w if isinstance(w, Word) else Word.from_dict(w) for w in whisper_words]
    tk = _tokenize_units([w.word for w in words])
    match_b, block = _match(script_tokens, tk.tokens)
    out: list[WordMap] = []
    for i, tok in enumerate(script_tokens):
        j = match_b[i]
        if j is None:
            out.append(WordMap(i, tok, None, None, None, False))
            continue
        g = tk.owner[j]
        first, last = tk.groups[g]
        single = first == last and tk.group_tokens[g] == 1
        out.append(
            WordMap(
                i,
                tok,
                first,
                words[first].start,
                words[last].end,
                single and block[i] >= EXACT_MIN_BLOCK,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Script structure (sentences, paragraphs, quotes, literal text)
# --------------------------------------------------------------------------- #

_SENT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'’”)\]])\s+")
_PARA_RE = re.compile(r"\n\s*\n")
# A "." after one of these is not a sentence end ("Mr. Smith", "Acme Inc. said").
# Dotted initialisms (U.S., e.g.) and single capital initials ("J. P. Morgan")
# are guarded too, except "I". A real sentence ending in one of them is missed:
# fewer boundaries, never an invented one.
_ABBREVIATIONS = frozenset(
    "mr mrs ms dr prof sen rep gov st jr sr vs inc corp ltd co etc".split()
)
_QUOTE_RE = re.compile(r'"[^"\n]+"|“[^”\n]+”')


@dataclass(frozen=True)
class _Structure:
    tokens: tuple[str, ...]
    literal_ok: bool  # per-word tokenization reproduces normalize_tokens(text)
    char_span: tuple[tuple[int, int], ...]  # per token, its unit group's chars
    first_of_group: tuple[bool, ...]
    last_of_group: tuple[bool, ...]
    sentence_starts: tuple[int, ...]  # token index of each sentence start; [0] == 0
    paragraph_starts: tuple[int, ...]
    quotes: tuple[tuple[int, int], ...]  # token intervals of quotations (>= 3 tokens)


def _ends_in_abbreviation(text: str, end: int) -> bool:
    """Does the whitespace at ``end`` follow an abbreviation's period?"""
    words = text[:end].split()
    core = words[-1].rstrip("\"'’”)]") if words else ""
    if not core.endswith("."):
        return False
    stem = core[:-1]
    return (
        stem.lower() in _ABBREVIATIONS
        or (len(stem) == 1 and stem.isupper() and stem != "I")
        or re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", stem) is not None
    )


@lru_cache(maxsize=256)
def _structure(text: str) -> _Structure:
    tokens = tuple(normalize_tokens(text))
    n = len(tokens)
    spans = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\S+", text)]
    tk = _tokenize_units([s[2] for s in spans])
    if tuple(tk.tokens) != tokens:
        # Cannot place tokens in the text: no literal text, no sentence structure.
        return _Structure(
            tokens, False, (), (True,) * n, (True,) * n, (0,) if n else (), (0,), ()
        )
    char_span = []
    first, last = [], []
    for t, g in enumerate(tk.owner):
        first_unit, last_unit = tk.groups[g]
        char_span.append((spans[first_unit][0], spans[last_unit][1]))
        first.append(t == 0 or tk.owner[t - 1] != g)
        last.append(t == n - 1 or tk.owner[t + 1] != g)
    starts = [cs[0] for cs in char_span]  # nondecreasing

    def token_at(c: int) -> int:  # first token whose unit starts at or after c
        return bisect.bisect_left(starts, c)

    def boundaries(*regexes: re.Pattern) -> tuple[int, ...]:
        idx = {0}
        for rx in regexes:
            for m in rx.finditer(text):
                if rx is _SENT_RE and _ends_in_abbreviation(text, m.start()):
                    continue
                idx.add(token_at(m.end()))
        return tuple(sorted(i for i in idx if 0 <= i < n)) or ((0,) if n else ())

    quotes = []
    for m in _QUOTE_RE.finditer(text):
        qs, qe = token_at(m.start()), token_at(m.end())
        if qe - qs >= 3:
            quotes.append((qs, qe))
    return _Structure(
        tokens,
        True,
        tuple(char_span),
        tuple(first),
        tuple(last),
        boundaries(_SENT_RE, _PARA_RE),
        boundaries(_PARA_RE),
        tuple(quotes),
    )


def _trigram_positions(tokens: Sequence[str]) -> dict[tuple[str, ...], list[int]]:
    """3-grams (letters >= MIN_GRAM_CHARS) occurring at least twice."""
    pos: dict[tuple[str, ...], list[int]] = {}
    for p in range(len(tokens) - 2):
        gram = tuple(tokens[p : p + 3])
        if sum(map(len, gram)) >= MIN_GRAM_CHARS:
            pos.setdefault(gram, []).append(p)
    return {g: v for g, v in pos.items() if len(v) >= 2}


def _repeated_gram(
    tri: dict[tuple[str, ...], list[int]], tokens: Sequence[str], s: int, e: int
) -> tuple[str, ...] | None:
    """A 3-gram inside ``[s, e)`` that also occurs entirely outside it."""
    for p in range(s, e - 2):
        gram = tuple(tokens[p : p + 3])
        for q in tri.get(gram, ()):
            if q + 3 <= s or q >= e:
                return gram
    return None


# --------------------------------------------------------------------------- #
# Base labeling (decision 2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SuspectSpan:
    """A script span whisper shows missing >= FAITHFUL_NET_MISSING tokens."""

    script_start: int
    script_end: int
    script_words: int
    transcript_words: int
    net_missing: int
    excerpt: str
    clip_start_s: float  # owner clip: this span +/- CLIP_PAD_S, clamped to audio
    clip_end_s: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SuspectSpan:
        return cls(**d)


@dataclass(frozen=True)
class BaseLabel:
    """Whisper's verdict on one base render, plus everything ``choose_cuts`` needs.

    ``label`` is ``"faithful"`` or ``"suspect"``; the owner's later call
    (faithful / natural_omission / defect / uncertain) lives in the Task 3
    artifact, not here. ``reasons`` is empty for a faithful base.
    """

    base_id: str
    label: Literal["faithful", "suspect"]
    reasons: tuple[str, ...]
    recall: float | None  # whisper recall vs the script; None for an empty script
    script_text: str
    script_tokens: tuple[str, ...]
    whisper_tokens: int
    words: tuple[Word, ...]
    word_map: tuple[WordMap, ...]
    total_samples: int
    sample_rate: int
    suspect_spans: tuple[SuspectSpan, ...]

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> BaseLabel:
        return cls(
            base_id=d["base_id"],
            label=d["label"],
            reasons=tuple(d["reasons"]),
            recall=d["recall"],
            script_text=d["script_text"],
            script_tokens=tuple(d["script_tokens"]),
            whisper_tokens=d["whisper_tokens"],
            words=tuple(Word.from_dict(w) for w in d["words"]),
            word_map=tuple(WordMap.from_dict(w) for w in d["word_map"]),
            total_samples=d["total_samples"],
            sample_rate=d["sample_rate"],
            suspect_spans=tuple(SuspectSpan.from_dict(s) for s in d["suspect_spans"]),
        )


def screen_base(
    script_text: str,
    whisper: dict | Sequence[dict],
    *,
    total_samples: int,
    base_id: str = "",
    sample_rate: int = PCM_SAMPLE_RATE,
) -> BaseLabel:
    """Label a base ``faithful`` or ``suspect`` from an independent ASR.

    Faithful: no span with ``net_missing >= 6`` and whisper recall >= 0.95, using
    the production normalizer and aligner (``verify._align``, anchor_min 3).
    Anything else is ``suspect`` and carries the suspect spans with owner-clip
    windows. ``total_samples`` is REQUIRED and must be the sample count of the PCM
    the label will cut (not whisper's rounded ``duration``): cut points are
    clamped against it. ``whisper`` may be a verbose_json dict or a bare words
    list. Whisper words running more than MAX_WHISPER_OVERRUN_S past the end of
    the PCM mean it was shown different audio and raise ``ValueError``.
    """
    if total_samples <= 0:
        raise ValueError(f"total_samples must be positive, got {total_samples}")
    words = parse_words(whisper)
    duration = total_samples / sample_rate
    if words and words[-1].end > duration + MAX_WHISPER_OVERRUN_S:
        raise ValueError(
            f"whisper words run to {words[-1].end:.2f}s, longer than the "
            f"{duration:.2f}s of PCM: not the same audio"
        )
    script = normalize_tokens(script_text)
    tk = _tokenize_units([w.word for w in words])
    word_map = map_words_to_script(script, words)

    reasons: list[str] = []
    recall: float | None = None
    suspects: list[SuspectSpan] = []
    if not script:
        reasons.append("empty_script")
    else:
        matched, spans = _align(script, tk.tokens, DEFAULT_THRESHOLDS)
        recall = sum(matched) / len(script)
        for s in spans:
            if s.net_missing >= FAITHFUL_NET_MISSING:
                suspects.append(_suspect_span(s, word_map, duration))
        if suspects:
            reasons.append("net_missing_span")
        if recall < FAITHFUL_RECALL:
            reasons.append(f"recall_below_{FAITHFUL_RECALL}")
    return BaseLabel(
        base_id=base_id,
        label="suspect" if reasons else "faithful",
        reasons=tuple(reasons),
        recall=recall,
        script_text=script_text,
        script_tokens=tuple(script),
        whisper_tokens=len(tk.tokens),
        words=tuple(words),
        word_map=tuple(word_map),
        total_samples=total_samples,
        sample_rate=sample_rate,
        suspect_spans=tuple(suspects),
    )


def _suspect_span(span: Any, word_map: Sequence[WordMap], duration: float):
    t0 = 0.0
    for i in range(span.script_start - 1, -1, -1):
        end = word_map[i].end
        if end is not None:
            t0 = end
            break
    t1 = duration
    for i in range(span.script_end, len(word_map)):
        start = word_map[i].start
        if start is not None:
            t1 = start
            break
    return SuspectSpan(
        script_start=span.script_start,
        script_end=span.script_end,
        script_words=span.script_words,
        transcript_words=span.transcript_words,
        net_missing=span.net_missing,
        excerpt=span.excerpt,
        clip_start_s=max(0.0, t0 - CLIP_PAD_S),
        clip_end_s=min(duration, t1 + CLIP_PAD_S),
    )


# --------------------------------------------------------------------------- #
# Cuts (decision 3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CutInterval:
    """One removed stretch: script tokens, whisper words and samples."""

    script_start: int  # token interval [start, end)
    script_end: int
    normalized_tokens: int  # script_end - script_start
    first_word: int  # whisper word indices, inclusive
    last_word: int
    sample_start: int  # [start, end) in samples: where the audio is ACTUALLY cut
    sample_end: int
    removed_text: str  # the literal script text of the interval
    literal: bool  # False: removed_text is the normalized tokens joined
    # The gap-midpoint cut points ``choose_cuts`` picked, and the ranges
    # (between the nominal midpoints of the adjacent words) that snapping may
    # never leave. ``finalize_cut`` moves ``sample_*`` to the quietest frame
    # inside them and sets ``snapped``; until then sample_* == nominal_*.
    nominal_start: int
    nominal_end: int
    start_bounds: tuple[int, int]
    end_bounds: tuple[int, int]
    snapped: bool = False
    # RMS of the 10 ms frame at each snapped point over the base's global RMS
    # (``None`` until ``finalize_cut``). Near 0: a real gap. Near or above 1: no
    # quiet place was in reach, so the cut may land inside speech.
    start_energy_ratio: float | None = None
    end_energy_ratio: float | None = None

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> CutInterval:
        d = dict(d)
        d["start_bounds"] = tuple(d["start_bounds"])
        d["end_bounds"] = tuple(d["end_bounds"])
        return cls(**d)


@dataclass(frozen=True)
class CutSpec:
    """A frozen, audio-level cut label. Written before any Gemini ASR sees the cut."""

    cut_id: str
    base_id: str
    family: str
    size_bin: int  # the requested size (``multi``: the requested TOTAL)
    intervals: tuple[CutInterval, ...]
    total_tokens: int  # actual normalized tokens removed, summed over intervals
    base_samples: int
    seed: str | int | None
    notes: dict = field(default_factory=dict)
    sample_rate: int = PCM_SAMPLE_RATE

    @property
    def snapped(self) -> bool:
        """Has ``finalize_cut`` placed every cut point on the audio?"""
        return bool(self.intervals) and all(i.snapped for i in self.intervals)

    @property
    def sample_intervals(self) -> list[tuple[int, int]]:
        return [(i.sample_start, i.sample_end) for i in self.intervals]

    @property
    def token_intervals(self) -> list[tuple[int, int]]:
        return [(i.script_start, i.script_end) for i in self.intervals]

    @property
    def removed_samples(self) -> int:
        return sum(e - s for s, e in self.sample_intervals)

    def apply(self, pcm: bytes) -> tuple[bytes, list[bytes]]:
        """Cut ``pcm`` (the base's PCM) per this spec; checks the sample count.

        Refuses a spec that ``finalize_cut`` has not snapped: shipping the
        nominal gap midpoints is the label error snapping exists to prevent.
        """
        if not self.snapped:
            raise ValueError(
                f"cut {self.cut_id} is not snapped; call finalize_cut(spec, pcm) first"
            )
        if len(pcm) != 2 * self.base_samples:
            raise ValueError(
                f"PCM has {len(pcm) // 2} samples, cut {self.cut_id} was "
                f"chosen for {self.base_samples}"
            )
        remaining, removed = cut_pcm(pcm, self.sample_intervals)
        if len(remaining) != 2 * (self.base_samples - self.removed_samples):
            raise RuntimeError("remaining sample count does not match the spec")
        return remaining, removed

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> CutSpec:
        return cls(
            cut_id=d["cut_id"],
            base_id=d["base_id"],
            family=d["family"],
            size_bin=d["size_bin"],
            intervals=tuple(CutInterval.from_dict(i) for i in d["intervals"]),
            total_tokens=d["total_tokens"],
            base_samples=d["base_samples"],
            seed=d["seed"],
            notes=dict(d.get("notes") or {}),
            sample_rate=d.get("sample_rate", PCM_SAMPLE_RATE),
        )


def _build_interval(
    base: BaseLabel, st: _Structure, s: int, e: int
) -> CutInterval | None:
    """Cut ``[s, e)`` at the midpoint of the gap before its first word and the
    gap after its last (the nominal points), never inside a boundary word, with
    the ranges snapping may use: between the midpoints of the adjacent words."""
    first, last = base.word_map[s].word_index, base.word_map[e - 1].word_index
    if first is None or last is None or last < first:
        return None
    words, rate, total = base.words, base.sample_rate, base.total_samples

    def mid(k: int) -> int:
        return round((words[k].start + words[k].end) / 2 * rate)

    start_bounds = (mid(first - 1) if first > 0 else 0, mid(first))
    end_bounds = (mid(last), mid(last + 1) if last + 1 < len(words) else total)
    end_bounds = (end_bounds[0], min(end_bounds[1], total))
    prev_end = words[first - 1].end if first > 0 else 0.0
    next_start = words[last + 1].start if last + 1 < len(words) else total / rate
    t0 = min((prev_end + words[first].start) / 2, words[first].start)
    t1 = max((words[last].end + next_start) / 2, words[last].end)
    s0 = min(max(round(t0 * rate), start_bounds[0]), start_bounds[1])
    s1 = min(max(round(t1 * rate), end_bounds[0]), end_bounds[1])
    if s1 <= s0 or start_bounds[0] > start_bounds[1] or end_bounds[0] > end_bounds[1]:
        return None
    if st.literal_ok:
        text = base.script_text[st.char_span[s][0] : st.char_span[e - 1][1]]
    else:
        text = " ".join(base.script_tokens[s:e])
    return CutInterval(
        script_start=s,
        script_end=e,
        normalized_tokens=e - s,
        first_word=first,
        last_word=last,
        sample_start=s0,
        sample_end=s1,
        removed_text=text,
        literal=st.literal_ok,
        nominal_start=s0,
        nominal_end=s1,
        start_bounds=start_bounds,
        end_bounds=end_bounds,
    )


def _finalize(
    base: BaseLabel,
    st: _Structure,
    family: str,
    size: int,
    windows: Sequence[tuple[int, int]],
    notes: dict,
    seed: str | int | None,
) -> CutSpec | None:
    intervals = []
    for s, e in windows:
        iv = _build_interval(base, st, s, e)
        if iv is None:
            return None
        intervals.append(iv)
    for a, b in zip(intervals, intervals[1:], strict=False):
        if b.sample_start < a.sample_end:
            return None
    removed = sum(i.normalized_tokens for i in intervals)
    if removed > MAX_REMOVED_FRACTION * len(base.script_tokens):
        return None
    return CutSpec(
        cut_id=f"{base.base_id}--{family}-{size}",
        base_id=base.base_id,
        family=family,
        size_bin=size,
        intervals=tuple(intervals),
        total_tokens=sum(i.normalized_tokens for i in intervals),
        base_samples=base.total_samples,
        seed=seed,
        notes=notes,
        sample_rate=base.sample_rate,
    )


def _word_timing_ok(words: Sequence[Word], k: int) -> bool:
    """Can word ``k`` bound a cut? Real whisper timings often have abutting
    words, zero-length words and an inflated neighbour; a boundary word must
    last >= MIN_BOUNDARY_WORD_S, so must both neighbours, and nothing overlaps."""
    for j in (k - 1, k, k + 1):
        if 0 <= j < len(words) and words[j].end - words[j].start < MIN_BOUNDARY_WORD_S:
            return False
    if k > 0 and words[k].start < words[k - 1].end - OVERLAP_TOLERANCE_S:
        return False
    return not (
        k + 1 < len(words) and words[k].end > words[k + 1].start + OVERLAP_TOLERANCE_S
    )


def boundary_eligibility(base: BaseLabel) -> list[bool]:
    """Per script token: may it be the first or last token of a cut?

    Exact (``WordMap.exact``) AND timed well enough to place a boundary next to
    (``_word_timing_ok``). ``choose_cuts`` additionally needs the token to sit
    on a whole script word.
    """
    timing = [_word_timing_ok(base.words, k) for k in range(len(base.words))]
    return [
        wm.exact and wm.word_index is not None and timing[wm.word_index]
        for wm in base.word_map
    ]


def _aligned_windows(
    starts: Sequence[int],
    n: int,
    size: int,
    ok_start: Sequence[bool],
    ok_end: Sequence[bool],
) -> list[tuple[int, int]]:
    """Runs of consecutive sentences/paragraphs whose length is near ``size``."""
    bounds = [*starts, n]
    lo, hi = (
        math.ceil((1 - SIZE_TOLERANCE) * size),
        math.floor((1 + SIZE_TOLERANCE) * size),
    )
    out = []
    for i in range(len(bounds) - 1):
        for j in range(i + 1, len(bounds)):
            length = bounds[j] - bounds[i]
            if length > hi:
                break
            if length >= lo and ok_start[bounds[i]] and ok_end[bounds[j] - 1]:
                out.append((bounds[i], bounds[j]))
    return out


def choose_cuts(
    base: BaseLabel,
    family: str,
    size: int,
    rng: random.Random,
    *,
    seed: str | int | None = None,
) -> CutSpec | None:
    """Pick one cut of ``family`` and ``size`` normalized tokens from a faithful base.

    Only intervals whose boundary tokens are eligible (``boundary_eligibility``:
    exactly matched with trustworthy word timing) and sit on whole script words
    are considered. The spec carries NOMINAL cut points; ``finalize_cut`` snaps
    them to the audio. Returns ``None`` when no
    interval satisfies the family's constraints (reported, never relaxed).
    ``seed`` is recorded in the spec only; reproducibility comes from ``rng``.

    - ``start`` / ``end``: begins within / finishes within EDGE_TOKENS of the edge.
    - ``mid_fluent``: begins and ends strictly inside sentences (never on a
      sentence boundary); a candidate entirely inside one sentence is preferred
      and ``notes["within_sentence"]`` says which it was.
    - ``sentence``: whole consecutive sentences, total within +/-40% of ``size``.
    - ``paragraph``: whole consecutive paragraphs, within +/-40% of ``size``.
    - ``predictable``: contains a quotation, or a >=3-gram that also occurs
      elsewhere in the chunk (``notes["predictable"]`` says which).
    - ``multi``: ``size`` is the TOTAL, split into 3 separated cuts (gap >= 20 tokens).

    Whatever the family, a cut that would remove more than MAX_REMOVED_FRACTION
    of the chunk is not offered.
    """
    if family not in FAMILIES:
        raise ValueError(f"unknown family {family!r}; expected one of {FAMILIES}")
    if size < 1 or (family == "multi" and size < MULTI_PARTS):
        raise ValueError(f"size {size} is too small for family {family}")
    if base.label != "faithful":
        raise ValueError(f"base {base.base_id!r} is {base.label}; only faithful cut")
    st = _structure(base.script_text)
    tokens = base.script_tokens
    if st.tokens != tokens:
        raise ValueError("script_text and script_tokens disagree")
    n = len(tokens)
    eligible = boundary_eligibility(base)
    ok_start = [eligible[i] and st.first_of_group[i] for i in range(n)]
    ok_end = [eligible[i] and st.last_of_group[i] for i in range(n)]

    def fixed(sz: int) -> list[int]:
        return [s for s in range(n - sz + 1) if ok_start[s] and ok_end[s + sz - 1]]

    def pick(cands: list[tuple[list[tuple[int, int]], dict]]) -> CutSpec | None:
        cands = list(cands)
        while cands:
            k = rng.randrange(len(cands))
            windows, notes = cands.pop(k)
            spec = _finalize(base, st, family, size, windows, notes, seed)
            if spec is not None:
                return spec
        return None

    sent = st.sentence_starts
    sent_starts = set(sent)
    sentence_bounds = {*sent, n}

    def sentence_of(i: int) -> int:
        return bisect.bisect_right(sent, i) - 1

    if family == "start":
        return pick([([(s, s + size)], {}) for s in fixed(size) if s < EDGE_TOKENS])
    if family == "end":
        return pick(
            [([(s, s + size)], {}) for s in fixed(size) if s + size > n - EDGE_TOKENS]
        )
    if family == "mid_fluent":
        inside: list = []
        across: list = []
        for s in fixed(size):
            e = s + size
            if s in sent_starts or e in sentence_bounds:
                continue
            within = sentence_of(s) == sentence_of(e - 1)
            (inside if within else across).append(
                ([(s, e)], {"within_sentence": within})
            )
        return pick(inside) or pick(across)
    if family == "sentence":
        return pick(
            [([w], {}) for w in _aligned_windows(sent, n, size, ok_start, ok_end)]
        )
    if family == "paragraph":
        return pick(
            [
                ([w], {})
                for w in _aligned_windows(
                    st.paragraph_starts, n, size, ok_start, ok_end
                )
            ]
        )
    if family == "predictable":
        tri = _trigram_positions(tokens)
        cands = []
        for s in fixed(size):
            e = s + size
            why = _predictable_why(st, tri, tokens, s, e)
            if why is not None:
                cands.append(([(s, e)], {"predictable": why}))
        return pick(cands)

    # multi: three separated cuts, sizes summing to ``size``
    parts = [
        size // MULTI_PARTS + (1 if k < size % MULTI_PARTS else 0) for k in range(3)
    ]
    starts = [fixed(p) for p in parts]
    if not all(starts):
        return None
    for _ in range(500):
        windows: list[tuple[int, int]] = []
        floor = 0
        for p, cand in zip(parts, starts, strict=True):
            usable = [s for s in cand if s >= floor]
            if not usable:
                break
            s = rng.choice(usable)
            windows.append((s, s + p))
            floor = s + p + MULTI_MIN_GAP_TOKENS
        else:
            spec = _finalize(base, st, family, size, windows, {"parts": parts}, seed)
            if spec is not None:
                return spec
    return None


def _predictable_why(
    st: _Structure,
    tri: dict[tuple[str, ...], list[int]],
    tokens: Sequence[str],
    s: int,
    e: int,
) -> dict | None:
    for qs, qe in st.quotes:
        if (s <= qs and qe <= e) or (qs <= s and e <= qe):
            return {"kind": "quote", "text": " ".join(tokens[qs : min(qe, qs + 12)])}
    gram = _repeated_gram(tri, tokens, s, e)
    if gram is not None:
        return {"kind": "repeat_3gram", "text": " ".join(gram)}
    return None


def global_mean_energy(pcm: bytes) -> float:
    """Mean squared sample value of 16-bit mono PCM (0.0 for empty audio)."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack_from(f"<{n}h", pcm)
    return sum(map(operator.mul, samples, samples)) / n


def _snap(
    pcm: bytes, nominal: int, bounds: tuple[int, int], radius: int, half: int
) -> tuple[int, float | None]:
    """``(point, mean_energy)``: the sample in ``[nominal-radius, nominal+radius]``
    (and within ``bounds``, inclusive) whose 2*half-sample window has the lowest
    mean energy; ties go to the point nearest ``nominal``. The energy is ``None``
    when no window could be measured."""
    total = len(pcm) // 2
    lo, hi = max(nominal - radius, bounds[0]), min(nominal + radius, bounds[1])
    if lo > hi:
        return min(max(nominal, bounds[0]), bounds[1]), None
    seg_lo, seg_hi = max(0, lo - half), min(total, hi + half)
    samples = struct.unpack_from(f"<{seg_hi - seg_lo}h", pcm, 2 * seg_lo)
    prefix = [0]
    for v in samples:
        prefix.append(prefix[-1] + v * v)
    best: tuple[float, int, int] | None = None
    for c in range(lo, hi + 1):
        a, b = max(seg_lo, c - half), min(seg_hi, c + half)
        if b <= a:
            continue
        key = ((prefix[b - seg_lo] - prefix[a - seg_lo]) / (b - a), abs(c - nominal), c)
        if best is None or key < best:
            best = key
    return (nominal, None) if best is None else (best[2], best[0])


def _ratio(energy: float | None, global_energy: float) -> float | None:
    if energy is None or global_energy <= 0:
        return None
    return (energy / global_energy) ** 0.5


def finalize_cut(
    spec: CutSpec, pcm: bytes, *, base_energy: float | None = None
) -> CutSpec:
    """Snap every cut point of ``spec`` to the audio in ``pcm`` (the base's PCM).

    Each point moves to the lowest-energy 10 ms frame within +/-100 ms of its
    NOMINAL point, clamped (inclusively) to the range between the nominal
    midpoints of the adjacent words (``start_bounds``/``end_bounds``). Whisper
    word timing is itself imprecise, so the clamp is not a guarantee about the
    words: a snapped point can sit as far as the midpoint of a kept neighbour,
    i.e. a cut may take up to half of a kept word (or leave up to half of a
    removed one) by whisper's timing. ``verify_cut_audio`` is the check on that.

    The result records both nominal and snapped points, and the energy of each
    snapped frame as an RMS ratio against the whole base (``base_energy``, the
    mean squared sample, is computed when not given), so a cut that could find
    no quiet frame is visible. Only a snapped spec goes to ``apply``. Always
    snaps from nominal, so it is idempotent. Falls back to the nominal points of
    an interval if snapping would collide with its neighbour.
    """
    if len(pcm) != 2 * spec.base_samples:
        raise ValueError(
            f"PCM has {len(pcm) // 2} samples, cut {spec.cut_id} was chosen for "
            f"{spec.base_samples}"
        )
    if base_energy is None:
        base_energy = global_mean_energy(pcm)
    radius = round(SNAP_RADIUS_S * spec.sample_rate)
    half = round(SNAP_FRAME_S * spec.sample_rate / 2)
    out: list[CutInterval] = []
    prev_end = 0
    for iv in spec.intervals:
        a, ea = _snap(pcm, iv.nominal_start, iv.start_bounds, radius, half)
        b, eb = _snap(pcm, iv.nominal_end, iv.end_bounds, radius, half)
        if not prev_end <= a < b <= spec.base_samples:
            a, b = iv.nominal_start, iv.nominal_end
            ea = eb = None
        out.append(
            replace(
                iv,
                sample_start=a,
                sample_end=b,
                snapped=True,
                start_energy_ratio=_ratio(ea, base_energy),
                end_energy_ratio=_ratio(eb, base_energy),
            )
        )
        prev_end = b
    return replace(spec, intervals=tuple(out))


def removed_sanity(expected_tokens: int, whisper_removed: dict) -> dict:
    """Decision 3's sanity check: whisper's transcript of the removed clip should
    have a token count within 25 percent of the script interval's. A failure
    discards the cut (never relabels it)."""
    observed = len(
        _tokenize_units([w.word for w in parse_words(whisper_removed)]).tokens
    )
    ratio = abs(observed - expected_tokens) / max(1, expected_tokens)
    return {
        "expected_tokens": expected_tokens,
        "observed_tokens": observed,
        "ratio": ratio,
        "ok": ratio <= REMOVED_SANITY_TOLERANCE,
    }


# --------------------------------------------------------------------------- #
# Whisper on the cut audio, and the reconstruction check (decision 6)
# --------------------------------------------------------------------------- #


def _heard_tokens(heard: Any) -> list[str]:
    """Normalized tokens of a whisper dict/words list, plain text, or token list."""
    if isinstance(heard, str):
        return normalize_tokens(heard)
    if isinstance(heard, dict):
        if "words" not in heard:  # no word timestamps: fall back to the text
            return normalize_tokens(str(heard.get("text", "")))
        return _tokenize_units([w.word for w in parse_words(heard)]).tokens
    if heard and isinstance(heard[0], (dict, Word)):
        return _tokenize_units([w.word for w in parse_words(heard)]).tokens
    return list(heard)


def _unexplained_spans(
    expected: Sequence[str], transcript: Sequence[str]
) -> list[tuple[int, list[str]]]:
    """Maximal runs of transcript tokens the diff does not match to ``expected``,
    each with its start index in the transcript."""
    sm = difflib.SequenceMatcher(a=expected, b=transcript, autojunk=False)
    matched = [False] * len(transcript)
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            matched[b.b + k] = True
    runs: list[tuple[int, list[str]]] = []
    start = None
    for j, m in enumerate(matched + [True]):
        if m:
            if start is not None:
                runs.append((start, list(transcript[start:j])))
            start = None
        elif start is None:
            start = j
    return runs


def _unexplained_runs(
    expected: Sequence[str], transcript: Sequence[str]
) -> list[list[str]]:
    return [run for _, run in _unexplained_spans(expected, transcript)]


def _hits(
    runs: Iterable[Sequence[str]], interval: Sequence[str], min_run: int
) -> list[bool]:
    hit = [False] * len(interval)
    for run in runs:
        sm = difflib.SequenceMatcher(a=interval, b=run, autojunk=False)
        for b in sm.get_matching_blocks():
            if b.size >= min_run:
                for k in range(b.size):
                    hit[b.a + k] = True
    return hit


def _removed_mask(spec: CutSpec, script: Sequence[str]) -> list[bool]:
    removed = [False] * len(script)
    for s, e in spec.token_intervals:
        if not 0 <= s < e <= len(script):
            raise ValueError(
                f"interval ({s}, {e}) outside the {len(script)}-token script"
            )
        for i in range(s, e):
            removed[i] = True
    return removed


def verify_cut_audio(
    spec: CutSpec,
    script_text: str,
    whisper_cut: dict | Sequence[dict] | str,
    *,
    base: BaseLabel | None = None,
    window: int = BOUNDARY_WINDOW,
    min_run: int = RECON_MIN_RUN,
) -> dict:
    """Post-cut check: does whisper's transcript of the CUT audio match the label?

    The cut audio should read as ``expected``, the script minus the removed
    intervals. Two ways the cut can be off by a word, which timestamps alone
    cannot rule out:

    - **leftover**: removed text is still audible. Counted for tokens of a
      removed interval that appear in transcript the diff cannot match to
      ``expected``, either at the cut point itself (the first/last ``window``
      tokens of the interval, any length: a single leaked word counts) or
      anywhere as a run of >= ``min_run`` tokens.
    - **extra_missing**: kept text next to a cut is gone. Counted for tokens of
      ``expected`` within ``window`` of each cut point that whisper did not
      hear. With ``base`` given, a token whisper also missed in the UNCUT base is
      whisper noise, not the cut's fault, and is not counted.

    ``ok`` requires both to be zero; Task 3 discards cuts that fail. Whisper
    noise next to a cut makes this conservative (it discards), never lenient.
    ``whisper_cut`` may be a verbose_json dict, a words list, or text.
    """
    script = normalize_tokens(script_text)
    removed = _removed_mask(spec, script)
    kept_idx = [i for i, r in enumerate(removed) if not r]
    expected = [script[i] for i in kept_idx]
    heard = _heard_tokens(whisper_cut)
    match_b, _ = _match(expected, heard)
    spans = _unexplained_spans(expected, heard)
    runs = [run for _, run in spans]

    missing_all: set[int] = set()
    per_interval = []
    leftover_total = 0
    for s, e in spec.token_intervals:
        interval = script[s:e]
        p = bisect.bisect_left(kept_idx, s)  # kept tokens before the cut
        lo, hi = max(0, p - window), min(len(expected), p + window)
        missing = []
        for x in range(lo, hi):
            if match_b[x] is not None:
                continue
            if base is not None and base.word_map[kept_idx[x]].word_index is None:
                continue  # whisper missed it in the uncut base too
            missing.append(x)
        missing_all.update(missing)

        left = next(
            (match_b[x] for x in range(p - 1, -1, -1) if match_b[x] is not None), None
        )
        right = next(
            (match_b[x] for x in range(p, len(expected)) if match_b[x] is not None),
            None,
        )
        between = heard[
            (-1 if left is None else left) + 1 : len(heard) if right is None else right
        ]
        hit = _hits(runs, interval, min_run)
        edge = set(range(min(window, len(interval)))) | set(
            range(max(0, len(interval) - window), len(interval))
        )
        for blk in difflib.SequenceMatcher(
            a=interval, b=between, autojunk=False
        ).get_matching_blocks():
            for k in range(blk.size):
                if blk.a + k in edge:
                    hit[blk.a + k] = True
        leftover_total += sum(hit)
        per_interval.append(
            {
                "script_start": s,
                "script_end": e,
                "cut_position": p,  # index into ``expected``
                "leftover": sum(hit),
                "leftover_text": [t for t, h in zip(interval, hit, strict=True) if h],
                "missing": len(missing),
                "missing_text": [expected[x] for x in missing],
            }
        )
    return {
        "ok": leftover_total == 0 and not missing_all,
        "leftover_tokens": leftover_total,
        "extra_missing_tokens": len(missing_all),
        "window": window,
        "expected_tokens": len(expected),
        "heard_tokens": len(heard),
        "intervals": per_interval,
    }


def _level(hits: int, tokens: int, min_run: int) -> str:
    if hits >= max(min_run, math.ceil(tokens / 2)):
        return "confirmed"
    return "partial" if hits >= min_run else "none"


def reconstruction(
    label: CutSpec,
    script_text: str,
    clean_transcript: str | None,
    cut_transcript: str,
    *,
    min_run: int = RECON_MIN_RUN,
    audio_residue: Any = None,
) -> dict:
    """Did the ASR transcribe removed text that is not in the cut audio?

    ``expected`` is the script with the removed intervals deleted: what a faithful
    transcript of the cut audio says. Transcript tokens the diff cannot match to
    it are *unexplained*; a removed interval is *hit* where runs of >= ``min_run``
    of its tokens appear inside unexplained transcript. Short matches are
    ignored, so a stray "the" is not a reconstruction. A repeated phrase whose
    other copy survives in the audio is told apart naturally: the surviving copy
    is matched to ``expected``, so only an EXTRA copy is unexplained.

    ``audio_residue`` (optional) is an independent ASR's transcript of the cut
    audio (a whisper dict/words list, text, or token list). Removed tokens it
    hears in the cut audio are residue (a cut a word late), not Gemini
    reconstruction, and are subtracted from ``cut_hits``; ``raw_cut_hits`` keeps
    the unsubtracted count.

    The same measure on the ASR's transcript of the UNCUT base (``clean_hits``)
    is the ceiling: what the ASR produces when the text really is there.

    Levels are per interval: ``confirmed`` when at least half its tokens (and at
    least ``min_run``) reappear, ``partial`` for ``min_run`` or more, else
    ``none``. The overall ``level`` is the maximum, so one fully reconstructed
    cut among several is not diluted. A confirmed reconstruction that the
    verifier passed stops the calibration.

    Blind spot, by design: a *paraphrased* reconstruction (the right content in
    other words, or with no block of ``min_run`` original tokens) reads as
    ``none``. This check finds verbatim reappearance only.
    """
    script = normalize_tokens(script_text)
    removed = _removed_mask(label, script)
    expected = [t for t, r in zip(script, removed, strict=True) if not r]
    cut_runs = _unexplained_runs(expected, normalize_tokens(cut_transcript))
    clean_runs = (
        _unexplained_runs(expected, normalize_tokens(clean_transcript))
        if clean_transcript
        else None
    )
    residue_runs = (
        _unexplained_runs(expected, _heard_tokens(audio_residue))
        if audio_residue is not None
        else None
    )
    per_interval = []
    cut_total = raw_total = clean_total = residue_total = removed_total = 0
    matched_text: list[str] = []
    rank = {"none": 0, "partial": 1, "confirmed": 2}
    level = "none"
    for s, e in label.token_intervals:
        interval = script[s:e]
        raw_hit = _hits(cut_runs, interval, min_run)
        residue_hit = (
            _hits(residue_runs, interval, min_run)
            if residue_runs is not None
            else [False] * len(interval)
        )
        cut_hit = [h and not r for h, r in zip(raw_hit, residue_hit, strict=True)]
        clean_hit = (
            _hits(clean_runs, interval, min_run) if clean_runs is not None else None
        )
        removed_total += e - s
        raw_total += sum(raw_hit)
        cut_total += sum(cut_hit)
        residue_total += sum(r and h for r, h in zip(residue_hit, raw_hit, strict=True))
        clean_total += sum(clean_hit) if clean_hit is not None else 0
        # contiguous hit runs, for reading
        run: list[str] = []
        for tok, h in zip(interval, cut_hit, strict=True):
            if h:
                run.append(tok)
            elif run:
                matched_text.append(" ".join(run))
                run = []
        if run:
            matched_text.append(" ".join(run))
        lv = _level(sum(cut_hit), e - s, min_run)
        if rank[lv] > rank[level]:
            level = lv
        per_interval.append(
            {
                "script_start": s,
                "script_end": e,
                "tokens": e - s,
                "cut_hits": sum(cut_hit),
                "clean_hits": sum(clean_hit) if clean_hit is not None else None,
                "level": lv,
            }
        )
    return {
        "removed_tokens": removed_total,
        "cut_hits": cut_total,
        "raw_cut_hits": raw_total,
        "audio_residue_tokens": residue_total if residue_runs is not None else None,
        "clean_hits": clean_total if clean_runs is not None else None,
        "fraction": cut_total / removed_total if removed_total else 0.0,
        "fraction_of_clean": (
            cut_total / clean_total if clean_runs is not None and clean_total else None
        ),
        "level": level,
        "matched_text": matched_text,
        "intervals": per_interval,
    }


# --------------------------------------------------------------------------- #
# Text-level simulation (decision 4)
# --------------------------------------------------------------------------- #

SimKind = Literal["delete", "delete_noise", "multi", "repeat_delete"]
_POSITIONS = ("start", "mid", "end", "random")


@dataclass(frozen=True)
class SimSpec:
    """A synthetic manipulation of a real transcript.

    - ``delete``: remove ``size`` script tokens at ``position``.
    - ``delete_noise``: same, then substitute every ``noise_every``-th of the next
      ``noise_span`` tokens with ``filler`` (the T2 blind spot: 40 / 45 / 3).
    - ``multi``: ``count`` separated deletions of ``size`` tokens each, at least
      ``min_gap`` tokens apart (``position`` ignored).
    - ``repeat_delete``: remove ``size`` tokens containing a 3-gram that also
      occurs outside the cut (``position`` ignored).
    """

    kind: SimKind
    size: int = 40
    position: str = "random"
    count: int = 3
    min_gap: int = MULTI_MIN_GAP_TOKENS
    noise_span: int = 45
    noise_every: int = 3
    filler: str = "xyzzy"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SimResult:
    """A synthetic transcript with known deleted script intervals."""

    spec: SimSpec
    tokens: tuple[str, ...]
    deleted: tuple[tuple[int, int], ...]  # script token intervals removed
    removed_transcript: tuple[tuple[int, int], ...]  # input-transcript indices cut
    noise_positions: tuple[int, ...]  # indices into ``tokens`` that were substituted

    @property
    def text(self) -> str:
        """Space-joined tokens; feed to ``verify.analyze`` as the transcript."""
        return " ".join(self.tokens)

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))


def _sim_intervals(
    script: Sequence[str], spec: SimSpec, rng: random.Random
) -> list[tuple[int, int]]:
    n, size = len(script), spec.size
    if size < 1 or size > n:
        raise ValueError(f"cannot delete {size} tokens from a {n}-token script")
    if spec.kind in ("delete", "delete_noise"):
        if spec.position not in _POSITIONS:
            raise ValueError(f"unknown position {spec.position!r}")
        s = {
            "start": 0,
            "mid": (n - size) // 2,
            "end": n - size,
            "random": rng.randint(0, n - size),
        }[spec.position]
        return [(s, s + size)]
    if spec.kind == "multi":
        for _ in range(500):
            floor, out = 0, []
            for _k in range(spec.count):
                room = n - size - floor
                if room < 0:
                    break
                s = rng.randint(floor, floor + room)
                out.append((s, s + size))
                floor = s + size + spec.min_gap
            else:
                return out
        raise ValueError("could not place separated deletions")
    if spec.kind == "repeat_delete":
        tri = _trigram_positions(script)
        cands = [
            s
            for s in range(n - size + 1)
            if _repeated_gram(tri, script, s, s + size) is not None
        ]
        if not cands:
            raise ValueError("no window contains a repeated 3-gram")
        s = rng.choice(cands)
        return [(s, s + size)]
    raise ValueError(f"unknown simulation kind {spec.kind!r}")


def simulate(
    transcript_tokens: Sequence[str],
    script_tokens: Sequence[str],
    spec: SimSpec,
    rng: random.Random,
) -> SimResult:
    """Delete script intervals from a real transcript and report which.

    Script intervals are mapped into the transcript with the verifier's own
    alignment (a boundary inside a substituted or dropped stretch maps to the
    stretch's start), the matching transcript tokens are removed, and for
    ``delete_noise`` the tokens after the cut are partly substituted. The result
    is a synthetic transcript with KNOWN deleted script intervals; it shortlists
    rules but is not evidence of end-to-end sensitivity.
    """
    script, transcript = list(script_tokens), list(transcript_tokens)
    intervals = _sim_intervals(script, spec, rng)
    opcodes = difflib.SequenceMatcher(
        a=script, b=transcript, autojunk=False
    ).get_opcodes()

    def to_b(i: int) -> int:
        if i >= len(script):
            return len(transcript)
        for tag, a1, a2, b1, b2 in opcodes:
            if a1 <= i < a2:
                return b1 + min(i - a1, b2 - b1) if tag != "delete" else b1
        return len(transcript)  # unreachable: every script index is in an op

    cuts = [(to_b(s), to_b(e)) for s, e in intervals]

    noisy: set[int] = set()
    if spec.kind == "delete_noise":
        _, be = cuts[0]
        noisy = {
            j
            for j in range(be, min(be + spec.noise_span, len(transcript)))
            if (j - be) % spec.noise_every == spec.noise_every - 1
        }
    filler = spec.filler
    taken = set(script) | set(transcript)
    while filler in taken:
        filler += "x"

    out: list[str] = []
    noise_positions: list[int] = []
    removed_mask = [False] * len(transcript)
    for bs, be in cuts:
        for j in range(bs, be):
            removed_mask[j] = True
    for j, tok in enumerate(transcript):
        if removed_mask[j]:
            continue
        if j in noisy:
            noise_positions.append(len(out))
            tok = filler
        out.append(tok)
    return SimResult(
        spec=spec,
        tokens=tuple(out),
        deleted=tuple(intervals),
        removed_transcript=tuple(cuts),
        noise_positions=tuple(noise_positions),
    )


# --------------------------------------------------------------------------- #
# Evaluation (decisions 5, 8)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalRecord:
    """One saved ASR transcript of one base or cut, ready to replay offline.

    ``unavailable`` is a ``TranscriptionUnavailable.reason`` (``transcript`` is
    then ignored). For a cut, ``script_text`` is the FULL chunk script the
    verifier would be given, and ``removed`` the script token intervals taken out.
    ``policy`` and ``repeat`` distinguish ASR runs of the same audio.
    """

    record_id: str
    kind: Literal["base", "cut"]
    base_id: str
    script_text: str
    transcript: str | None
    unavailable: str | None = None
    split: str = "dev"
    feed: str = ""
    model: str = ""
    voice: str = ""
    policy: str = ""
    repeat: int = 0
    family: str | None = None
    size_bin: int | None = None
    removed: tuple[tuple[int, int], ...] = ()

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> EvalRecord:
        d = dict(d)
        d["removed"] = tuple(tuple(r) for r in d.get("removed") or ())
        return cls(**d)


@dataclass(frozen=True)
class Outcome:
    """What one threshold setting says about one record."""

    status: Literal["pass", "omission", "unavailable"]
    reasons: tuple[str, ...]
    recall: float | None
    flagged_spans: int
    # Cuts only: ``localized`` is true only when EVERY removed interval overlaps
    # a flagged span (``localized_intervals`` says which did). A multi cut that
    # finds one of its three removals has been detected, not localized.
    localized: bool
    localized_intervals: tuple[bool, ...] = ()


def replay(record: EvalRecord, thresholds: VerifyThresholds) -> Outcome:
    """Re-run the verifier on a saved transcript, as ``verify_audio`` would."""
    if record.unavailable is not None:
        return Outcome("unavailable", (record.unavailable,), None, 0, False)
    transcript = record.transcript or ""
    if not normalize_tokens(transcript):
        return Outcome("unavailable", ("asr_empty",), None, 0, False)
    a = analyze(record.script_text, transcript, thresholds)
    flagged = [s for s in a.spans if s.flagged]
    per = tuple(
        any(s.script_start < e and s.script_end > st for s in flagged)
        for st, e in record.removed
    )
    return Outcome(
        a.status, a.reasons, a.recall, len(flagged), bool(per) and all(per), per
    )


def default_grid(
    *,
    deficits: Sequence[int | None] = (None, 12, 16, 20, 24),
    floors: Sequence[float] = (0.85, 0.90, 0.93),
    base: VerifyThresholds = DEFAULT_THRESHOLDS,
) -> list[VerifyThresholds]:
    """Decision 5's grid; ``None`` (rule off) is included as the reference row."""
    return [
        replace(base, net_deficit_min=m, recall_floor=f)
        for m in deficits
        for f in floors
    ]


def _bump(table: dict, key: str, field_name: str) -> None:
    row = table.setdefault(key, {})
    row[field_name] = row.get(field_name, 0) + 1


def _dims(r: EvalRecord) -> dict[str, str]:
    dims = {
        "split": r.split,
        "feed": r.feed,
        "model": r.model,
        "policy": r.policy,
    }
    if r.kind == "cut":
        dims["family"] = str(r.family)
        dims["size_bin"] = str(r.size_bin)
        dims["family_size"] = f"{r.family}/{r.size_bin}"
    return dims


def evaluate(
    records: Iterable[EvalRecord], thresholds_grid: Iterable[VerifyThresholds]
) -> list[dict]:
    """Replay the verifier over every record for every grid point.

    One result per grid point, JSON-ready::

        {"thresholds": {...},
         "cuts":  {"overall": C, "by_split": {k: C}, "by_family": ..., "by_size_bin":
                   ..., "by_family_size": ..., "by_feed": ..., "by_model": ...,
                   "by_policy": ..., "missed": [record_id], "unlocalized": [record_id],
                   "partially_localized": [record_id]},
         "bases": {"overall": B, "by_split": {k: B}, ..., "false_alarms": [record_id],
                   "false_alarm_bases": [base_id]}}

    where ``C = {n, caught, caught_localized, missed, unavailable}`` (``n ==
    caught + missed + unavailable``; an unavailable record is NEVER counted
    caught; ``caught_localized`` is the caught cuts whose flagged spans overlap
    EVERY removed interval (a multi cut that finds one of three is only
    ``partially_localized``), and is the selection metric: a catch by recall alone,
    far from the cut, is not evidence the cut was found) and
    ``B = {n, passed, false_alarms, unavailable, bases, bases_with_false_alarm}``
    (``n`` counts ASR repeats; ``bases`` distinct base ids).

    Each result also carries ``acceptance_ok`` and the numbers behind it,
    ``acceptance = {base_false_alarms, base_unavailable,
    cuts_not_caught_localized, cut_unavailable}``. ``acceptance_ok`` is true
    only when there is at least one base and one cut, every faithful base
    passed on every ASR repeat, every cut was caught AND localized, and nothing
    was unavailable. It is never vacuously true.
    """
    records = list(records)
    results = []
    for th in thresholds_grid:
        cuts: dict[str, Any] = {"overall": {}}
        bases: dict[str, Any] = {"overall": {}}
        missed: list[str] = []
        unlocalized: list[str] = []
        partial: list[str] = []
        false_alarms: list[str] = []
        fa_bases: set[str] = set()
        seen_bases: dict[str, set[str]] = {}
        for r in records:
            o = replay(r, th)
            dims = _dims(r)
            if r.kind == "cut":
                outcome = {
                    "omission": "caught",
                    "pass": "missed",
                    "unavailable": "unavailable",
                }[o.status]
                fields = ["n", outcome]
                if outcome == "caught" and o.localized:
                    fields.append("caught_localized")
                if outcome == "missed":
                    missed.append(r.record_id)
                if outcome == "caught" and not o.localized:
                    unlocalized.append(r.record_id)
                    if any(o.localized_intervals):
                        partial.append(r.record_id)
                target = cuts
            else:
                outcome = {
                    "omission": "false_alarms",
                    "pass": "passed",
                    "unavailable": "unavailable",
                }[o.status]
                fields = ["n", outcome]
                if o.status == "omission":
                    false_alarms.append(r.record_id)
                    fa_bases.add(r.base_id)
                target = bases
            for f in fields:
                _bump(target, "overall", f)
            for dim, value in dims.items():
                if r.kind == "base" and dim in ("family", "size_bin", "family_size"):
                    continue
                for f in fields:
                    _bump(target.setdefault(f"by_{dim}", {}), value, f)
            if r.kind == "base":
                seen_bases.setdefault("overall", set()).add(r.base_id)
        for table in (cuts, bases):
            for key, rows in list(table.items()):
                if key == "overall":
                    table[key] = _complete(rows, table is cuts)
                else:
                    table[key] = {
                        k: _complete(v, table is cuts) for k, v in rows.items()
                    }
        bases["overall"]["bases"] = len(seen_bases.get("overall", ()))
        bases["overall"]["bases_with_false_alarm"] = len(fa_bases)
        cuts["missed"] = missed
        cuts["unlocalized"] = unlocalized
        cuts["partially_localized"] = partial
        bases["false_alarms"] = false_alarms
        bases["false_alarm_bases"] = sorted(fa_bases)
        acceptance = {
            "base_false_alarms": bases["overall"]["false_alarms"],
            "base_unavailable": bases["overall"]["unavailable"],
            "cuts_not_caught_localized": (
                cuts["overall"]["n"] - cuts["overall"]["caught_localized"]
            ),
            "cut_unavailable": cuts["overall"]["unavailable"],
        }
        results.append(
            {
                "thresholds": _jsonable(asdict(th)),
                "acceptance_ok": (
                    cuts["overall"]["n"] > 0
                    and bases["overall"]["n"] > 0
                    and not any(acceptance.values())
                ),
                "acceptance": acceptance,
                "cuts": cuts,
                "bases": bases,
            }
        )
    return results


def _complete(row: dict, is_cut: bool) -> dict:
    names = (
        ("n", "caught", "caught_localized", "missed", "unavailable")
        if is_cut
        else ("n", "passed", "false_alarms", "unavailable")
    )
    return {k: row.get(k, 0) for k in names}
