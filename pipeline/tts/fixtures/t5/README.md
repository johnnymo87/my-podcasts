# T5 calibration regression fixtures

Real evidence from the T5 omission-detector calibration, pinned by
`pipeline/tts/test_t5_fixtures.py` under `DEFAULT_THRESHOLDS`. They were captured as verifier v2 evidence; v3 keeps every verdict.
Each file holds a script, the Gemini transcript of real audio (ASR policy
`gemini-3.8-flash|prompt-v1|temp0|thinking-low`) and a `provenance` block
(source paths under `/persist/my-podcasts/tts-eval/t5`, audio and chunk sha256,
policy string). The audio itself is not stored.

- `cut-<family>.json` (7): one real DEV cut per family, from Rundown/FP bases
  only (our own generated text), smallest size bin available, with the cut label
  (token intervals, literal removed text, `exact` or `approx` status and bounds).
- `faithful-{flash,lite}.json`: faithful DEV renders; must pass.
- `levine-{kore-0,charon-2}.json`: natural Gemini-TTS omissions in a Levine
  chunk (57-token and 24-token skips, confirmed by whisper). The repo is public:
  these hold only a TRIMMED window (at most 150 words either side of the span).

A failing fixture means the calibration needs another look (and a
`VERIFIER_VERSION` bump), not a fixture edit. Fixtures were extracted read-only;
there is no regeneration script.
