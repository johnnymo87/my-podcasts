# Email Processor

The Email Processor is a tool that transforms raw email content into clean, TTS-friendly text. It now exposes a data-driven API through the `EmailProcessor` class so that you can easily integrate it with other systems (like an RSS feed generator) or write the output to a file for use with text-to-speech services.

> **Note:** For overall project setup and development instructions, please refer to the README in the project root.

## Features

- **Robust Input Handling:** Accepts emails provided via stdin, as a command-line argument, or loaded from a file.
- **HTML Extraction:** Extracts the HTML part from both multipart and single‑part emails.
- **Data‑Driven API:** Parses email metadata (such as date and subject) and cleans the HTML into readable text.
- **TTS-Friendly Cleaning:** Removes hidden elements, normalizes whitespace, annotates block quotes, and preserves paragraph breaks.
- **File Output:** Easily write the cleaned text to disk using a standardized filename format.

## Usage

### As a Library

You can import and use the public API directly from Python:

```python
from email_processor.api import EmailProcessor, NoHtmlContentFoundError

raw_email = """\
Date: Mon, 27 Jan 2025 19:32:33 +0000
Subject: Test Email
Content-Type: text/html; charset="UTF-8"
MIME-Version: 1.0

<html>
  <body>
    <p>This is the <strong>HTML</strong> content.</p>
  </body>
</html>
"""

try:
    processor = EmailProcessor(raw_email)
    result = processor.parse()
    # result is a dict with keys: "date", "subject", "body"
    print(result)
except NoHtmlContentFoundError as e:
    print("Error processing email:", e)
```

### From the Command Line

A CLI is provided that leverages the public API. You can output the processed data as JSON or write the cleaned text to a file.

```bash
# Print the structured result as JSON:
poetry run python -m email_processor --json-output --input-file path/to/raw_email.txt

# Write the cleaned text to a file (saved in the emails/ directory):
poetry run python -m email_processor --write-text-file --input-file path/to/raw_email.txt
```

Also, the program can take input from STDIN.

```bash
pbpaste | poetry run python -m email_processor --write-text-file
```

If neither flag is provided, the CLI will notify you that no output option was selected.

## Testing

A comprehensive test suite verifies the functionality of the Email Processor. To run the tests:

```bash
poetry run pytest tests/
```

The tests cover the following aspects:
- Correct extraction of HTML from multipart and single‑part emails.
- Removal of unwanted content (e.g. elements with `display: none`).
- Proper handling of artificial line breaks, block quotes, and paragraph formatting.
- Extraction and formatting of metadata (date and subject), including fallback values.
- End-to-end integration, including file writing with the correct filename convention.

## Future Enhancements

While this version focuses on the pure refactoring into a data‑driven API, additional features (such as harvesting hyperlinks for advanced integrations) can be added in subsequent releases.

---

For additional details about project setup (Python version, Poetry, CI, etc.), please see the main [README in the project root](../README.md).
