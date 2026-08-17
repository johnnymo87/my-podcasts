from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from pipeline.fp_collector import _slugify, collect_fp_artifacts
from pipeline.fp_editor import FPResearchPlan
from pipeline.fp_processor import process_fp_digest_job
from pipeline.fp_writer import generate_fp_script
from pipeline.processor import process_r2_email_key
from pipeline.things_happen_processor import process_things_happen_job


if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client
    from pipeline.things_happen_editor import RundownResearchPlan


# Framing matters: this text is retrieved by keyword search and is not
# guaranteed to cover the same story. Telling the writer that is the cheap
# mitigation for a wrong-story match in a pipeline with no human review.
_OPEN_ACCESS_HEADING = (
    "## Related coverage from other outlets\n"
    "(Retrieved by search. Use only the parts that clearly describe the "
    "story in the headline above; ignore anything that does not match.)"
)

_BLOG_POLL_INTERVAL = 6 * 3600  # 6 hours
_last_blog_poll = 0.0

RUNDOWN_SCRIPT_ARCHIVE_DIR = Path("/persist/my-podcasts/scripts/the-rundown")
FP_DIGEST_SCRIPT_ARCHIVE_DIR = Path("/persist/my-podcasts/scripts/fp-digest")


def _work_dir_base() -> Path:
    """Return the base directory where daily-job work directories live.

    Production default is exactly "/tmp" (deployed code, systemd-managed,
    in-flight jobs on disk). Overridable via MY_PODCASTS_WORK_DIR_BASE so
    tests can point work dirs at tmp_path instead of littering the host's
    real /tmp.
    """
    return Path(os.getenv("MY_PODCASTS_WORK_DIR_BASE", "/tmp"))


def _compute_lookback(
    store: StateStore, feed_slug: str, default: int = 2, cap: int = 14
) -> int:
    """Compute adaptive lookback days based on last episode date."""
    days = store.days_since_last_episode(feed_slug)
    if days is None:
        return default
    return min(max(2, days + 1), cap)


