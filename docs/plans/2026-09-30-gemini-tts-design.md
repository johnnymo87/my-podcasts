# Gemini TTS migration — design

Bead: `my-podcasts-byl`. Status: approved by owner 2026-09-30; adversarial review (astra) buy-in
after rev 3, same day.

## Why

Every feed renders through the `ttsjoin` CLI (PyPI `tts-joinery` 1.0.5) → OpenAI `tts-1-hd`.
Google released Gemini 3.8 Flash TTS and Flash-Lite TTS on 2026-09-22/23. We measured them
(2026-09-28) on three real published inputs (Rundown 9/28, FP Digest 9/28, Levine 9/24 email body):

- **Fidelity** (Gemini transcribe-back, word recall vs input): on par with tts-1-hd on Rundown/FP
  (0.992–0.998 vs baseline 0.995–0.996); Levine 0.968–0.985 vs baseline 0.985. Most of the gap is
  number formatting.
- **Synthesis cost** at last-30-days volume (~34 h audio/month): tts-1-hd ~$60/mo (list-price
  estimate); Flash-Lite ~$24 (~$47 from 2027); Flash ~$36 (~$71 from 2027). Billing measured at
  ~32 audio tokens/s. These exclude verification, retries and fallback renders; the manifests
  (below) measure the real total. **Flash is not a saving from 2027**; only Flash-Lite is.
- **Speed**: 9–20 min episodes render in 35–94 s wall with 4 chunks in parallel.

Three failure modes rule out a naive swap:

1. **Silent skip.** One Flash render of a Levine chunk dropped ~40 words including a quoted sentence,
   joined fluently (confirmed by 3 independent transcriptions). Chunk recall 0.86; episode recall
   still 0.968. `finish_reason` was `STOP`. (That audio was lost in a `/tmp` cleanup.)
2. **Empty response.** A Levine paragraph quoting an S&P force-majeure clause returns a candidate with
   no content, `finish_reason=OTHER`, no `prompt_feedback`. Succeeds ~1 in 4 on *both* models; the
   paragraph alone 1 in 3. Google documents `OTHER` only as "unknown reason". FP Digest (6 runs,
   war-heavy text) never hit it.
3. **Spoken instructions.** 3.8 treats input text as a verbatim transcript; a style preamble in the
   text was read aloud. Google's documented channel is per-part `speech_metadata.style` — verified
   accepted over plain REST on 2026-09-30 (not yet confirmed audibly honored).

Other verified facts: unary responses are a WAV container, 24 kHz mono s16le;
`responseMimeType: audio/l16` is rejected with 400. `speech_metadata` and designed voices are not in
our pinned `google-genai` 1.65.0 (need ≥2.25.0). Runtime is Python 3.14.

Prior-art search (ChatGPT deep research, 2026-09-30): no maintained Python library does Gemini 3.8
long-form TTS with retries, fallback and fidelity checks. Closest is Jellypod Speech SDK (TypeScript).
LiteLLM `speech()` supports 3.8 but does no long-form orchestration. Decision: write our own.

## Approach

In-repo renderer package `pipeline/tts/`, called as a Python function from every TTS call site,
replacing the `ttsjoin` subprocess. (Rejected: forking tts-joinery — keeps the subprocess boundary
and the nltk hang, no home for fidelity/fallback logic; LiteLLM underneath — large dependency with a
buggy Gemini TTS history, saves only two thin provider classes.)

Central invariant: **a Gemini problem costs an OpenAI episode, delayed by at most the Gemini budget
(6 min).** It cannot make an episode go missing; OpenAI failing on its own still can, exactly as
today. Every rule below about budgets, verification and telemetry exists to keep that true.

## Components

| Module | Job |
|---|---|
| `config.py` | `RenderConfig(provider, openai_model, openai_voice, gemini_model, gemini_voice, gemini_style)` and `resolve_render_config(feed_slug, *, voice_override=None, model_override=None)`. Precedence below. |
| `chunker.py` | Lossless partition: paragraphs packed to ≤ ~3000 chars; oversize paragraph → sentences; oversize sentence → whitespace; unbroken string → hard cut. A hard per-provider ceiling (OpenAI 4096) is asserted. Regex, no nltk. |
| `providers.py` | `synthesize(text, config) -> pcm` (24 kHz mono s16le). `GeminiProvider`: REST via `requests`, `speech_metadata.style`, WAV parsed with `wave` (format + length validated, not a fixed header strip), classifies errors (below). `OpenAIProvider`: `openai` SDK, `response_format="pcm"`, **SDK `max_retries=0` and explicit timeout** — the renderer owns all retries. Gemini client created lazily. |
| `verify.py` | Large-omission detector (see Verification). Used synchronously only on Gemini renders. |
| `render.py` | `render_episode(text, config, out_mp3, *, episode_id, manifest_dir=None) -> RenderResult`. |
| `cache.py` | Completed-render reuse (see below). |

