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
    """Remove things-happen work directories older than max_age_days."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=max_age_days)
    tmp = Path("/tmp")
    for d in tmp.glob("things-happen-*"):
        if not d.is_dir():
            continue
        try:
            mtime = datetime.fromtimestamp(d.stat().st_mtime, tz=UTC)
            if mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                print(f"Cleaned up old work dir: {d.name}")
        except OSError:
            pass


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

                            # Use shutil.copy to overwrite if it exists
                            shutil.copy(script_file, persist_path)

                        finally:
                            # Clean up old work directories (>6 months)
                            _cleanup_old_work_dirs()
                            stop_agent()
                    elif not is_agent_running():
                        # No script, no agent — run collection phase then launch agent.
                        print(
                            f"Running collection phase for job: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        links_raw = json.loads(job["links_json"])
                        collect_all_artifacts(job["id"], links_raw, work_dir)

                        print(
                            f"Launching Things Happen agent for job: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        launch_things_happen_agent(job, work_dir)
                    # else: agent is running, wait for it to finish.
                except Exception as exc:
                    print(f"Failed Things Happen job {job['id']}: {exc}")
        except Exception as exc:
            print(f"Error checking Things Happen jobs: {exc}")

        if not messages:
            time.sleep(poll_interval)
