from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from email_processor.email_processor import (
    NoHtmlContentFoundError,
    clean_html,
    extract_html_part,
    process_raw_email,
)


def test_extract_html_part_multi_multipart() -> None:
    """
    Test that we can extract HTML content from a multipart email.
    """
    raw_email = """\
Content-Type: multipart/alternative; boundary="ABC"
MIME-Version: 1.0

--ABC
Content-Type: text/plain

This is plain text content.

--ABC
Content-Type: text/html

<html><body><p>This is an HTML part.</p></body></html>

--ABC--
"""
    html_content = extract_html_part(raw_email)
    assert "<html>" in html_content
    assert "<body>" in html_content
    assert "This is an HTML part." in html_content


def test_extract_html_part_single_part() -> None:
    """
    Test that we can extract HTML content from a single-part (non-multipart) email
    that is of type text/html.
    """
    raw_email = """\
Content-Type: text/html
MIME-Version: 1.0

<html>
<head></head>
<body>
    <p>Single part HTML content.</p>
</body>
</html>
"""
    html_content = extract_html_part(raw_email)
    assert "Single part HTML content." in html_content


def test_extract_html_no_html_part() -> None:
    """
    Test that NoHtmlContentFoundError is raised if there's no text/html part.
    """
    raw_email = """\
Content-Type: text/plain
MIME-Version: 1.0

Plain text only.
"""
    with pytest.raises(NoHtmlContentFoundError):
        extract_html_part(raw_email)


def test_clean_html_removes_display_none() -> None:
    """
    Test that elements with style="display: none" are removed from final text.
    """
    html_content = """\
<html>
<head></head>
<body>
    <div style="display: none;">Hidden preview text</div>
    <p>Visible content</p>
</body>
</html>
"""
    cleaned = clean_html(html_content)
    assert "Hidden preview text" not in cleaned
    assert "Visible content" in cleaned


def test_clean_html_removes_artificial_breaks() -> None:
    """
    Test that artificial line breaks (e.g. "= \n") are removed,
    but standard line breaks between paragraphs are preserved.
    """
    html_content = """\
<html>
<body>
    <p>First paragraph</p>
    Second line=
    more text

    <p>Second paragraph</p>
    Another=
    line=
    break
</body>
</html>
"""
    cleaned = clean_html(html_content)

    # Check that the artificial breaks "= \n" or "=  \n" are removed
    assert "= " not in cleaned

    # Check paragraphs remain separated by at least one blank line
    # because we inserted `\n\n` before each <p>
    paragraphs = cleaned.split("\n\n")
    # We expect at least 2 paragraphs
    assert len(paragraphs) >= 2
    assert "First paragraph" in paragraphs[0]
    assert "Second paragraph" in paragraphs[1]


def test_process_raw_email_integration() -> None:
    """
    Test the high-level process_raw_email function:
      1) extracts HTML part,
      2) cleans it,
      3) returns the cleaned text.
    """
    raw_email = """\
Content-Type: multipart/alternative; boundary="ABC"
MIME-Version: 1.0

--ABC
Content-Type: text/plain

This is plain text part.

--ABC
Content-Type: text/html

<html>
<head></head>
<body>
    <div style="display: none;">Preview text</div>
    <p>Some visible paragraph.</p>
</body>
</html>

--ABC--
"""
    cleaned_text, _ = process_raw_email(raw_email)
    assert "Preview text" not in cleaned_text
    assert "Some visible paragraph." in cleaned_text


def test_process_raw_email_raises_nohtml() -> None:
    """
    Test that process_raw_email raises NoHtmlContentFoundError
    if the email doesn't contain HTML.
    """
    raw_email = """\
Content-Type: text/plain
MIME-Version: 1.0

Just some text, no HTML here.
"""
    with pytest.raises(NoHtmlContentFoundError):
        process_raw_email(raw_email)


def test_clean_html_blockquote_markers() -> None:
    """
    Test that blockquote tags in the HTML are annotated with
    'Block quote begins.' and 'Block quote ends.' in the cleaned text.
    """
    html_content = """
    <html>
      <body>
        <p>Regular paragraph</p>
        <blockquote>
          <p>Blockquoted text, line one.</p>
          <p>Blockquoted text, line two.</p>
        </blockquote>
        <p>Another paragraph</p>
      </body>
    </html>
    """

    cleaned = clean_html(html_content)

    # Check that the blockquote markers are present
    assert "Block quote begins." in cleaned
    assert "Block quote ends." in cleaned

    # Check that original content is still present
    assert "Blockquoted text, line one." in cleaned
    assert "Blockquoted text, line two." in cleaned

    # Ensure paragraphs remain
    assert "Regular paragraph" in cleaned
    assert "Another paragraph" in cleaned


def test_process_raw_email_generates_filename(tmp_path: Path) -> None:
    """Check that process_raw_email returns a recommended filename
    based on the email's Date, <title>, and first heading.
    """
    raw_email = """\
Date: Tue, 01 Feb 2022 10:11:12 +0000
Content-Type: text/html
MIME-Version: 1.0

<html>
<head>
    <title>This is the Title</title>
</head>
<body>
<h1>My Main Header</h1>
<p>Some text here.</p>
</body>
</html>
"""

    cleaned_text, recommended_filename = process_raw_email(raw_email)
    assert cleaned_text is not None
    # The date was 2022-02-01
    # title is "This is the Title"
    # first header is "My Main Header"
    # So we expect: "2022-02-01-This-is-the-Title-My-Main-Header.txt"
    assert recommended_filename == "2022-02-01-This-is-the-Title-My-Main-Header.txt"

    # Optionally, write it out in a temp dir and confirm the file is written:
    emails_dir = tmp_path / "emails"
    emails_dir.mkdir(exist_ok=True)
    out_path = emails_dir / recommended_filename
    out_path.write_text(cleaned_text, encoding="utf-8")

    # Confirm the file got created
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8") == cleaned_text
