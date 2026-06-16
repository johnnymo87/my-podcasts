from __future__ import annotations

import re
from urllib.parse import urlparse


_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)")


def matches(url: str) -> bool:
    ref = url.strip()
    host = urlparse(ref).netloc.lower()
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return True
    bare = re.sub(r"(?i)^arxiv:", "", ref)
    return bool(_ID_RE.fullmatch(bare))


def resolve(url: str):  # pragma: no cover - replaced in Task 3
    raise NotImplementedError
