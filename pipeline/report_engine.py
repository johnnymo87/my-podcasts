"""Shared opencode-serve mechanics for every spoken-briefing writer.

These mechanics -- create a session, send one instruction, wait for idle,
read the last assistant message, pull ``<summary>``/``<script>`` out of it,
always delete the session -- existed in four near-identical copies:
``pipeline/chinatalk_writer.py``, ``pipeline/yglesias_writer.py``,
``pipeline/report_writer.py``, and a variant in ``pipeline/rundown_writer.py``.
This module is their single home. Later migrations delegate each of those
writers to ``run_report_prompt`` instead of re-implementing it.

Leaf module: imports only ``pipeline.opencode_client`` (plus stdlib), so any
writer can depend on this module without an import cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.opencode_client import (
    create_session,
    delete_session,
    get_last_assistant_text,
    get_messages,
    send_prompt_async,
    wait_for_idle,
)


WRITER_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class ReportOutput:
    script: str
    summary: str


def extract_script(text: str) -> str:
    """Extract the spoken script from ``<script>...</script>`` tags.

    Three real production defects converge here, which is why this lives in
    exactly one place instead of being copy-pasted per writer.

    1. **Placeholder selection.** The model sometimes emits a placeholder
       ``<script>...</script>`` while planning, before writing the real one.
       A non-greedy ``re.search`` matched that 3-character placeholder and FP
       Digest shipped a 2636-byte mp3 on 2026-08-18. The fix -- pick the
       **longest** matched block, not the first -- landed only in
       ``rundown_writer.py`` (commit 39589e3) and nowhere else.

    2. **Unclosed final block** (bead ``my-podcasts-ne0``). On 2026-06-16 the
       model closed the script with a mangled tag (e.g. ``</scrip>``),
       ``re.findall`` matched nothing, and the old fallback returned the
       ENTIRE model output for narration -- including the model's own
       reasoning and any ``<summary>``/tag text. This **composes** with
       defect 1: a well-formed placeholder pair followed by a mis-closed real
       script leaves ``findall`` seeing only the placeholder, so the
       recovered unclosed tail must be added as a candidate and compete on
       length -- it cannot be a last resort that only fires when the
       candidate list is empty.

    3. **No-tag fallback leaking summary prose.**
       ``pipeline/script_processor.py:strip_markdown_for_tts`` does not strip
       HTML, so when there is no ``<script>`` tag at all, returning the raw
       model output verbatim narrates the ``<summary>`` block and any stray
       literal tags aloud. The fallback here strips the ``<summary>`` block
       and any leftover ``<script>``/``</script>`` tags before returning.

    Residual known ambiguity, left alone rather than engineered around: an
    unclosed ``<summary>`` block with no ``<script>`` tags at all is
    ambiguous input from the model. It is not specially detected here --
    instead it is caught loudly downstream by the emptiness and ``min_chars``
    guards in ``run_report_prompt``, which is the right place to fail a
    malformed generation rather than silently guessing at intent.
    """
    candidates = re.findall(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    last_open = text.rfind("<script>")
    if last_open != -1 and last_open > text.rfind("</script>"):
        tail = text[last_open + len("<script>") :].strip()
        tail = re.sub(r"</?scr[a-z]*[^>]*>?\s*$", "", tail).strip()
        candidates.append(tail)
    if candidates:
        return max(candidates, key=len).strip()
    fallback = re.sub(r"<summary>.*?</summary>", "", text, flags=re.DOTALL)
    return fallback.replace("<script>", "").replace("</script>", "").strip()


def extract_summary(text: str) -> str:
    """Extract the ``<summary>`` block, returning an empty string if absent."""
    m = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def run_report_prompt(
    instruction: str,
    *,
    label: str,
    min_chars: int = 0,
) -> ReportOutput:
    """Run one instruction through opencode-serve and parse the result.

    Creates a session, sends ``instruction``, waits up to
    ``WRITER_TIMEOUT_SECONDS`` for the reply, extracts the script and summary,
    and always deletes the session -- on success, on timeout, or on any
    extraction failure.

    ``label`` names the caller (a feed slug or writer style) in every raised
    error message, so a failure log line points at the right pipeline.

    ``min_chars`` is an opt-in plausibility floor for callers whose output
    goes straight to TTS. It defaults to 0 -- no floor -- so callers that
    review output before publishing (like the one-off ``report_writer`` path)
    are unaffected. Emptiness alone is the wrong test on a publish path: the
    2026-08-18 incident's placeholder was a 3-character string, not empty.
    """
    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)
        if not wait_for_idle(session_id, timeout=WRITER_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"{label} report writer did not complete within "
                f"{WRITER_TIMEOUT_SECONDS} seconds"
            )
        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        script = extract_script(full_text)
        summary = extract_summary(full_text)
        if not script.strip():
            raise RuntimeError(f"{label} report writer returned empty script")
        if min_chars and len(script.strip()) < min_chars:
            raise RuntimeError(
                f"{label} report writer returned a script too short to be "
                f"real: {len(script.strip())} chars (minimum {min_chars})"
            )
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
