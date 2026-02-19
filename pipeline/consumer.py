from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

from pipeline.processor import process_r2_email_key


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


def consume_forever(
    store: StateStore,
    r2_client: R2Client,
    poll_interval: int = 10,
) -> None:
    consumer = CloudflareQueueConsumer()

    while True:
        messages = consumer.pull(batch_size=5)
        if not messages:
            time.sleep(poll_interval)
            continue

        ack_messages: list[QueueMessage] = []
        for message in messages:
            if store.is_processed(message.key):
                ack_messages.append(message)
                continue

            try:
                process_r2_email_key(message.key, message.route_tag, store, r2_client)
            except Exception as exc:
                print(f"Failed processing {message.key}: {exc}")
                continue
            ack_messages.append(message)

        consumer.ack(ack_messages)
