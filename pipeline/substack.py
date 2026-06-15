from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


@dataclass(frozen=True)
class SubstackPost:
    title: str
    subtitle: str
    description: str
    canonical_url: str
    body_html: str
    slug: str
    host: str
    audience: str
    wordcount: int


_SHORT_LINK_RE = re.compile(r"/p-(\d+)\b")
_SLUG_RE = re.compile(r"/p/([^/?#]+)")


def _api_url(url_or_id: str) -> str:
    """Map a Substack post reference to its JSON API URL.

    Accepts a bare numeric id, a short link (.../p-<id>), or a canonical
    slug URL (.../p/<slug>).
    """
    ref = url_or_id.strip()
    if ref.isdigit():
        return f"https://substack.com/api/v1/posts/by-id/{ref}"

    parsed = urlparse(ref)
    host = parsed.netloc or "substack.com"
    path = parsed.path

    m = _SHORT_LINK_RE.search(path)
    if m:
        return f"https://{host}/api/v1/posts/by-id/{m.group(1)}"

    m = _SLUG_RE.search(path)
    if m:
        return f"https://{host}/api/v1/posts/{m.group(1)}"

    raise ValueError(f"Unrecognized Substack post reference: {url_or_id!r}")


def resolve_post(url_or_id: str, *, timeout: int = 30) -> SubstackPost:
    """Fetch a Substack post via the JSON API.

    Raises ValueError if the post has no usable body (empty or paywalled).
    """
    api_url = _api_url(url_or_id)
    resp = requests.get(api_url, timeout=timeout)
    resp.raise_for_status()
    post = resp.json()["post"]

    body_html = post.get("body_html") or ""
    audience = post.get("audience") or ""
    if not body_html:
        raise ValueError(f"Substack post has no body: {url_or_id!r}")
    if audience != "everyone" and (
        post.get("should_send_free_preview") or post.get("truncated_body_text")
    ):
        raise ValueError(
            f"Substack post is behind a paywall (audience={audience!r}); "
            f"cannot fetch full text: {url_or_id!r}"
        )

    parsed = urlparse(post.get("canonical_url") or api_url)
    return SubstackPost(
        title=post.get("title") or "",
        subtitle=post.get("subtitle") or "",
        description=post.get("description") or "",
        canonical_url=post.get("canonical_url") or "",
        body_html=body_html,
        slug=post.get("slug") or "",
        host=parsed.netloc,
        audience=audience,
        wordcount=int(post.get("wordcount") or 0),
    )
