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
        EmailParsingError: if email parsing fails
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

    # 5. Remove artificial line breaks (quoted-printable = \n)
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


def extract_metadata(raw_email_str: str, html_content: str) -> tuple[str, str, str]:
    """
    Return metadata for naming the output file:
      (date_part, title_text, first_header_text)
    - date_part is 'YYYY-MM-DD' derived from the Date header.
    - title_text is the <title> from the HTML (or "Untitled" if missing).
    - first_header_text is the text from the first <h1>.. <h6> found (or
      "NoHeader" if none found).
    """

    # 1. Parse the email for the date
    msg: Message = email.message_from_string(raw_email_str)
    raw_date = msg["Date"]

    try:
        dt = parsedate_to_datetime(raw_date)
        date_part = dt.strftime("%Y-%m-%d")
    except Exception:
        # fallback if Date header is missing or can't parse
        date_part = "9999-12-31"

    # 2. Parse the HTML to get the <title> and the first <h#> tag
    soup = BeautifulSoup(html_content, "html.parser")

    title_text = "Untitled"
    title_tag = soup.find("title")
    if title_tag:
        # .get_text(strip=True) to remove extra whitespace
        possible_title = title_tag.get_text(strip=True)
        if possible_title:
            title_text = possible_title

    # Try to find the first heading, e.g. <h1>, <h2>, ...
    first_header_text = "NoHeader"
    # This regex matches h1, h2, h3, h4, h5, or h6
    heading_tag = soup.find(re.compile(r"^h[1-6]$"))
    if heading_tag and heading_tag.get_text(strip=True):
        first_header_text = heading_tag.get_text(strip=True)

    return date_part, title_text, first_header_text


def process_raw_email(raw_email_str: str) -> tuple[str, str]:
    """
    High-level function:
      1) Extracts the HTML part from the raw email,
      2) Cleans it for TTS,
      3) Extracts 3 metadata strings (date_part, title, first_header),
      4) Returns (cleaned_text, recommended_filename_no_path).
    """
    html_content = extract_html_part(raw_email_str)
    cleaned_text = clean_html(html_content)

    date_part, title_text, first_header_text = extract_metadata(
        raw_email_str, html_content
    )
    # Replace spaces in title with dashes
    title_text_slug = re.sub(r"\s+", "-", title_text.strip())
    first_header_text_slug = re.sub(r"\s+", "-", first_header_text.strip())
    # e.g. "2025-01-27-Title-Of-Email-The-First-Header.txt"
    recommended_filename = f"{date_part}-{title_text_slug}-{first_header_text_slug}.txt"

    return cleaned_text, recommended_filename
