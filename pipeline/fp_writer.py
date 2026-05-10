from __future__ import annotations

from pathlib import Path

from pipeline.rundown_writer import (
    WriterOutput,
    _extract_script,
    parse_covered,
    parse_summary,
)

from pipeline.opencode_client import (
    create_session,
    delete_session,
    get_last_assistant_text,
    get_messages,
    send_prompt_async,
    wait_for_idle,
)


PROMPT_TEMPLATE = """\
You are generating a daily foreign policy podcast briefing. Today's date is {date_str}.

Your job is to produce a natural, conversational podcast script covering the most
important foreign policy stories of the day, grouped by theme. The script will be
read aloud by a TTS engine.

Write naturally and conversationally, as though you're a knowledgeable friend
catching someone up on the day's foreign policy news. This will be read aloud by
a TTS engine, so use plain spoken English -- no markdown, bullet points, or
special characters.

LENGTH: Aim for 800-2200 words depending on how much genuinely new material
there is. A tight 5-8 minute episode that covers three or four real developments
is far better than a 15-minute episode that rehashes yesterday. Do not pad.
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


def build_fp_prompt(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
) -> str:
    """Build the LLM prompt for the FP podcast script."""
    # Build context block if prior episode scripts are provided
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
            "  episode. If you covered the school strike death toll yesterday, do not",
            "  repeat it today unless the number has changed.",
            "- A shorter episode built from genuinely new material is always better",
            "  than a longer episode that recycles prior coverage.\n",
        ]
        for i, script in enumerate(context_scripts, 1):
            context_lines.append(f"[Prior Episode {i}]:\n{script}\n")
        context_block = "\n".join(context_lines) + "\n"
    else:
        context_block = ""

    # Build themes list
    themes_list = "\n".join(f"- {theme}" for theme in themes)

    # Build stories block grouped by theme
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


def generate_fp_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
    work_dir: Path | None = None,
) -> WriterOutput:
    """Generate a FP podcast script via the shared opencode server."""
    # work_dir is accepted for API compatibility; persistence is added in
    # the next task.
    _ = work_dir
    prompt = build_fp_prompt(themes, articles_by_theme, date_str, context_scripts)

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
            raise RuntimeError("opencode session did not complete within 900 seconds")

        messages = get_messages(session_id)
        full_text = get_last_assistant_text(messages).strip()
        covered = parse_covered(full_text)
        summary_result = parse_summary(full_text)
        script = _extract_script(summary_result.script)
        if not script.strip():
            raise RuntimeError("FP writer returned empty script")
        return WriterOutput(
            script=script,
            summary=summary_result.summary,
            covered_headlines=covered,
        )
    finally:
        delete_session(session_id)
