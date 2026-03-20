"""Headline freshness annotation for theme rotation.

Classifies today's headlines against recently covered themes and
annotates them with [FRESH] or [RECURRING] tags so the editor can
enforce a freshness budget.
"""

from __future__ import annotations

import logging
import os

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ScriptThemes(BaseModel):
    """LLM output: themes extracted from a prior episode script."""

    themes: list[str]  # 3-5 theme names


class HeadlineClassification(BaseModel):
    """LLM output: maps a headline to a prior theme or marks it fresh."""

    headline_index: int
    matched_theme: str | None  # None means FRESH


class ClassificationResult(BaseModel):
    """LLM output: list of headline classifications."""

    classifications: list[HeadlineClassification]


def format_coverage_ledger(
    coverage_summary: list[dict],
    window_days: int = 3,
) -> str:
    """Format coverage summary as a markdown table for the editor prompt.

    Returns empty string if no coverage data.
    """
    if not coverage_summary:
        return ""

    lines = [
        f"## COVERAGE LEDGER (last {window_days} episodes)",
        "",
        "| Theme | Days Covered | Lead? | Dates |",
        "|-------|-------------|-------|-------|",
    ]
    for entry in coverage_summary:
        theme = entry["theme"]
        days = entry["days_covered"]
        lead = "LEAD" if entry["was_lead"] else ""
        dates = ", ".join(entry["episode_dates"])
        lines.append(f"| {theme} | {days}/{window_days} | {lead} | {dates} |")

    lines.append("")
    lines.append("## EDITORIAL GUIDANCE")
    lines.append(
        "Your editorial goal is to produce a briefing that is genuinely "
        "valuable to a listener who heard the last few episodes. Prioritize "
        "new developments and under-covered stories. Don't rehash yesterday's "
        "analysis. But don't shy away from running stories when something "
        "significant has changed — a ground invasion, a major escalation, "
        "a policy reversal, new casualty figures. Your listeners count on you "
        "for comprehensive coverage of the most important foreign policy "
        "developments, even if a region has been in the news for days. A story "
        "that appears under a familiar theme can still be the most important "
        "story of the day."
    )
    lines.append(
        "If you notice that your selection skews heavily toward recurring "
        "themes, set rotation_override explaining your reasoning."
    )

    return "\n".join(lines)


def build_freshness_prompt(
    headlines: list[str],
    coverage_summary: list[dict],
) -> str:
    """Build the Gemini prompt for classifying headlines against prior themes."""
    prompt = (
        "You are classifying news headlines against a list of previously "
        "covered themes from recent podcast episodes.\n\n"
        "PRIOR THEMES:\n"
    )
    for entry in coverage_summary:
        prompt += f'- "{entry["theme"]}" (covered {entry["days_covered"]} days)\n'

    prompt += (
        "\nFor each headline below, determine if it belongs to one of the "
        "prior themes above. If it does, set matched_theme to the exact "
        "theme name string. If the headline covers a genuinely different "
        "topic, set matched_theme to null.\n\n"
        "Match headlines to prior themes when they cover essentially the "
        "same angle or analysis. But if a headline represents a significant "
        "new development — escalation, de-escalation, new actors, new "
        "theaters, or a distinct sub-conflict within a broader war — it "
        "should be considered fresh (matched_theme = null) even if it falls "
        "within the same broad region or conflict.\n\n"
        "HEADLINES:\n"
    )
    for i, headline in enumerate(headlines):
        first_line = headline.split("\n")[0]
        prompt += f"{i}: {first_line}\n"

    prompt += (
        "\nReturn a JSON object with a 'classifications' array. Each element "
        "must have 'headline_index' (int) and 'matched_theme' (string or null)."
    )
    return prompt


def classify_headlines(
    headlines: list[str],
    coverage_summary: list[dict],
) -> list[HeadlineClassification]:
    """Call Gemini Flash-Lite to classify headlines against prior themes.

    Returns a HeadlineClassification for each headline. If the API call
    fails or coverage_summary is empty, returns all headlines as FRESH.
    """
    if not coverage_summary or not headlines:
        return [
            HeadlineClassification(headline_index=i, matched_theme=None)
            for i in range(len(headlines))
        ]

    prompt = build_freshness_prompt(headlines, coverage_summary)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; marking all headlines FRESH")
        return [
            HeadlineClassification(headline_index=i, matched_theme=None)
            for i in range(len(headlines))
        ]

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ClassificationResult,
                temperature=0.1,
            ),
        )
        parsed = response.parsed
        if parsed and parsed.classifications:
            return parsed.classifications
    except Exception:
        logger.exception("Gemini classification failed; marking all FRESH")

    return [
        HeadlineClassification(headline_index=i, matched_theme=None)
        for i in range(len(headlines))
    ]


def annotate_headlines(
    headlines: list[str],
    classifications: list[HeadlineClassification],
    coverage_summary: list[dict],
) -> list[str]:
    """Prepend [FRESH] or [RECURRING ...] tags to each headline string."""
    theme_info = {entry["theme"]: entry for entry in coverage_summary}
    class_by_idx = {c.headline_index: c for c in classifications}

    annotated = []
    for i, headline in enumerate(headlines):
        classification = class_by_idx.get(i)
        if classification and classification.matched_theme:
            theme = classification.matched_theme
            info = theme_info.get(theme, {})
            days = info.get("days_covered", "?")
            window = 3
            lead = ", LEAD" if info.get("was_lead") else ""
            tag = f'[RECURRING: "{theme}" - {days}/{window} days{lead}]'
        else:
            tag = "[FRESH]"
        annotated.append(f"{tag} {headline}")

    return annotated


def extract_themes_from_scripts(
    scripts: list[str],
) -> list[dict]:
    """Extract theme names from prior episode scripts via LLM.

    Fallback for when articles_json is not available. Returns a
    coverage_summary-compatible list of dicts.
    """
    if not scripts:
        return []

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return []

    all_themes: dict[str, set[str]] = {}  # theme -> set of script indices

    for i, script in enumerate(scripts):
        truncated = script[:3000]
        prompt = (
            "Extract the 3-5 main themes or storylines from this podcast "
            "episode script. Return short theme names (2-5 words each).\n\n"
            f"Script:\n{truncated}"
        )
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ScriptThemes,
                    temperature=0.1,
                ),
            )
            parsed = response.parsed
            if parsed and parsed.themes:
                for theme in parsed.themes:
                    if theme not in all_themes:
                        all_themes[theme] = set()
                    all_themes[theme].add(str(i))
        except Exception:
            logger.exception("Failed to extract themes from script %d", i)
            continue

    return [
        {
            "theme": theme,
            "days_covered": len(indices),
            "article_count": 0,
            "episode_dates": sorted(indices),
            "was_lead": False,
        }
        for theme, indices in all_themes.items()
    ]
