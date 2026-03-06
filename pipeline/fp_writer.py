from __future__ import annotations

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

IMPORTANT RULES:
- Write naturally and conversationally for TTS. No markdown, no bullet points,
  no special characters.
- Aim for 1500-2200 words (approximately 10-15 minutes of spoken content).
- Introduce each theme section clearly for the listener.
- Start with a brief welcome and overview of today's themes.
- End with a brief sign-off.
- Do NOT use the word "delve" or any of its variants.
{context_block}
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
            "\nPRIOR EPISODE CONTEXT:",
            "Below are scripts from recent prior episodes. Build on these — avoid",
            "repeating the same framing, and reference relevant prior coverage where",
            "appropriate.\n",
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
) -> str:
    """Generate a FP podcast script via the shared opencode server."""
    prompt = build_fp_prompt(themes, articles_by_theme, date_str, context_scripts)

    instruction = (
        "Read the following prompt and generate the podcast briefing script. "
        "Output ONLY the script text, nothing else.\n\n" + prompt
    )

    session_id = create_session()
    try:
        send_prompt_async(session_id, instruction)

        if not wait_for_idle(session_id, timeout=120):
            raise RuntimeError("opencode session did not complete within 120 seconds")

        messages = get_messages(session_id)
        return get_last_assistant_text(messages).strip()
    finally:
        delete_session(session_id)
