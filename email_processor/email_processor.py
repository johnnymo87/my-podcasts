from __future__ import annotations

import email
import re
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from email.message import Message

from bs4 import BeautifulSoup


class NoHtmlContentFoundError(Exception):
    """Raised when no HTML part is found in the email."""


def _extract_payload_as_str(part: Message) -> str:
    """
    Helper to get the payload from a Message and ensure it's returned as a string,
    decoding bytes if needed.
    """
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(part.get_content_charset() or "utf-8")
    elif isinstance(payload, str):
        return payload
    else:
        raise TypeError(f"Expected bytes or str but got: {type(payload)}")


def extract_html_part(raw_email_str: str) -> str:
    """
    Extracts and returns the HTML content from a raw email string.
    Raises:
        NoHtmlContentFoundError: if no HTML part is found
    """
    msg: Message = email.message_from_string(raw_email_str)

    if msg.is_multipart():
        # Walk through the email parts to find an HTML part
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return _extract_payload_as_str(part)
    else:
        # If it's not multipart, check if this single part has HTML
        if msg.get_content_type() == "text/html":
            return _extract_payload_as_str(msg)

    # If we get here, we never found HTML
    raise NoHtmlContentFoundError("No HTML part found in the email")


def clean_html(html_content: str) -> str:
    """
    Cleans the HTML content:
      - Removes elements with style="display: none"
      - Removes artificial line breaks
      - Preserves paragraph breaks
      - Inserts blockquote markers
      - Returns cleaned text suitable for TTS.
    """

    soup = BeautifulSoup(html_content, "html.parser")

    # 1. Remove elements with style="display: none"
    for tag in soup.select('[style*="display: none"]'):
        tag.decompose()

    # 2. Insert blockquote markers
    for bq_tag in soup.find_all("blockquote"):
        bq_tag.insert_before("\n\nBlock quote begins.\n")
        bq_tag.insert_after("\n\nBlock quote ends.\n")

    # 3. Insert paragraph separators by adding extra newlines before <p>
    for p_tag in soup.find_all("p"):
        p_tag.insert_before("\n\n")

    # 4. Extract text
    text_content = soup.get_text()

    # 5. Remove artificial line breaks (e.g. "= \n")
    text_content = re.sub(r"=\s*\n", "", text_content)

    # 6. Collapse runs of blank lines into one double-newline
    text_content = re.sub(r"\n\s*\n\s*\n+", "\n\n", text_content)

    # 7. Convert runs of spaces/tabs (not newlines) into a single space
    text_content = re.sub(r"[^\S\r\n]+", " ", text_content)

    # 8. Trim trailing spaces before newlines
    text_content = re.sub(r" +(\n)", r"\1", text_content)

    # 9. Finally, strip leading/trailing whitespace
    cleaned = text_content.strip()

    return cleaned


def extract_date_and_subject(raw_email_str: str) -> tuple[str, str]:
    """
    Return (date_part, subject_slug), where:
      - date_part = 'YYYY-MM-DD' from the 'Date' header
      - subject_slug = the 'Subject' header, minus punctuation, spaces replaced
        by dashes
    """
    msg: Message = email.message_from_string(raw_email_str)

    # 1. Parse the Date header
    raw_date = msg.get("Date", "")
    try:
        dt = parsedate_to_datetime(raw_date)
        date_part = dt.strftime("%Y-%m-%d")
    except Exception:
        # Fallback if Date header is missing or can't parse
        date_part = "9999-12-31"

    # 2. Parse the Subject header
    raw_subject = msg.get("Subject", "No Subject")
    # Remove punctuation (e.g. ":"), then replace runs of whitespace with a dash
    # Example: "My apocalypse: the end?" -> "My-apocalypse-the-end"
    # (You could refine as needed.)
    without_punc = re.sub(r"[^\w\s-]", "", raw_subject)
    subject_slug = re.sub(r"\s+", "-", without_punc.strip())

    return date_part, subject_slug


def process_raw_email(raw_email_str: str) -> tuple[str, str]:
    """
    High-level function:
      1) Extracts the HTML part from the raw email,
      2) Cleans it for TTS,
      3) Extracts (date_part, subject_slug),
      4) Returns (cleaned_text, recommended_filename).
    """
    html_content = extract_html_part(raw_email_str)
    cleaned_text = clean_html(html_content)

    date_part, subject_slug = extract_date_and_subject(raw_email_str)
    recommended_filename = f"{date_part}-{subject_slug}.txt"

    return cleaned_text, recommended_filename
