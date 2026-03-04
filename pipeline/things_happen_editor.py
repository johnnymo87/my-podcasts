from __future__ import annotations

import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


class ResearchDirective(BaseModel):
    headline: str = Field(description="The original headline")
    needs_exa: bool = Field(
        description="True if the article needs general web search for alternative sources (e.g. if the original is likely paywalled, like Bloomberg/WSJ)"  # noqa: E501
    )
    exa_query: str = Field(
        description="A specific search query for Exa to find similar open-access reporting (3-6 keywords). Empty string if needs_exa is false."  # noqa: E501
    )
    needs_xai: bool = Field(
        description="True if understanding public sentiment, expert commentary, or Twitter discussion adds value to this story"  # noqa: E501
    )
    xai_query: str = Field(
        description="A concise query for Twitter/X search to find commentary (3-5 keywords). Empty string if needs_xai is false."  # noqa: E501
    )
    is_foreign_policy: bool = Field(
        description="True if the story relates to war, geopolitics, international relations, or military conflicts"  # noqa: E501
    )
    fp_query: str = Field(
        description="A concise query to search antiwar/independent RSS feeds (2-4 keywords). Empty string if is_foreign_policy is false."  # noqa: E501
    )
    is_ai: bool = Field(
        description="True if the story focuses on artificial intelligence, LLMs, AI companies, or AI safety"  # noqa: E501
    )
    ai_query: str = Field(
        description="A concise query to search AI-focused independent RSS feeds (2-4 keywords). Empty string if is_ai is false."  # noqa: E501
    )


class ResearchPlan(BaseModel):
    directives: list[ResearchDirective]


def generate_research_plan(
    headlines_with_snippets: list[str],
) -> list[ResearchDirective]:
    """Ask Gemini 3.1 Flash-Lite Preview to generate a research plan for the provided articles."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return []

    if not headlines_with_snippets:
        return []

    client = genai.Client(api_key=api_key)

    prompt = "Analyze these headlines and generate a research plan for a podcast briefing.\n\n"  # noqa: E501
    for item in headlines_with_snippets:
        prompt += f"- {item}\n"

    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ResearchPlan,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, ResearchPlan):
            return parsed.directives
        return []
    except Exception as e:
        print(f"Error generating research plan: {e}")
        return []
