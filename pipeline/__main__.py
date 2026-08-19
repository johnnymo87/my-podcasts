from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from pipeline import script_processor
from pipeline.consumer import _work_dir_base as _consumer_work_dir_base
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


def find_rundown_article_source(
    directive: Any, work_dir: Path
) -> tuple[str, str | None, str | None]:
    """Find article text and its work-dir-relative source path for a directive.

    Searches (in order):
    1. headline_index.json — exact match on original headline
    2. headline_index.json — unique slug match (absorbs reformulation)
    3. Legacy filesystem matching (flat Levine, Semafor, Zvi)
    4. Exa enrichment by slug

    Returns (text, source_path, miss_reason). source_path is None on a
    miss, and is always relative to work_dir (matching the keys used in
    tiers.json and headline_index.json) so callers can join resolution
    results against those files.

    miss_reason is None on any hit. There is exactly one miss return (the
    final one, below) because every lookup above cascades into the next --
    so a miss there means the index lookup AND the legacy slug fallback AND
    the Exa fallback all missed. There is no separate "slug missed" or "exa
    missed" reason; the only thing that varies across a miss is the state
    of headline_index.json at the point the cascade started, so that is
    what miss_reason describes:
    - "no_index": headline_index.json did not exist.
    - "index_unreadable": it existed but failed to parse.
    - "index_no_match": it parsed, but neither the exact-match nor the
      unique-slug lookup found a hit.
    - "slug_ambiguous": more than one indexed headline shares the
      directive's slug; refusing to guess ends the cascade immediately
      (see the elif below) rather than letting a filesystem tier pick one.

    There is deliberately no word-overlap tier. Measured against real data,
    a *wrong* article scored at least one query word in 50 of 54 cases and
    tied the correct article at a perfect score in one, so no threshold
    could separate them -- while exact+slug already covered 54/54. Article
    text is fed verbatim to the writer and published unread, so a miss
    (observable, degrades the section) is strictly preferable to a wrong
    match (invisible, fabricates confidently). See
    pipeline/article_resolver.py:resolve_headline for the shared cascade.
    """
    from pipeline.article_resolver import (
        DIRECT_EXA_HEADING,
        load_index,
        resolve_headline,
    )
    from pipeline.exa_client import exa_file_path, exa_result_sections
    from pipeline.things_happen_collector import _slugify

    headline = directive.headline
    slug = _slugify(headline)

    miss_reason = "no_index"

    # --- Index-based lookup (handles editor headline reformulation) ---
    index_path = work_dir / "headline_index.json"
    if index_path.exists():
        index = load_index(work_dir)
        if index is None:
            # None means unparseable or wrong-shape; the exists() check above
            # already ruled out "absent" (that keeps the "no_index" default).
            # A validly-empty {} is NOT this case -- it falls through and
            # resolves to "index_no_match", because the file read fine.
            miss_reason = "index_unreadable"
        else:
            rel_path, miss_reason = resolve_headline(headline, index)
            if rel_path is not None:
                fpath = work_dir / rel_path
                if fpath.exists():
                    return fpath.read_text(encoding="utf-8"), rel_path, None
                # Index points at a file that is gone: fall through to the
                # filesystem tiers rather than reporting a hit we cannot read.
                miss_reason = "index_no_match"
            elif miss_reason == "slug_ambiguous":
                # Refusing to guess is the whole point of the uniqueness
                # check, and the filesystem tiers below would immediately
                # undo it: two Levine files whose headlines share a 50-char
                # slug BOTH match the anchored glob, and it returns the
                # first. Stop here.
                return "", None, miss_reason

    # --- Legacy slug-based fallback ---
    # Every tier below keys off the slug, and an empty slug (a headline of
    # pure punctuation) turns their globs into wildcards -- the Zvi tier's
    # `*{slug}*.md` becomes `*.md` and would return an arbitrary file as this
    # directive's PRIMARY text. Refuse instead; show_notes._find_article_file
    # has had this guard all along.
    if not slug:
        return "", None, miss_reason

    # Flat Levine articles are written as "{NN}-{slug}.md"
    # (things_happen_collector.py:143). A bare glob(f"*{slug}.md") is a
    # SUFFIX match -- slug "ai" matches "00-openai.md" -- so match the real
    # shape. This also fixes an empty-slug catastrophe: a punctuation-only
    # headline slugifies to "", turning the old pattern into glob("*.md"),
    # which matched ANY flat article.
    articles_dir = work_dir / "articles"
    if articles_dir.exists():
        for match in sorted(articles_dir.glob(f"*-{slug}.md")):
            if match.parent != articles_dir:  # Only top-level, not subdirs
                continue
            if re.fullmatch(rf"\d+-{re.escape(slug)}\.md", match.name):
                return (
                    match.read_text(encoding="utf-8"),
                    str(match.relative_to(work_dir)),
                    None,
                )

    # Semafor articles
    semafor_file = work_dir / "articles" / "semafor" / f"{slug}.md"
    if semafor_file.exists():
        return (
            semafor_file.read_text(encoding="utf-8"),
            str(semafor_file.relative_to(work_dir)),
            None,
        )

    # Zvi articles. Filenames are "{date}-{post_slug}-{section_slug}.md", so
    # matching is a substring test on BOTH sides and cannot be anchored to
    # an exact name. Refuse rather than pick arbitrarily when more than one
    # file matches -- same principle as the slug tier above.
    zvi_dir = work_dir / "articles" / "zvi"
    if zvi_dir.exists():
        zvi_matches = sorted(zvi_dir.glob(f"*{slug}*.md"))
        if len(zvi_matches) == 1:
            match = zvi_matches[0]
            return (
                match.read_text(encoding="utf-8"),
                str(match.relative_to(work_dir)),
                None,
            )

    # Exa enrichment. Reached with no stub above it -- neither the index nor
    # any legacy filesystem tier resolved this directive -- so, unlike the
    # append-to-stub path in consumer._assemble_writer_inputs, there is no
    # true headline anchoring the section. Frame it as third-party coverage
    # rather than handing it to the writer as if it were the primary
    # article; see article_resolver.DIRECT_EXA_HEADING.
    exa_text = exa_result_sections(work_dir, slug)
    if exa_text:
        exa_path = exa_file_path(work_dir, slug)
        framed_text = f"{DIRECT_EXA_HEADING}\n\n{exa_text}"
        return framed_text, str(exa_path.relative_to(work_dir)), None

    return "", None, miss_reason


