"""Download a podcast enclosure to disk, refusing partial or oversized files."""

from __future__ import annotations

import os
from pathlib import Path

import httpx


MAX_BYTES = 300 * 1024 * 1024
_TIMEOUT = httpx.Timeout(30.0, read=60.0)


class DownloadError(RuntimeError):
    pass


def download_enclosure(
    url: str,
    dest: Path,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = MAX_BYTES,
) -> int:
    if dest.exists() and dest.stat().st_size > 0:
        return dest.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    own = client is None
    client = client or httpx.Client(
        follow_redirects=True,
        timeout=_TIMEOUT,
        headers={"User-Agent": "my-podcasts/1.0"},
    )
    try:
        with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as resp:
            if resp.status_code != 200:
                raise DownloadError(f"HTTP {resp.status_code} for {url}")
            declared = resp.headers.get("content-length")
            written = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise DownloadError(f"download exceeds {max_bytes} bytes")
                    fh.write(chunk)
        if (
            declared is not None
            and declared.isascii()
            and declared.isdigit()
            and int(declared) != written
        ):
            raise DownloadError(f"incomplete download: {written} of {declared} bytes")
        if written == 0:
            raise DownloadError("incomplete download: empty body")
        os.replace(tmp, dest)
        return written
    except httpx.RemoteProtocolError as exc:
        raise DownloadError(f"incomplete download: {exc!r}") from exc
    except httpx.HTTPError as exc:
        raise DownloadError(f"download failed: {type(exc).__name__}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
        if own:
            client.close()
