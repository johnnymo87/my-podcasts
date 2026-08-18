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

       That recovery was originally unconditional -- ANY trailing
       ``<script>`` with no matching close was treated as an unclosed block,
       regardless of whether a perfectly good, properly-closed script already
       existed. Adversarial review found this was ITSELF a silent-wrong path:
       a well-formed real script followed by model chatter that merely
       mentions ``<script>`` in passing (e.g. "Note: I wrapped it in
       <script>...") let that chatter compete on length and win. That is the
       same improbability class as defect 1 -- models chattering around tags
       is proven production behavior, not a hypothetical -- so the recovery
       is now conditional: the tail is added as a candidate only when either
       (a) the mangled-tag cleanup regex actually stripped something from it
       (the ne0 shape: a real mangled closing tag like ``</scrip>``), or (b)
       there are no well-formed candidates at all (nothing else to prefer).
       A true "placeholder plus never-closed, no mangled tag" case now falls
       through to just the placeholder candidate, which is short enough to
       trip ``min_chars`` downstream -- a loud failure (no episode) instead
       of a silent wrong one (wrong episode), which is the correct direction
       for this pipeline.

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
    guard in ``parse_report``.

    What no longer needs a guard here: a malformed result that still
    contains a literal ``<script>``/``<summary>``/``<covered>`` tag (an
    unclosed ``<summary>`` with no ``<script>`` tags at all, or the
    nested-tag case above) is caught by ``parse_report``'s leaked-markup
    check, which runs after this function returns. This function does not
    special-case that input -- it would only duplicate a check that already
    exists at the boundary that owns ``label`` and the other publish-path
    refusals.
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
        tail_before = text[last_open_end:].strip()
        tail_after = re.sub(
            r"</?scr[a-z]*[^>]*>?\s*$", "", tail_before, flags=re.IGNORECASE
        ).strip()
        mangled_tag_was_stripped = tail_after != tail_before
        # Only trust this tail as the real script when it carries positive
        # evidence of a mis-closed tag (ne0), or when there is nothing else
        # to prefer. Otherwise a stray mention of "<script>" in trailing
        # model chatter would compete on length against a real, well-formed
        # script and could win -- see the docstring above.
        if mangled_tag_was_stripped or not candidates:
            candidates.append(tail_after)
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
    ``<script>`` tags is caught by ``parse_report``'s leaked-markup guard
    regardless of what this function returns.

    Tag matching is case-insensitive, matching ``extract_script``.
    """
    candidates = re.findall(
        r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL | re.IGNORECASE
    )
    if not candidates:
        return ""
    return max(candidates, key=len).strip()


def fetch_report_text(instruction: str, *, label: str) -> str:
    """Run one instruction through opencode-serve and return the raw reply.

    Owns the session lifecycle ONLY -- create session, send ``instruction``,
    wait up to ``WRITER_TIMEOUT_SECONDS`` for the reply, read the last
    assistant message, ``.strip()`` it, and return it verbatim. It does no
    parsing and applies none of the publish-boundary refusals; those live in
    ``parse_report``. The session is always deleted in a ``finally``, on
    success, on timeout, or on any exception raised while talking to
    opencode-serve.

    This function exists as its own seam (rather than being fused with
    parsing, as it used to be inside a single ``run_report_prompt``) so a
    caller that needs to persist the raw model output before it is parsed --
    e.g. to retry a parse failure without re-paying a 900-second model call
    -- has somewhere to interpose. See
    ``docs/plans/2026-08-18-daily-writer-migration-plan.md`` for the callers
    that need exactly this (the daily writers).

    ``label`` names the caller (a feed slug or writer style) in the raised
    timeout error, so a failure log line points at the right pipeline.
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
        return get_last_assistant_text(messages).strip()
    finally:
        delete_session(session_id)


def parse_report(
    text: str,
    *,
    label: str,
    min_chars: int = 0,
    require_tags: bool = False,
) -> ReportOutput:
    """Parse raw model output into a ``ReportOutput``, or refuse to publish it.

    Owns extraction (``extract_script`` / ``extract_summary``) PLUS every
    publish-boundary refusal, in this order: ``require_tags`` (checked
    against the raw ``text``, before extraction), extract script, extract
    summary, refuse empty, refuse leaked markup, refuse below ``min_chars``.

    **Why every refusal lives inside this function, with no bypass.** This
    is the one public entry point for turning raw model text into something
    safe to hand to TTS. There is deliberately no "parse without the guard"
    function and no flag that disables a refusal -- a caller that wants to
    interpose (e.g. to persist raw output between fetch and parse, see
    ``fetch_report_text``) still has to route back through this same guarded
    function to get a ``ReportOutput``. The hazard being closed is a future
    caller composing ``fetch_report_text`` with its own hand-rolled
    extraction and silently dropping a refusal -- the exact drift that
    produced beads ``78b`` (a second, drifted assembly implementation) and
    ``ne0`` (a 3-character placeholder shipped as an episode because nothing
    downstream re-checked it).

    ``label`` names the caller in every raised error message.

    ``min_chars`` is an opt-in plausibility floor for callers whose output
    goes straight to TTS. It defaults to 0 -- no floor -- so callers that
    review output before publishing (like the one-off ``report_writer``
    path) are unaffected. Emptiness alone is the wrong test on a publish
    path: the 2026-08-18 incident's placeholder was a 3-character string,
    not empty.

    The leaked-markup guard runs after the emptiness check and before
    ``min_chars``: ``extract_script`` is a total function (see its
    docstring) that can return text still containing a literal
    ``<script>``/``<summary>``/``<covered>`` tag when the model's tag
    structure was malformed (an unclosed ``<summary>`` with no ``<script>``
    tags at all, a nested ``<script>``, or a ``<covered>`` block that leaked
    through untouched). That text is not empty and can be arbitrarily long,
    so it would otherwise clear both the emptiness check and any
    ``min_chars`` floor and reach TTS with raw markup narrated aloud. It runs
    before ``min_chars`` deliberately: a leaked-markup script is disqualified
    on its own terms regardless of length, so a caller should never see a
    "too short" message for a script that was actually rejected for
    malformed tags.

    **Why ``covered`` is hardcoded into the guard rather than a parameter.**
    The guard's semantics are "known pipeline markup must never reach TTS,"
    which is a universal property of every caller of this module, not a
    per-caller preference -- a caller that forgot to pass ``covered`` into a
    parameterized tag set would get a silent hole for zero benefit. Measured
    gap that motivated adding it: the input
    ``'<summary>s</summary><covered>- h1</covered> ' + long_text`` (no
    ``<script>`` tags at all) used to return a script containing the literal
    ``<covered>`` block, which ``strip_markdown_for_tts`` does not remove, so
    it was narrated aloud. Only the daily writers (``rundown_writer`` /
    ``fp_writer``) emit ``<covered>``, but the guard protects the shared
    module, not one caller.

    **Why ``require_tags`` defaults to ``False`` rather than being the
    module-wide behavior.** When no ``<script>`` tag is present at all,
    ``extract_script``'s fallback returns essentially the raw model output
    (with the ``<summary>`` block and stray tags stripped) -- measured, 6522
    chars in and 6521 chars out, i.e. cosmetic. That means the model's raw
    *reasoning* gets narrated to subscribers, and ``min_chars`` can never
    catch it because raw output is long. ``report_writer`` (one-off,
    operator-reviewed) relies on exactly this permissive fallback today --
    an operator reviews the dry-run output before publishing, so the
    fallback is a convenience, not a live-feed hazard. Automated,
    no-human-in-the-loop publish paths (the daily writers, the transcript
    report path) should pass ``require_tags=True`` to convert that fallback
    into a loud refusal instead.
    """
    if require_tags and not re.search(r"<script>", text, re.IGNORECASE):
        raise RuntimeError(
            f"{label} report writer returned no <script> tag at all; "
            "refusing rather than falling back to narrating raw model "
            "output (require_tags=True for this caller)"
        )
    script = extract_script(text)
    summary = extract_summary(text)
    if not script.strip():
        raise RuntimeError(f"{label} report writer returned empty script")
    if re.search(r"</?(?:script|summary|covered)\b", script, re.IGNORECASE):
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


def run_report_prompt(
    instruction: str,
    *,
    label: str,
    min_chars: int = 0,
    require_tags: bool = False,
) -> ReportOutput:
    """Run one instruction through opencode-serve and parse the result.

    This is literally the composition of ``fetch_report_text`` (session
    lifecycle) and ``parse_report`` (extraction plus every publish-boundary
    refusal) -- nothing else happens here. It exists so existing callers
    (``transcript_report.py``, ``report_writer.py``) keep one call with an
    unchanged signature and unchanged behavior; a caller that needs to
    interpose between fetch and parse (to persist raw output, for retries
    that skip the model call) composes the two functions itself instead of
    calling this one, and still goes through ``parse_report``'s guards
    either way.

    See ``fetch_report_text`` and ``parse_report`` for what each half does
    and why the refusals are where they are.
    """
    return parse_report(
        fetch_report_text(instruction, label=label),
        label=label,
        min_chars=min_chars,
        require_tags=require_tags,
    )
