from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from pipeline.consumer import consume_forever
from pipeline.db import StateStore
from pipeline.feed import regenerate_and_upload_feed
from pipeline.processor import process_local_eml_file
from pipeline.r2 import R2Client
from pipeline.script_processor import publish_script
from pipeline.source_cache import (
    sync_antiwar_homepage_cache,
    sync_antiwar_rss_cache,
    sync_semafor_cache,
)
from pipeline.zvi_cache import sync_zvi_cache


def _find_rundown_article_text(directive: Any, work_dir: Path) -> str:
    """Find article text for a Rundown directive.

    Searches:
    - work_dir/articles/{nn}-{slug}.md (flat Levine articles)
    - work_dir/articles/semafor/{slug}.md
    - work_dir/articles/zvi/{slug}.md
    - work_dir/enrichment/exa/{slug}.md
    """
    from pipeline.things_happen_collector import _slugify

    slug = _slugify(directive.headline)

    # Flat Levine articles (e.g. "00-headline.md")
    articles_dir = work_dir / "articles"
    if articles_dir.exists():
        for match in articles_dir.glob(f"*{slug}.md"):
            if match.parent == articles_dir:  # Only top-level, not subdirs
                return match.read_text(encoding="utf-8")

    # Semafor articles
    semafor_file = work_dir / "articles" / "semafor" / f"{slug}.md"
    if semafor_file.exists():
        return semafor_file.read_text(encoding="utf-8")

    # Zvi articles
    zvi_dir = work_dir / "articles" / "zvi"
    if zvi_dir.exists():
        for match in zvi_dir.glob(f"*{slug}*.md"):
            return match.read_text(encoding="utf-8")

    # Exa enrichment
    exa_file = work_dir / "enrichment" / "exa" / f"{slug}.md"
    if exa_file.exists():
        return exa_file.read_text(encoding="utf-8")

    return ""


def _default_state_db_path() -> Path:
    return Path(os.getenv("MY_PODCASTS_STATE_DB", "/persist/my-podcasts/state.sqlite3"))


def _jobs_work_dir_base() -> Path:
    """Return the base directory where daily-job work directories live."""
    return Path("/tmp")


# ---------------------------------------------------------------------------
# jobs group
# ---------------------------------------------------------------------------

_ARTIFACT_FILES = ("script.txt", "summary.txt", "covered.json")

_FEED_WORK_DIR_PREFIX: dict[str, str] = {
    "fp-digest": "fp-digest-",
    "the-rundown": "the-rundown-",
}


@click.group()
def jobs() -> None:
    """Inspect and manage daily podcast jobs."""


@jobs.command("list")
@click.option(
    "--feed", "feed_slug", default=None, type=str, help="Filter by feed slug."
)
@click.option(
    "--status",
    default="errored",
    show_default=True,
    type=str,
    help="Filter by job status.",
)
def jobs_list_command(feed_slug: str | None, status: str) -> None:
    """List daily jobs filtered by status (and optionally by feed)."""
    store = StateStore(_default_state_db_path())
    try:
        feeds = [feed_slug] if feed_slug else list(store._FEED_SLUG_TO_TABLE.keys())
        found_any = False
        for slug in feeds:
            try:
                rows = store.list_daily_jobs(slug, status)
            except ValueError as exc:
                click.echo(f"Error: {exc}", err=True)
                continue
            for row in rows:
                found_any = True
                click.echo(
                    f"{slug}\t{row['id']}\t{row['date_str']}\t{row['status']}"
                    f"\tfailures={row['failure_count']}"
                    f"\terror={row['last_error'] or ''}"
                )
        if not found_any:
            click.echo(f"No jobs found with status={status!r}.")
    finally:
        store.close()


