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


PROMPT_TEMPLATE = """\
You are writing a spoken briefing about a podcast conversation that ran
in the ChinaTalk newsletter today. Your listener does NOT want to hear
the transcript read aloud — they want a clear, structured report on
what was said.

Subject line: {subject}

Below is the full transcript. Read it, then produce a 5–10 minute
spoken briefing (roughly 800–1500 words) covering:

- Who participated (host and guests, with affiliations if stated).
- What topics were discussed, in the order that best illuminates the
  conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("Schneider argued...", "the guest pushed back,
  saying...").
- Any notable disagreements or points of tension.
- Concrete details, numbers, and examples that gave the conversation
  weight.

Write for the ear: plain spoken English, no markdown, no bullet
points, no headers. Use natural transitions. You are a smart friend
explaining what a podcast got into, not reading a summary out loud.
Do not editorialize beyond what the participants themselves said,
and do not invent facts.

TRANSCRIPT:

{body}
"""


@dataclass(frozen=True)
class ReportOutput:
    script: str
    summary: str


def build_report_prompt(*, body: str, subject: str) -> str:
    return PROMPT_TEMPLATE.format(subject=subject, body=body)


def _extract_script(text: str) -> str:
    """Extract the spoken script from ``<script>...</script>`` tags.

    If the tag is absent (model didn't follow the format), the full
    response is returned as-is. The empty-script guard in
    ``generate_report`` will reject the result if it is whitespace only.
    """
    m = re.search(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _extract_summary(text: str) -> str:
    """Extract the ``<summary>`` block, returning an empty string if absent."""
    m = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def generate_report(*, body: str, subject: str) -> ReportOutput:
    """Generate a spoken-briefing report on a chinatalk transcript."""
    prompt = build_report_prompt(body=body, subject=subject)
    instruction = (
        "Read the following transcript and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n"
        + prompt
    )

    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)
        if not wait_for_idle(session_id, timeout=900):
            raise RuntimeError(
                "chinatalk report writer did not complete within 900 seconds"
            )
        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        script = _extract_script(full_text)
        summary = _extract_summary(full_text)
        if not script.strip():
            raise RuntimeError("chinatalk report writer returned empty script")
        return ReportOutput(script=script, summary=summary)
    finally:
        delete_session(session_id)
