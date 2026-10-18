"""Podcast audio -> speaker-labelled transcript, in ~10-minute Gemini pieces.

``transcribe_episode`` decodes an mp3, splits it at quiet points, encodes each
piece to a small mono mp3, and sends the pieces *sequentially* to an injected
transcriber (``GeminiPieceTranscriber`` in production, a fake in tests). Every
piece must finish with ``STOP`` and clear a gross word-rate floor, and the
decoded duration must match the RSS duration, or the whole call raises
``TranscriptionError``: a partial transcript is never returned or written.

Plain synchronous function on purpose. The Gemini SDK timeout is per-read, not
wall-clock (see ``pipeline/tts/asr.py``), so the caller is expected to run this
inside a killable child process with a deadline.

Does not reuse or modify ``pipeline.tts.asr.ASR_PROMPT`` (calibrated for the TTS
omission verifier); it shares only the client helper.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from google.genai import errors as genai_errors
from google.genai import types

from pipeline.tts import asr as _asr
from pipeline.tts.config import PCM_BYTES_PER_SECOND, PCM_SAMPLE_RATE
from pipeline.tts.segment import decode_to_pcm_with_log, split_pcm


logger = logging.getLogger(__name__)

PIECE_PROMPT_VERSION = "1"
ASR_MODEL = "gemini-3.8-flash"
# A sanity ceiling, not a tuned limit: Gemini's inline request cap is ~20 MB
# including base64 inflation (~1.33x), and a 640 s piece at 64 kbps is ~5 MB, so
# anything near 15 MB means the encode went wrong.
MAX_PIECE_BYTES = 15 * 1024 * 1024
MIN_WORDS_PER_MIN = 80
# Looping or hallucinated output can still end with STOP; conversational speech
# is ~150-200 wpm, so 400 is a runaway signal, not a tight bound.
MAX_WORDS_PER_MIN = 400
# Only the FLOOR is skipped for pieces shorter than this: a 20-60 s outro or
# music tail legitimately has few words/min and must not abort a 90-minute run.
# The ceiling applies to every piece (a runaway tail is still a runaway).
MIN_CHECKED_PIECE_S = 120.0
# Absolute slack added to the ceiling, so a very short piece is not flagged by a
# rate computed over a few seconds: allowed = MAX_WORDS_PER_MIN * minutes + this.
MAX_WORDS_CUSHION = 60
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = (5.0, 20.0)
# Generation settings. The SDK config (``_generation_config``) and the policy
# digest (``transcription_policy_digest``) both read these, so they cannot drift.
TEMPERATURE = 0
THINKING_LEVEL = "LOW"  # a ``types.ThinkingLevel`` member name
# Encoding policy for each piece (``encode_piece``).
ENCODE_BITRATE = "64k"
ENCODE_CHANNELS = 1
PIECE_TARGET_S = 600.0
PIECE_SEARCH_S = 20.0
CONTEXT_LINES = 20
MAX_OUTPUT_TOKENS = 16384
ENCODE_TIMEOUT_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 300.0
DURATION_TOLERANCE_MIN_S = 120.0
DURATION_TOLERANCE_FRACTION = 0.08

TRANSCRIPT_NAME = "transcript.txt"
MANIFEST_NAME = "transcript.json"


class TranscriptionError(RuntimeError):
    """``reason`` is the structured class (``asr_timeout``, ``asr_blocked``,
    ``asr_error``, ``asr_empty``, ``asr_incomplete``, ...); ``code`` is the HTTP
    status when the failure was an API error. Retry decisions use these, never
    the message text. Without an explicit reason, an ``asr_*:`` message prefix
    supplies it."""

    def __init__(
        self, message: str, *, reason: str | None = None, code: int | None = None
    ) -> None:
        super().__init__(message)
        if reason is None:
            head = message.split(":", 1)[0]
            reason = head if head.startswith("asr_") else "error"
        self.reason = reason
        self.code = code


def _is_retryable(exc: TranscriptionError) -> bool:
    if exc.reason in ("asr_timeout", _asr.ASR_BLOCKED):
        return True
    if exc.reason == "asr_error":
        return exc.code is None or exc.code == 429 or exc.code >= 500
    return False


@dataclass(frozen=True)
class PieceResult:
    text: str
    finish_reason: str
    input_tokens: int | None
    output_tokens: int | None


# --- encoding -----------------------------------------------------------------


def encode_piece(pcm: bytes) -> bytes:
    """Encode 24 kHz mono s16le PCM to a small mp3 (ffmpeg, via pipes); the
    bitrate and channel count are ``ENCODE_BITRATE``/``ENCODE_CHANNELS``."""
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(ENCODE_CHANNELS),
        "-i",
        "pipe:0",
        "-b:a",
        ENCODE_BITRATE,
        "-f",
        "mp3",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=pcm,
            check=True,
            capture_output=True,
            timeout=ENCODE_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode(errors="replace")[-500:]
        raise TranscriptionError(f"mp3 encode failed: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError("mp3 encode timed out") from exc
    except OSError as exc:  # no ffmpeg, permissions, ...
        raise TranscriptionError(f"mp3 encode: ffmpeg not runnable: {exc!r}") from exc
    if not proc.stdout:
        raise TranscriptionError("mp3 encode produced no output")
    return proc.stdout


# --- prompt -------------------------------------------------------------------

_PIECE_PROMPT = """\
Transcribe this podcast audio verbatim. This is piece {n} of {count} of one episode.

