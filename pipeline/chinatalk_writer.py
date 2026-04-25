from __future__ import annotations

from dataclasses import dataclass


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
