from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from email_processor.api import EmailProcessor, NoHtmlContentFoundError

# ------------------------------------------------------------------------------
# Sample Emails for Testing
# ------------------------------------------------------------------------------

# A well-formed multipart email with both plain text and HTML parts.
MULTIPART_EMAIL = """\
Delivered-To: test@example.com
Date: Mon, 27 Jan 2025 19:32:33 +0000
Subject: Test Email
Content-Type: multipart/alternative; boundary="BOUNDARY"
MIME-Version: 1.0

--BOUNDARY
Content-Type: text/plain; charset="UTF-8"

This is plain text.

--BOUNDARY
Content-Type: text/html; charset="UTF-8"

<html>
  <head><title>Test Email</title></head>
  <body>
    <p>This is the <strong>HTML</strong> content.</p>
  </body>
</html>
--BOUNDARY--
"""

# A single-part email that only contains an HTML part.
SINGLEPART_EMAIL = """\
Date: Tue, 28 Jan 2025 10:00:00 +0000
Subject: SinglePart Test
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <head></head>
  <body>
    <p>Single part email content.</p>
  </body>
</html>
"""

# An email which does not contain any HTML part.
NO_HTML_EMAIL = """\
Content-Type: text/plain; charset="UTF-8"
MIME-Version: 1.0

Just plain text, no HTML here.
"""

# An email that exercises the HTML cleaning logic.
# It includes hidden content, artificial line breaks, a blockquote, and paragraphs.
CLEANING_EMAIL = """\
Date: Wed, 29 Jan 2025 12:00:00 +0000
Subject: Cleaning Test!
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <body>
    <div style="display: none;">Hidden preview text</div>
    <p>First paragraph with an artificial line break=
    \ncontinued on the same paragraph.</p>
    <blockquote>
      <p>Quote Line 1.</p>
      <p>Quote Line 2.</p>
    </blockquote>
    <p>Second paragraph.</p>
  </body>
</html>
"""

# An email with an invalid Date header and a subject with punctuation.
INVALID_DATE_EMAIL = """\
Date: Not a real date
Subject: My apocalypse: the end is near!
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <head></head>
  <body>
    <p>Some content here.</p>
  </body>
</html>
"""


def test_parse_email_returns_correct_keys() -> None:
    """Test that parsing a well-formed email returns the expected keys."""
    processor = EmailProcessor(MULTIPART_EMAIL)
    result = processor.parse()

    # Check that all required keys are present.
    assert "date" in result
    assert "subject" in result
    assert "body" in result

    # Verify that the cleaned body includes HTML text
    assert "HTML content" in result["body"]
    # Check the date formatting (YYYY-MM-DD)
    assert re.match(r"\d{4}-\d{2}-\d{2}", result["date"])
    # Check that the subject slug does not contain punctuation
    assert ":" not in result["subject"]
    assert " " not in result["subject"]


def test_parse_email_single_part() -> None:
    """Test that single-part HTML emails are processed correctly."""
    processor = EmailProcessor(SINGLEPART_EMAIL)
    result = processor.parse()
    assert "Single part email content." in result["body"]


def test_no_html_email_raises_error() -> None:
    """Test that an email without any HTML part raises NoHtmlContentFoundError."""
    processor = EmailProcessor(NO_HTML_EMAIL)
    with pytest.raises(NoHtmlContentFoundError):
        processor.parse()


def test_hidden_content_removed() -> None:
    """
    Test that HTML elements with style 'display: none' are removed from the
    output.
    """
    processor = EmailProcessor(CLEANING_EMAIL)
    result = processor.parse()
    # The hidden text should not appear in the cleaned body.
    assert "Hidden preview text" not in result["body"]


def test_artificial_line_break_removal() -> None:
    """
    Test that artificial line breaks (e.g. '= \n') are removed.
    In our sample, note that the artificial break appears as "= "
    followed by a newline in the raw HTML.
    """
    processor = EmailProcessor(CLEANING_EMAIL)
    result = processor.parse()
    # There should be no "=\n" patterns in the cleaned output.
    assert "= " not in result["body"]
    assert "continued on the same paragraph." in result["body"]


def test_blockquote_markers() -> None:
    """
    Test that <blockquote> sections in the HTML are annotated with markers
    indicating the beginning and end of a blockquote.
    """
    processor = EmailProcessor(CLEANING_EMAIL)
    result = processor.parse()
    cleaned = result["body"]
    assert "Block quote begins." in cleaned
    assert "Block quote ends." in cleaned
    # Make sure that the original quoted text is preserved.
    assert "Quote Line 1." in cleaned
    assert "Quote Line 2." in cleaned


def test_paragraph_separation() -> None:
    """
    Test that <p> tags yield extra newlines so that paragraphs remain
    clearly separated.
    """
    processor = EmailProcessor(CLEANING_EMAIL)
    result = processor.parse()
    cleaned = result["body"]
    # Check that there are at least two consecutive newlines before "Second paragraph."
    assert "\n\nSecond paragraph." in cleaned


def test_invalid_date_and_subject_slugification() -> None:
    """
    Test that if the Date header is missing or invalid, a default date is used,
    and that the subject is slugified (i.e. punctuation removed and spaces
    replaced with dashes).
    """
    processor = EmailProcessor(INVALID_DATE_EMAIL)
    result = processor.parse()
    # As the date is invalid, we expect fallback value "9999-12-31".
    assert result["date"] == "9999-12-31"
    # The subject "My apocalypse: the end is near!" should be slugified.
    # Expected slug: "My-apocalypse-the-end-is-near"
    assert result["subject"] == "My-apocalypse-the-end-is-near"


