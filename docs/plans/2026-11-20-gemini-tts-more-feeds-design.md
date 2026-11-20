# Gemini TTS for the remaining feeds (design)

Date: 2026-11-20. Follows T6/T7 (`docs/plans/2026-09-30-gemini-tts-design.md`), which moved
the-rundown, fp-digest and levine to Gemini Flash-Lite.

## Goal

Move every remaining active feed from OpenAI TTS to Gemini Flash-Lite, with one distinct
Gemini voice per feed and each feed's current OpenAI voice as its whole-episode fallback.

## Voices (owner delegated the choice, 2026-11-20)

Same gender as each feed's current OpenAI voice where one exists, no voice shared across
feeds. Model `gemini-3.8-flash-lite-tts`, style `calm, measured news anchor` (unchanged).

| feed | Gemini voice | fallback (current) | note |
|---|---|---|---|
| the-rundown | Kore (F, firm) | nova | unchanged |
| fp-digest | Alnilam (M, firm) | onyx | unchanged |
| levine | Enceladus (M, breathy) | ash | unchanged |
| yglesias | **Sulafat** (F, warm) | shimmer | email posts + The Argument audio reports |
| silver | **Charon** (M, informative) | echo | |
| chinatalk | **Erinome** (F, clear) | alloy | alloy is neutral; Gemini has none, F balances the set |
| aaronson | **Sadaltager** (M, knowledgeable) | fable | blog poller |
| general | (stays OpenAI `ash`) | - | zero episodes; nothing to gain |

Result: 3 F / 4 M across the seven active feeds, seven distinct voices. Feeds not in
`FEED_VOICES` (one-off `papers`, `dwarkesh`, legacy) keep the `nova` default.

Length is not a concern: a 1973 s Levine render's Gemini phase took 125 s of the 360 s
budget; the longest recent chinatalk render is 2564 s.

## The one structural change: the podcast child must be non-daemonic

The Argument audio reports render inside `podcast_child` (a `spawn`ed
`multiprocessing.Process` with `daemon=True`). A Gemini primary renders in a spawned child
of its own (`tts.gemini_phase`), and a daemonic process may not start children
(`AssertionError: daemonic processes are not allowed to have children`). Flipping
`yglesias` without changing this would make every Argument report fall back to `shimmer`
with an alert (`spawn_failed`), i.e. one feed, two voices. A test pins this today
(`test_the_feed_renders_with_openai_because_the_child_is_a_daemon_process`).

**Change:** `podcast_child._make_process` uses `daemon=False`.

What `daemon=True` provided, and what covers it instead:

- *Terminate the child at the parent's normal interpreter exit.* `run_episode_in_child`
  already kills and joins the child in a `finally` on every path (`_Reaper.reap`), so a
  child never outlives the call except when it cannot be killed at all (logged). A
  non-daemonic child still alive at interpreter exit would make multiprocessing's atexit
  `join` it; the only way that happens is the "could not be killed" case, which a daemonic
  child would not have fixed either (terminate on an unkillable process does nothing).
- *Orphan cleanup when the consumer dies abnormally.* The child's watchdog exits when
  `getppid()` changes or the deadline passes; the Gemini-phase grandchild has the same
  watchdog against its own parent. systemd's default `KillMode=control-group` also kills
  every process of `my-podcasts-consumer.service` on stop/restart.

Deadline: transcription (~4.5 min) + writer (<=900 s) + Gemini phase (<=360 s + reap) +
OpenAI fallback render (~1-2 min) stays within the 1800 s child deadline in normal runs;
the parent's `kill()` still bounds the worst case. If the podcast child is killed
mid-Gemini-phase, the grandchild's watchdog ends it and its scratch dir is swept after 6 h.

Rejected alternative: render in the parent after the child returns the script. The render
was moved into the killable child on purpose (OpenAI/Gemini clients can trickle forever on
a single-threaded consumer); moving it back reintroduces that stall.

## Tests

