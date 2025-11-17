Here's my main flow for how I use this code.

I copy an email to my clipboard, then run the following command to extract the body text and save it to a file:
```
❯ xpaste | uv run python -m email_processor --write-text-file
Body text saved to emails/2025-11-17-Money-Stuff-Private-Markets-Are-the-New-Securities-Fraud.txt
```

Next, I run the following command to convert the text file to an audio file using TTS:
```
❯ uv run \
  ttsjoin \
  --input-file emails/2025-11-17-Money-Stuff-Private-Markets-Are-the-New-Securities-Fraud.txt \
  --output-file audio/2025-11-17-Money-Stuff-Private-Markets-Are-the-New-Securities-Fraud.mp3 \
  --model tts-1-hd \
  --voice ash

Chunking sentences  [####################################]  100%
Preparing to run TTS in 6 chunks...([4021, 3993, 4046, 4029, 3987, 3629])
Running chunked TTS  [####################################]  100%
Processing audio files  [####################################]  100%
Finalizing...
```