def test_write_text_file(tmp_path: Path) -> None:
    """
    Test that write_text_file writes out the cleaned body text
    into the emails directory with the correct filename.
    """
    processor = EmailProcessor(MULTIPART_EMAIL)
    output_path = processor.write_text_file(output_dir=tmp_path)

    # The file should exist.
    assert output_path.exists()

    # The content of the file should match the cleaned text.
    file_content = output_path.read_text(encoding="utf-8")
    result = processor.parse()
    assert file_content == result["body"]

    # Test that the filename is constructed as "{date}-{subject}.txt"
    expected_filename = f"{result['date']}-{result['subject']}.txt"
    assert output_path.name == expected_filename


def test_integration_full_processing() -> None:
    """
    Integration test: Process an email from raw input through the entire pipeline
    and verify that the output dictionary meets all requirements.
    """
    raw_email = CLEANING_EMAIL
    processor = EmailProcessor(raw_email)
    result = processor.parse()

    # Check metadata extraction
    assert re.match(r"\d{4}-\d{2}-\d{2}", result["date"])
    # Check subject has been slugified
    assert result["subject"] == "Cleaning-Test"
    # Check that the body does not contain hidden content or artificial breaks,
    # but does contain markers for blockquotes and extra newlines for <p>.
    body = result["body"]

    assert "Hidden preview text" not in body
    assert "Block quote begins." in body
    assert "Block quote ends." in body
    # There should be a double newline before each paragraph (heuristic)
    assert "\n\n" in body


def test_footnotes_inlining() -> None:
    """
    Test that a footnote pointer in the main text is replaced with an inline
    version (without the original pointer) that includes the footnote text, and
    that the standalone footnote definition is removed.
    """
    raw_email = """\
Date: Thu, 30 Jan 2025 15:00:00 +0000
Subject: Footnote Test
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <body>
    <p>This is main text with a footnote pointer [1] in it.</p>
    <p>Some additional text [2] in the body.</p>
    <hr>
    <p>[1] This is the first footnote content that should be inlined.</p>
    <p>[2] Second footnote, inlining as well.</p>
  </body>
</html>
"""
    processor = EmailProcessor(raw_email)
    result = processor.parse()
    body = result["body"]

    # The pointers should be replaced with the inlined footnotes, without the
    # original pointer strings.
    assert (
        "Footnote begins. This is the first footnote content that should be inlined."
        " Footnote ends." in body
    )
    assert "Footnote begins. Second footnote, inlining as well. Footnote ends." in body
    # Confirm that the original pointers [1] and [2] do not remain.
    assert "[1]" not in body
    assert "[2]" not in body


def test_raise_value_error_on_missing_footnote() -> None:
    """
    Test that if a footnote pointer is encountered in the main text but no matching
    footnote definition exists, a ValueError is raised.
    """
    raw_email = """\
Date: Fri, 31 Jan 2025 12:00:00 +0000
Subject: Missing Footnote Test
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <body>
    <p>Main text with an undefined footnote pointer [3].</p>
    <hr>
    <p>[1] This footnote is defined, but pointer [3] is not.</p>
  </body>
</html>
"""
    processor = EmailProcessor(raw_email)
    with pytest.raises(ValueError) as excinfo:
        processor.parse()
    assert "Footnote 3 not found." in str(excinfo.value)


def test_remove_text_after_last_footnote_div() -> None:
    """
    Verify that text appearing after the final footnote-(digit) DIV
    is removed. In this example, there are two footnote DIVs, then
    additional text. That extra text should not appear in the final
    output at all.
    """
    raw_email = """\
Date: Sat, 01 Feb 2025 10:00:00 +0000
Subject: Footnotes Div Test
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
<body>
  <div class="article">
    <p>This is the main article content.</p>
    <p>This is main text with a footnote pointer [1] in it.</p>
    <p>Some additional text [2] in the body.</p>
    <div id="footnote-1" style="font-style: italic;">
      <p>[1] This is the first footnote content that should be inlined.</p>
    </div>
    <div id="footnote-2" style="font-style: italic;">
      <p>[2] Second footnote, inlining as well.</p>
    </div>
  </div>
  <div class="post-article-content">
    <h2>Related Articles</h2>
    <ul>
      <li><a href="#">Article 1</a></li>
      <li><a href="#">Article 2</a></li>
    </ul>
    <p>This is some extra text after the footnotes that should be removed.</p>
  </div>
</body>
</html>
"""
    processor = EmailProcessor(raw_email)
    result = processor.parse()
    body = result["body"]

    # Main article content should remain.
    assert "This is the main article content." in body

    # The pointers should be replaced with the inlined footnotes, without the
    # original pointer strings.
    assert (
        "Footnote begins. This is the first footnote content that should be inlined."
        " Footnote ends." in body
    )
    assert "Footnote begins. Second footnote, inlining as well. Footnote ends." in body

    # All following content should be removed.
    assert "Related Articles" not in body
    assert "Article 1" not in body
    assert "Article 2" not in body
    assert (
        "This is some extra text after the footnotes that should be removed" not in body
    )
