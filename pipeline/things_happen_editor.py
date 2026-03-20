from __future__ import annotations

import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


class RundownStoryDirective(BaseModel):
    headline: str = Field(description="The original headline")
    source: str = Field(
        description="Which source this came from (e.g. 'levine', 'semafor', 'zvi')"
    )
    priority: int = Field(description="1-5, where 1 is the lead story")
    theme: str = Field(
        description="Grouping label (e.g. 'AI & Machine Learning', 'Markets & Finance', 'Media & Culture')"
    )
    needs_exa: bool = Field(
        description="True if the article is paywalled or inaccessible and needs an open-access alternative via web search"
    )
    exa_query: str = Field(
        description="Search query for finding open-access alternatives (3-6 keywords). Empty string if needs_exa is false."
    )
    is_foreign_policy: bool = Field(
        description="True if the story relates to war, geopolitics, international relations, or military conflicts"
    )
    fp_query: str = Field(
        description="A concise query to search antiwar/independent RSS feeds (2-4 keywords). Empty string if is_foreign_policy is false."
    )
    include_in_episode: bool = Field(
        description="True if this story should be included in today's episode. Exclude foreign policy stories."
    )


class RundownResearchPlan(BaseModel):
    themes: list[str] = Field(
        description="The dominant themes or story arcs identified across all sources today"
    )
    directives: list[RundownStoryDirective]
    rotation_override: str | None = None


def _empty_plan() -> RundownResearchPlan:
    return RundownResearchPlan(themes=[], directives=[])


def generate_rundown_research_plan(
    headlines_with_snippets: list[str],
    context_scripts: list[str] | None = None,
    coverage_ledger: str | None = None,
) -> RundownResearchPlan:
    """Ask Gemini Flash-Lite to triage stories into themes and select which to include."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _empty_plan()

    if not headlines_with_snippets:
        return _empty_plan()

    client = genai.Client(api_key=api_key)

    prompt = (
        "You are the editor of The Rundown, a daily podcast covering business, "
        "technology, AI, law, media, science, and culture. Foreign policy goes to "
        "a separate podcast — flag FP stories but do NOT include them in the episode.\n\n"
        "Analyze these headlines. Identify the dominant themes, assign each story "
        "to a theme, set priorities (1=lead), and select the stories that a smart, "
        "busy listener most needs to hear today. Prefer breadth over depth — cover "
        "the important stories rather than clustering on one topic. Flag paywalled "
        "articles that need open-access alternatives via Exa. Flag foreign policy "
        "stories (is_foreign_policy=true, include_in_episode=false).\n\n"
        "Headlines:\n"
    )
    for item in headlines_with_snippets:
        prompt += f"- {item}\n"

    if coverage_ledger:
        prompt += f"\n\n{coverage_ledger}\n"
    elif context_scripts:
        prompt += (
            "\n\nPrevious episodes (listeners already heard these). Your goal is "
            "to produce an episode that is genuinely valuable to someone who heard "
            "these — prioritize new developments and under-covered stories, but "
            "don't shy away from running stories when something significant has "
            "changed.\n"
        )
        for script in context_scripts:
            prompt += f"\n---\n{script}\n"

    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RundownResearchPlan,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, RundownResearchPlan):
            return parsed
        return _empty_plan()
    except Exception as e:
        print(f"Error generating Rundown research plan: {e}")
        return _empty_plan()
