import json
import sys
from typing import TextIO

import click

from email_processor.api import EmailProcessor, NoHtmlContentFoundError


@click.command()
@click.option(
    "--input-file",
    type=click.File("r"),
    help="Path to a file containing raw email content (if not provided, "
    " raw_email argument or stdin will be used).",
)
@click.option(
    "--json-output/--no-json-output",
    default=False,
    help="If set, print the processed email data as JSON.",
)
@click.option(
    "--write-text-file/--no-write-text-file",
    default=False,
    help="If set, write the cleaned body text to a file (under emails/).",
)
@click.argument("raw_email", required=False)
def main(
    input_file: TextIO | None,
    json_output: bool,
    write_text_file: bool,
    raw_email: str | None,
) -> None:
    """
    Process raw email content into a dictionary containing email data.
    Depending on the flags, it will print JSON and/or write the cleaned text to a file.
    """
    if input_file:
        content = input_file.read()
    elif raw_email:
        content = raw_email
    else:
        content = click.get_text_stream("stdin").read()

    try:
        processor = EmailProcessor(content)
        result = processor.parse()

        if json_output:
            click.echo(json.dumps(result, indent=2))

        if write_text_file:
            output_path = processor.write_text_file()
            click.echo(f"Body text saved to {output_path}")

        if not json_output and not write_text_file:
            click.echo(
                "Processing complete. Use --json-output or --write-text-file"
                " to output the results."
            )
    except NoHtmlContentFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