@jobs.command("reset")
@click.option("--feed", "feed_slug", required=True, type=str, help="Feed slug.")
@click.option("--date", "date_str", default=None, type=str, help="Date (YYYY-MM-DD).")
@click.option("--job-id", "job_id", default=None, type=str, help="Job UUID.")
@click.option(
    "--keep-artifacts",
    is_flag=True,
    default=False,
    help="Do not remove script.txt/summary.txt/covered.json from the work dir.",
)
def jobs_reset_command(
    feed_slug: str, date_str: str | None, job_id: str | None, keep_artifacts: bool
) -> None:
    """Reset an errored daily job back to pending so the consumer retries it."""
    if date_str is None and job_id is None:
        raise click.UsageError("Provide --date or --job-id.")

    store = StateStore(_default_state_db_path())
    try:
        # Resolve job_id from date_str if needed
        if job_id is None:
            rows = store.list_daily_jobs(feed_slug, "errored")
            matching = [r for r in rows if r["date_str"] == date_str]
            if not matching:
                # Also try pending jobs
                rows_pending = store.list_daily_jobs(feed_slug, "pending")
                matching = [r for r in rows_pending if r["date_str"] == date_str]
            if not matching:
                click.echo(
                    f"No job found for feed={feed_slug!r} date={date_str!r}.", err=True
                )
                raise SystemExit(1)
            job_id = matching[0]["id"]

        # Reset DB row
        try:
            if feed_slug == "fp-digest":
                store.reset_fp_digest_job(job_id)
            elif feed_slug == "the-rundown":
                store.reset_the_rundown_job(job_id)
            else:
                click.echo(f"Unknown feed: {feed_slug!r}", err=True)
                raise SystemExit(1)
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)

        click.echo(f"Reset {feed_slug} job {job_id} to pending.")

        # Clear stale artifacts unless --keep-artifacts
        if not keep_artifacts:
            prefix = _FEED_WORK_DIR_PREFIX.get(feed_slug, f"{feed_slug}-")
            work_dir = _jobs_work_dir_base() / f"{prefix}{job_id}"
            if work_dir.exists():
                for filename in _ARTIFACT_FILES:
                    artifact = work_dir / filename
                    if artifact.exists():
                        artifact.unlink()
                        click.echo(f"  Removed {artifact}")
    finally:
        store.close()


@click.group()
def cli() -> None:
    """My Podcasts pipeline commands."""


cli.add_command(jobs)


@cli.command("consume")
@click.option("--poll-interval", default=10, show_default=True, type=int)
def consume_command(poll_interval: int) -> None:
    """Run queue pull-consumer loop."""
    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        consume_forever(store, r2_client, poll_interval=poll_interval)
    finally:
        store.close()


@cli.command("process")
@click.option(
    "--input-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--route-tag", default=None, type=str)
def process_command(input_file: Path, route_tag: str | None) -> None:
    """Process a single local .eml file and upload resulting MP3 + feed."""
    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        result = process_local_eml_file(
            input_file,
            route_tag=route_tag,
            store=store,
            r2_client=r2_client,
        )
        click.echo(f"Uploaded episode: {result.r2_key}")
        click.echo(f"Title: {result.title}")
        click.echo(f"Route tag: {result.route_tag or 'none'}")
        click.echo(f"Preset: {result.preset_name}")
        click.echo(f"Feed: {result.feed_slug}")
        click.echo(f"Category: {result.category}")
        click.echo(f"Size: {result.size_bytes} bytes")
        if result.duration_seconds is not None:
            click.echo(f"Duration: {result.duration_seconds} sec")
    finally:
        store.close()


@cli.command("feed")
@click.option(
    "--output-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
)
def feed_command(output_file: Path | None) -> None:
    """Regenerate feed.xml from SQLite and upload to R2."""
    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        regenerate_and_upload_feed(store, r2_client, output_file=output_file)
        click.echo("Feed generated and uploaded to R2 as feed.xml")
    finally:
        store.close()


@cli.command("fp-digest")
@click.option(
    "--date",
    "date_str",
    default=None,
    type=str,
    help="Date (YYYY-MM-DD). Defaults to today.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Stop after script generation (skip TTS and publish).",
)
@click.option(
    "--lookback",
    "lookback_days",
    default=None,
    type=int,
    help="Override lookback days (default: adaptive based on last episode).",
)
def fp_digest_command(
    date_str: str | None, dry_run: bool, lookback_days: int | None
) -> None:
    """Create and process an FP Digest episode."""
    from datetime import UTC, datetime

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    if dry_run:
        _fp_digest_dry_run(date_str, lookback_days)
    else:
        _fp_digest_full_run(date_str, lookback_days)


