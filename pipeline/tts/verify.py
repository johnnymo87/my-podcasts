"""Large-omission detector: align an ASR transcript against the script.

Changing normalization, alignment, or DEFAULT_THRESHOLDS changes verifier
policy: bump VERIFIER_VERSION (v2: the thresholds calibrated in T5). The ASR
side (model, prompt, generation config) is covered by asr.ASR_POLICY; bump
asr.ASR_PROMPT_VERSION on any prompt text change. T3 must fold VERIFIER_POLICY
(both halves), not just VERIFIER_VERSION, into the render cache key.

Claimed scope: caught every labeled contiguous omission of 8 or more script
tokens in calibration; 6-7 tokens is untested and a real 6-token skip often
aligns to a net deficit of 5, so it may be missed (see VerifyThresholds for the
calibration claim and its limits). It does not
detect changed numbers, negations, repetitions or added speech. See the design
doc, "Verification".

Alignment: difflib matching blocks over normalized tokens. Blocks of at least
``anchor_min`` tokens are anchors; the script-side gaps between consecutive
anchors (and before the first / after the last) are candidate spans. A span is
flagged when it is long (>= min_span_words script tokens) and the transcript
side is much shorter (<= max_span_ratio of it), OR when its net deficit (script
tokens minus transcript tokens in the gap, ``net_missing``) reaches
``net_deficit_min`` (None turns that rule off). Recall
counts ALL matched script tokens, not only anchored ones. Coordinates are
indices into the normalized token lists, not characters or seconds.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from pipeline.tts.asr import ASR_POLICY, Transcription, TranscriptionUnavailable
from pipeline.tts.normalize import normalize_tokens


VERIFIER_VERSION = "2"


def verifier_policy(asr_policy: str = ASR_POLICY) -> str:
    """Both halves of the policy for a given ASR policy string."""
    return f"verifier-v{VERIFIER_VERSION}|{asr_policy}"


VERIFIER_POLICY = verifier_policy()
_EXCERPT_TOKENS = 30


@dataclass(frozen=True)
class VerifyThresholds:
    """Calibrated in T5 (``DEFAULT_THRESHOLDS``, verifier v2).

    Chosen on labeled audio-level evidence, not guessed; the evidence, its
    scope and its limits are in ``docs/plans/2026-09-30-gemini-tts-t5-evidence.md``.
    In short: on faithful dev renders (policy ``thinking-low``) no span had
    ``net_missing`` above 2 and recall was never below 0.973, while every
    labeled cut (8 to 80+ tokens, seven families) left its largest span with
    ``net_missing`` of at least 8 (single 8-token intervals of multi cuts
    aligned to 7). ``net_deficit_min=6`` sits between the two;
    ``recall_floor=0.95`` catches scattered losses that never form one span.
    The claim is "caught every labeled cut of these sizes and families", never
    "catches every omission of N tokens"; see the evidence doc for what the
    evidence does not cover.
    """

    anchor_min: int = 3
    min_span_words: int = 12
    max_span_ratio: float = 0.5
    recall_floor: float = 0.95
    # Also flag a span whose net_missing (script tokens minus transcript tokens
    # in the gap) reaches this. None = off. It catches a cut merged with nearby
    # substitution noise, where the ratio test misses (the T2 blind spot).
    net_deficit_min: int | None = 6

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
        if self.net_deficit_min is not None and self.net_deficit_min < 1:
            raise ValueError(
                f"net_deficit_min must be None or >= 1, got {self.net_deficit_min}"
            )


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
    # "ok" | "empty_script" | "long_unmatched_span" | "recall_below_floor", and,
    # from project_chunks only when the whole episode passed but a chunk's own
    # recall fell below the floor: "chunk_recall_below_floor"
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
                    )
                    or (
                        th.net_deficit_min is not None
                        and s_words - t_words >= th.net_deficit_min
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
    an omission if the whole analysis is one OR any chunk's projection is (then
    with reason ``chunk_recall_below_floor``). A flagged span is
    attributed to a chunk only if the chunk holds a substantial share of the
    span's unmatched tokens; see ``_span_belongs_to_chunk``.
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
            if s.flagged and _span_belongs_to_chunk(s, matched, start, end, thresholds)
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
    # A passing whole has no flagged span, so a failing chunk can only have
    # failed on its own recall (flagged_spans is 0 for every chunk here).
    if whole.status == "pass" and any(p.status == "omission" for p in projections):
        whole = replace(whole, status="omission", reasons=("chunk_recall_below_floor",))
    return whole, projections


def _span_belongs_to_chunk(
    span: Span, matched: list[bool], start: int, end: int, th: VerifyThresholds
) -> bool:
    """Is a flagged span substantially *this chunk's* omission?

    A span merely touching a chunk (one substituted word at its edge, next to a
    wholly dropped neighbour) must not flag it. Attribute the span only when the
    span's UNMATCHED script tokens inside the chunk reach
    ``max(anchor_min, 0.25 * min(chunk_tokens, span_unmatched_total))``.
    """
    lo, hi = max(span.script_start, start), min(span.script_end, end)
    if lo >= hi:
        return False
    inside = sum(1 for i in range(lo, hi) if not matched[i])
    total = sum(1 for i in range(span.script_start, span.script_end) if not matched[i])
    return inside >= max(th.anchor_min, 0.25 * min(end - start, total))


Transcriber = Callable[[bytes, str], Transcription]


@dataclass(frozen=True)
class AsrInfo:
    model: str
    prompt_version: str
    finish_reason: str
    elapsed_s: float
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    transcript_chars: int
    policy: str | None = None  # the transcriber's ASR policy; None = unknown


@dataclass(frozen=True)
class Verdict:
    status: Literal["pass", "omission", "unavailable"]
    reasons: tuple[str, ...]
    detail: str  # human-readable; "" when nothing to add
    analysis: Analysis | None
    asr: AsrInfo | None
    thresholds: VerifyThresholds
    verifier_version: str
    verifier_policy: str  # VERIFIER_POLICY: what T3's cache key must include
    mode: Literal["chunk"]

    @property
    def recall(self) -> float | None:
        return self.analysis.recall if self.analysis else None

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))


def _unavailable(
    reason: str,
    detail: str,
    th: VerifyThresholds,
    asr: AsrInfo | None,
    policy: str = VERIFIER_POLICY,
) -> Verdict:
    return Verdict(
        "unavailable",
        (reason,),
        detail,
        None,
        asr,
        th,
        VERIFIER_VERSION,
        policy,
        "chunk",
    )


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

    The verdict's ``verifier_policy`` names the ASR policy actually used: the
    transcription's own, else the transcriber's ``policy`` attribute, else the
    production default ``ASR_POLICY``.
    """
    declared = getattr(transcriber, "policy", None)
    fallback_policy = declared if isinstance(declared, str) and declared else ASR_POLICY
    try:
        tr = transcriber(audio, mime_type)
    except TranscriptionUnavailable as exc:
        return _unavailable(
            exc.reason, str(exc), thresholds, None, verifier_policy(fallback_policy)
        )
    policy = verifier_policy(tr.policy or fallback_policy)
    info = AsrInfo(
        model=tr.model,
        prompt_version=tr.prompt_version,
        finish_reason=tr.finish_reason,
        elapsed_s=tr.elapsed_s,
        input_tokens=tr.input_tokens,
        output_tokens=tr.output_tokens,
        thinking_tokens=tr.thinking_tokens,
        transcript_chars=len(tr.text),
        policy=tr.policy,
    )
    if not normalize_tokens(tr.text):
        return _unavailable(
            "asr_empty", "transcript has no word tokens", thresholds, info, policy
        )
    a = analyze(script_text, tr.text, thresholds)
    return Verdict(
        a.status,
        a.reasons,
        "",
        a,
        info,
        thresholds,
        VERIFIER_VERSION,
        policy,
        "chunk",
    )
