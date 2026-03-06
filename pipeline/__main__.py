from __future__ import annotations

import os
from pathlib import Path

import click

from pipeline.consumer import consume_forever
from pipeline.db import StateStore
from pipeline.feed import regenerate_and_upload_feed
from pipeline.processor import process_local_eml_file
from pipeline.r2 import R2Client


def _default_state_db_path() -> Path:
    return Path(os.getenv("MY_PODCASTS_STATE_DB", "/persist/my-podcasts/state.sqlite3"))


@click.group()
def cli() -> None:
    """My Podcasts pipeline commands."""


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
def fp_digest_command(date_str: str | None) -> None:
    """Create and process an FP Digest episode."""
    import shutil
    from datetime import UTC, datetime

    from pipeline.fp_collector import collect_fp_artifacts
    from pipeline.fp_editor import FPResearchPlan
    from pipeline.fp_processor import process_fp_digest_job
    from pipeline.fp_writer import generate_fp_script

    if date_str is None:
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")

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

        work_dir = Path(f"/tmp/fp-digest-{job['id']}")
        click.echo("Collecting sources...")
        collect_fp_artifacts(job["id"], work_dir)

        plan_path = work_dir / "plan.json"
        if not plan_path.exists():
            click.echo("Error: no plan generated")
            return

        plan = FPResearchPlan.model_validate_json(plan_path.read_text())
        click.echo(f"Themes: {', '.join(plan.themes)}")
        selected = sum(1 for d in plan.directives if d.include_in_episode)
        click.echo(f"Selected {selected} stories")

        # Build articles by theme
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
        script = generate_fp_script(
            themes=plan.themes,
            articles_by_theme=articles_by_theme,
            date_str=date_str,
            context_scripts=context_scripts,
        )

        script_file = work_dir / "script.txt"
        script_file.write_text(script, encoding="utf-8")

        click.echo("Running TTS...")
        process_fp_digest_job(job, store, r2_client, script_path=script_file)

        persist_dir = Path("/persist/my-podcasts/scripts/fp-digest")
        persist_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(script_file, persist_dir / f"{date_str}.txt")

        click.echo(f"Published FP Digest for {date_str}")
    finally:
        store.close()


if __name__ == "__main__":
    cli()  # type: ignore[call-arg]
