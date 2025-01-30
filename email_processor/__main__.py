import sys
from pathlib import Path
from typing import Optional, TextIO

import click

from email_processor.email_processor import (
    NoHtmlContentFoundError,
    process_raw_email,
)


@click.command()
@click.option(
    "--input-file",
    type=click.File("r"),
    help="Path to a file containing raw email content (if not provided, "
    + "raw_email argument or stdin will be used).",
)
@click.argument("raw_email", required=False)
def main(
    input_file: Optional[TextIO],
    raw_email: Optional[str],
) -> None:
    """Process raw email content into clean text suitable for TTS,
    then save the result in emails/ with a filename derived from the
    email metadata (Date header, <title>, and first <h#>).
    """

    # 1. Determine input source
    if input_file:
        content = input_file.read()
    elif raw_email:
        content = raw_email
    else:
        # Read from stdin if no other input provided
        content = click.get_text_stream("stdin").read()

    # 2. Process the email
    try:
        cleaned_text, recommended_filename = process_raw_email(content)
    except NoHtmlContentFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # 3. Construct the final path in emails/ directory
    emails_dir = Path("emails")
    emails_dir.mkdir(parents=True, exist_ok=True)
    output_path = emails_dir / recommended_filename

    # 4. Write out the file
    output_path.write_text(cleaned_text, encoding="utf-8")

    click.echo(f"Processed text saved to {output_path}")


if __name__ == "__main__":
    main()