### Config precedence

1. An explicit caller override (`TTS_MODEL`/`TTS_VOICE` env on the email path, `publish_script`'s
   `voice=` kwarg, the CLI `--voice` flags) **forces OpenAI** with that voice/model. Legacy
   semantics are preserved exactly, including `publish_script`'s `nova` default.
2. Otherwise the feed's entry in `FEED_VOICES`.
3. Unknown feed slugs (one-off `episode` feeds) → OpenAI with today's default.

OpenAI voice names are never sent to Gemini. The resolved primary *and* fallback config are recorded
at render start.

PR 1 keeps each call site's current voice source and passes it through; the `FEED_VOICES`
consolidation lands in PR 2 with provider selection.

## Render flow (Gemini feeds)

```
text -> chunks
  Gemini phase runs in a CHILD PROCESS (spawn), hard-killed at 6 min:
    ≤4 requests in flight (threads inside the child)
    chunk error classes:
      content-transient: empty / finish_reason OTHER
                                  -> retry, max 3 attempts per chunk; never counts as systemic
      infra-transient: 5xx, transient 429, connection error, per-request timeout
                                  -> retry, max 3 attempts per chunk
      fatal: 401/403, 429 RESOURCE_EXHAUSTED (quota), or infra-transient
             exhausting retries on 2 distinct chunks
                                  -> child exits "gemini unavailable" immediately
    verify each chunk (ASR, SDK retries off, same budget); omission -> re-render, max 1 extra
    child writes verified PCM per chunk to a temp dir, then a done-marker
  parent: join(timeout = budget); still alive -> kill(); no done-marker, any chunk failed,
          or ASR unavailable/empty
      -> discard ALL Gemini audio
      -> render WHOLE episode with OpenAI in the parent (no verification on this path)
  concatenate PCM (no added silence) -> one mp3 encode, pinned 32 kbps / 24 kHz / mono
  both providers fail -> raise; existing job retry/backoff applies
```

The child process is the deadline's enforcement boundary: `requests` read timeouts are per-socket
gaps, not absolute, and threads cannot be cancelled, so only killing a process makes "6 minutes"
true for synthesis, ASR, backoff sleeps and shutdown alike. Per-request timeouts inside the child
(`min(remaining, 90 s)`) still apply so one slow chunk doesn't consume the whole budget.
Tests must cover expiry **during a blocked request** (a fake provider that sleeps past the budget).

No paragraph re-split tier in the first rollout: retries then whole-episode fallback. Add splitting
only if manifests show fallback cost justifies it. OpenAI-phase worst case: up to 3 attempts × chunk
count × per-request timeout, plus backoff (SDK `max_retries=0`; the renderer allows 2 retries per
chunk). That is a timeout-based estimate, not an absolute wall-clock guarantee — the OpenAI phase
runs in-process, as it effectively does today. Encoding and `ffprobe` get timeouts too.

**Accepted:** there is no cross-episode provider cooldown. During a Gemini outage, each render
spends up to the 6-minute budget discovering it (fatal errors usually end it in seconds; a hang
costs the full budget). The bound is **up to 6 minutes per uncached render attempt** — ~30 min/day
at a normal 3–5 attempts, but more when attempts multiply (OpenAI also failing → daily-job retries,
up to 51; email redelivery, which has no application-level cap). It self-heals when Gemini
recovers. A cooldown is a later addition if manifests show it matters.

### Completed-render reuse

Replaces joinery's per-chunk cache, which today prevents an R2 upload failure from re-buying audio.

- **Key:** sha256 of canonical JSON `{text, primary_config, fallback_config, renderer_version}`,
  where `text` is the exact prepared TTS input. `RENDERER_VERSION` is a constant bumped on any
  change to chunking, encoding, provider request shape, or verifier policy (a test pins that the
  key changes when it does).
- **Entry:** a directory `/persist/my-podcasts/tts-cache/<key>/` holding `audio.mp3` and
  `result.json` (actual provider and config that rendered it, verification status, render
  timestamp). Written into a temp dir and committed with one `os.rename`; an entry without a valid
  `result.json` is treated as a miss.