def _default_state_db_path() -> Path:
    return Path(os.getenv("MY_PODCASTS_STATE_DB", "/persist/my-podcasts/state.sqlite3"))


def _jobs_work_dir_base() -> Path:
    """Return the base directory where daily-job work directories live.

    Delegates to pipeline.consumer._work_dir_base so there is a single
    implementation of the /tmp override seam (MY_PODCASTS_WORK_DIR_BASE).
    """
    return _consumer_work_dir_base()


def _stale_daily_jobs(store: StateStore, feed_slug: str, today: str) -> list[dict]:
    """Return daily job rows for *feed_slug* left unfinished on an earlier date.

    A row still 'pending' or 'errored' for a date before *today* means a
    previous run was enqueued but never carried to completion - the signal that
    the consumer is wedged, stopped, or exhausted its retries. Comparison is
    lexical because date_str is always YYYY-MM-DD.
    """
    stale: list[dict] = []
    for status in ("pending", "errored"):
        stale.extend(
            row
            for row in store.list_daily_jobs(feed_slug, status)
            if row["date_str"] < today
        )
    return sorted(stale, key=lambda r: r["date_str"])


_DAILY_FEED_LABELS: dict[str, str] = {
    "the-rundown": "The Rundown",
    "fp-digest": "FP Digest",
}


def _enqueue_daily_job(store: StateStore, feed_slug: str, date_str: str) -> str | None:
    """Insert a pending daily job and report the outcome. Returns the job id.

    Returns None when a row for *date_str* already exists, which is a normal,
    successful outcome: date_str is UNIQUE, so a Persistent=true timer catch-up
    fire is idempotent for free.
    """
    label = _DAILY_FEED_LABELS.get(feed_slug)
    if label is None:
        raise ValueError(f"Unknown feed_slug: {feed_slug!r}")

    # Reject a malformed date here rather than letting the consumer retry a
    # garbage row for ~12h. The old inline CLI failed loudly in the operator's
    # terminal; enqueue-only would otherwise make the same typo silent.
    #
    # The round-trip comparison is the load-bearing half, not decoration:
    # strptime alone accepts non-zero-padded dates, so '2026-8-5' parses and is
    # stored as a string DISTINCT from '2026-08-05'. UNIQUE(date_str) would not
    # collide them, the consumer would execute both, and the feed would carry
    # two episodes for one day with different r2_keys - a fresh instance of the
    # very bug this module was rewritten to remove. It also enforces the
    # invariant _stale_daily_jobs relies on for its lexical date comparison.
    if datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d") != date_str:
        raise ValueError(
            f"date must be zero-padded YYYY-MM-DD, got {date_str!r} "
            f"(did you mean {datetime.strptime(date_str, '%Y-%m-%d'):%Y-%m-%d}?)"
        )

    if feed_slug == "the-rundown":
        job_id = store.insert_pending_the_rundown(date_str)
    else:
        job_id = store.insert_pending_fp_digest(date_str)

    if job_id is not None:
        click.echo(f"Queued {label} job {job_id} for {date_str}.")
        click.echo("The consumer will pick it up within ~10s. Follow it with:")
        click.echo("  journalctl -fu my-podcasts-consumer")
        return job_id

    # A row already exists. Report its ACTUAL status: 'errored' rows are NOT
    # eligible for execution (list_due_* filters status='pending'), so a bare
    # "already exists" would leave the operator believing work was queued when
    # nothing will ever run.
    existing = next(
        (
            row
            for status in ("pending", "errored", "completed")
            for row in store.list_daily_jobs(feed_slug, status)
            if row["date_str"] == date_str
        ),
        None,
    )
    status = existing["status"] if existing else "unknown"
    click.echo(f"{label} job already exists for {date_str} (status={status}).")
    if status == "errored":
        click.echo("It is errored, so the consumer will NOT run it. Reset it with:")
        click.echo(
            f"  uv run python -m pipeline jobs reset --feed {feed_slug} "
            f"--date {date_str}"
        )
    elif status == "completed":
        click.echo("Already published; nothing to do.")
    return None


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
            raise SystemExit(1) from exc

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


