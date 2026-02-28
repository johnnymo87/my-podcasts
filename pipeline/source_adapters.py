from __future__ import annotations

import email
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse, urlunparse

import requests


class SourceAdapter(Protocol):
    def format_title(
        self,
        *,
        date_str: str,
        subject_raw: str,
        subject_slug: str,
    ) -> str: ...

    def clean_body(self, *, raw_email: bytes, body: str) -> str: ...

    def extract_source_url(
        self,
        *,
        raw_email: bytes,
        date_str: str,
        subject_raw: str,
    ) -> str | None: ...


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _slugify_for_url(text: str) -> str:
    lower = text.lower().strip()
    lower = re.sub(r"[^a-z0-9\s-]", "", lower)
    lower = re.sub(r"\s+", "-", lower)
    return re.sub(r"-+", "-", lower).strip("-")


def _extract_candidate_links(raw_email: bytes) -> list[str]:
    msg = email.message_from_bytes(raw_email)
    chunks: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    chunks.append(
                        payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="replace",
                        )
                    )
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            chunks.append(
                payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="replace",
                )
            )

    text = "\n".join(chunks)
    url_pattern = re.compile(r"https?://[^\s<>'\"]+")
    urls = [u.rstrip(").,>") for u in url_pattern.findall(text)]
    return list(dict.fromkeys(urls))


def _extract_plain_text_part(raw_email: bytes) -> str | None:
    msg = email.message_from_bytes(raw_email)
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace",
                    )
    elif msg.get_content_type() == "text/plain":
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode(
                msg.get_content_charset() or "utf-8",
                errors="replace",
            )
    return None


def _resolve_once(url: str) -> str | None:
    try:
        response = requests.get(url, timeout=10, allow_redirects=False)
    except requests.RequestException:
        return None
    location = response.headers.get("Location")
    if not location:
        return None
    if location.startswith("/"):
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{location}"
    return location


def _extract_levine_source_url(
    raw_email: bytes,
    date_str: str,
    subject_raw: str,
) -> str | None:
    links = _extract_candidate_links(raw_email)

    newsletter_pattern = re.compile(
        r"^https://www\.bloomberg\.com/opinion/newsletters/\d{4}-\d{2}-\d{2}/[^/?#]+",
        flags=re.IGNORECASE,
    )

    for link in links:
        if newsletter_pattern.match(link):
            return _canonicalize_url(link)

    preferred_shortlinks = [
        link
        for link in links
        if link.startswith("https://bloom.bg/")
        or link.startswith("https://links.message.bloomberg.com/")
    ]
    for shortlink in preferred_shortlinks:
        redirected = _resolve_once(shortlink)
        if redirected and newsletter_pattern.match(redirected):
            return _canonicalize_url(redirected)

    match = re.match(r"^Money Stuff:\s*(.+)$", subject_raw.strip(), flags=re.IGNORECASE)
    if match:
        inferred_slug = _slugify_for_url(match.group(1))
        if inferred_slug:
            return (
                "https://www.bloomberg.com/opinion/newsletters/"
                f"{date_str}/{inferred_slug}"
            )
    return None


def _extract_substack_source_url(raw_email: bytes, domain: str) -> str | None:
    msg = email.message_from_bytes(raw_email)
    list_post = msg.get("List-Post", "")

    escaped_domain = re.escape(domain)
    substack_post_pattern = re.compile(
        rf"https://(?:www\.)?{escaped_domain}/p/[^\s<>?#]+",
        flags=re.IGNORECASE,
    )

    if list_post:
        header_match = substack_post_pattern.search(list_post)
        if header_match:
            return _canonicalize_url(header_match.group(0))

    links = _extract_candidate_links(raw_email)
    for link in links:
        if substack_post_pattern.match(link):
            return _canonicalize_url(link)

    redirect_links = [
        link
        for link in links
        if link.startswith("https://substack.com/redirect/")
        or f"{domain}/action/" in link
    ]
    for link in redirect_links:
        redirected = _resolve_once(link)
        if redirected and substack_post_pattern.match(redirected):
            return _canonicalize_url(redirected)

    return None


def _clean_substack_body(raw_email: bytes, fallback_body: str) -> str:
    source_text = _extract_plain_text_part(raw_email) or fallback_body
    text = source_text.replace("\r", "")
    text = text.replace("\u00ad", "").replace("\u034f", "")

    text = re.sub(r"^View this post on the web at .*\n+", "", text)
    text = re.sub(r"\s*\[\s*https?://[^\]]+\s*\]", "", text)
    text = re.sub(r"\nUnsubscribe\s+https?://.*", "", text, flags=re.DOTALL)
    text = text.replace("READ IN APP", "")
    text = text.replace("Subscribed", "")

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +\n", "\n", text)
    return text.strip()


@dataclass(frozen=True)
class DefaultAdapter:
    def format_title(
        self,
        *,
        date_str: str,
        subject_raw: str,
        subject_slug: str,
    ) -> str:
        return subject_raw.strip() or subject_slug.replace("-", " ")

    def clean_body(self, *, raw_email: bytes, body: str) -> str:
        return body

    def extract_source_url(
        self,
        *,
        raw_email: bytes,
        date_str: str,
        subject_raw: str,
    ) -> str | None:
        return None


@dataclass(frozen=True)
class LevineAdapter(DefaultAdapter):
    def format_title(
        self,
        *,
        date_str: str,
        subject_raw: str,
        subject_slug: str,
    ) -> str:
        subject = subject_raw.strip() or subject_slug.replace("-", " ")
        match = re.match(r"^Money Stuff:\s*(.+)$", subject, flags=re.IGNORECASE)
        if match:
            return f"{date_str} - Money Stuff - {match.group(1).strip()}"
        return f"{date_str} - {subject}"

    def extract_source_url(
        self,
        *,
        raw_email: bytes,
        date_str: str,
        subject_raw: str,
    ) -> str | None:
        return _extract_levine_source_url(raw_email, date_str, subject_raw)


@dataclass(frozen=True)
class SubstackAdapter(DefaultAdapter):
    brand_name: str
    domain: str

    def format_title(
        self,
        *,
        date_str: str,
        subject_raw: str,
        subject_slug: str,
    ) -> str:
        subject = subject_raw.strip() or subject_slug.replace("-", " ")
        subject = re.sub(
            rf"^{re.escape(self.brand_name)}:\s*",
            "",
            subject,
            flags=re.IGNORECASE,
        )
        return f"{date_str} - {self.brand_name} - {subject}"

    def clean_body(self, *, raw_email: bytes, body: str) -> str:
        return _clean_substack_body(raw_email, body)

    def extract_source_url(
        self,
        *,
        raw_email: bytes,
        date_str: str,
        subject_raw: str,
    ) -> str | None:
        return _extract_substack_source_url(raw_email, self.domain)


DEFAULT_ADAPTER = DefaultAdapter()

ADAPTERS: dict[str, SourceAdapter] = {
    "levine": LevineAdapter(),
    "yglesias": SubstackAdapter(brand_name="Slow Boring", domain="slowboring.com"),
    "silver": SubstackAdapter(brand_name="Silver Bulletin", domain="natesilver.net"),
}


def get_source_adapter(feed_slug: str) -> SourceAdapter:
    return ADAPTERS.get(feed_slug, DEFAULT_ADAPTER)