def _fp_digest_dry_run(date_str: str, lookback_override: int | None = None) -> None:
    """Run collection + script generation without touching the DB."""
    import uuid

    from pipeline.fp_collector import collect_fp_artifacts
    from pipeline.fp_editor import FPResearchPlan
    from pipeline.fp_writer import generate_fp_script

    run_id = str(uuid.uuid4())
    work_dir = Path(f"/tmp/fp-digest-{run_id}")
    click.echo(f"Dry run for {date_str} (no DB entry created)")

    click.echo("Collecting sources...")
    collect_fp_artifacts(
        run_id,
        work_dir,
        homepage_cache_dir=Path("/persist/my-podcasts/antiwar-homepage-cache"),
        antiwar_rss_cache_dir=Path("/persist/my-podcasts/antiwar-rss-cache"),
        semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
        lookback_days=lookback_override or 2,
    )

    plan_path = work_dir / "plan.json"
    if not plan_path.exists():
        click.echo("Error: no plan generated")
        return

    plan = FPResearchPlan.model_validate_json(plan_path.read_text())
    click.echo(f"Themes: {', '.join(plan.themes)}")
    selected = sum(1 for d in plan.directives if d.include_in_episode)
    click.echo(f"Selected {selected} stories")

    from pipeline.consumer import _find_article_text

    articles_by_theme: dict[str, list[str]] = {}
    for directive in plan.directives:
        if not directive.include_in_episode:
            continue
        text = _find_article_text(directive, work_dir)
        if text:
            articles_by_theme.setdefault(directive.theme, []).append(text)

    context_scripts = []
    context_dir = work_dir / "context"
    if context_dir.exists():
        for f in sorted(context_dir.glob("*.txt"), reverse=True):
            context_scripts.append(f.read_text(encoding="utf-8"))

    click.echo("Generating script...")
    writer_output = generate_fp_script(
        themes=plan.themes,
        articles_by_theme=articles_by_theme,
        date_str=date_str,
        context_scripts=context_scripts,
    )

    script_file = work_dir / "script.txt"
    script_file.write_text(writer_output.script, encoding="utf-8")
    summary_file = work_dir / "summary.txt"
    summary_file.write_text(writer_output.summary, encoding="utf-8")
    if writer_output.covered_headlines:
        covered_file = work_dir / "covered.json"
        covered_file.write_text(
            json.dumps(writer_output.covered_headlines), encoding="utf-8"
        )

    click.echo(f"Dry run complete. Script saved to: {script_file}")
    click.echo(f"Work directory: {work_dir}")


