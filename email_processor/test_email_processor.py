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
    cleaned_text = process_raw_email(raw_email)
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
