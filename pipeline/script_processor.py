from __future__ import annotations

import re

import markdown as md_lib


def strip_markdown_for_tts(text: str) -> str:
    """Strip markdown formatting from script text for TTS input.

    Removes headings, horizontal rules, bold/italic markers, and end markers.
    Preserves the actual content text.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Skip horizontal rules
        if re.match(r"^-{3,}$", stripped):
            continue
        # Skip end-of-script marker
        if re.match(r"^\*?\[END OF SCRIPT\]\*?$", stripped):
            continue
        # Strip heading markers
        line = re.sub(r"^#{1,6}\s+", "", line)
        # Strip bold/italic markers (*** first, then **, then *)
        line = re.sub(r"\*{3}(.+?)\*{3}", r"\1", line)
        line = re.sub(r"\*{2}(.+?)\*{2}", r"\1", line)
        line = re.sub(r"\*(.+?)\*", r"\1", line)
        result_lines.append(line)
    return "\n".join(result_lines)


def extract_summary(show_notes_md: str) -> str | None:
    """Extract the Episode Summary section from show notes markdown.

    Looks for a '## Episode Summary' heading and returns the text
    between it and the next heading or horizontal rule.
    """
    lines = show_notes_md.split("\n")
    in_summary = False
    summary_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if re.match(r"^##\s+Episode Summary", stripped, re.IGNORECASE):
            in_summary = True
            continue
        if in_summary:
            # Stop at next heading or horizontal rule
            if re.match(r"^#{1,6}\s+", stripped) or re.match(r"^-{3,}$", stripped):
                break
            summary_lines.append(line)

    text = "\n".join(summary_lines).strip()
    return text if text else None


def render_show_notes_html(show_notes_md: str) -> str:
    """Convert markdown show notes to HTML for feed content:encoded."""
    return md_lib.markdown(
        show_notes_md,
        extensions=["tables", "fenced_code"],
    )
