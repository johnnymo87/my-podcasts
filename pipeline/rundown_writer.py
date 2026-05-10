from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.opencode_client import (
    create_session,
    delete_session,
    get_last_assistant_text,
    get_messages,
    send_prompt_async,
    wait_for_idle,
)


PROMPT_TEMPLATE = """\
You are generating today's episode of The Rundown, a daily podcast covering business,
technology, AI, law, media, science, and culture. Today's date is {date_str}.

Foreign policy goes to a separate podcast. Skip it entirely.

Your job is to produce a natural, conversational podcast script covering the most
important stories of the day, grouped by theme. The script will be read aloud by
a TTS engine.

You are a smart friend explaining the day's news over drinks. You are genuinely
curious about how things work and why they matter. You have opinions and you share
them, but you hold them lightly and you're honest about uncertainty. You explain
complex things clearly without talking down to the listener — they're well-read,
they just haven't had time to read everything today. You draw connections across
stories when they exist. You have fun with language. You let the material breathe
when it deserves it and move briskly when it doesn't.

Write for the ear, not the page. Use plain spoken English — no markdown, bullet
points, or special characters.

LENGTH: Aim for 800-2200 words depending on how much genuinely new material there
is. A tight 5-8 minute episode that covers three or four real developments is far
better than a 15-minute episode that rehashes yesterday. Do not pad.
{context_block}
Introduce each theme section clearly, start with a brief welcome and overview,
and end with a brief sign-off.

---

TODAY'S THEMES:
{themes_list}

---

STORIES BY THEME:

{stories_block}
"""


def build_rundown_prompt(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
) -> str:
    """Build the LLM prompt for The Rundown podcast script."""
    if context_scripts:
        context_lines = [
            "\nPRIOR EPISODES (your listeners already heard these):",
            "Treat the content below as what your audience already knows. Your job",
            "today is to tell them what is NEW.",
            "",
            "Rules for handling prior coverage:",
            "- If a running story has a material new development, cover the new",
            "  development. Do not re-explain the background — listeners already",
            "  have it. A single sentence like 'as we discussed yesterday' is enough",
            "  to orient them before delivering the update.",
            "- If a running story has NO material new development since the last",
            "  episode, skip it entirely or give it at most one sentence.",
            "- Never restate facts, figures, or analysis that appeared in a prior",
            "  episode. If you covered a story yesterday, do not repeat it today",
            "  unless the situation has materially changed.",
            "- A shorter episode built from genuinely new material is always better",
            "  than a longer episode that recycles prior coverage.\n",
        ]
        for i, script in enumerate(context_scripts, 1):
            context_lines.append(f"[Prior Episode {i}]:\n{script}\n")
        context_block = "\n".join(context_lines) + "\n"
    else:
        context_block = ""

    themes_list = "\n".join(f"- {theme}" for theme in themes)

    story_sections: list[str] = []
    for theme in themes:
        articles = articles_by_theme.get(theme, [])
        section_lines = [f"## {theme}"]
        for j, article_text in enumerate(articles, 1):
            section_lines.append(f"### Source {j}")
            section_lines.append(article_text)
        story_sections.append("\n".join(section_lines))
    stories_block = "\n\n".join(story_sections)

    return PROMPT_TEMPLATE.format(
        date_str=date_str,
        context_block=context_block,
        themes_list=themes_list,
        stories_block=stories_block,
    )


@dataclass(frozen=True)
class WriterOutput:
    script: str
    summary: str
    covered_headlines: list[str] = field(default_factory=list)


def parse_summary(text: str) -> WriterOutput:
    """Extract <summary>...</summary> block from writer output.

    Returns WriterOutput with summary and the remaining script text.
    If no summary tags found, summary is empty string.
    """
    match = re.search(r"<summary>\s*(.*?)\s*</summary>", text, re.DOTALL)
    if match:
        summary = match.group(1).strip()
        script = text[: match.start()] + text[match.end() :]
        script = script.strip()
        return WriterOutput(script=script, summary=summary)
    return WriterOutput(script=text, summary="")


def parse_covered(text: str) -> list[str]:
    """Extract covered headlines from ``<covered>...</covered>`` tags.

    Returns a list of headline strings.  Lines are stripped of leading
    dashes and whitespace.  Empty lines are skipped.
    """
    m = re.search(r"<covered>\s*(.*?)\s*</covered>", text, re.DOTALL)
    if not m:
        return []
    headlines: list[str] = []
    for line in m.group(1).splitlines():
        cleaned = line.strip().lstrip("-").strip()
        if cleaned:
            headlines.append(cleaned)
    return headlines


def _extract_script(text: str) -> str:
    """Extract the podcast script from ``<script>...</script>`` tags."""
    import re

    m = re.search(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def generate_rundown_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
    work_dir: Path | None = None,
) -> WriterOutput:
    """Generate a Rundown podcast script via the shared opencode server.

    If ``work_dir`` is provided, the model's raw output is persisted to
    ``work_dir/raw_writer_output.txt`` the moment it's available. Subsequent
    calls with the same ``work_dir`` skip the model call entirely and reuse
    the persisted text. If parsing the persisted text fails, the file is
    deleted so the next retry regenerates instead of looping on the same
    broken content.
    """
    raw_path = work_dir / "raw_writer_output.txt" if work_dir else None

    if raw_path is not None and raw_path.exists():
        full_text = raw_path.read_text(encoding="utf-8")
    else:
        prompt = build_rundown_prompt(
            themes, articles_by_theme, date_str, context_scripts
        )
        instruction = (
            "Read the following prompt and generate the podcast briefing script. "
            "First, write a 2-3 sentence summary of today's episode wrapped in "
            "<summary>...</summary> tags. "
            "Then list the headlines of the stories you actually cover in the script, "
            "wrapped in <covered>...</covered> tags, one headline per line prefixed "
            "with a dash. Use the exact headlines from the source material. "
            "Then write the full spoken script wrapped in "
            "<script>...</script> tags. Do NOT include any analysis, reasoning, or "
            "meta-commentary outside these tags — only the summary, covered list, "
            "and the script that will be read aloud.\n\n" + prompt
        )

        session_id = create_session()
        try:
            send_prompt_async(session_id, instruction)
            if not wait_for_idle(session_id, timeout=900):
                raise RuntimeError(
                    "opencode session did not complete within 900 seconds"
                )
            messages = get_messages(session_id)
            full_text = get_last_assistant_text(messages).strip()
            if raw_path is not None:
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(full_text, encoding="utf-8")
        finally:
            delete_session(session_id)

    try:
        covered = parse_covered(full_text)
        summary_result = parse_summary(full_text)
        script = _extract_script(summary_result.script)
        if not script.strip():
            raise RuntimeError("Rundown writer returned empty script")
    except RuntimeError:
        if raw_path is not None:
            try:
                raw_path.unlink()
            except FileNotFoundError:
                pass
        raise

    return WriterOutput(
        script=script,
        summary=summary_result.summary,
        covered_headlines=covered,
    )
