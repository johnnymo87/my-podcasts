# openai_tts

A CLI tool to generate speech from text using OpenAI's Text-to-Speech API.

## Installation

Make sure your `OPENAI_API_KEY` environment variable is set. Since you have an `.envrc` file, you can load it using `direnv allow` once you set it in there.

## Run the CLI

Use the CLI to generate speech from text.
```bash
poetry run python -m openai_tts "Your text here"
```

This will generate an `output.mp3` file in your current directory.

You can customize the model, voice, and output file name using options:

```bash
poetry run python -m openai_tts --model tts-1-hd --voice coral --output speech.mp3 "Today is a wonderful day!"
```

- `--model`: Choose between `tts-1` and `tts-1-hd`.
- `--voice`: Select a voice like `alloy`, `ash`, `coral`, etc.
- `--output`: Specify the output file name.