def _fp_digest_full_run(date_str: str, lookback_override: int | None = None) -> None:
    """Run the full pipeline: collect, generate script, TTS, publish."""
    import shutil

    from pipeline.consumer import _compute_lookback
    from pipeline.fp_collector import collect_fp_artifacts
    from pipeline.fp_editor import FPResearchPlan
    from pipeline.fp_processor import process_fp_digest_job
    from pipeline.fp_writer import generate_fp_script

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()

        job_id = store.insert_pending_fp_digest(date_str)
        if job_id is None:
            click.echo(f"FP Digest job already exists for {date_str}")
            due = store.list_due_fp_digest()
            job = next((j for j in due if j["date_str"] == date_str), None)
            if job is None:
                click.echo("Job exists but is not pending.")
                return
        else:
            click.echo(f"Created FP Digest job {job_id} for {date_str}")
            due = store.list_due_fp_digest()
            job = next((j for j in due if j["id"] == job_id), None)
            if job is None:
                click.echo("Error: job not found")
                return

        lookback = lookback_override or _compute_lookback(store, "fp-digest")
        work_dir = Path(f"/tmp/fp-digest-{job['id']}")
        fp_coverage = store.recent_coverage_summary("fp-digest", days=3)
        fp_prior_urls = store.recent_article_urls("fp-digest", days=3)
        click.echo("Collecting sources...")
        collect_fp_artifacts(
            job["id"],
            work_dir,
            homepage_cache_dir=Path("/persist/my-podcasts/antiwar-homepage-cache"),
            antiwar_rss_cache_dir=Path("/persist/my-podcasts/antiwar-rss-cache"),
            semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
            lookback_days=lookback,
            coverage_summary=fp_coverage,
            prior_urls=fp_prior_urls,
        )

        plan_path = work_dir / "plan.json"
        if not plan_path.exists():
            click.echo("Error: no plan generated")
            return

        plan = FPResearchPlan.model_validate_json(plan_path.read_text())
        click.echo(f"Themes: {', '.join(plan.themes)}")
        selected = sum(1 for d in plan.directives if d.include_in_episode)
        click.echo(f"Selected {selected} stories")

        from pipeline.consumer import _find_article_text

        articles_by_theme: dict[str, list[str]] = {}
        for directive in plan.directives:
            if not directive.include_in_episode:
                continue
            text = _find_article_text(directive, work_dir)
            if text:
                articles_by_theme.setdefault(directive.theme, []).append(text)

        context_scripts = []
        context_dir = work_dir / "context"
        if context_dir.exists():
            for f in sorted(context_dir.glob("*.txt"), reverse=True):
                context_scripts.append(f.read_text(encoding="utf-8"))

        click.echo("Generating script...")
        writer_output = generate_fp_script(
            themes=plan.themes,
            articles_by_theme=articles_by_theme,
            date_str=date_str,
            context_scripts=context_scripts,
        )

        script_file = work_dir / "script.txt"
        script_file.write_text(writer_output.script, encoding="utf-8")
        summary_file = work_dir / "summary.txt"
        summary_file.write_text(writer_output.summary, encoding="utf-8")
        if writer_output.covered_headlines:
            covered_file = work_dir / "covered.json"
            covered_file.write_text(
                json.dumps(writer_output.covered_headlines), encoding="utf-8"
            )

        click.echo("Running TTS...")
        process_fp_digest_job(
            job,
            store,
            r2_client,
            script_path=script_file,
            work_dir=work_dir,
            summary=writer_output.summary,
        )

        persist_dir = Path("/persist/my-podcasts/scripts/fp-digest")
        persist_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(script_file, persist_dir / f"{date_str}.txt")

        click.echo(f"Published FP Digest for {date_str}")
    finally:
        store.close()


