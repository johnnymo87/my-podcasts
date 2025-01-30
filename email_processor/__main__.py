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
    help="Path to a file containing raw email content",
)
@click.option(
    "--output-file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to save the processed text",
    default="processed_email.txt",
)
@click.argument("raw_email", required=False)
def main(
    input_file: Optional[TextIO],
    output_file: Path,
    raw_email: Optional[str],
) -> None:
    """Process raw email content into clean text suitable for TTS processing."""

    # Determine input source
    if input_file:
        content = input_file.read()
    elif raw_email:
        content = raw_email
    else:
        # Read from stdin if no other input provided
        content = click.get_text_stream("stdin").read()

    # Process the email
    try:
        cleaned_text = process_raw_email(content)
    except NoHtmlContentFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Write output
    output_file.write_text(cleaned_text)
    click.echo(f"Processed text saved to {output_file}")


if __name__ == "__main__":
    main()