@jobs.command("complete")
@click.option("--feed", "feed_slug", required=True, type=str, help="Feed slug.")
@click.option("--date", "date_str", default=None, type=str, help="Date (YYYY-MM-DD).")
@click.option("--job-id", "job_id", default=None, type=str, help="Job UUID.")
def jobs_complete_command(
    feed_slug: str, date_str: str | None, job_id: str | None
) -> None:
    """Mark a daily job completed without running the pipeline.

    Closes the manual-publish trap: --dry-run then publish-script publishes
    an episode but never touches the job row, so the row stays pending and
    the returning consumer executes it again, publishing a duplicate. Run
    this immediately after a manual publish to close the row.
    """
    if date_str is None and job_id is None:
        raise click.UsageError("Provide --date or --job-id.")

    # Validate the slug before touching the store. list_daily_jobs raises
    # ValueError for an unknown feed, and on the --date path that call sits
    # outside the handler below, so an operator typo would surface as a raw
    # traceback rather than a clean message.
    if feed_slug not in _DAILY_FEED_LABELS:
        raise click.UsageError(
            f"Unknown feed {feed_slug!r}. Expected one of: "
            f"{', '.join(sorted(_DAILY_FEED_LABELS))}."
        )

    store = StateStore(_default_state_db_path())
    try:
        # Resolve job_id from date_str if needed
        if job_id is None:
            matching = [
                row
                for status in ("pending", "errored")
                for row in store.list_daily_jobs(feed_slug, status)
                if row["date_str"] == date_str
            ]
            if not matching:
                # Distinguish "already done" from "you typo'd the date" - only
                # pending/errored rows are searched above, so a completed row
                # would otherwise report as missing and send the operator
                # hunting for a mistake they did not make.
                already = [
                    row
                    for row in store.list_daily_jobs(feed_slug, "completed")
                    if row["date_str"] == date_str
                ]
                if already:
                    click.echo(
                        f"{feed_slug} job for {date_str} is already completed; "
                        f"nothing to do."
                    )
                    return
                click.echo(
                    f"No job found for feed={feed_slug!r} date={date_str!r}.", err=True
                )
                raise SystemExit(1)
            job_id = matching[0]["id"]

        try:
            store.complete_daily_job(feed_slug, job_id)
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1) from exc

        click.echo(f"Marked {feed_slug} job {job_id} completed.")
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
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if date_str is None:
        date_str = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    if dry_run:
        _fp_digest_dry_run(date_str, lookback_days)
        return

    if lookback_days is not None:
        raise click.UsageError(
            "--lookback only applies to --dry-run. The consumer computes the "
            "lookback window when it executes the job."
        )

    store = StateStore(_default_state_db_path())
    try:
        _enqueue_daily_job(store, "fp-digest", date_str)
        _audit_previous_daily_run(store, "fp-digest", date_str)
    finally:
        store.close()


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
        work_dir=work_dir,
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
        return

    if lookback_days is not None:
        raise click.UsageError(
            "--lookback only applies to --dry-run. The consumer computes the "
            "lookback window when it executes the job."
        )

    store = StateStore(_default_state_db_path())
    try:
        _enqueue_daily_job(store, "the-rundown", date_str)
        _audit_previous_daily_run(store, "the-rundown", date_str)
    finally:
        store.close()