Output one speaker turn per line, formatted as "Speaker Name: words spoken".
Hosts: {hosts}. Episode description (may name guests): {description}

Attribute a turn to a named person ONLY when the voice has been identified by a
self-introduction, by being addressed by name, or because it is clearly the same
voice as a named speaker in the context lines below (if any). The hosts listed
above are possible participants, not identified voices. Otherwise label it
"Unknown speaker A", "Unknown speaker B", and keep those labels consistent
within this piece. Never guess who is speaking from the opinions expressed.

The first and last words of this piece may be cut off mid-word; transcribe only
what is audible.

Wrap advertisements and sponsor reads in [SPONSOR] ... [/SPONSOR] on their own
lines. No timestamps, headings, or commentary.
{context_block}"""

# A speaker label: optional markdown emphasis around "Name", then a colon.
_LABEL_RE = re.compile(r"^[*_\s]*([^:\n\[\]]{1,60}?)[*_\s]*:[*_]*\s")
_UNKNOWN_LABEL_RE = re.compile(
    r"^([*_\s]*)Unknown speaker\s+\w+([*_\s]*:)", re.IGNORECASE
)
_NAME_PARTICLES = frozenset(
    {"de", "van", "von", "der", "den", "da", "di", "del", "la", "le", "bin", "al", "el"}
)


def build_piece_prompt(
    *,
    index: int,
    count: int,
    hosts: tuple[str, ...],
    description: str,
    context: str | None,
) -> str:
    """``context`` is the already-built context block (see ``_context_block``)."""
    return _PIECE_PROMPT.format(
        n=index + 1,
        count=count,
        hosts=", ".join(hosts) if hosts else "(not listed)",
        description=description.strip() or "(none)",
        context_block=f"\n{context}\n" if context else "",
    )


def _looks_like_name(name: str) -> bool:
    """1-4 capitalized tokens, optionally with one lowercase particle between
    them ("Ludwig van Beethoven"). Rejects "And here's the thing" and friends,
    which a bare ``text: text`` pattern would happily call a speaker."""
    tokens = name.split()
    if not 1 <= len(tokens) <= 4:
        return False
    particles = 0
    for i, tok in enumerate(tokens):
        if tok[0].isupper():
            continue
        if tok in _NAME_PARTICLES and 0 < i < len(tokens) - 1:
            particles += 1
            continue
        return False
    return particles <= 1 and not name.lower().startswith("unknown speaker")


def _speakers_in(text: str, sponsor_open: bool) -> tuple[list[str], bool]:
    """Named speakers in ``text`` in order of first appearance, plus whether a
    ``[SPONSOR]`` block is still open at its end (a read can span pieces).
    Sponsor reads and ``Unknown speaker *`` labels are not speakers."""
    seen: list[str] = []
    for line in text.splitlines():
        if "[SPONSOR]" in line:
            sponsor_open = "[/SPONSOR]" not in line
            continue
        if "[/SPONSOR]" in line:
            sponsor_open = False
            continue
        if sponsor_open:
            continue
        m = _LABEL_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip("*_ \t")
        if _looks_like_name(name) and name not in seen:
            seen.append(name)
    return seen, sponsor_open


def _context_block(previous_text: str, speakers: list[str]) -> str:
    lines = [ln for ln in previous_text.splitlines() if ln.strip()]
    # "Unknown speaker A" is only meaningful inside one piece; show the labels as
    # unidentified so the model does not continue them into the next piece.
    tail = "\n".join(
        _UNKNOWN_LABEL_RE.sub(r"\1[unidentified speaker]\2", ln)
        for ln in lines[-CONTEXT_LINES:]
    )
    block = (
        "Context from the end of the previous piece (do not repeat it; use it "
        "to keep speaker names consistent):\n"
        f"{tail}"
    )
    if speakers:
        block += (
            "\nNames already used for voices in earlier pieces (match by voice, "
            f"not by this list alone): {', '.join(speakers)}"
        )
    return block


# --- Gemini -------------------------------------------------------------------


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _generation_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        temperature=TEMPERATURE,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel[THINKING_LEVEL]
        ),
    )


class GeminiPieceTranscriber:
    """Callable ``(audio_mp3_bytes, prompt) -> PieceResult``. Client is lazy."""

    def __init__(
        self, *, model: str = ASR_MODEL, timeout_s: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.model = model
        self._timeout_s = timeout_s
        self._client: Any = None
        self._lock = threading.Lock()

    def __enter__(self) -> GeminiPieceTranscriber:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                try:
                    # Module-attribute lookup at call time: the autouse test
                    # guard patches this exact name.
                    self._client = _asr._make_genai_client(self._timeout_s)
                except (KeyError, ValueError) as exc:
                    # Not `Exception`: the guard's AssertionError must propagate.
                    raise TranscriptionError(f"asr_error: {exc!r}") from exc
            return self._client

    def __call__(self, audio: bytes, prompt: str) -> PieceResult:
        client = self._get_client()
        try:
            resp = client.models.generate_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(data=audio, mime_type="audio/mpeg"),
                    prompt,
                ],
                config=_generation_config(),
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise TranscriptionError(
                f"asr_timeout: {type(exc).__name__}: {exc}", reason="asr_timeout"
            ) from exc
        except genai_errors.APIError as exc:
            code = _int_or_none(getattr(exc, "code", None))
            status = getattr(exc, "status", None)
            raise TranscriptionError(
                f"asr_error: HTTP {code} {status}: {type(exc).__name__}: {exc}",
                reason="asr_error",
                code=code,
            ) from exc
        except Exception as exc:  # any other SDK/transport failure
            raise TranscriptionError(
                f"asr_error: {type(exc).__name__}: {exc}", reason="asr_error"
            ) from exc

        if not resp.candidates:
            feedback = getattr(resp, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None)
            detail = "no candidates"
            if block is not None:
                detail += f" (block_reason={getattr(block, 'name', block)})"
            # Only a retryable block (OTHER) is asr_blocked; the rule lives in
            # pipeline.tts.asr so the two cannot drift.
            reason = (
                _asr.ASR_BLOCKED if _asr._is_retryable_block(block) else "asr_empty"
            )
            raise TranscriptionError(f"{reason}: {detail}", reason=reason)
        finish = resp.candidates[0].finish_reason
        finish_name = finish.name if finish is not None else "NONE"
        if finish_name != "STOP":
            raise TranscriptionError(
                f"asr_incomplete: finish_reason={finish_name}", reason="asr_incomplete"
            )
        text = resp.text or ""  # excludes thought parts; None if no text parts
        if not text.strip():
            raise TranscriptionError(
                "asr_empty: no transcript text in response", reason="asr_empty"
            )
        usage = getattr(resp, "usage_metadata", None)
        return PieceResult(
            text=text,
            finish_reason=finish_name,
            input_tokens=_int_or_none(getattr(usage, "prompt_token_count", None)),
            output_tokens=_int_or_none(getattr(usage, "candidates_token_count", None)),
        )


# --- orchestration --------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def transcription_policy_digest(model: str) -> str:
    """One digest of everything about *how* audio becomes text that is not the
    audio or the per-episode prompt inputs: prompt version and template, model,
    the generation settings the SDK is sent, the piece encoding, and how the
    audio is split. It keys both the piece checkpoints and the whole-transcript
    reuse, so a change to any of it reuses nothing already paid for."""
    payload = {
        "prompt_version": PIECE_PROMPT_VERSION,
        "template": _PIECE_PROMPT,
        "model": model,
        "temperature": TEMPERATURE,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "thinking_level": THINKING_LEVEL,
        "encode_bitrate": ENCODE_BITRATE,
        "encode_channels": ENCODE_CHANNELS,
        "encode_sample_rate": PCM_SAMPLE_RATE,
        "target_s": PIECE_TARGET_S,
        "search_s": PIECE_SEARCH_S,
        "context_lines": CONTEXT_LINES,
    }
    return _sha256_text(json.dumps(payload, sort_keys=True))


def _prompt_inputs_sha(hosts: tuple[str, ...], description: str) -> str:
    """The per-episode text that is rendered into every piece prompt."""
    payload = {"hosts": list(hosts), "description": description}
    return _sha256_text(json.dumps(payload, sort_keys=True))


def _load_reusable(
    work_dir: Path,
    *,
    audio_sha: str,
    model: str,
    carry_context: bool,
    prompt_inputs_sha: str,
    policy_sha: str,
    rss_duration_s: int | None,
) -> str | None:
    """The stored transcript, iff BOTH files exist and the manifest matches this
    audio, prompt version, model, carry_context, prompt inputs and
    transcription policy digest, and
    transcript.txt is the text the manifest recorded, AND the stored result still
    clears the *current* duration and per-piece word-rate rules (the rules may
    have changed, or the RSS duration may differ from the one it was accepted
    under). Anything else is a miss (re-transcribe)."""
    txt_path = work_dir / TRANSCRIPT_NAME
    manifest_path = work_dir / MANIFEST_NAME
    if not (txt_path.is_file() and manifest_path.is_file()):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        text = txt_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    if (
        manifest.get("audio_sha256") != audio_sha
        or manifest.get("prompt_version") != PIECE_PROMPT_VERSION
        or manifest.get("model") != model
        or manifest.get("carry_context") != carry_context
        or manifest.get("prompt_inputs_sha256") != prompt_inputs_sha
        or manifest.get("policy_sha256") != policy_sha
        or manifest.get("transcript_sha256") != _sha256_text(text)
    ):
        return None
    if not _cached_result_passes_current_checks(manifest, rss_duration_s):
        return None
    return text


def _cached_result_passes_current_checks(
    manifest: dict[str, Any], rss_duration_s: int | None
) -> bool:
    """Re-run today's duration and word-rate rules on a stored manifest. A
    malformed manifest is a failure."""
    decoded_s = manifest.get("decoded_duration_s")
    pieces = manifest.get("pieces")
    if isinstance(decoded_s, bool) or not isinstance(decoded_s, int | float):
        return False
    if not isinstance(pieces, list) or not pieces:
        return False
    try:
        _check_duration(float(decoded_s), rss_duration_s)
        for i, piece in enumerate(pieces):
            piece_s = float(piece["end_s"]) - float(piece["start_s"])
            if piece_s <= 0 or not isinstance(piece["text"], str):
                return False
            if _piece_rate_error(i, len(piece["text"].split()), piece_s):
                return False
    except TranscriptionError:
        return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _check_duration(decoded_s: float, rss_duration_s: int | None) -> bool:
    """Return True when the check was skipped (no RSS duration)."""
    if rss_duration_s is None:
        return True
    allowed = max(
        DURATION_TOLERANCE_MIN_S, DURATION_TOLERANCE_FRACTION * rss_duration_s
    )
    if abs(decoded_s - rss_duration_s) > allowed:
        raise TranscriptionError(
            f"duration mismatch: decoded {decoded_s:.0f}s vs RSS "
            f"{rss_duration_s}s (allowed +/-{allowed:.0f}s); audio is likely "
            "truncated or the wrong file"
        )
    return False


def _piece_rate_error(
    index: int, words: int, piece_s: float
) -> TranscriptionError | None:
    """The word-rate refusal for one piece, or None when it is acceptable. The
    floor needs a piece of ``MIN_CHECKED_PIECE_S``; the ceiling has no length
    exemption, only the ``MAX_WORDS_CUSHION`` allowance."""
    minutes = piece_s / 60
    if piece_s >= MIN_CHECKED_PIECE_S and words / minutes < MIN_WORDS_PER_MIN:
        return TranscriptionError(
            f"piece {index}: only {words} words in {minutes:.1f} min "
            f"({words / minutes:.0f} words/min < {MIN_WORDS_PER_MIN}); transcript "
            "is likely incomplete",
            reason="asr_incomplete",
        )
    if words > MAX_WORDS_PER_MIN * minutes + MAX_WORDS_CUSHION:
        return TranscriptionError(
            f"piece {index}: {words} words in {minutes:.1f} min "
            f"({words / minutes:.0f} words/min > {MAX_WORDS_PER_MIN} + "
            f"{MAX_WORDS_CUSHION}-word cushion); output is likely looping or "
            "hallucinated",
            reason="asr_runaway",
        )
    return None


def _checkpoint_path(work_dir: Path, index: int) -> Path:
    return work_dir / "pieces" / f"{index:04d}.json"


def _checkpoint_key(
    audio_sha: str, start: int, end: int, policy_sha: str, prompt: str
) -> str:
    """Identity of one piece's transcription. The full prompt is in it, and the
    prompt carries the previous piece's tail, so a checkpoint is only reused
    when everything upstream of it is identical too. ``policy_sha`` is
    ``transcription_policy_digest`` (model, settings, encoding, version)."""
    return _sha256_text(json.dumps([audio_sha, start, end, policy_sha, prompt]))


def _load_checkpoint(path: Path, key: str) -> dict[str, Any] | None:
    try:
        cp = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cp, dict) or cp.get("key") != key:
        return None
    if not isinstance(cp.get("text"), str) or not isinstance(cp.get("mp3_sha256"), str):
        return None
    if cp.get("finish_reason") != "STOP" or not isinstance(cp.get("bytes"), int):
        return None
    return cp


def _transcribe_with_retry(
    transcriber: Callable[[bytes, str], PieceResult],
    mp3: bytes,
    prompt: str,
    *,
    index: int,
    sleep: Callable[[float], None],
) -> tuple[PieceResult, int]:
    """Up to ``MAX_ATTEMPTS`` tries for retryable failures; returns the result and
    the number of attempts used. Non-retryable failures raise at once."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return transcriber(mp3, prompt), attempt
        except TranscriptionError as exc:
            if not _is_retryable(exc) or attempt >= MAX_ATTEMPTS:
                suffix = f" (after {attempt} attempts)" if attempt > 1 else ""
                raise TranscriptionError(
                    f"piece {index}: {exc}{suffix}", reason=exc.reason, code=exc.code
                ) from exc
            sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S)) - 1])


