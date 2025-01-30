# Email Processor

A tool to process raw email content into clean text suitable for TTS processing.

## Usage

Process raw email content from a file:

```bash
poetry run python -m email_processor --input-file raw_email.txt
```

Or pipe content directly:

```bash
cat raw_email.txt | poetry run python -m email_processor
```

Or provide content as an argument:

```bash
poetry run python -m email_processor "Raw email content here..."
```

The processed text will be saved to a file located in the `emails/` folder. The filename is derived from the email’s `Date` header, the `<title>` from the HTML, and the first header tag (e.g. `<h1>`). For example:

```
emails/2025-01-27-Subject.txt
```

You can then use this resulting file with `tts-joinery`, for example:

```bash
poetry run ttsjoin \
  --input-file emails/2025-01-27-Subject.txt \
  --output-file audio/2025-01-27-Subject.mp3 \
  --model tts-1-hd \
  --voice ash
```
