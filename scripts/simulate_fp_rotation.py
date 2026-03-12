#!/usr/bin/env python3
"""Simulate FP Digest generation across multiple days to test theme rotation.

Runs the collector + editor + writer pipeline in dry-run mode for a sequence
of dates, feeding each day's output as context to the next day.

Usage:
    uv run python scripts/simulate_fp_rotation.py

Compares story selection WITH freshness annotation vs the actual episodes
that were produced WITHOUT it.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# Simulation parameters
DATES = ["2026-03-09", "2026-03-10", "2026-03-11"]
SIM_DIR = Path("/tmp/fp-rotation-sim")
SCRIPTS_DIR = Path("/persist/my-podcasts/scripts/fp-digest")

# Cache dirs
HOMEPAGE_CACHE = Path("/persist/my-podcasts/antiwar-homepage-cache")
RSS_CACHE = Path("/persist/my-podcasts/antiwar-rss-cache")
SEMAFOR_CACHE = Path("/persist/my-podcasts/semafor-cache")
FP_ROUTED_DIR = Path("/persist/my-podcasts/fp-routed-links")


def get_prior_scripts(date_str: str, sim_scripts: dict[str, str]) -> list[str]:
    """Get up to 3 prior scripts: prefer simulated, fall back to actual."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    scripts = []
    for i in range(1, 4):
        prior_date = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if prior_date in sim_scripts:
            scripts.append(sim_scripts[prior_date])
        else:
            actual = SCRIPTS_DIR / f"{prior_date}.txt"
            if actual.exists():
                scripts.append(actual.read_text(encoding="utf-8"))
    return scripts


def build_coverage_from_plans(sim_plans: dict[str, dict]) -> list[dict]:
    """Build a coverage_summary from previously simulated plan.json data."""
    theme_stats: dict[str, dict] = {}

    for date_str, plan_data in sim_plans.items():
        themes_list = plan_data.get("themes", [])
        directives = plan_data.get("directives", [])

        lead_theme = themes_list[0] if themes_list else ""

        seen_themes: dict[str, int] = {}
        for d in directives:
            if d.get("include_in_episode", False):
                theme = d.get("theme", "")
                if theme:
                    seen_themes[theme] = seen_themes.get(theme, 0) + 1

        for theme, count in seen_themes.items():
            if theme not in theme_stats:
                theme_stats[theme] = {"dates": set(), "articles": 0, "lead_count": 0}
            theme_stats[theme]["dates"].add(date_str)
            theme_stats[theme]["articles"] += count
            if theme == lead_theme:
                theme_stats[theme]["lead_count"] += 1

    result = []
    for theme, stats in theme_stats.items():
        result.append(
            {
                "theme": theme,
                "days_covered": len(stats["dates"]),
                "article_count": stats["articles"],
                "episode_dates": sorted(stats["dates"]),
                "was_lead": stats["lead_count"] > 0,
            }
        )
    result.sort(key=lambda r: (-r["days_covered"], -r["article_count"]))
    return result


def build_prior_urls(
    sim_plans: dict[str, dict], work_dirs: dict[str, Path]
) -> set[str]:
    """Collect ALL URLs from prior work directory article files.

    Excludes all articles that were in any prior day's candidate pool,
    not just the selected ones. This prevents the same article from
    being offered to the editor across multiple days.
    """
    urls: set[str] = set()
    for date_str, work_dir in work_dirs.items():
        articles_dir = work_dir / "articles"
        if not articles_dir.exists():
            continue
        for md_path in articles_dir.rglob("*.md"):
            text = md_path.read_text(encoding="utf-8")
            for line in text.split("\n"):
                if line.startswith("URL: "):
                    url = line[5:].strip()
                    if url:
                        urls.add(url)
    return urls


