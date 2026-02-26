from __future__ import annotations

import email
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup


if TYPE_CHECKING:
    from email.message import Message


class NoHtmlContentFoundError(Exception):
    """Raised when no HTML part is found in the email."""


class EmailProcessor:
    """
    Public interface for processing raw emails.

    Usage:

      processor = EmailProcessor(raw_email_str)
      result = processor.parse()

      Returns a dict: {"date": ..., "subject": ..., "body": ...}

    Optionally, you can write out the TTS-friendly cleaned body text to a file:

      output_path = processor.write_text_file()

      Writes to emails/<date-subject>.txt
    """

    def __init__(self, raw_email: str | bytes) -> None:
        if isinstance(raw_email, bytes):
            self._msg = email.message_from_bytes(raw_email)
        else:
            self._msg = email.message_from_string(raw_email)

    def parse(self) -> dict[str, str]:
        """
        Process the raw email string and return a dictionary containing:
          - "date": a date string in the format "YYYY-MM-DD"
          - "subject": a slugified subject string (e.g. "My-Apocalypse-The-End")
          - "body": TTS-friendly cleaned text from the HTML part

        Raises:
            NoHtmlContentFoundError: if no HTML part is found.
        """
        html_content = self._extract_html_part()
        date_str, subject_str, subject_raw = self._extract_date_and_subject()

        # Parse the HTML to prepare for cleaning.
        soup = BeautifulSoup(html_content, "html.parser")
        cleaned_body = self._clean_html(str(soup))
        # Inline any footnotes in the cleaned text.
        cleaned_body = self._inline_footnotes(cleaned_body)

        return {
            "date": date_str,
            "subject": subject_str,
            "subject_raw": subject_raw,
            "body": cleaned_body,
        }

    def write_text_file(self, output_dir: Path | None = None) -> Path:
        """
        Write the cleaned body text to a file using date and subject to name the file.
        Returns the Path to the created file.
        """
        result = self.parse()
        filename = f"{result['date']}-{result['subject']}.txt"

        if output_dir is None:
            output_dir = Path("emails")
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / filename
        output_path.write_text(result["body"], encoding="utf-8")
        return output_path

    # --------- Private helper methods below: ---------

    def _extract_html_part(self) -> str:
        """
        Extracts and returns the first 'text/html' part from a raw email string.
        Raises:
            NoHtmlContentFoundError: If none is found.
        """
        if self._msg.is_multipart():
            for part in self._msg.walk():
                if part.get_content_type() == "text/html":
                    return self._decode_payload(part)
        else:
            if self._msg.get_content_type() == "text/html":
                return self._decode_payload(self._msg)

        raise NoHtmlContentFoundError("No HTML part found in the email")

    def _decode_payload(self, part: Message) -> str:
        """
        Decodes and returns the payload of an email message part as a string.
        """
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode(part.get_content_charset() or "utf-8")
        else:
            raise TypeError(f"Expected bytes but got: {type(payload)}")

    def _extract_date_and_subject(self) -> tuple[str, str, str]:
        """
        Extracts a date (YYYY-MM-DD) and a slugified subject from the email headers.
        """
        raw_date = self._msg.get("Date", "")
        try:
            dt = parsedate_to_datetime(raw_date)
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            date_str = "9999-12-31"

        raw_subject_header = self._msg.get("Subject", "No Subject")
        try:
            raw_subject = str(make_header(decode_header(raw_subject_header))).strip()
        except Exception:
            raw_subject = str(raw_subject_header).strip()

        without_punc = re.sub(r"[^\w\s-]", "", raw_subject)
        subject_slug = re.sub(r"\s+", "-", without_punc.strip())

        return date_str, subject_slug, raw_subject

    def _clean_html(self, html_content: str) -> str:
        """
        Cleans HTML content so that the resulting text is friendly for TTS.
        Steps include:
          - Removing elements with style "display: none"
          - Removing everything after the last <div id="footnote-n"> (if present)
          - Inserting blockquote markers
          - Inserting extra Newlines for paragraphs
          - Removing artificial line breaks and extra spaces
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # First remove elements with style="display: none".
        for tag in soup.select('[style*="display: none"]'):
            tag.decompose()

        # Identify footnote divs by id=footnote-<digits>.
        footnote_divs = soup.find_all("div", id=re.compile(r"^footnote-\d+$"))
        if footnote_divs:
            # The last footnote div in the HTML:
            last_footnote_div = footnote_divs[-1]

            # Find all elements that come after the last footnote div in
            # document order. We need to traverse up to find the common
            # ancestor and remove everything after.
            current = last_footnote_div
            while current.parent:
                # For each sibling that comes after the current element, remove it
                for sibling in list(current.find_next_siblings()):
                    sibling.decompose()
                current = current.parent

        # Insert blockquote markers.
        for bq_tag in soup.find_all("blockquote"):
            bq_tag.insert_before("\n\nBlock quote begins.\n")
            bq_tag.insert_after("\n\nBlock quote ends.\n")

        # Insert extra newlines before paragraphs and headings.
        for p_tag in soup.find_all("p"):
            p_tag.insert_before("\n\n")
        for heading_tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            heading_tag.insert_before("\n\n")

        # Convert to text, then do final text cleanup.
        text_content = soup.get_text()

        # Remove artificial line breaks like '=\n'.
        text_content = re.sub(r"=\s*\n", "", text_content)
        # Collapse multiple blank lines to a double newline.
        text_content = re.sub(r"\n\s*\n\s*\n+", "\n\n", text_content)
        # Normalize all internal whitespace runs to single spaces (except newlines).
        text_content = re.sub(r"[^\S\r\n]+", " ", text_content)
        # Remove trailing spaces before newlines.
        text_content = re.sub(r" +(\n)", r"\1", text_content)

        return text_content.strip()

    def _inline_footnotes(self, text: str) -> str:
        """
        Finds and inlines footnotes of the form:

            [1] This is footnote #1 text
            [2] This is footnote #2 text

        at the bottom of the text. A pointer like [1] in the main body text is replaced
        with "Footnote begins. ... Footnote ends."

        If a pointer is encountered but no corresponding footnote is found, a ValueError
        is raised.
        """
        # Look for footnotes defined in lines at the bottom.
        # For example: a line like "[1] This is the footnote text."
        footnote_pattern = re.compile(r"^\[(\d+)\]\s*(.+)$", flags=re.MULTILINE)
        # Collect all footnotes into a dictionary: { "1": "This is the footnote text." }
        footnotes = dict(footnote_pattern.findall(text))

        # Remove lines that define footnotes from the text.
        text_without_footnotes = footnote_pattern.sub("", text)

        # For each occurrence of a footnote pointer in the main text (e.g. "[1]"),
        # replace it with an inline version containing the footnote text.
        def replace_pointer(match: re.Match[str]) -> str:
            num = match.group(1)
            if num in footnotes:
                return f"Footnote begins. {footnotes[num].strip()} Footnote ends."
            else:
                raise ValueError(f"Footnote {num} not found.")

        inline_pattern = re.compile(r"\[(\d+)\]")
        text_inlined = inline_pattern.sub(replace_pointer, text_without_footnotes)

        # Clean up any extra newlines and return the final result.
        return text_inlined.strip()
