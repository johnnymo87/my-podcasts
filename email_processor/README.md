# Email Processor

A tool to process raw email content into clean text suitable for TTS processing.

## Usage

Process a raw email file:
```bash
poetry run python -m email_processor --input-file raw_email.txt --output-file clean_text.txt
```

Or pipe content directly:
```bash
cat raw_email.txt | poetry run python -m email_processor
```

Or provide content as an argument:
```bash
poetry run python -m email_processor "Raw email content here..."
```

The processed text will be saved to a file (default: processed_email.txt) and can then be used with tts-joinery:
```bash
poetry run ttsjoin --input-file processed_email.txt --output-file audio/email_speech.mp3 --model tts-1-hd --voice ash
```