def _report_run_stats(
    work_dir: Path, job_id: str, date_str: str, reused_collection: bool = False
) -> None:
    """Emit the content-acquisition funnel for a finished script stage.

    Deliberately total: this runs after script.txt, summary.txt and covered.json
    are already on disk, and swallows everything, so a reporting bug can never
    fail a job or burn retry budget.

    The three sinks below (local JSON, durable JSONL, Telegram) are guarded
    independently rather than under one try/except. They previously shared a
    single try, so a disk failure in the JSONL append (e.g. a full /persist)
    would skip the Telegram send entirely -- suppressing the human-visible
    report on exactly the day an operator most needs it. Now each sink's
    failure is isolated to that sink; send_alert already logs the rendered
    report to journald when it can't reach Telegram, so as long as the send
    step still runs, the report reaches an operator one way or another.

    The run_stats_sent marker keeps a retry (which reuses collection but reruns
    the writer) from sending a second message. A /tmp marker is safe here
    because the retry budget is ~12 hours and /tmp is reaped at 10 days.
    """
    try:
        from pipeline.run_stats import collect_run_stats

        stats = collect_run_stats(
            work_dir,
            job_id=job_id,
            date_str=date_str,
            reused_collection=reused_collection,
        )
    except Exception as exc:
        print(f"[consumer] run stats collection failed: {exc}")
        return

    try:
        (work_dir / "run_stats.json").write_text(
            stats.model_dump_json(indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[consumer] run stats json write failed: {exc}")

    try:
        from pipeline.run_stats import append_jsonl

        append_jsonl(stats, Path("/persist/my-podcasts/run-stats.jsonl"))
    except Exception as exc:
        print(f"[consumer] run stats jsonl append failed: {exc}")

    try:
        from pipeline.alerts import send_alert
        from pipeline.run_stats import render_report

        marker = work_dir / "run_stats_sent"
        if not marker.exists():
            if send_alert(render_report(stats), severity="info"):
                marker.touch()
    except Exception as exc:
        print(f"[consumer] run stats send failed: {exc}")


@dataclass(frozen=True)
class QueueMessage:
    id: str
    lease_id: str
    key: str
    route_tag: str | None


class CloudflareQueueConsumer:
    def __init__(self) -> None:
        self._account_id = os.environ["R2_ACCOUNT_ID"]
        self._queue_id = os.environ["CLOUDFLARE_QUEUE_ID"]
        self._api_token = os.environ["CLOUDFLARE_API_TOKEN"]
        self._session = requests.Session()
        self._base_url = (
            "https://api.cloudflare.com/client/v4"
            f"/accounts/{self._account_id}/queues/{self._queue_id}/messages"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

    def pull(
        self,
        batch_size: int = 1,
        visibility_timeout: int = 120,
    ) -> list[QueueMessage]:
        response = self._session.post(
            f"{self._base_url}/pull",
            headers=self._headers(),
            json={
                "batch_size": batch_size,
                "visibility_timeout": visibility_timeout,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", {})
        messages: list[dict[str, Any]] = result.get("messages", [])

        parsed: list[QueueMessage] = []
        for message in messages:
            message_id = str(message.get("id") or message.get("message_id") or "")
            lease_id = str(message.get("lease_id") or "")
            body_raw = message.get("body", {})
            body: dict[str, Any]
            if isinstance(body_raw, str):
                try:
                    decoded = json.loads(body_raw)
                    body = decoded if isinstance(decoded, dict) else {}
                except json.JSONDecodeError:
                    body = {}
            elif isinstance(body_raw, dict):
                body = body_raw
            else:
                body = {}

            key = str(body.get("key", ""))
            route_tag_raw = body.get("route_tag")
            route_tag = str(route_tag_raw) if route_tag_raw else None

            if message_id and lease_id and key:
                parsed.append(
                    QueueMessage(
                        id=message_id,
                        lease_id=lease_id,
                        key=key,
                        route_tag=route_tag,
                    )
                )
        return parsed

    def ack(self, messages: list[QueueMessage]) -> None:
        if not messages:
            return
        ack_payload = [
            {"id": message.id, "lease_id": message.lease_id} for message in messages
        ]
        response = self._session.post(
            f"{self._base_url}/ack",
            headers=self._headers(),
            json={"acks": ack_payload},
            timeout=30,
        )
        response.raise_for_status()


def _cleanup_old_work_dirs(max_age_days: int = 180) -> None:
    """Remove things-happen and fp-digest work directories older than max_age_days."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=max_age_days)
    tmp = _work_dir_base()
    for pattern in ("things-happen-*", "the-rundown-*", "fp-digest-*"):
        for d in tmp.glob(pattern):
            if not d.is_dir():
                continue
            try:
                mtime = datetime.fromtimestamp(d.stat().st_mtime, tz=UTC)
                if mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    print(f"Cleaned up old work dir: {d.name}")
            except OSError:
                pass

    # Clean up old routed link files (7-day retention)
    routed_cutoff = datetime.now(tz=UTC) - timedelta(days=7)
    routed_dir = Path("/persist/my-podcasts/fp-routed-links")
    if routed_dir.exists():
        for f in routed_dir.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                if mtime < routed_cutoff:
                    f.unlink()
                    print(f"Cleaned up old routed links: {f.name}")
            except OSError:
                pass

    # Clean up old Zvi cache files (180-day retention)
    zvi_cache_dir = Path("/persist/my-podcasts/zvi-cache")
    if zvi_cache_dir.exists():
        for f in zvi_cache_dir.glob("*.md"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                if mtime < cutoff:
                    f.unlink()
                    print(f"Cleaned up old Zvi cache: {f.name}")
            except OSError:
                pass

    # Clean up old Semafor, Antiwar RSS, and Antiwar homepage caches (180-day retention)
    for cache_name, cache_path in [
        ("Semafor cache", Path("/persist/my-podcasts/semafor-cache")),
        ("Antiwar RSS cache", Path("/persist/my-podcasts/antiwar-rss-cache")),
        ("Antiwar homepage cache", Path("/persist/my-podcasts/antiwar-homepage-cache")),
    ]:
        if cache_path.exists():
            for f in cache_path.glob("*.md"):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                    if mtime < cutoff:
                        f.unlink()
                        print(f"Cleaned up old {cache_name}: {f.name}")
                except OSError:
                    pass


def _find_article_text(directive: Any, work_dir: Path) -> str:
    """Find the article file matching a directive by slugifying the headline.

    Searches:
    - work_dir/articles/homepage/*/{slug}.md (rglob)
    - work_dir/articles/rss/*/{slug}.md (rglob)
    - work_dir/articles/routed/{slug}.md
    - work_dir/articles/semafor/{slug}.md
    - work_dir/enrichment/exa/{slug}.md

    Returns the file content or empty string.
    """
    slug = _slugify(directive.headline)

    # Search homepage articles
    homepage_dir = work_dir / "articles" / "homepage"
    if homepage_dir.exists():
        for match in homepage_dir.rglob(f"{slug}.md"):
            return match.read_text(encoding="utf-8")

    # Search RSS articles
    rss_dir = work_dir / "articles" / "rss"
    if rss_dir.exists():
        for match in rss_dir.rglob(f"{slug}.md"):
            return match.read_text(encoding="utf-8")

    # Search routed articles
    routed_file = work_dir / "articles" / "routed" / f"{slug}.md"
    if routed_file.exists():
        return routed_file.read_text(encoding="utf-8")

    # Search semafor articles
    semafor_file = work_dir / "articles" / "semafor" / f"{slug}.md"
    if semafor_file.exists():
        return semafor_file.read_text(encoding="utf-8")

    # Search Exa enrichment
    exa_file = work_dir / "enrichment" / "exa" / f"{slug}.md"
    if exa_file.exists():
        return exa_file.read_text(encoding="utf-8")

    return ""


def _assemble_writer_inputs(
    plan: RundownResearchPlan, work_dir: Path
) -> tuple[list[tuple[str, list[str]]], list[dict]]:
    """Resolve each selected directive to article text. Pure function of disk state.

    Returns (sections, writer_inputs). `sections` is an ordered list of
    (theme, article_texts) built once here so nothing downstream can
    re-derive or drop a theme: plan themes keep plan.themes order and omit
    themes with zero resolved articles (no bare headers); a directive whose
    theme is absent from plan.themes (an orphan -- the editor invented a
    near-miss name) gets appended as its own trailing section in
    first-seen order, under its own name, never folded into a similar
    plan theme.
    """
    from pipeline.__main__ import find_rundown_article_source
    from pipeline.exa_client import exa_result_sections
    from pipeline.things_happen_collector import _slugify as _th_slugify

    plan_theme_order = {theme: i for i, theme in enumerate(plan.themes)}
    by_theme: dict[str, list[str]] = {}
    orphan_order: list[str] = []
    writer_inputs: list[dict] = []
    for directive in plan.directives:
        if not directive.include_in_episode:
            continue
        text, src, miss_reason = find_rundown_article_source(directive, work_dir)

        # A stub is resolved by the exact/slug match ahead of the Exa tier
        # (find_rundown_article_source's cascade below), so retrieved
        # open-access text never stands alone -- it is appended here.
        # Never replace: with a fully automated writer and no human review,
        # replacing a stub with a wrong-story article would make the writer
        # confidently narrate a false story under a true headline --
        # appending leaves the true headline anchoring the section, so a
        # mismatch degrades instead.
        exa_extra = ""
        if src is not None and not src.startswith("enrichment/exa/"):
            exa_extra = exa_result_sections(work_dir, _th_slugify(directive.headline))
            if exa_extra:
                text = f"{text}\n\n{_OPEN_ACCESS_HEADING}\n\n{exa_extra}"

        # reached_prompt is filled in after `sections` is built, by checking
        # actual membership -- deliberately NOT set to bool(text) here.
        # bool(text) would be a tautology against "chars": len(text) in this
        # same dict literal, so the funnel's dropped_before_prompt canary
        # could never fire no matter how badly section assembly broke. Deriving
        # it from the built sections instead makes it an independent check:
        # if any future change to the section-building below drops a theme
        # that has text, this goes False and the funnel says so.
        # miss_reason is None on a hit; see find_rundown_article_source's
        # docstring for the taxonomy (no_index / index_unreadable /
        # index_no_match / slug_ambiguous) and why the cascade only
        # supports one reason per miss, not a reason per lookup stage.
        writer_inputs.append(
            {
                "headline": directive.headline,
                "theme": directive.theme,
                "source_path": src,
                "chars": len(text),
                "exa_appended": bool(exa_extra),
                "exa_chars": len(exa_extra),
                "miss_reason": miss_reason,
            }
        )
        if text:
            if (
                directive.theme not in plan_theme_order
                and directive.theme not in by_theme
            ):
                orphan_order.append(directive.theme)
            by_theme.setdefault(directive.theme, []).append(text)

    # `plan.themes` comes from an LLM and carries no uniqueness constraint, so
    # a repeated theme name would otherwise render its articles twice in the
    # prompt. Deduplicate on first occurrence, preserving plan order.
    _seen: set[str] = set()
    sections: list[tuple[str, list[str]]] = [
        (theme, by_theme[theme])
        for theme in plan.themes
        if by_theme.get(theme) and not (theme in _seen or _seen.add(theme))
    ]
    sections += [(theme, by_theme[theme]) for theme in orphan_order]

    # Derived from the sections that were actually built, not from bool(text),
    # so this is a genuine cross-check rather than a restatement of "chars".
    # A directive reached the prompt iff it resolved to text AND its theme
    # survived into a rendered section.
    section_names = {theme for theme, _ in sections}
    for entry in writer_inputs:
        entry["reached_prompt"] = entry["chars"] > 0 and entry["theme"] in section_names

    return sections, writer_inputs


def consume_forever(
    store: StateStore,
    r2_client: R2Client,
    poll_interval: int = 10,
) -> None:
    consumer = CloudflareQueueConsumer()

    while True:
        try:
            messages = consumer.pull(batch_size=5)
        except Exception as exc:
            print(f"Queue pull failed: {exc}")
            time.sleep(poll_interval)
            continue

        if messages:
            ack_messages: list[QueueMessage] = []
            for message in messages:
                if store.is_processed(message.key):
                    ack_messages.append(message)
                    continue

                try:
                    process_r2_email_key(
                        message.key, message.route_tag, store, r2_client
                    )
                except Exception as exc:
                    print(f"Failed processing {message.key}: {exc}")
                    continue
                ack_messages.append(message)

            consumer.ack(ack_messages)

        # Process any due Rundown jobs.
        try:
            due_jobs = store.list_due_the_rundown()
            for job in due_jobs:
                try:
                    work_dir = _work_dir_base() / f"the-rundown-{job['id']}"
                    script_file = work_dir / "script.txt"

                    if script_file.exists():
                        # Script already generated — run TTS + publish.
                        try:
                            dry_run = os.environ.get("THE_RUNDOWN_DRY_RUN", "").strip()
                            if dry_run:
                                print(
                                    f"DRY RUN: skipping TTS for {job['id']} "
                                    f"({job['date_str']}). Script at: {script_file}"
                                )
                                store.mark_the_rundown_completed(job["id"])
                            else:
                                print(
                                    f"Processing Rundown job with script: "
                                    f"{job['id']} ({job['date_str']})"
                                )
                                summary_text = None
                                summary_path = work_dir / "summary.txt"
                                if summary_path.exists():
                                    summary_text = summary_path.read_text(
                                        encoding="utf-8"
                                    )
                                process_things_happen_job(
                                    job,
                                    store,
                                    r2_client,
                                    script_path=script_file,
                                    work_dir=work_dir,
                                    summary=summary_text,
                                )
                                store.mark_the_rundown_completed(job["id"])
                                print(f"Completed Rundown job: {job['id']}")

                            # Copy to persistent storage
                            persist_dir = RUNDOWN_SCRIPT_ARCHIVE_DIR
                            persist_dir.mkdir(parents=True, exist_ok=True)
                            persist_path = persist_dir / f"{job['date_str']}.txt"
                            shutil.copy(script_file, persist_path)
                        finally:
                            _cleanup_old_work_dirs()

                    else:
                        # No script yet — run the full synchronous pipeline.
                        from pipeline.rundown_writer import generate_rundown_script
                        from pipeline.things_happen_collector import (
                            collect_all_artifacts,
                        )
                        from pipeline.things_happen_editor import RundownResearchPlan

                        collection_sentinel = work_dir / "collection_done.json"
                        plan_path = work_dir / "plan.json"

                        reused_collection = (
                            collection_sentinel.exists() and plan_path.exists()
                        )
                        if reused_collection:
                            print(
                                f"Reusing prior collection for Rundown: "
                                f"{job['id']} ({job['date_str']})"
                            )
                        else:
                            print(
                                f"Running Rundown collection: "
                                f"{job['id']} ({job['date_str']})"
                            )
                            rundown_lookback = _compute_lookback(store, "the-rundown")
                            rundown_coverage = store.recent_coverage_summary(
                                "the-rundown", days=3
                            )
                            rundown_prior_urls = store.recent_article_urls(
                                "the-rundown", days=3
                            )
                            collect_all_artifacts(
                                job["id"],
                                work_dir,
                                levine_cache_dir=Path(
                                    "/persist/my-podcasts/levine-cache"
                                ),
                                semafor_cache_dir=Path(
                                    "/persist/my-podcasts/semafor-cache"
                                ),
                                zvi_cache_dir=Path("/persist/my-podcasts/zvi-cache"),
                                fp_routed_dir=Path(
                                    "/persist/my-podcasts/fp-routed-links"
                                ),
                                lookback_days=rundown_lookback,
                                coverage_summary=rundown_coverage,
                                prior_urls=rundown_prior_urls,
                            )

                        if not plan_path.exists():
                            print(
                                f"No plan generated for Rundown {job['id']}, skipping"
                            )
                            continue

                        plan = RundownResearchPlan.model_validate_json(
                            plan_path.read_text()
                        )

                        sections, writer_inputs = _assemble_writer_inputs(
                            plan, work_dir
                        )
                        # A directive resolving to nothing used to vanish here
                        # with no counter and no log.
                        (work_dir / "writer_inputs.json").write_text(
                            json.dumps(writer_inputs, indent=2), encoding="utf-8"
                        )

                        context_scripts: list[str] = []
                        context_dir = work_dir / "context"
                        if context_dir.exists():
                            for f in sorted(context_dir.glob("*.txt"), reverse=True):
                                context_scripts.append(f.read_text(encoding="utf-8"))

                        writer_output = generate_rundown_script(
                            sections=sections,
                            date_str=job["date_str"],
                            context_scripts=context_scripts,
                            work_dir=work_dir,
                        )
                        script_file.parent.mkdir(parents=True, exist_ok=True)
                        script_file.write_text(writer_output.script, encoding="utf-8")
                        # Save summary for the processor
                        summary_file = work_dir / "summary.txt"
                        summary_file.write_text(writer_output.summary, encoding="utf-8")
                        # Save covered headlines for show notes filtering
                        if writer_output.covered_headlines:
                            import json as _json

                            covered_file = work_dir / "covered.json"
                            covered_file.write_text(
                                _json.dumps(writer_output.covered_headlines),
                                encoding="utf-8",
                            )
                        _report_run_stats(
                            work_dir,
                            job_id=job["id"],
                            date_str=job["date_str"],
                            reused_collection=reused_collection,
                        )
                        # Next loop will pick up the script and run TTS

                except Exception as exc:
                    retry = store.mark_the_rundown_failed(job["id"], str(exc))
                    if retry.exhausted:
                        print(
                            f"Failed Rundown job {job['id']}: {exc} "
                            f"(retry budget exhausted after #{retry.failure_count}; "
                            f"marked errored)"
                        )
                        from pipeline.alerts import send_alert

                        label = "The Rundown"
                        send_alert(
                            f"{label} job {job['date_str']} gave up after "
                            f"{retry.failure_count} failures.\n"
                            f"Last error: {exc}",
                            severity="error",
                        )
                    else:
                        print(
                            f"Failed Rundown job {job['id']}: {exc} "
                            f"(retry #{retry.failure_count} at {retry.process_after})"
                        )
        except Exception as exc:
            print(f"Error checking Rundown jobs: {exc}")

        # Process any due FP Digest jobs.
        try:
            fp_jobs = store.list_due_fp_digest()
            for job in fp_jobs:
                try:
                    work_dir = _work_dir_base() / f"fp-digest-{job['id']}"
                    script_file = work_dir / "script.txt"

                    if script_file.exists():
                        # Script already generated -- TTS + publish
                        dry_run = os.environ.get("FP_DIGEST_DRY_RUN", "").strip()
                        if dry_run:
                            print(
                                f"DRY RUN: skipping TTS for FP digest "
                                f"{job['id']} ({job['date_str']})"
                            )
                            store.mark_fp_digest_completed(job["id"])
                        else:
                            print(
                                f"Processing FP digest with script: "
                                f"{job['id']} ({job['date_str']})"
                            )
                            summary_text = None
                            summary_path = work_dir / "summary.txt"
                            if summary_path.exists():
                                summary_text = summary_path.read_text(encoding="utf-8")
                            process_fp_digest_job(
                                job,
                                store,
                                r2_client,
                                script_path=script_file,
                                work_dir=work_dir,
                                summary=summary_text,
                            )
                            print(f"Completed FP digest: {job['id']}")
                        # Copy to persistent storage
                        persist_dir = FP_DIGEST_SCRIPT_ARCHIVE_DIR
                        persist_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy(script_file, persist_dir / f"{job['date_str']}.txt")
                    else:
                        # Full pipeline: collect -> edit -> write -> save script
                        collection_sentinel = work_dir / "collection_done.json"
                        plan_path = work_dir / "plan.json"

                        if collection_sentinel.exists() and plan_path.exists():
                            print(
                                f"Reusing prior collection for FP digest: "
                                f"{job['id']} ({job['date_str']})"
                            )
                        else:
                            print(
                                f"Running FP digest collection: "
                                f"{job['id']} ({job['date_str']})"
                            )
                            fp_lookback = _compute_lookback(store, "fp-digest")
                            fp_coverage = store.recent_coverage_summary(
                                "fp-digest", days=3
                            )
                            fp_prior_urls = store.recent_article_urls(
                                "fp-digest", days=3
                            )
                            collect_fp_artifacts(
                                job["id"],
                                work_dir,
                                fp_routed_dir=Path(
                                    "/persist/my-podcasts/fp-routed-links"
                                ),
                                homepage_cache_dir=Path(
                                    "/persist/my-podcasts/antiwar-homepage-cache"
                                ),
                                antiwar_rss_cache_dir=Path(
                                    "/persist/my-podcasts/antiwar-rss-cache"
                                ),
                                semafor_cache_dir=Path(
                                    "/persist/my-podcasts/semafor-cache"
                                ),
                                lookback_days=fp_lookback,
                                coverage_summary=fp_coverage,
                                prior_urls=fp_prior_urls,
                            )

                        if not plan_path.exists():
                            print(
                                f"No plan generated for FP digest {job['id']}, skipping"
                            )
                            continue

                        plan = FPResearchPlan.model_validate_json(plan_path.read_text())

                        # Build articles_by_theme
                        articles_by_theme: dict[str, list[str]] = {}
                        for directive in plan.directives:
                            if not directive.include_in_episode:
                                continue
                            text = _find_article_text(directive, work_dir)
                            if text:
                                articles_by_theme.setdefault(
                                    directive.theme, []
                                ).append(text)

                        # Load context scripts
                        context_scripts = []
                        context_dir = work_dir / "context"
                        if context_dir.exists():
                            for f in sorted(context_dir.glob("*.txt"), reverse=True):
                                context_scripts.append(f.read_text(encoding="utf-8"))

                        writer_output = generate_fp_script(
                            themes=plan.themes,
                            articles_by_theme=articles_by_theme,
                            date_str=job["date_str"],
                            context_scripts=context_scripts,
                            work_dir=work_dir,
                        )
                        script_file.parent.mkdir(parents=True, exist_ok=True)
                        script_file.write_text(writer_output.script, encoding="utf-8")
                        # Save summary for the processor
                        summary_file = work_dir / "summary.txt"
                        summary_file.write_text(writer_output.summary, encoding="utf-8")
                        # Save covered headlines for show notes filtering
                        if writer_output.covered_headlines:
                            import json as _json

                            covered_file = work_dir / "covered.json"
                            covered_file.write_text(
                                _json.dumps(writer_output.covered_headlines),
                                encoding="utf-8",
                            )
                        # Next loop will pick up the script and run TTS
                except Exception as exc:
                    retry = store.mark_fp_digest_failed(job["id"], str(exc))
                    if retry.exhausted:
                        print(
                            f"Failed FP digest job {job['id']}: {exc} "
                            f"(retry budget exhausted after #{retry.failure_count}; "
                            f"marked errored)"
                        )
                        from pipeline.alerts import send_alert

                        label = "FP Digest"
                        send_alert(
                            f"{label} job {job['date_str']} gave up after "
                            f"{retry.failure_count} failures.\n"
                            f"Last error: {exc}",
                            severity="error",
                        )
                    else:
                        print(
                            f"Failed FP digest job {job['id']}: {exc} "
                            f"(retry #{retry.failure_count} at {retry.process_after})"
                        )
        except Exception as exc:
            print(f"Error checking FP digest jobs: {exc}")

        # Poll blog sources for new posts.
        try:
            global _last_blog_poll  # noqa: PLW0603
            now = time.time()
            if now - _last_blog_poll >= _BLOG_POLL_INTERVAL:
                from pipeline.blog_poller import poll_all_blogs

                print("Polling blog sources for new posts...")
                poll_all_blogs(store, r2_client)
                _last_blog_poll = now
        except Exception as exc:
            print(f"Error polling blog sources: {exc}")

        if not messages:
            time.sleep(poll_interval)