def run_day(
    date_str: str,
    sim_scripts: dict[str, str],
    sim_plans: dict[str, dict],
    sim_work_dirs: dict[str, Path],
) -> None:
    """Run collection + editor + writer for one day."""
    from pipeline.fp_collector import collect_fp_artifacts
    from pipeline.fp_editor import FPResearchPlan

    run_id = str(uuid.uuid4())[:8]
    work_dir = SIM_DIR / f"{date_str}-{run_id}"

    print(f"\n{'=' * 60}")
    print(f"  Simulating FP Digest for {date_str}")
    print(f"{'=' * 60}")

    # Build coverage from prior simulated plans
    coverage_summary = build_coverage_from_plans(sim_plans)
    if coverage_summary:
        print(f"\n  Coverage ledger ({len(coverage_summary)} themes):")
        for entry in coverage_summary[:5]:
            lead = " [LEAD]" if entry["was_lead"] else ""
            print(
                f"    {entry['theme']}: {entry['days_covered']} days, "
                f"{entry['article_count']} articles{lead}"
            )

    # Build prior URLs from previous simulated episodes
    prior_urls = build_prior_urls(sim_plans, sim_work_dirs)
    if prior_urls:
        print(f"  Prior URLs to exclude: {len(prior_urls)}")

    # Run collection with freshness annotation + URL dedup
    print(f"\n  Collecting sources (lookback=2)...")
    collect_fp_artifacts(
        run_id,
        work_dir,
        homepage_cache_dir=HOMEPAGE_CACHE,
        antiwar_rss_cache_dir=RSS_CACHE,
        semafor_cache_dir=SEMAFOR_CACHE,
        fp_routed_dir=FP_ROUTED_DIR,
        lookback_days=2,
        coverage_summary=coverage_summary if coverage_summary else None,
        prior_urls=prior_urls if prior_urls else None,
    )

    plan_path = work_dir / "plan.json"
    if not plan_path.exists():
        print("  ERROR: no plan generated")
        return

    plan = FPResearchPlan.model_validate_json(plan_path.read_text())
    plan_data = json.loads(plan_path.read_text())

    print(f"\n  Themes: {', '.join(plan.themes)}")
    if plan.rotation_override:
        print(f"  ROTATION OVERRIDE: {plan.rotation_override}")

    selected = [d for d in plan.directives if d.include_in_episode]
    print(f"  Selected {len(selected)} stories:")
    for d in selected:
        print(f"    [{d.priority}] [{d.theme}] {d.headline[:70]}")

    # Skip script generation (expensive, and we're testing story selection)
    # Use the actual script for next day's context if available
    actual_script = SCRIPTS_DIR / f"{date_str}.txt"
    if actual_script.exists():
        sim_scripts[date_str] = actual_script.read_text(encoding="utf-8")
        print(f"\n  Using actual script as context for next day")
    else:
        print(f"\n  No actual script available for {date_str}")

    sim_plans[date_str] = plan_data
    sim_work_dirs[date_str] = work_dir
    print(f"  Work dir: {work_dir}")


def main() -> None:
    # Clean up previous sim
    if SIM_DIR.exists():
        shutil.rmtree(SIM_DIR)
    SIM_DIR.mkdir(parents=True)

    sim_scripts: dict[str, str] = {}
    sim_plans: dict[str, dict] = {}
    sim_work_dirs: dict[str, Path] = {}

    for date_str in DATES:
        run_day(date_str, sim_scripts, sim_plans, sim_work_dirs)

    # Final comparison: themes selected per day
    print(f"\n{'=' * 60}")
    print("  COMPARISON: Themes by day")
    print(f"{'=' * 60}")
    for date_str in DATES:
        plan_data = sim_plans.get(date_str)
        if plan_data:
            themes = plan_data.get("themes", [])
            print(f"\n  {date_str}: {', '.join(themes)}")
            directives = plan_data.get("directives", [])
            selected = [d for d in directives if d.get("include_in_episode")]
            for d in selected:
                print(
                    f"    [{d.get('priority')}] [{d.get('theme')}] "
                    f"{d.get('headline', '')[:60]}"
                )


if __name__ == "__main__":
    main()