def _the_rundown_dry_run(date_str: str, lookback_override: int | None = None) -> None:
    """Run collection + script generation without touching the DB."""
    import uuid

    from pipeline.consumer import _assemble_writer_inputs
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

    sections, writer_inputs = _assemble_writer_inputs(plan, work_dir)
    (work_dir / "writer_inputs.json").write_text(
        json.dumps(writer_inputs, indent=2), encoding="utf-8"
    )

    context_scripts = []
    context_dir = work_dir / "context"
    if context_dir.exists():
        for f in sorted(context_dir.glob("*.txt"), reverse=True):
            context_scripts.append(f.read_text(encoding="utf-8"))

    click.echo("Generating script...")
    writer_output = generate_rundown_script(
        sections=sections,
        date_str=date_str,
        context_scripts=context_scripts,
        work_dir=work_dir,
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


def _audit_previous_daily_run(store: StateStore, feed_slug: str, today: str) -> None:
    """Alert if a previous run of *feed_slug* was enqueued but never finished.

    The timer is now the watchdog for its own previous fire. Since the CLI only
    enqueues, a green timer unit no longer implies an episode shipped, and this
    is what closes that gap without needing a change to the Nix-managed units.

    Never raises: a monitoring failure must not break the enqueue.
    """
    try:
        stale = _stale_daily_jobs(store, feed_slug, today)
        if not stale:
            return

        from pipeline.alerts import send_alert

        label = _DAILY_FEED_LABELS.get(feed_slug, feed_slug)
        lines = [f"{label}: {len(stale)} earlier job(s) never completed."]
        for row in stale:
            lines.append(
                f"  {row['date_str']} status={row['status']} "
                f"failures={row['failure_count']} last_error={row['last_error']}"
            )
        lines.append("The consumer may be stopped or wedged. Check:")
        lines.append("  systemctl status my-podcasts-consumer")
        send_alert("\n".join(lines), severity="warning")
    except Exception as exc:  # noqa: BLE001 - monitoring must never break the job
        print(f"[daily-audit] skipped ({type(exc).__name__}: {exc})")


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


@cli.command("episode")
@click.option("--url", required=True, type=str, help="Source URL or id.")
@click.option(
    "--source",
    default=None,
    type=click.Choice(["arxiv", "substack"]),
    help="Force a source adapter (otherwise auto-detected from the URL).",
)
@click.option(
    "--mode",
    type=click.Choice(["report", "read"]),
    default="report",
    show_default=True,
    help="report: spoken briefing; read: faithful full reading.",
)
@click.option("--feed-slug", "feed_slug", required=True, type=str)
@click.option(
    "--style",
    default=None,
    type=click.Choice(["interview", "paper"]),
    help="Override the report prompt style (defaults to the source's style).",
)
@click.option(
    "--title",
    default=None,
    type=str,
    help="Override episode title (report mode prepends 'Report: ' if not set).",
)
@click.option("--voice", default="nova", show_default=True, type=str)
@click.option(
    "--category",
    default=None,
    type=str,
    help="Override iTunes category (defaults to the source's category).",
)
@click.option("--date", "date_str", default=None, type=str, help="Date (YYYY-MM-DD).")
@click.option(
    "--script-file",
    "script_file_opt",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Publish this pre-written script verbatim, skipping generation. "
        "Metadata (title, source link, show notes) still comes from the source."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Generate the script only; skip TTS and publish.",
)
def episode_command(
    url: str,
    source: str | None,
    mode: str,
    feed_slug: str,
    style: str | None,
    title: str | None,
    voice: str,
    category: str | None,
    date_str: str | None,
    script_file_opt: Path | None,
    dry_run: bool,
) -> None:
    """Turn a source URL (Substack post, arXiv paper, ...) into a one-off episode."""
    import tempfile
    from datetime import UTC, datetime

    from pipeline import report_writer, sources

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    click.echo(f"Resolving {url} ...")
    try:
        doc = sources.resolve_document(url, source=source)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Title: {doc.title} ({doc.wordcount} words, style={doc.style})")

    if mode == "read" and doc.read_html is None:
        raise click.ClickException(
            f"read mode is not supported for this source (style={doc.style}); "
            "use --mode report."
        )

    if script_file_opt is not None:
        script_text = script_file_opt.read_text(encoding="utf-8")
        episode_title = title or (
            f"Report: {doc.title}" if mode == "report" else doc.title
        )
        click.echo(
            f"Using pre-written script ({len(script_text)} chars); skipping generation."
        )
    elif mode == "report":
        out = report_writer.generate_report(
            body=doc.report_text,
            subject=doc.title,
            style=style or doc.style,
            byline=doc.byline,
        )
        script_text = out.script
        episode_title = title or f"Report: {doc.title}"
    else:  # read
        from pipeline.blog_poller import adapt_for_audio

        assert doc.read_html is not None  # guaranteed by the read-mode guard above
        click.echo("Adapting source for audio...")
        adapted = adapt_for_audio(doc.read_html, doc.title)
        if not adapted:
            raise click.ClickException(
                "Audio adaptation failed (is GEMINI_API_KEY set?)."
            )
        script_text = adapted
        episode_title = title or doc.title

    if dry_run:
        out_path = (
            Path(tempfile.gettempdir()) / f"episode-{doc.slug or 'post'}-{date_str}.txt"
        )
        out_path.write_text(script_text, encoding="utf-8")
        click.echo("Dry run complete. No episode published.")
        click.echo(f"Script: {out_path}")
        return

    notes_md = f"## Episode Summary\n\n{doc.description}\n\n"
    if doc.byline and doc.byline != doc.description:
        notes_md += f"By {doc.byline}\n\n"
    notes_md += f"---\n\n[Original source]({doc.canonical_url})\n"

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        with tempfile.TemporaryDirectory(prefix="episode-") as tmp_dir:
            tmp = Path(tmp_dir)
            script_file = tmp / "script.md"
            script_file.write_text(script_text, encoding="utf-8")
            notes_file = tmp / "notes.md"
            notes_file.write_text(notes_md, encoding="utf-8")

            result = script_processor.publish_script(
                script_file=script_file,
                title=episode_title,
                feed_slug=feed_slug,
                store=store,
                r2_client=r2_client,
                show_notes_file=notes_file,
                voice=voice,
                category=(category or doc.default_category),
                date_str=date_str,
                source_url=doc.canonical_url or None,
            )
        click.echo(f"Published: {result.r2_key}")
        click.echo(f"Title: {result.title}")
        click.echo(f"Feed: {result.feed_slug}")
        if result.duration_seconds is not None:
            click.echo(f"Duration: {result.duration_seconds} sec")
    finally:
        store.close()


@cli.command("poll-blogs")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Fetch and adapt but don't TTS or publish.",
)
def poll_blogs_command(dry_run: bool) -> None:
    """Poll all blog RSS feeds and process new posts."""
    import requests

    from pipeline.blog_poller import adapt_for_audio, parse_blog_feed, poll_all_blogs
    from pipeline.blog_sources import BLOG_SOURCES

    if dry_run:
        for source in BLOG_SOURCES:
            click.echo(f"Polling: {source.name} ({source.feed_url})")
            try:
                resp = requests.get(source.feed_url, timeout=30)
                resp.raise_for_status()
                posts = parse_blog_feed(resp.text)
                click.echo(f"  Found {len(posts)} posts in feed")

                store = StateStore(_default_state_db_path())
                try:
                    for post in posts:
                        processed = store.is_blog_post_processed(post.url)
                        status = "SKIP (already processed)" if processed else "NEW"
                        click.echo(f"  [{status}] {post.title}")
                        if not processed:
                            adapted = adapt_for_audio(post.html_content, post.title)
                            if adapted:
                                click.echo(f"    Adapted: {len(adapted)} chars")
                            else:
                                click.echo("    Adaptation failed (no API key?)")
                finally:
                    store.close()
            except Exception as e:
                click.echo(f"  FAILED: {e}", err=True)
        return

    store = StateStore(_default_state_db_path())
    try:
        r2_client = R2Client()
        poll_all_blogs(store, r2_client)
        click.echo("Blog polling complete.")
    finally:
        store.close()


@cli.command("run-stats")
@click.option("--work-dir", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--send", is_flag=True, help="Also post to the Telegram General topic.")
def run_stats_command(work_dir: Path, send: bool) -> None:
    """Render the content-acquisition funnel for an existing work dir."""
    from pipeline.alerts import send_alert
    from pipeline.run_stats import collect_run_stats, render_report

    name = work_dir.name
    if name.startswith("fp-digest-"):
        feed, job_id = "fp-digest", name[len("fp-digest-") :]
    else:
        feed, job_id = "the-rundown", name.replace("the-rundown-", "")
    stats = collect_run_stats(work_dir, job_id=job_id, date_str="", feed=feed)
    report = render_report(stats)
    click.echo(report)
    if send:
        # Deliberately ignores run_stats_sent: a manual send is a manual send.
        click.echo("sent" if send_alert(report) else "send failed")


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
