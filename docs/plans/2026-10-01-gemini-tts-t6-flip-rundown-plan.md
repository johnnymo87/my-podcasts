# T6: flip The Rundown to Gemini (my-podcasts-9p3.7)

Spec: `docs/plans/2026-09-30-gemini-tts-design.md`, Rollout item 4. Owner gate (9p3.6, closed
2026-10-01): GO, `gemini-3.8-flash-lite-tts` everywhere; The Rundown on Kore; style unchanged
(`calm, measured news anchor`). Other feeds are T7 (9p3.8) and do not change here.

## Change

`pipeline/tts/config.py` `FEED_VOICES["the-rundown"]`:

```python
RenderConfig(
    primary=GeminiConfig(model="gemini-3.8-flash-lite-tts", voice="Kore",
                         style="calm, measured news anchor"),
    fallback=OpenAIConfig(model="tts-1-hd", voice="nova"),
)
```

The fallback is the feed's current OpenAI voice, so a fallback episode sounds exactly like
today's. No other feed, no renderer, verifier or cache-version change.

## Pre-flip facts (checked 2026-10-01)

- **Key in production.** The consumer's start script exports `GEMINI_API_KEY` from
  `/run/secrets/gemini_api_key`, and the running process has it in its environment. The spawned
  Gemini child inherits it. The consumer runs `python -m pipeline consume`, so the spawn
  `__main__` re-import is safe.
- **Every path that renders the-rundown picks it up through `resolve_render_config`:** the
  daily processor (`things_happen_processor.py`), the email path (route tag `the-rundown`),
  and the consumer-down recovery `publish-script` (default voice follows `FEED_VOICES` since
  #29; `--dry-run` passes `notify_fallback=False`). An explicit `--voice` / `TTS_VOICE` still
  forces OpenAI.
- **Cache.** A Gemini primary is a new cache key (config + `VERIFIER_POLICY`), so the first
  Gemini render of any script is cold. `RENDERER_VERSION` stays `"2"`. A fallback render is
  cached as OpenAI audio and replays on `jobs reset`; delete the entry (manifest `cache_key`) to
  retry Gemini.
- **Alert path.** One Telegram General alert per fallback from the parent
  (`TTS fallback: the-rundown <episode_id>: Gemini gemini-3.8-flash-lite-tts/Kore <reason> ->
  OpenAI nova rendered`), never on a cache hit.
- **Wall clock.** Worst case adds the 6-minute Gemini budget before an OpenAI render; the daily
  job is not time-critical at that scale.

## Tasks

1. TDD: update the hard-coded golden tables so they assert the new the-rundown entry,
   re-literaled (never derived from `FEED_VOICES`): `pipeline/tts/test_config.py`
   `test_golden_feed_voices`, `pipeline/test_feed_voices.py` `_EMAIL_GOLDEN`. Add a test that the
   daily processor and an email routed `the-rundown` hand `render_episode` the Gemini config, and
   that an override on the-rundown still yields OpenAI with the fallback's model. Watch them fail,
   then make the config change.
2. Docs: drop "no feed is Gemini yet" (`AGENTS.md` Core Paths, `pipeline/AGENTS.md` TTS Renderer
   and T3b); state that the-rundown is Gemini Flash-Lite/Kore with nova fallback, and where to
   look after a fallback.
3. Paid smoke (about $0.2): render the 2026-10-01 Rundown script through production
   `render_episode` with `resolve_render_config("the-rundown")` from this branch,
   `notify_fallback=False`, `cache_dir=None`, manifest under `/persist/my-podcasts/tts-eval/t6/`.
   Record outcome, fallback (if any), audio seconds, tokens, wall time, per-chunk
   `max_net_missing` and unmatched tokens on short chunks.
4. Full suite + ruff; code review; adversarial review (astra probe, opus fallback); PR.

## After merge (owner)

Deploy is the owner's `systemctl restart my-podcasts-consumer`. Then review ~5 episodes
(manifests in `/persist/my-podcasts/tts-renders/the-rundown/`, Telegram fallback alerts):
fallback rate, retries, omissions, real cost including verification, and the v3 watch item
(unmatched tokens on chunks under 300 tokens; revisit if a faithful chunk exceeds 12).
Rollback is reverting the one `FEED_VOICES` line.

## Smoke result (2026-10-01, this branch)

The 2026-10-01 Rundown script (13064 chars) through production `render_episode` with
`resolve_render_config("the-rundown")`: **Gemini rendered, no fallback**, 5 chunks, every chunk
verified on its first attempt, 73 s Gemini phase (78.5 s wall). 885.2 s of audio versus 807 s for
the published nova episode of the same day (Kore with the "calm, measured" style reads about 10%
slower). Tokens: synth audio 28329, ASR input 22315, ASR output 2746, thinking 0: about $0.17
synth + $0.04 ASR. Per chunk (script tokens / unmatched / `max_net_missing`): 441/2/0, 417/3/0,
431/13/2, 503/3/0, 357/4/1. No chunk under 300 tokens, so the v3 watch item has no data point
yet. Artifacts: `/persist/my-podcasts/tts-eval/t6/` (mp3, manifest, log).