- **Stored:** completed renders only — verified Gemini, or OpenAI (including fallback, recorded as
  such in `result.json`, so a Gemini request's retry reuses its fallback audio and knows it).
- **Best-effort:** any cache read/write error is logged ("reduced retry protection") and ignored;
  valid audio is always returned.
- **Not used by `tts-audition`** (`use_cache=False`), so an audition can never present cached
  fallback audio as Gemini.
- 14-day retention. A retry after a downstream (upload/DB) failure costs nothing.

## Verification (large-omission detector)

Claimed scope: catches **large omissions**. It does not protect against changed numbers, negations,
repetitions or added speech.

- ASR prompt gets audio only — never the script — then the transcript is aligned against the script
  afterwards.
- Flag: an unmatched source span (between alignment anchors, whether the aligner calls it delete or
  replace) of ≥ N words where the transcript side of the span is much shorter; plus a per-chunk
  recall floor to catch several shorter cuts.
- Numbers: a defined normalization (digits, `%`, `$`, years → spoken words) — not a blanket
  tolerance for numeric substitutions.
- Empty or truncated ASR output = verification unavailable, not a pass.
- Calibration (offline, before any feed flips): clean controls (published OpenAI audio and fresh
  Gemini renders, ~10 across feeds) plus **audio-level** cuts — PCM segments removed at start,
  middle, end, paragraph-sized, and several short cuts in one chunk. N and the recall floor are set
  so all controls pass and all cuts are caught; ambiguous cases are checked by ear, not by tuning N
  until they pass. We also attempt to reproduce the real skip (repeat Flash on the Levine chunk) and
  keep any reproduction as a fixture.

## Telemetry

- Manifest per render attempt, `/persist/my-podcasts/tts-renders/{feed}/{episode_id}-{attempt_ts}.json`:
  input/config hashes, primary and fallback config, per-chunk chars/attempts/finish reasons/seconds/
  recall/longest unmatched span/tokens, deadline use, and the **rendered** provider. It says
  rendered, not published — only the caller knows about publishing. Writes are best-effort and
  atomic; a write failure is logged and never discards valid audio. `manifest_dir=None` (dry runs,
  local use) skips it. 60-day retention.
- Every fallback is logged and recorded in the manifest (the durable record). Telegram: one
  best-effort alert per fallback render, carrying feed, episode and reason. No dedupe state and no
  summary: render reuse already suppresses repeats on publish retries, and a rare duplicate (crash
  between alert and cache commit) is acceptable. Per-chunk retries never go to Telegram.
  `send_alert` is already non-throwing; its failure never blocks rendering.

## Rollout

1. **PR 1 — controlled renderer replacement, OpenAI only.** `pipeline/tts/` with chunker, OpenAI
   provider, render-reuse cache, manifest; all **six** call sites (`processor.py`,
   `script_processor.py`, `__main__.py` publish-script `--dry-run`, `things_happen_processor.py`,
   `fp_processor.py`, `blog_poller.py`) switch to `render_episode` with their current voice/model.
   `openai` becomes a main dependency; `tts-joinery` removed, closing `my-podcasts-4ld`. No
   synchronous verification. This is *not* behavior-neutral: chunk boundaries (paragraph vs NLTK
   sentence) and PCM-vs-MP3 concatenation change. Pre-merge: render a Rundown, FP and a long Levine
   body; compare duration and bitrate, and listen to joins, long sentences and abbreviations against
   the published mp3.
2. **PR 2 — Gemini provider, `FEED_VOICES`, verifier, fallback, offline tools. No feed flipped.**
   - `python -m pipeline tts-audition --feed <slug> --script <file> --voices Kore,Charon,...`:
     local mp3s only, one per OpenAI/Gemini variant, filenames carry model/voice; an audition never
     substitutes OpenAI silently (a failed Gemini variant is reported as failed).
   - `python -m pipeline tts-verify`: runs the detector over given audio + script (used for
     calibration and baseline).
3. **Gate — calibration done and owner listening test.** Owner chooses Flash vs Flash-Lite and
   per-feed voice/style, or stops. Recorded in the bead.
4. **PR 3 — flip The Rundown.** After ~5 episodes, review manifests + alerts: fallback rate,
   retries, skips, real cost including verification.
5. **PRs 4+ — flip remaining feeds one at a time**, email feeds after both daily feeds. Levine last.

Out of scope (separate beads): `google-genai` 2.25+ upgrade and designed voices; the email path's
pre-existing unbounded redelivery (unchanged by this work — the render itself is now bounded).

## Testing

- Unit, fake providers, no network: chunker is lossless and respects ceilings (oversize sentence,
  unbroken string);   WAV parse/validation; error classification → retry vs immediate abort (OTHER never systemic);
  deadline expiry during a blocked request kills the child and falls back within budget + margin; one failing chunk yields a whole-episode OpenAI render,
  never mixed; ASR unavailable → fallback; no verification on the OpenAI path; render reuse hits on
  identical key, misses on changed config or renderer version, treats a half-written entry as a
  miss, and a cache write failure still returns audio; `result.json` records fallback provenance;
  manifest write failure does not fail the render; alert fires on fallback; both-fail raises; config precedence (override forces OpenAI; unknown feed →
  default; OpenAI voice never sent to Gemini).
- Verifier: alignment/normalization unit tests on real script text; the audio-level calibration set
  above (offline, via `tts-verify`).
- Existing tests mocking `subprocess.run` for `ttsjoin` are rewritten to mock `render_episode`.
