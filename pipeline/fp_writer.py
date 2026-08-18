from __future__ import annotations

from pathlib import Path

from pipeline.report_engine import fetch_report_text, parse_report, persist_raw_output
from pipeline.rundown_writer import WriterOutput, parse_covered


PROMPT_TEMPLATE = """\
You are generating a daily foreign policy podcast briefing. Today's date is {date_str}.

Your job is to produce a spoken briefing on the most important foreign policy
stories of the day. The script will be read aloud by a TTS engine. Your listener
follows the world closely but hasn't had time to read everything today — they
want a clear, structured report on what happened and what the people writing
about it actually said.

Write the briefing as flowing prose, not a recitation of themes. Cover the
stories in whatever order best illuminates the day — group related items
together when the connection is real, and use natural transitions to move
between them. You don't need to announce each theme as a separate section;
the theme list is a planning aid for you, not scaffolding the listener has
to hear. A brief opening to orient the listener is useful, but skip the
"here are today's themes, theme one is..." preamble.

Do NOT end with a recap. No "so here is where things stand this Monday"
summary, no story-by-story wrap-up of what you just said. The listener heard
it moments ago, and repeating it costs a tenth of the episode. Close with a
single short sign-off line and stop -- for example: "That's your briefing for
Monday, August seventeenth. Talk to you tomorrow." One or two sentences at
most, naming no stories.

Attribute claims to the people who made them. When a source argued, reported,
or pushed back on something, say so — name the publication, the analyst, or
the author when the material gives you a name. Lean into concrete details,
numbers, names, and examples that give the briefing weight.

Do not editorialize beyond what the sources themselves said. Report the
arguments and the disagreements; don't add opinions, jokes, or asides of
your own. If a source is uncertain, convey that uncertainty; don't resolve
it for them. Do not invent facts.

Write for the ear, not the page. Use plain spoken English -- no markdown,
bullet points, or special characters.

LENGTH: Aim for 800-2200 words depending on how much genuinely new material
there is. A tight 5-8 minute briefing that covers three or four real
developments is far better than a 15-minute episode that rehashes yesterday.
Do not pad.
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
    """Generate a FP podcast script via the shared opencode server.

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

        full_text = fetch_report_text(instruction, label="FP digest")
        if raw_path is not None:
            persist_raw_output(raw_path, full_text)

    try:
        covered = parse_covered(full_text)
        # min_chars=500 is re-derived here, not copied from the transcript
        # path's 2000 -- see generate_rundown_script's comment for why the
        # daily writers' bounded-retry regime justifies the lower floor.
        # require_tags=True because this is an automated, no-human-in-the-loop
        # publish path -- a missing <script> tag must be a loud refusal, not
        # a narration of the model's raw reasoning.
        report = parse_report(
            full_text, label="FP digest", min_chars=500, require_tags=True
        )
    except RuntimeError:
        if raw_path is not None:
            try:
                raw_path.unlink()
            except FileNotFoundError:
                pass
        raise

    return WriterOutput(
        script=report.script,
        summary=report.summary,
        covered_headlines=covered,
    )