def transcribe_episode(
    audio_path: Path,
    work_dir: Path,
    *,
    rss_duration_s: int | None,
    description: str,
    hosts: tuple[str, ...],
    transcriber: Callable[[bytes, str], PieceResult],
    carry_context: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Transcribe ``audio_path``; write ``transcript.txt`` + ``transcript.json``.

    Pieces run sequentially (later prompts depend on earlier output when
    ``carry_context``). Each accepted piece is checkpointed under
    ``work_dir/pieces/`` so a rerun after a failure does not repeat the paid
    work. Raises ``TranscriptionError`` on any failure, leaving neither final
    output file behind.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_sha = _sha256_file(audio_path)
    model = str(getattr(transcriber, "model", "unknown"))
    inputs_sha = _prompt_inputs_sha(hosts, description)
    policy_sha = transcription_policy_digest(model)

    reused = _load_reusable(
        work_dir,
        audio_sha=audio_sha,
        model=model,
        carry_context=carry_context,
        prompt_inputs_sha=inputs_sha,
        policy_sha=policy_sha,
        rss_duration_s=rss_duration_s,
    )
    if reused is not None:
        logger.info("reusing transcript already in %s", work_dir)
        return reused

    # Not reusable: drop anything stale first, so a failed run cannot leave a
    # transcript.txt that belongs to different audio or an older prompt.
    (work_dir / MANIFEST_NAME).unlink(missing_ok=True)
    (work_dir / TRANSCRIPT_NAME).unlink(missing_ok=True)

    try:
        pcm, ffmpeg_warnings = decode_to_pcm_with_log(audio_path)
    except RuntimeError as exc:
        raise TranscriptionError(f"audio decode failed: {exc}") from exc
    if not pcm:
        raise TranscriptionError("audio decode produced no samples")
    decoded_s = len(pcm) / PCM_BYTES_PER_SECOND
    skipped = _check_duration(decoded_s, rss_duration_s)

    ranges = split_pcm(pcm, target_s=PIECE_TARGET_S, search_s=PIECE_SEARCH_S)
    expected_start = 0
    for start, end in ranges:
        if start != expected_start or end <= start:
            raise TranscriptionError("segmentation produced non-contiguous pieces")
        expected_start = end
    if expected_start != len(pcm):
        raise TranscriptionError("segmentation did not cover the whole audio")

    count = len(ranges)
    pieces: list[dict[str, Any]] = []
    texts: list[str] = []
    speakers: list[str] = []  # names seen in earlier output; hosts are not seeded
    sponsor_open = False
    previous = ""
    for i, (start, end) in enumerate(ranges):
        piece_started = time.monotonic()
        context = (
            _context_block(previous, speakers) if (carry_context and i > 0) else None
        )
        prompt = build_piece_prompt(
            index=i,
            count=count,
            hosts=hosts,
            description=description,
            context=context,
        )
        key = _checkpoint_key(audio_sha, start, end, policy_sha, prompt)
        cp_path = _checkpoint_path(work_dir, i)
        cp = _load_checkpoint(cp_path, key)
        if cp is not None and _piece_rate_error(
            i, len(cp["text"].split()), (end - start) / PCM_BYTES_PER_SECOND
        ):
            # Stored text that fails today's rules is a miss, not a reuse: ask
            # again (and refuse if the fresh answer fails too).
            cp = None
        if cp is not None:
            result = PieceResult(
                cp["text"],
                cp["finish_reason"],
                _int_or_none(cp.get("input_tokens")),
                _int_or_none(cp.get("output_tokens")),
            )
            mp3_sha, mp3_bytes = cp["mp3_sha256"], cp["bytes"]
            attempts = _int_or_none(cp.get("attempts")) or 1
        else:
            mp3 = encode_piece(pcm[start:end])
            if len(mp3) > MAX_PIECE_BYTES:
                raise TranscriptionError(
                    f"piece {i}: encoded size {len(mp3)} bytes exceeds "
                    f"{MAX_PIECE_BYTES} bytes"
                )
            result, attempts = _transcribe_with_retry(
                transcriber, mp3, prompt, index=i, sleep=sleep
            )
            mp3_sha, mp3_bytes = hashlib.sha256(mp3).hexdigest(), len(mp3)
        if result.finish_reason != "STOP":
            raise TranscriptionError(
                f"piece {i}: asr_incomplete: finish_reason={result.finish_reason}",
                reason="asr_incomplete",
            )
        text = result.text.strip()
        words = len(text.split())
        piece_s = (end - start) / PCM_BYTES_PER_SECOND
        wpm = words / (piece_s / 60)
        rate_error = _piece_rate_error(i, words, piece_s)
        if rate_error is not None:
            raise rate_error
        if cp is None:
            _atomic_write(
                cp_path,
                json.dumps(
                    {
                        "key": key,
                        "text": text,
                        "finish_reason": result.finish_reason,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "attempts": attempts,
                        "mp3_sha256": mp3_sha,
                        "bytes": mp3_bytes,
                    }
                ),
            )
        names, sponsor_open = _speakers_in(text, sponsor_open)
        for name in names:
            if name not in speakers:
                speakers.append(name)
        logger.info(
            "piece %d/%d: %.1fs, %s, %d words",
            i + 1,
            count,
            time.monotonic() - piece_started,
            "from checkpoint" if cp is not None else f"{attempts} attempt(s)",
            words,
        )
        previous = text
        texts.append(text)
        pieces.append(
            {
                "index": i,
                "start_s": start / PCM_BYTES_PER_SECOND,
                "end_s": end / PCM_BYTES_PER_SECOND,
                "mp3_sha256": mp3_sha,
                "bytes": mp3_bytes,
                "finish_reason": result.finish_reason,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "words": words,
                "words_per_min": wpm,
                "attempts": attempts,
                "from_checkpoint": cp is not None,
                "text": text,
            }
        )

    transcript = "\n".join(texts)
    manifest = {
        "audio_sha256": audio_sha,
        "rss_duration_s": rss_duration_s,
        "decoded_duration_s": decoded_s,
        "duration_check_skipped": skipped,
        "ffmpeg_warnings": ffmpeg_warnings,
        "prompt_version": PIECE_PROMPT_VERSION,
        "prompt_inputs_sha256": inputs_sha,
        "policy_sha256": policy_sha,
        "model": model,
        "carry_context": carry_context,
        "transcript_sha256": _sha256_text(transcript),
        "pieces": pieces,
    }
    # Transcript first, manifest last: the manifest's transcript_sha256 makes a
    # crash between the two writes a reuse miss, never a wrong reuse.
    _atomic_write(work_dir / TRANSCRIPT_NAME, transcript)
    _atomic_write(work_dir / MANIFEST_NAME, json.dumps(manifest, indent=2))
    return transcript
