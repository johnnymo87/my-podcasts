from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.opencode_client import (
    create_session,
    delete_session,
    get_last_assistant_text,
    get_messages,
    send_prompt_async,
    wait_for_idle,
)


if TYPE_CHECKING:
    from pipeline.article_fetcher import FetchedArticle


PROMPT_TEMPLATE = """\
You are generating a podcast briefing script for a listener who reads Matt Levine's Money Stuff newsletter. Today's date is {date_str}. Below are the stories from the "Things Happen" section at the end of the newsletter. Your job is to brief the listener on each story: what happened, why it matters, and which stories are the biggest deals today.

Write naturally and conversationally, as though you're a knowledgeable friend catching someone up. This will be read aloud by a TTS engine, so use plain spoken English -- no markdown, bullet points, or special characters.

For each story, clearly state your source quality BEFORE summarizing:
  - If marked "PUBLICLY AVAILABLE PORTION": you have the article content. Summarize the key points.
  - If marked "HEADLINE ONLY": you only have the headline. Give brief context based on your knowledge, and be upfront that you're working from the headline alone.

Start with a brief intro, flag the 2-3 biggest stories early on, and end with a brief sign-off.

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
    """Generate a TTS briefing script via the shared opencode server."""
    prompt = build_prompt(articles, date_str)

    instruction = (
        "Read the following prompt with instructions and article content. "
        "Follow the instructions exactly and generate the podcast briefing "
        "script. Output ONLY the script text, nothing else.\n\n" + prompt
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
