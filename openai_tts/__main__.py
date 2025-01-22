import os
import sys
from typing import Literal, Optional, TextIO, cast

import click
from openai import OpenAI

VoiceType = Literal["alloy", "echo", "fable", "onyx", "nova", "shimmer"]


@click.command()
@click.option(
    "--model",
    default="tts-1",
    help="Model to use for TTS (e.g., tts-1, tts-1-hd)",
)
@click.option(
    "--voice",
    default="alloy",
    type=click.Choice(
        [
            "alloy",
            "ash",
            "coral",
            "echo",
            "fable",
            "onyx",
            "nova",
            "sage",
            "shimmer",
        ]
    ),
    help="Voice to use for TTS",
)
@click.option("--output", default="output.mp3", help="Output file name")
@click.option(
    "--input-file",
    type=click.File("r"),
    help="Path to a text file containing the input text",
)
@click.argument("text", required=False)
def tts(
    model: str,
    voice: str,
    output: str,
    input_file: Optional[TextIO],
    text: Optional[str],
) -> None:
    """Generate speech from TEXT using OpenAI's TTS API."""

    # Read API key
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise click.ClickException("OPENAI_API_KEY environment variable not set")

    # Determine the input text
    if input_file:
        text = input_file.read()
    elif text:
        pass  # Text is already provided via positional argument
    else:
        # Read from stdin if no text or input file is provided
        if not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            raise click.ClickException(
                "No input provided. Please provide TEXT as an argument, use "
                "--input-file, or pipe input into the script."
            )

    assert text is not None  # Ensure text is a string (not None)

    client = OpenAI(api_key=api_key)

    try:
        voice = cast("VoiceType", voice)
        response = client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
        )
        response.stream_to_file(output)
        click.echo(f"Audio saved to {output}")
    except Exception as e:
        click.echo(f"An error occurred: {e}", err=True)


if __name__ == "__main__":
    tts()
