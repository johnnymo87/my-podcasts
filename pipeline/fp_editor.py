from __future__ import annotations

import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


_EMPTY_PLAN: FPResearchPlan


class FPStoryDirective(BaseModel):
    headline: str = Field(description="The original headline")
    source: str = Field(
        description="Which source this came from (e.g. 'homepage/iran', 'rss/antiwar_news', 'rss/caitlinjohnstone')"  # noqa: E501
    )
    priority: int = Field(description="1-5, where 1 is the lead story")
    theme: str = Field(
        description="Grouping label (e.g. 'Iran War', 'Lebanon Escalation', 'NATO Expansion')"  # noqa: E501
    )
    needs_exa: bool = Field(
        description="True if the article is paywalled or inaccessible and needs an open-access alternative via web search"  # noqa: E501
    )
    exa_query: str = Field(
        description="Search query for finding open-access alternatives (3-6 keywords). Empty string if needs_exa is false."  # noqa: E501
    )
    include_in_episode: bool = Field(
        description="True if this story should be included in today's episode."
    )


class FPResearchPlan(BaseModel):
    themes: list[str] = Field(
        description="The dominant themes or story arcs identified across all sources today"
    )
    directives: list[FPStoryDirective]
    rotation_override: str | None = None  # Explanation if freshness budget unmet


_EMPTY_PLAN = FPResearchPlan(themes=[], directives=[])


def generate_fp_research_plan(
    headlines_with_snippets: list[str],
    context_scripts: list[str] | None = None,  # Keep for backward compat
    coverage_ledger: str | None = None,  # New: structured coverage data
) -> FPResearchPlan:
    """Ask Gemini Flash-Lite to triage FP stories into themes and select which to include."""  # noqa: E501
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _EMPTY_PLAN

    if not headlines_with_snippets:
        return _EMPTY_PLAN

    client = genai.Client(api_key=api_key)

    prompt = (
        "You are the editor of Foreign Policy Digest, an antiwar podcast covering "
        "geopolitics, military conflicts, and international relations.\n\n"
        "Analyze these headlines. Identify the dominant themes, assign each story "
        "to a theme, set priorities (1=lead), and select the stories that a "
        "thoughtful listener most needs to hear today. Prefer breadth over depth — "
        "cover the important developments across different regions and conflicts "
        "rather than clustering on one theater. Flag paywalled articles that need "
        "open-access alternatives via Exa.\n\n"
        "Headlines:\n"
    )
    for item in headlines_with_snippets:
        prompt += f"- {item}\n"

    # Prefer coverage_ledger over context_scripts
    if coverage_ledger:
        prompt += f"\n\n{coverage_ledger}\n"
    elif context_scripts:
        # Fallback for transition period
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
                response_schema=FPResearchPlan,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, FPResearchPlan):
            return parsed
        return _EMPTY_PLAN
    except Exception as e:
        print(f"Error generating FP research plan: {e}")
        return _EMPTY_PLAN
