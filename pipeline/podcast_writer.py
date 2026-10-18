"""Spoken-briefing writer for audio-derived podcast transcripts (The Argument).

Composes ``report_engine.fetch_report_text`` (session lifecycle) and
``report_engine.parse_report`` (extraction + every publish-boundary refusal)
itself, rather than calling ``run_report_prompt``, so the model's raw reply can
be persisted between the two -- the same shape as ``rundown_writer`` and
``fp_writer``. A retry then re-parses the saved reply instead of paying for
another 900-second generation (``WRITER_TIMEOUT_SECONDS``, owned by
``report_engine``).

The raw-output file name carries a key over everything that shaped the prompt
(transcript, title, description, prompt version). The daily writers use one
fixed file name because their work dir is per-job; a podcast work dir may see
several transcripts or prompt revisions, and a reply written for one must never
be replayed for another.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline.report_engine import (
    ReportOutput,
    fetch_report_text,
    parse_report,
    persist_raw_output,
)
from pipeline.transcript_report import (
    AUDIO_REPORT_PROMPT_VERSION,
    build_audio_report_instruction,
)


LABEL = "argument"

# Same floor as the transcript path (see transcript_report._MIN_SCRIPT_CHARS):
# the prompt asks for 800-1500 words.
MIN_SCRIPT_CHARS = 2000


def _raw_key(transcript: str, title: str, description: str) -> str:
    # JSON-encode so field boundaries are unambiguous ("ab"+"c" != "a"+"bc").
    payload = json.dumps(
        [AUDIO_REPORT_PROMPT_VERSION, title, description, transcript],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def write_report(
    *, transcript: str, title: str, description: str, work_dir: Path
) -> ReportOutput:
    """Generate (or re-parse a persisted) spoken report for one transcript.

    A refusal from ``parse_report`` (a ``RuntimeError``) deletes the persisted
    raw reply before re-raising, so the next retry regenerates instead of
    looping on the same broken content.
    """
    raw_path = (
        work_dir / f"raw_writer_output-{_raw_key(transcript, title, description)}.txt"
    )

    if raw_path.exists():
        full_text = raw_path.read_text(encoding="utf-8")
    else:
        instruction = build_audio_report_instruction(
            transcript=transcript, title=title, description=description
        )
        full_text = fetch_report_text(instruction, label=LABEL)
        persist_raw_output(raw_path, full_text)

    try:
        # require_tags=True: automated, no-human-in-the-loop path -- a missing
        # <script> must be a loud refusal, not a narration of raw reasoning.
        return parse_report(
            full_text, label=LABEL, min_chars=MIN_SCRIPT_CHARS, require_tags=True
        )
    except RuntimeError:
        try:
            raw_path.unlink()
        except FileNotFoundError:
            pass
        raise
