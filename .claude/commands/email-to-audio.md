---
allowed-tools: Bash(xpaste:*), Bash(uv run:*)
description: Convert email from clipboard to audio file using TTS
---

# Email to Audio Conversion

Extract email content from clipboard and convert it to an audio file.

## Steps

1. Extract the email body from clipboard and save to a text file:
   ```
   xpaste | uv run python -m email_processor --write-text-file
   ```
   Note the output filename (e.g., `emails/2025-12-18-Money-Stuff-Example-Title.txt`)

2. Convert the text file to audio using TTS with the same base filename:
   ```
   uv run ttsjoin \
     --input-file emails/<filename>.txt \
     --output-file audio/<filename>.mp3 \
     --model tts-1-hd \
     --voice ash
   ```

Replace `<filename>` with the actual filename from step 1 (keeping the date and title portion).
