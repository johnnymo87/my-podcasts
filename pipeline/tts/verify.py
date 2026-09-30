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
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

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
            raise ValueError(
                f"max_span_ratio must be in [0, 1], got {self.max_span_ratio}"
            )
        if not 0.0 <= self.recall_floor <= 1.0:
            raise ValueError(f"recall_floor must be in [0, 1], got {self.recall_floor}")


DEFAULT_THRESHOLDS = VerifyThresholds()

Status = Literal["pass", "omission"]


def _jsonable(value: Any) -> Any:
    """``asdict`` keeps tuples as tuples; reports want plain JSON lists."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


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
    # "ok" | "empty_script" | "long_unmatched_span" | "recall_below_floor"
    reasons: tuple[str, ...]
    recall: float | None  # None only when the script has no tokens
    script_tokens: int
    transcript_tokens: int
    matched_tokens: int
    longest_span_words: int  # max script_words over all candidate spans; 0 if none
    spans: tuple[Span, ...]  # every candidate gap with >= 1 script token, in order
    thresholds: VerifyThresholds

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))


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
        return Analysis(
            "pass", ("empty_script",), None, 0, len(transcript), 0, 0, (), th
        )
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
    script_text: str,
    transcript_text: str,
    thresholds: VerifyThresholds = DEFAULT_THRESHOLDS,
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
            1
            for s in spans
            if s.flagged and s.script_start < end and s.script_end > start
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
        whole = replace(whole, status="omission", reasons=("recall_below_floor",))
    return whole, projections
