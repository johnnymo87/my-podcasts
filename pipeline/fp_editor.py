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
        description="True if this story should be included in today's episode. Select 8-12 stories that best cover the major themes."  # noqa: E501
    )


class FPResearchPlan(BaseModel):
    themes: list[str] = Field(
        description="The 3-5 dominant themes or story arcs identified across all sources today"  # noqa: E501
    )
    directives: list[FPStoryDirective]


_EMPTY_PLAN = FPResearchPlan(themes=[], directives=[])


def generate_fp_research_plan(
    headlines_with_snippets: list[str],
    context_scripts: list[str] | None = None,
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
        "Analyze these headlines and:\n"
        "1. Identify 3-5 dominant themes or story arcs across all sources today\n"
        "2. Assign each story to a theme and set its priority (1=lead story)\n"
        "3. Select 8-12 stories that best cover the major themes — avoid redundancy\n"
        "4. Flag paywalled or inaccessible articles that need open-access alternatives via Exa\n\n"  # noqa: E501
        "Headlines:\n"
    )
    for item in headlines_with_snippets:
        prompt += f"- {item}\n"

    if context_scripts:
        prompt += (
            "\n\nPrevious episodes (listeners already heard these):\n"
            "Deprioritize stories that were covered in depth unless there is a\n"
            "significant new development (new facts, new numbers, a policy change,\n"
            "a concrete event — not just continued coverage of the same situation).\n"
            "When in doubt, prefer fresh stories over continuing threads.\n"
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
