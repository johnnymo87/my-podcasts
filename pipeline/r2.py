from __future__ import annotations

import os
from typing import TYPE_CHECKING

import boto3


if TYPE_CHECKING:
    from pathlib import Path


class R2Client:
    def __init__(self, bucket_name: str = "my-podcasts") -> None:
        account_id = os.environ["R2_ACCOUNT_ID"]
        access_key_id = os.environ["R2_ACCESS_KEY_ID"]
        secret_access_key = os.environ["R2_SECRET_ACCESS_KEY"]

        self._bucket_name = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    def get_object_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket_name, Key=key)
        body = response["Body"].read()
        if not isinstance(body, bytes):
            raise TypeError("R2 get_object returned non-bytes body")
        return body

    def put_object_bytes(
        self,
        key: str,
        body: bytes,
        content_type: str | None = None,
    ) -> None:
        kwargs: dict[str, str | bytes] = {
            "Bucket": self._bucket_name,
            "Key": key,
            "Body": body,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        self._client.put_object(**kwargs)

    def upload_file(
        self,
        local_path: Path,
        key: str,
        content_type: str | None = None,
    ) -> None:
        extra_args: dict[str, str] | None = None
        if content_type:
            extra_args = {"ContentType": content_type}
        self._client.upload_file(
            Filename=str(local_path),
            Bucket=self._bucket_name,
            Key=key,
            ExtraArgs=extra_args,
        )

    def head_object_size(self, key: str) -> int:
        response = self._client.head_object(Bucket=self._bucket_name, Key=key)
        size = response.get("ContentLength")
        if not isinstance(size, int):
            raise TypeError(f"Invalid ContentLength for {key}")
        return size
