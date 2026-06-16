from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from pipeline.document import Document


_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)")
_ATOM = "{http://www.w3.org/2005/Atom}"


def matches(url: str) -> bool:
    ref = url.strip()
    host = urlparse(ref).netloc.lower()
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return True
    bare = re.sub(r"(?i)^arxiv:", "", ref)
    return bool(_ID_RE.fullmatch(bare))


def parse_arxiv_id(url_or_id: str) -> str:
    ref = re.sub(r"(?i)^arxiv:", "", url_or_id.strip())
    m = _ID_RE.search(ref)
    if not m:
        raise ValueError(f"Unrecognized arXiv reference: {url_or_id!r}")
    return m.group(1)


def _fetch_metadata(arxiv_id: str, *, timeout: int) -> dict:
    api_url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    resp = requests.get(api_url, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        raise ValueError(f"arXiv API returned no entry for {arxiv_id!r}")
    title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
    summary = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
    authors = [
        " ".join((a.findtext(f"{_ATOM}name") or "").split())
        for a in entry.findall(f"{_ATOM}author")
    ]
    abs_id = (entry.findtext(f"{_ATOM}id") or "").strip()
    # abs_id looks like http://arxiv.org/abs/2407.16314v1 — derive versioned id.
    versioned = parse_arxiv_id(abs_id) if abs_id else arxiv_id
    canonical = (
        abs_id.replace("http://", "https://")
        if abs_id
        else f"https://arxiv.org/abs/{versioned}"
    )
    return {
        "title": title,
        "summary": summary,
        "authors": [a for a in authors if a],
        "canonical_url": canonical,
        "versioned_id": versioned,
    }


def _fetch_body_html(versioned_id: str, *, timeout: int) -> str:
    html_url = f"https://arxiv.org/html/{versioned_id}"
    resp = requests.get(html_url, timeout=timeout)
    if resp.status_code == 404:
        raise ValueError(
            f"arXiv paper {versioned_id!r} has no HTML rendering (PDF only); "
            "cannot build a report."
        )
    resp.raise_for_status()
    return resp.text


def _html_to_report_text(body_html: str) -> str:
    soup = BeautifulSoup(body_html, "html.parser")
    article = soup.find("article") or soup
    for tag in ("script", "style", "nav"):
        for n in article.find_all(tag):
            n.decompose()
    # Remove bibliography before extracting text (keeps body clean, no bib blow-up).
    # Guard: the compound selector can match both a section and its child ul;
    # decomposing the section detaches the child — skip already-detached nodes.
    for n in article.select("section.ltx_bibliography, ul.ltx_biblist"):
        if n.parent is not None:
            n.decompose()
    # Remove footnotes — LaTeXML renders them inline with triple-repeated markers
    # (e.g. "1 1 1 …") that leak ~700 words of noise into the report body.
    for n in article.select(".ltx_note"):
        n.decompose()
    # Remove math and image elements — they don't render as text.
    for n in article.find_all("math"):
        n.decompose()
    for n in article.find_all("img"):
        n.decompose()
    # Replace figures (incl. ltx_table figures) with their caption text only.
    for fig in article.find_all("figure"):
        cap = fig.find("figcaption")
        fig.replace_with(cap.get_text(" ", strip=True) if cap else "")
    # Replace any remaining standalone tables with their caption text only.
    for tbl in article.find_all("table"):
        cap = tbl.find("caption")
        tbl.replace_with(cap.get_text(" ", strip=True) if cap else "")
    text = article.get_text(separator="\n\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def resolve(url: str, *, timeout: int = 30) -> Document:
    arxiv_id = parse_arxiv_id(url)
    meta = _fetch_metadata(arxiv_id, timeout=timeout)
    versioned = meta["versioned_id"]
    body_html = _fetch_body_html(versioned, timeout=timeout)
    report_text = _html_to_report_text(body_html)
    if not report_text.strip():
        raise ValueError(f"arXiv HTML produced empty report text for {versioned!r}")
    return Document(
        title=meta["title"],
        byline=", ".join(meta["authors"]),
        canonical_url=meta["canonical_url"],
        description=meta["summary"],
        report_text=report_text,
        read_html=None,
        slug=versioned.replace(".", "-"),
        style="paper",
        wordcount=len(report_text.split()),
        default_category="Science",
    )
