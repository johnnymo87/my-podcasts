from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pipeline import arxiv
from pipeline import substack as substack_mod
from pipeline.document import Document


@dataclass(frozen=True)
class Adapter:
    name: str
    matches: Callable[[str], bool]
    resolve: Callable[[str], Document]


# Wrappers defer attribute lookup to call time so tests can patch
# pipeline.arxiv.resolve / pipeline.sources._substack_resolve.
#
# Arxiv: _arxiv_resolve calls arxiv.resolve via module-attribute lookup, which
# @patch("pipeline.arxiv.resolve") intercepts naturally.
#
# Substack: _substack_resolve is defined in *this* module, so storing it
# directly in Adapter would freeze the reference at import time and bypass
# @patch. _substack_resolve_dispatch does a call-time globals() lookup instead.
# Any future adapter whose resolve function lives in sources.py needs the same
# dispatch pattern; adapters backed by an external module (like arxiv) do not.
def _arxiv_matches(url: str) -> bool:
    return arxiv.matches(url)


def _arxiv_resolve(url: str) -> Document:
    return arxiv.resolve(url)


def _substack_matches(url: str) -> bool:
    ref = url.strip().lower()
    if ref.isdigit():  # bare Substack post id (legacy substack-command behavior)
        return True
    # /p/<slug> and /p-<id> are Substack path conventions,
    # present on custom domains too.
    return (
        "substack.com" in ref
        or "/p/" in ref
        or "/p-" in ref
        or ".dwarkesh.com" in ref
    )


def _substack_resolve(url: str) -> Document:
    post = substack_mod.resolve_post(url)
    return Document(
        title=post.title,
        byline=post.subtitle,
        canonical_url=post.canonical_url,
        description=post.subtitle or post.description,
        report_text=substack_mod.html_to_clean_text(post.body_html),
        read_html=post.body_html,
        slug=post.slug or "post",
        style="interview",
        wordcount=post.wordcount,
        default_category="Technology",
    )


def _substack_resolve_dispatch(url: str) -> Document:
    """Indirection so @patch('pipeline.sources._substack_resolve') is intercepted."""
    return globals()["_substack_resolve"](url)


# Order: arxiv first (specific), substack last (permissive fallback).
ADAPTERS: list[Adapter] = [
    Adapter("arxiv", _arxiv_matches, _arxiv_resolve),
    Adapter("substack", _substack_matches, _substack_resolve_dispatch),
]


def resolve_document(url: str, *, source: str | None = None) -> Document:
    if source is not None:
        for a in ADAPTERS:
            if a.name == source:
                return a.resolve(url)
        raise ValueError(f"Unknown source adapter: {source!r}")
    for a in ADAPTERS:
        if a.matches(url):
            return a.resolve(url)
    raise ValueError(f"No source adapter matched: {url!r}")
