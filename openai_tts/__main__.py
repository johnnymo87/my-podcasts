import os
from typing import Literal, cast

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
        ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]
    ),
    help="Voice to use for TTS",
)
@click.option("--output", default="output.mp3", help="Output file name")
@click.argument("text", required=True)
def tts(model: str, voice: str, output: str, text: str) -> None:
    """Generate speech from TEXT using OpenAI's TTS API."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise click.ClickException("OPENAI_API_KEY environment variable not set")

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
