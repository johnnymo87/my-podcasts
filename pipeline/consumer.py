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
from pipeline.things_happen_agent import (
    is_agent_running,
    launch_things_happen_agent,
    stop_agent,
)
from pipeline.things_happen_collector import collect_all_artifacts
from pipeline.things_happen_processor import process_things_happen_job


if TYPE_CHECKING:
    from pipeline.db import StateStore
    from pipeline.r2 import R2Client


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
    tmp = Path("/tmp")
    for pattern in ("things-happen-*", "fp-digest-*"):
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

        # Process any due Things Happen jobs.
        try:
            due_jobs = store.list_due_things_happen()
            for job in due_jobs:
                try:
                    work_dir = Path(f"/tmp/things-happen-{job['id']}")
                    script_file = work_dir / "script.txt"

                    session_id = store.get_things_happen_session_id(job["id"])

                    if script_file.exists():
                        # Agent finished — run TTS + publish with the script.
                        try:
                            dry_run = os.environ.get(
                                "THINGS_HAPPEN_DRY_RUN", ""
                            ).strip()
                            if dry_run:
                                print(
                                    f"DRY RUN: skipping TTS for "
                                    f"{job['id']} ({job['date_str']}). "
                                    f"Script at: {script_file}"
                                )
                                store.mark_things_happen_completed(job["id"])
                            else:
                                print(
                                    f"Processing Things Happen job with "
                                    f"agent script: "
                                    f"{job['id']} ({job['date_str']})"
                                )
                                process_things_happen_job(
                                    job,
                                    store,
                                    r2_client,
                                    script_path=script_file,
                                )
                                print(f"Completed Things Happen job: {job['id']}")

                            # Copy to persistent storage for future context
                            persist_dir = Path(
                                "/persist/my-podcasts/scripts/things-happen"
                            )
                            persist_dir.mkdir(parents=True, exist_ok=True)
                            persist_path = persist_dir / f"{job['date_str']}.txt"
                            shutil.copy(script_file, persist_path)

                        finally:
                            _cleanup_old_work_dirs()
                            stop_agent(session_id)
                            store.clear_things_happen_session_id(job["id"])
                    elif not is_agent_running(session_id):
                        # No script, no agent — run collection then launch.
                        if session_id:
                            store.clear_things_happen_session_id(job["id"])

                        print(
                            f"Running collection phase for job: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        links_raw = json.loads(job["links_json"])
                        collect_all_artifacts(
                            job["id"],
                            links_raw,
                            work_dir,
                            fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
                        )

                        print(
                            f"Launching Things Happen agent for job: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        new_session_id = launch_things_happen_agent(job, work_dir)
                        if new_session_id:
                            store.set_things_happen_session_id(
                                job["id"], new_session_id
                            )
                    # else: agent is running, wait for it to finish.
                except Exception as exc:
                    print(f"Failed Things Happen job {job['id']}: {exc}")
        except Exception as exc:
            print(f"Error checking Things Happen jobs: {exc}")

        # Process any due FP Digest jobs.
        try:
            fp_jobs = store.list_due_fp_digest()
            for job in fp_jobs:
                try:
                    work_dir = Path(f"/tmp/fp-digest-{job['id']}")
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
                            process_fp_digest_job(
                                job, store, r2_client, script_path=script_file
                            )
                            print(f"Completed FP digest: {job['id']}")
                        # Copy to persistent storage
                        persist_dir = Path("/persist/my-podcasts/scripts/fp-digest")
                        persist_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy(script_file, persist_dir / f"{job['date_str']}.txt")
                    else:
                        # Full pipeline: collect -> edit -> write -> save script
                        print(
                            f"Running FP digest collection: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        collect_fp_artifacts(
                            job["id"],
                            work_dir,
                            fp_routed_dir=Path("/persist/my-podcasts/fp-routed-links"),
                        )

                        plan_path = work_dir / "plan.json"
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

                        script = generate_fp_script(
                            themes=plan.themes,
                            articles_by_theme=articles_by_theme,
                            date_str=job["date_str"],
                            context_scripts=context_scripts,
                        )
                        script_file.parent.mkdir(parents=True, exist_ok=True)
                        script_file.write_text(script, encoding="utf-8")
                        # Next loop will pick up the script and run TTS
                except Exception as exc:
                    print(f"Failed FP digest job {job['id']}: {exc}")
        except Exception as exc:
            print(f"Error checking FP digest jobs: {exc}")

        if not messages:
            time.sleep(poll_interval)