- Replace the daemon pin with: `_make_process` builds a non-daemonic process, and
  `yglesias` resolves to the Gemini config (re-literaled).
- A real-spawn test: a non-daemonic podcast child (test runner seam) calls
  `render_episode` with a Gemini primary using an offline fake provider/transcriber and
  succeeds, proving the nested spawn works (this test fails with `daemon=True`).
- Golden tables: `pipeline/test_feed_voices.py` (`_EMAIL_GOLDEN` moves yglesias, silver,
  aaronson, chinatalk to Gemini entries), `pipeline/tts/test_config.py`, and any processor
  / blog-poller test pinning those voices.

## Docs

`pipeline/AGENTS.md` (TTS Renderer: Gemini feed table, rollback, "Every other feed is
OpenAI"), root `AGENTS.md` (Core Paths line and the "`yglesias` must stay an OpenAI voice"
constraint in Podcast Audio Reports step 3), `config.py` comment.

## Rollout and rollback

One PR. Deploy = `git pull` on main + `sudo systemctl restart my-podcasts-consumer`, at a
moment when `podcast-report list` shows nothing `in_progress`. Verify the first manifest
per flipped feed under `/persist/my-podcasts/tts-renders/<feed>/` (`rendered_config`,
`fallback_reason`). Rollback per feed: replace its `_gemini(...)` entry with
`_openai("<fallback>")` and update goldens (as for T6/T7). `daemon=False` is harmless to
keep even if yglesias rolls back.

## Adversarial review (adversarial-reviewer-opus): buy-in, with these adopted

- Nested spawn confirmed on Python 3.14 (`python -m` consumer -> spawned child ->
  spawned grandchild); `daemon=False` costs nothing at exit (the consumer installs no
  SIGTERM handler; reaped children leave `_children`).
- The real-spawn test must not inherit production paths: conftest isolation does not
  cross `spawn`, so the test runner (`_podcast_child_testing.gemini_render_runner`)
  passes explicit `manifest_dir`/`cache_dir` under the work dir, rebinds
  `gemini_phase.run_gemini_phase` with fake factories and `deny_network`, and the test
  asserts the audio came from Gemini. A companion test with `daemon=True` asserts
  `spawn_failed`.
- Sulafat, Erinome and Sadaltager had never been rendered here; a voice name the model
  rejects would be a fatal 400 and a permanent fallback. All four new voices were
  auditioned on a 3000-char Argument script before the flip
  (`/persist/my-podcasts/tts-eval/more-feeds-2026-11-20/`): all rendered, all passed
  verification.
- A fallback alert sent from the podcast child can be lost (`os._exit`), so the child's
  ok result and `stats_json` gain `tts_rendered` and `tts_fallback_reason`, and the
  executor logs a WARNING on a fallback.
- Pre-PR diff review asked for a content check, not just a voice check. One whole
  recent email per feed (silver, chinatalk, yglesias) through its production voice:
  chinatalk passed; silver failed `second_omission` because the Substack plain-text
  part repeats a mailto target in brackets (`addr [ mailto:addr ]`), Gemini read the
  address once, and the verifier flagged the skipped duplicate. `_clean_substack_body`
  now strips `[ mailto:... ]` as it already stripped `[ https://... ]`; after that
  silver and yglesias both passed every chunk. (aaronson was not checked: its text is
  a Gemini adaptation for audio, not raw markup.)
- Not adopted: clamping the Gemini budget to the child's remaining deadline (measured
  headroom: 300-472 s used of 1800 s; a late kill only costs a cheap retry).

## Risks accepted

- During a Gemini account outage (402/429) every feed falls back, with one alert per
  episode on top of the quota alerts.

- Fallback rate on long essay/transcript text may sit between the daily scripts (~0%) and
  Levine (~42% before the #44 preheader fix). A fallback ships the old voice with one
  alert; the owner accepted this.
- No listening gate: the owner delegated voice choice; a voice swap is a one-line change.
- First render of every flipped feed is a cache miss (new config in the cache key).
