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
import os
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.genai import types

from pipeline.tts import asr as _asr
from pipeline.tts.config import PCM_BYTES_PER_SECOND, PCM_SAMPLE_RATE
from pipeline.tts.segment import decode_to_pcm_with_log, split_pcm


PIECE_PROMPT_VERSION = "1"
ASR_MODEL = "gemini-3.8-flash"
MAX_PIECE_BYTES = 15 * 1024 * 1024
MIN_WORDS_PER_MIN = 80
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
    pass


@dataclass(frozen=True)
class PieceResult:
    text: str
    finish_reason: str
    input_tokens: int | None
    output_tokens: int | None


# --- encoding -----------------------------------------------------------------


def encode_piece(pcm: bytes) -> bytes:
    """Encode 24 kHz mono s16le PCM to a 64 kbps mono mp3 (ffmpeg, via pipes)."""
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-b:a",
        "64k",
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
self-introduction, by being addressed by name, or by the speaker list carried
over from earlier pieces below. Otherwise label it "Unknown speaker A",
"Unknown speaker B", and keep those labels consistent within this piece. Never
guess who is speaking from the opinions expressed.

Wrap advertisements and sponsor reads in [SPONSOR] ... [/SPONSOR] on their own
lines. No timestamps, headings, or commentary.
{context_block}"""

_SPEAKER_RE = re.compile(r"^([A-Za-z][^:\[\]\n]{0,58}):\s")


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


def _speakers_in(text: str) -> list[str]:
    """Named speakers, in order of first appearance; sponsor reads and
    ``Unknown speaker *`` labels are not speakers."""
    seen: list[str] = []
    in_sponsor = False
    for line in text.splitlines():
        if "[SPONSOR]" in line:
            in_sponsor = "[/SPONSOR]" not in line
            continue
        if "[/SPONSOR]" in line:
            in_sponsor = False
            continue
        if in_sponsor:
            continue
        m = _SPEAKER_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        if name.lower().startswith("unknown speaker") or len(name.split()) > 5:
            continue
        if name not in seen:
            seen.append(name)
    return seen


def _context_block(previous_text: str, speakers: list[str]) -> str:
    lines = [ln for ln in previous_text.splitlines() if ln.strip()]
    tail = "\n".join(lines[-CONTEXT_LINES:])
    return (
        "Context from the end of the previous piece (do not repeat it; use it "
        "to keep speaker names consistent):\n"
        f"{tail}\n"
        f"Speakers identified so far: {', '.join(speakers)}"
    )


# --- Gemini -------------------------------------------------------------------


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.LOW
                    ),
                ),
            )
        except Exception as exc:  # any SDK/transport failure = no transcript
            raise TranscriptionError(f"asr_error: {exc!r}") from exc

        if not resp.candidates:
            feedback = getattr(resp, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None)
            detail = "no candidates"
            if block is not None:
                detail += f" (block_reason={getattr(block, 'name', block)})"
            raise TranscriptionError(f"asr_empty: {detail}")
        finish = resp.candidates[0].finish_reason
        finish_name = finish.name if finish is not None else "NONE"
        if finish_name != "STOP":
            raise TranscriptionError(f"asr_incomplete: finish_reason={finish_name}")
        text = resp.text or ""
        if not text.strip():
            raise TranscriptionError("asr_empty: blank transcript")
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


def _load_reusable(
    work_dir: Path, *, audio_sha: str, model: str, carry_context: bool
) -> str | None:
    """The stored transcript, iff BOTH files exist and the manifest matches this
    audio, prompt version, model and carry_context, and transcript.txt is the
    text the manifest recorded. Anything else is a miss (re-transcribe)."""
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
        or manifest.get("transcript_sha256") != _sha256_text(text)
    ):
        return None
    return text


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


def transcribe_episode(
    audio_path: Path,
    work_dir: Path,
    *,
    rss_duration_s: int | None,
    description: str,
    hosts: tuple[str, ...],
    transcriber: Callable[[bytes, str], PieceResult],
    carry_context: bool = True,
) -> str:
    """Transcribe ``audio_path``; write ``transcript.txt`` + ``transcript.json``.

    Pieces run sequentially (later prompts depend on earlier output when
    ``carry_context``). Raises ``TranscriptionError`` on any failure, leaving
    neither output file behind.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_sha = _sha256_file(audio_path)
    model = str(getattr(transcriber, "model", "unknown"))

    reused = _load_reusable(
        work_dir, audio_sha=audio_sha, model=model, carry_context=carry_context
    )
    if reused is not None:
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
    speakers: list[str] = []
    previous = ""
    for i, (start, end) in enumerate(ranges):
        mp3 = encode_piece(pcm[start:end])
        if len(mp3) > MAX_PIECE_BYTES:
            raise TranscriptionError(
                f"piece {i}: encoded size {len(mp3)} bytes exceeds "
                f"{MAX_PIECE_BYTES} bytes"
            )
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
        try:
            result = transcriber(mp3, prompt)
        except TranscriptionError as exc:
            raise TranscriptionError(f"piece {i}: {exc}") from exc
        if result.finish_reason != "STOP":
            raise TranscriptionError(
                f"piece {i}: asr_incomplete: finish_reason={result.finish_reason}"
            )
        text = result.text.strip()
        words = len(text.split())
        minutes = (end - start) / PCM_BYTES_PER_SECOND / 60
        floor = MIN_WORDS_PER_MIN * minutes
        if words < floor:
            raise TranscriptionError(
                f"piece {i}: only {words} words in {minutes:.1f} min "
                f"(floor {floor:.0f}); transcript is likely incomplete"
            )
        for name in _speakers_in(text):
            if name not in speakers:
                speakers.append(name)
        previous = text
        texts.append(text)
        pieces.append(
            {
                "index": i,
                "start_s": start / PCM_BYTES_PER_SECOND,
                "end_s": end / PCM_BYTES_PER_SECOND,
                "mp3_sha256": hashlib.sha256(mp3).hexdigest(),
                "bytes": len(mp3),
                "finish_reason": result.finish_reason,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "words": words,
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
