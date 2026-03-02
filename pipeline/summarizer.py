from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pipeline.article_fetcher import FetchedArticle


PROMPT_TEMPLATE = """\
You are generating a podcast briefing script for a listener who reads Matt Levine's Money Stuff newsletter. Today's date is {date_str}. Below are the stories from the "Things Happen" section at the end of the newsletter. Your job is to brief the listener on each story: what happened, why it matters, and which stories are the biggest deals today.

IMPORTANT RULES:
- Be conversational and concise. This will be read aloud by a TTS engine.
- For each story, clearly state your source quality BEFORE summarizing:
  - If marked "PUBLICLY AVAILABLE PORTION": you have the article content. Summarize the key points.
  - If marked "HEADLINE ONLY": you only have the headline. Give brief context based on your knowledge, and be upfront that you're working from the headline alone.
- Start with a brief intro: "Here are today's Things Happen stories from Money Stuff."
- End with a brief sign-off.
- Flag the 2-3 biggest stories early on.
- Do NOT use any markdown formatting, bullet points, or special characters. Plain spoken English only.
- Do NOT use the word "delve" or any of its variants.

---

STORIES:

{stories_block}
"""


def build_prompt(articles: list[FetchedArticle], date_str: str) -> str:
    """Build the LLM prompt from fetched articles."""
    stories: list[str] = []
    for i, article in enumerate(articles, 1):
        tier_label = article.source_label.upper()
        stories.append(
            f"Story {i} [{tier_label}]:\n"
            f"URL: {article.url}\n"
            f"Content:\n{article.content}\n"
        )
    stories_block = "\n---\n".join(stories)
    return PROMPT_TEMPLATE.format(date_str=date_str, stories_block=stories_block)


def generate_briefing_script(
    articles: list[FetchedArticle],
    date_str: str,
) -> str:
    """Generate a TTS briefing script using Claude Opus 4.6 via opencode."""
    prompt = build_prompt(articles, date_str)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="things-happen-prompt-",
        delete=False,
    ) as f:
        f.write(prompt)
        prompt_path = f.name

    try:
        cmd = [
            "opencode",
            "run",
            "--file",
            prompt_path,
            "Read the attached file. It contains a prompt with instructions "
            "and article content. Follow the instructions in the file exactly "
            "and generate the podcast briefing script. Output ONLY the script "
            "text, nothing else.",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"opencode run failed (exit {result.returncode}): {result.stderr}"
            )
        output = result.stdout.strip()
        lines = output.splitlines()
        content_lines = []
        past_header = False
        for line in lines:
            if not past_header and (line.startswith(">") or line.strip() == ""):
                continue
            past_header = True
            content_lines.append(line)
        return "\n".join(content_lines).strip()
    finally:
        Path(prompt_path).unlink(missing_ok=True)
