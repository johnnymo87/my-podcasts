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

    This function is total and never raises: any input produces some string.
    Whether that string is safe to publish is a separate question, answered
    by the leaked-markup guard in ``run_report_prompt`` -- not here. Three
    real production defects converge in this function, which is why it lives
    in exactly one place instead of being copy-pasted per writer.

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

    Tag matching is case-insensitive throughout (``<SCRIPT>`` is as valid as
    ``<script>``), including the mangled-tail cleanup regex and the no-tag
    fallback's tag stripping.

    Known limitation, deliberately not engineered around: a ``<script>``
    nested inside another ``<script>`` (e.g.
    ``<script>outer <script>inner</script> tail</script>``) is not parsed as
    real HTML/XML would be -- the outer tag's content is truncated at the
    *first* closing tag seen, silently dropping the text after it. This is
    now caught loudly rather than silently: the truncated result still
    contains a literal ``<script>`` substring, which trips the leaked-markup
    guard in ``run_report_prompt``.

    What no longer needs a guard here: a malformed result that still
    contains a literal ``<script>``/``<summary>`` tag (an unclosed
    ``<summary>`` with no ``<script>`` tags at all, or the nested-tag case
    above) is caught by ``run_report_prompt``'s leaked-markup check, which
    runs after this function returns. This function does not special-case
    that input -- it would only duplicate a check that already exists at the
    boundary that owns ``label`` and the other publish-path refusals.
    """
    candidates = re.findall(
        r"<script>\s*(.*?)\s*</script>", text, re.DOTALL | re.IGNORECASE
    )
    open_tags = list(re.finditer(r"<script>", text, re.IGNORECASE))
    close_tags = list(re.finditer(r"</script>", text, re.IGNORECASE))
    last_open_end = open_tags[-1].end() if open_tags else -1
    last_open_start = open_tags[-1].start() if open_tags else -1
    last_close_start = close_tags[-1].start() if close_tags else -1
    if open_tags and last_open_start > last_close_start:
        tail = text[last_open_end:].strip()
        tail = re.sub(r"</?scr[a-z]*[^>]*>?\s*$", "", tail, flags=re.IGNORECASE).strip()
        candidates.append(tail)
    if candidates:
        return max(candidates, key=len).strip()
    fallback = re.sub(
        r"<summary>.*?</summary>", "", text, flags=re.DOTALL | re.IGNORECASE
    )
    fallback = re.sub(r"</?script>", "", fallback, flags=re.IGNORECASE)
    return fallback.strip()


def extract_summary(text: str) -> str:
    """Extract the ``<summary>`` block, returning an empty string if absent.

    Picks the **longest** matched block, mirroring ``extract_script``'s
    defense: the same "placeholder while planning" model behavior that
    motivated that fix for ``<script>`` is not specific to the script tag,
    so this applies the identical defense for symmetry rather than leaving
    the asymmetry (and the risk) undocumented.

    Unlike ``extract_script``, this does not attempt unclosed-tail recovery.
    A lost or truncated summary degrades the episode's feed ``<description>``
    text, not the audio itself, and an unclosed ``<summary>`` with no
    ``<script>`` tags is caught by ``run_report_prompt``'s leaked-markup
    guard regardless of what this function returns.

    Tag matching is case-insensitive, matching ``extract_script``.
    """
    candidates = re.findall(
        r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL | re.IGNORECASE
    )
    if not candidates:
        return ""
    return max(candidates, key=len).strip()


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

    A leaked-markup guard runs after the emptiness check and before
    ``min_chars``: ``extract_script`` is a total function (see its
    docstring) that can return text still containing a literal
    ``<script>``/``<summary>`` tag when the model's tag structure was
    malformed (an unclosed ``<summary>`` with no ``<script>`` tags at all, or
    a nested ``<script>``). That text is not empty and can be arbitrarily
    long, so it would otherwise clear both the emptiness check and any
    ``min_chars`` floor and reach TTS with raw markup narrated aloud. It runs
    before ``min_chars`` deliberately: a leaked-markup script is disqualified
    on its own terms regardless of length, so a caller should never see a
    "too short" message for a script that was actually rejected for
    malformed tags.
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
        if re.search(r"</?(?:script|summary)\b", script, re.IGNORECASE):
            raise RuntimeError(
                f"{label} report writer returned a script with leaked "
                "markup; the model's tag structure was malformed"
            )
        if min_chars and len(script.strip()) < min_chars:
            raise RuntimeError(
                f"{label} report writer returned a script too short to be "
                f"real: {len(script.strip())} chars (minimum {min_chars})"
            )
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
