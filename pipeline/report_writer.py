from __future__ import annotations

from pipeline.report_engine import ReportOutput, run_report_prompt


__all__ = ["ReportOutput", "build_report_prompt", "generate_report"]


_INTERVIEW_TEMPLATE = """\
You are writing a spoken briefing about a long-form interview podcast.
Your listener does NOT want to hear the transcript read aloud — they want
a clear, structured report on what was discussed.

The post may open with a short written introduction before the transcript
begins; use it for context but focus your report on the conversation itself.

Title: {subject}
{byline}
Below is the transcript. Read it, then produce a spoken briefing
(roughly 1200–2000 words) covering:

- Who participated (host and guest, with affiliations if stated).
- The main themes and questions explored, in the order that best
  illuminates the conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("the guest argued...", "the host pushed back, asking...").
- Notable disagreements, uncertainties, or surprising points.
- Concrete details, numbers, names, and examples that gave the
  conversation weight.

Write for the ear: plain spoken English, no markdown, no bullet points,
no headers. Use natural transitions. You are a smart friend explaining
what an interview got into, not reading a summary out loud. Do not
editorialize beyond what the participants themselves said, and do not
invent facts.

SOURCE TEXT:

{body}
"""

_PAPER_TEMPLATE = """\
You are writing a spoken briefing about an academic research paper.
Your listener wants to understand what the paper argues and why it matters,
without reading it or hearing it read aloud verbatim.

Title: {subject}
{byline}
Below is the paper's text. References and equations have been removed and
figure/table bodies have been dropped, though brief captions may remain.
Read it, then produce a spoken briefing (roughly 1200–2000 words) covering:

- The authors and the research question or problem the paper takes on.
- The paper's central claims and contributions.
- The method, model, or framework used to argue them.
- The key findings or results, with the concrete details that matter.
- Why the work is significant, and any caveats, assumptions, or
  limitations the authors acknowledge.

Write for the ear: plain spoken English, no markdown, no bullet points,
no headers, no LaTeX or math notation. Explain technical ideas plainly,
as a knowledgeable friend would. Do not overstate the results and do not
invent findings the paper does not make.

SOURCE TEXT:

{body}
"""

_TEMPLATES = {"interview": _INTERVIEW_TEMPLATE, "paper": _PAPER_TEMPLATE}


def build_report_prompt(
    *, body: str, subject: str, style: str = "interview", byline: str = ""
) -> str:
    try:
        template = _TEMPLATES[style]
    except KeyError:
        raise ValueError(f"Unknown report style: {style!r}") from None
    byline_line = f"Authors/Participants: {byline}\n" if byline else ""
    return template.format(subject=subject, body=body, byline=byline_line)


def generate_report(
    *, body: str, subject: str, style: str = "interview", byline: str = ""
) -> ReportOutput:
    """Generate a spoken-briefing report on a source document."""
    prompt = build_report_prompt(body=body, subject=subject, style=style, byline=byline)
    instruction = (
        "Read the following source text and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n" + prompt
    )
    return run_report_prompt(instruction, label="report")