@cli.command("the-rundown")
@click.option(
    "--date",
    "date_str",
    default=None,
    type=str,
    help="Date (YYYY-MM-DD). Defaults to today ET.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run collection only, skip DB and agent launch.",
)
@click.option(
    "--lookback",
    "lookback_days",
    default=None,
    type=int,
    help="Override lookback days.",
)
def the_rundown_command(
    date_str: str | None, dry_run: bool, lookback_days: int | None
) -> None:
    """Create and launch a Rundown episode."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if date_str is None:
        date_str = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    if dry_run:
        _the_rundown_dry_run(date_str, lookback_days)
    else:
        _the_rundown_full_run(date_str, lookback_days)


def _the_rundown_dry_run(date_str: str, lookback_override: int | None = None) -> None:
    """Run collection + script generation without touching the DB."""
    import uuid

    from pipeline.rundown_writer import generate_rundown_script
    from pipeline.things_happen_collector import collect_all_artifacts
    from pipeline.things_happen_editor import RundownResearchPlan

    run_id = str(uuid.uuid4())
    work_dir = Path(f"/tmp/the-rundown-{run_id}")
    click.echo(f"Dry run for {date_str} (no DB entry created)")

    click.echo("Collecting sources...")
    collect_all_artifacts(
        run_id,
        work_dir,
        levine_cache_dir=Path("/persist/my-podcasts/levine-cache"),
        semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
        zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
        fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
        lookback_days=lookback_override or 2,
    )

    plan_path = work_dir / "plan.json"
    if not plan_path.exists():
        click.echo("Error: no plan generated")
        return

    plan = RundownResearchPlan.model_validate_json(plan_path.read_text())
    click.echo(f"Themes: {', '.join(plan.themes)}")
    selected = sum(1 for d in plan.directives if d.include_in_episode)
    click.echo(f"Selected {selected} stories")

    articles_by_theme: dict[str, list[str]] = {}
    for directive in plan.directives:
        if not directive.include_in_episode:
            continue
        text = _find_rundown_article_text(directive, work_dir)
        if text:
            articles_by_theme.setdefault(directive.theme, []).append(text)

    context_scripts = []
    context_dir = work_dir / "context"
    if context_dir.exists():
        for f in sorted(context_dir.glob("*.txt"), reverse=True):
            context_scripts.append(f.read_text(encoding="utf-8"))

    click.echo("Generating script...")
    writer_output = generate_rundown_script(
        themes=plan.themes,
        articles_by_theme=articles_by_theme,
        date_str=date_str,
        context_scripts=context_scripts,
    )

    script_file = work_dir / "script.txt"
    script_file.write_text(writer_output.script, encoding="utf-8")
    summary_file = work_dir / "summary.txt"
    summary_file.write_text(writer_output.summary, encoding="utf-8")
    if writer_output.covered_headlines:
        covered_file = work_dir / "covered.json"
        covered_file.write_text(
            json.dumps(writer_output.covered_headlines), encoding="utf-8"
        )

    click.echo(f"Dry run complete. Script saved to: {script_file}")
    click.echo(f"Work directory: {work_dir}")


def _the_rundown_full_run(date_str: str, lookback_override: int | None = None) -> None:
    """Run the full pipeline: collect, generate script, TTS, publish."""
    import shutil

    from pipeline.consumer import _compute_lookback
    from pipeline.rundown_writer import generate_rundown_script
    from pipeline.things_happen_collector import collect_all_artifacts
    from pipeline.things_happen_editor import RundownResearchPlan
    from pipeline.things_happen_processor import process_things_happen_job

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()

        job_id = store.insert_pending_the_rundown(date_str)
        if job_id is None:
            click.echo(f"The Rundown job already exists for {date_str}")
            due = store.list_due_the_rundown()
            job = next((j for j in due if j["date_str"] == date_str), None)
            if job is None:
                click.echo("Job exists but is not pending.")
                return
        else:
            click.echo(f"Created The Rundown job {job_id} for {date_str}")
            due = store.list_due_the_rundown()
            job = next((j for j in due if j["id"] == job_id), None)
            if job is None:
                click.echo("Error: job not found")
                return

        lookback = lookback_override or _compute_lookback(store, "the-rundown")
        work_dir = Path(f"/tmp/the-rundown-{job['id']}")
        rundown_coverage = store.recent_coverage_summary("the-rundown", days=3)
        rundown_prior_urls = store.recent_article_urls("the-rundown", days=3)
        click.echo("Collecting sources...")
        collect_all_artifacts(
            job["id"],
            work_dir,
            levine_cache_dir=Path("/persist/my-podcasts/levine-cache"),
            semafor_cache_dir=Path("/persist/my-podcasts/semafor-cache"),
            zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
            fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
            lookback_days=lookback,
            coverage_summary=rundown_coverage,
            prior_urls=rundown_prior_urls,
        )

        plan_path = work_dir / "plan.json"
        if not plan_path.exists():
            click.echo("Error: no plan generated")
            return

        plan = RundownResearchPlan.model_validate_json(plan_path.read_text())
        click.echo(f"Themes: {', '.join(plan.themes)}")
        selected = sum(1 for d in plan.directives if d.include_in_episode)
        click.echo(f"Selected {selected} stories")

        articles_by_theme: dict[str, list[str]] = {}
        for directive in plan.directives:
            if not directive.include_in_episode:
                continue
            text = _find_rundown_article_text(directive, work_dir)
            if text:
                articles_by_theme.setdefault(directive.theme, []).append(text)

        context_scripts = []
        context_dir = work_dir / "context"
        if context_dir.exists():
            for f in sorted(context_dir.glob("*.txt"), reverse=True):
                context_scripts.append(f.read_text(encoding="utf-8"))

        click.echo("Generating script...")
        writer_output = generate_rundown_script(
            themes=plan.themes,
            articles_by_theme=articles_by_theme,
            date_str=date_str,
            context_scripts=context_scripts,
        )

        script_file = work_dir / "script.txt"
        script_file.write_text(writer_output.script, encoding="utf-8")
        summary_file = work_dir / "summary.txt"
        summary_file.write_text(writer_output.summary, encoding="utf-8")
        if writer_output.covered_headlines:
            covered_file = work_dir / "covered.json"
            covered_file.write_text(
                json.dumps(writer_output.covered_headlines), encoding="utf-8"
            )

        click.echo("Running TTS...")
        process_things_happen_job(
            job,
            store,
            r2_client,
            script_path=script_file,
            work_dir=work_dir,
            summary=writer_output.summary,
        )
        store.mark_the_rundown_completed(job["id"])

        persist_dir = Path("/persist/my-podcasts/scripts/the-rundown")
        persist_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(script_file, persist_dir / f"{date_str}.txt")

        click.echo(f"Published The Rundown for {date_str}")
    finally:
        store.close()


@cli.command("publish-script")
@click.option(
    "--script-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the script file (markdown or plain text).",
)
@click.option("--title", required=True, type=str, help="Episode title.")
@click.option("--feed-slug", required=True, type=str, help="Feed slug to publish on.")
@click.option(
    "--show-notes",
    "show_notes_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to markdown show notes file.",
)
@click.option("--voice", default="nova", show_default=True, type=str)
@click.option("--category", default="Technology", show_default=True, type=str)
@click.option(
    "--date",
    "date_str",
    default=None,
    type=str,
    help="Date (YYYY-MM-DD). Defaults to today.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run TTS locally but don't publish to R2 or insert into DB.",
)
def publish_script_command(
    script_file: Path,
    title: str,
    feed_slug: str,
    show_notes_file: Path | None,
    voice: str,
    category: str,
    date_str: str | None,
    dry_run: bool,
) -> None:
    """Publish a podcast episode from a pre-written script file."""
    from datetime import UTC, datetime

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    if dry_run:
        import subprocess
        import tempfile

        from pipeline.script_processor import TTS_MODEL, strip_markdown_for_tts

        raw = script_file.read_text(encoding="utf-8")
        tts_text = strip_markdown_for_tts(raw)

        with tempfile.TemporaryDirectory(prefix="publish-script-dry-") as tmp_dir:
            tmp = Path(tmp_dir)
            input_txt = tmp / "dry-run.txt"
            output_mp3 = tmp / "dry-run.mp3"
            input_txt.write_text(tts_text, encoding="utf-8")

            cmd = [
                "ttsjoin",
                "--input-file",
                str(input_txt),
                "--output-file",
                str(output_mp3),
                "--model",
                TTS_MODEL,
                "--voice",
                voice,
            ]
            click.echo(f"Running TTS (dry run, voice={voice})...")
            subprocess.run(cmd, check=True)
            size = output_mp3.stat().st_size
            click.echo(f"MP3 generated: {output_mp3} ({size} bytes)")
            click.echo("Dry run complete. No episode published.")
        return

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        click.echo(f"Publishing '{title}' to feed '{feed_slug}'...")
        result = publish_script(
            script_file=script_file,
            title=title,
            feed_slug=feed_slug,
            store=store,
            r2_client=r2_client,
            show_notes_file=show_notes_file,
            voice=voice,
            category=category,
            date_str=date_str,
        )
        click.echo(f"Published: {result.r2_key}")
        click.echo(f"Title: {result.title}")
        click.echo(f"Feed: {result.feed_slug}")
        click.echo(f"Size: {result.size_bytes} bytes")
        if result.duration_seconds is not None:
            click.echo(f"Duration: {result.duration_seconds} sec")
    finally:
        store.close()


@cli.command("sync-sources")
def sync_sources_command() -> None:
    """Sync all source caches (Zvi, Semafor, Antiwar RSS, Antiwar homepage)."""
    caches = [
        ("Zvi", sync_zvi_cache, Path("/persist/my-podcasts/zvi-cache")),
        ("Semafor", sync_semafor_cache, Path("/persist/my-podcasts/semafor-cache")),
        (
            "Antiwar RSS",
            sync_antiwar_rss_cache,
            Path("/persist/my-podcasts/antiwar-rss-cache"),
        ),
        (
            "Antiwar Homepage",
            sync_antiwar_homepage_cache,
            Path("/persist/my-podcasts/antiwar-homepage-cache"),
        ),
    ]

    for name, sync_fn, cache_dir in caches:
        click.echo(f"Syncing {name}...")
        try:
            new_files = sync_fn(cache_dir)
            click.echo(f"  {name}: {len(new_files)} new files cached")
        except Exception as e:
            click.echo(f"  {name}: FAILED - {e}", err=True)


if __name__ == "__main__":
    cli()  # type: ignore[call-arg]
