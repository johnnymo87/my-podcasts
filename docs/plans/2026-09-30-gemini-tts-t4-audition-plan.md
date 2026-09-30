# T4 tts-audition Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** A local-only `python -m pipeline tts-audition` command that renders one script through the
feed's current OpenAI voice and every requested Gemini model x voice, into local mp3s plus a
summary, so the owner can compare them by ear (bead `my-podcasts-9p3.4`, design doc "Rollout" 2).

**Architecture:** Logic lives in a new leaf-ish module `pipeline/tts/audition.py` (imports only
`pipeline.tts.*`, never call-site modules, R2, DB or feed code). It builds one `RenderConfig` per
variant and calls the production `render_episode` with `cache_dir=None`,
`notify_fallback=False`, and a Gemini variant's `fallback=None` — so a Gemini failure raises
`TTSRenderError` and is reported FAILED, never replaced by OpenAI. The click command in
`pipeline/__main__.py` is a thin wrapper.

**Tech Stack:** Python 3, click, pytest; existing `pipeline.tts.render_episode`.

---

## Binding decisions

1. **Scope fence.** No publish option. No R2, DB, feed, cache or alert side effects. The module
   must not import `pipeline.r2`, `pipeline.db`, `pipeline.feed`, `pipeline.alerts` — a test pins
   this in a fresh subprocess (`sys.modules` check after importing `pipeline.tts.audition`).
   Uploading the audition set for the owner is a separate, manual controller step, not code.
2. **Variants.**
   - Baseline (unless `--no-openai`): the feed's OpenAI leaf — `FEED_VOICES[feed].primary` if it
     is an `OpenAIConfig`, else its `fallback`, else `DEFAULT_RENDER_CONFIG.primary`. Rendered as
     `RenderConfig(leaf, None)`.
   - For each model in `--models` (default `gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts`) and
     each voice in `--voices` (required, comma-separated): `RenderConfig(GeminiConfig(model,
     voice, style), None)`. Order: baseline, then models in given order, voices in given order.
   - `--style` default `"calm, measured news anchor"` (the style the owner liked, 9p3.6 note).
     `--style ""` is allowed and means no style. One style per run.
   - Unknown feed slug (not in `FEED_VOICES`) is a click usage error. Duplicate voices/models are
     de-duplicated preserving order. An OpenAI voice name in `--voices` is a usage error
     (`GeminiConfig` raises `ValueError`; convert to `click.BadParameter` before any render).
3. **Labels come from what rendered.** Render each variant to a temp path in the out dir
   (`.partial-<index>.mp3`), then name it from `RenderResult.rendered`:
   `<feed>--<provider>--<model>--<voice>.mp3` with each field sanitized to `[A-Za-z0-9._-]`
   (anything else -> `_`). If `result.rendered != requested leaf` (impossible with
   `fallback=None`, but the invariant is what makes a label trustworthy) the variant is FAILED
   with reason `rendered_mismatch` and the partial file is deleted. A failed variant never leaves
   an mp3 behind (delete the partial in `finally` unless it was renamed).
4. **Failure handling.** Per variant: `TTSRenderError` -> FAILED with its message. Any other
   `Exception` -> FAILED with `"<Type>: <msg>"` (truncated to 500 chars), and the run continues
   with the next variant. `KeyboardInterrupt`/`SystemExit` propagate (partial deleted).
   Preflight before any render: Gemini variants need `GEMINI_API_KEY`, the baseline needs
   `OPENAI_API_KEY` (non-empty after strip) -> otherwise `click.UsageError` naming the variable,
   never printing its value.
5. **Overwrite.** Before rendering, compute every requested target filename; if any exists in the
   out dir (or `summary.json` exists) and `--force` is not given -> `click.UsageError` listing
   them. With `--force`, existing targets are replaced only when that variant succeeds (atomic
   `os.replace`); a variant that fails with `--force` deletes the stale target so no old mp3 can
   be mistaken for the new variant.
6. **Excerpt.** `--max-chars N` (optional, >= 200): cut the script at the last paragraph boundary
   (`"\n\n"`) at or before N characters, strip trailing whitespace. If there is no boundary at or
   before N -> usage error (never cut mid-paragraph). The exact text rendered is written to
   `<out>/script.txt` in every run (so every mp3 is reproducible), and its sha256 goes in the
   summary.
7. **Outputs** in `--out-dir` (required; created if missing): the mp3s, `script.txt`,
   `manifests/` (passed as `manifest_dir`), and `summary.json` written atomically after every
   variant (so a crash still leaves an up-to-date summary). `episode_id` for every variant:
   `audition-<UTC yyyymmddThhmmss>` fixed per run.
8. **Summary.** `summary.json`:
   ```json
   {"schema": 1, "feed": "...", "style": "...", "script_sha256": "...", "script_chars": 0,
    "started_at": "iso", "variants": [
      {"label": "file stem or null", "requested": {asdict leaf}, "rendered": {asdict leaf}|null,
       "status": "ok"|"failed", "error": null|"...", "file": "name.mp3"|null,
       "audio_seconds": float|null, "wall_seconds": float, "chunks": int|null,
       "manifest": "relative path"|null,
       "verify": [{"index": 0, "attempts": 1, "verdict": "pass", "recall": 0.99}]|null,
       "tokens": {...}|null}]}
   ```
   `verify` and `tokens` come from the manifest's `gemini_phase` (per chunk: number of
   attempts, the last attempt's `asr.status` or its `outcome` when ASR never ran, and
   `asr.recall`); `null` for OpenAI. For a failed variant, read the newest manifest written for
   it if one can be found (`RenderResult` is unavailable): glob `manifests/<feed>/<episode_id-
   sanitized>-*.json` and pick the file created during that variant (track the set before/after
   the render). Summary reading must never raise: on any problem those fields are `null`.
   `audio_seconds`: the manifest's `total_audio_seconds`.
9. **Stdout.** One line per variant as it finishes, then a final table:
   `OK      <file>  <audio mm:ss>  <wall s>  verify: pass,pass,...` or
   `FAILED  <provider>/<model>/<voice>  <error>`. Final line:
   `tts-audition: N ok, M FAILED; files in <out>`. Exit 0 if all ok, 1 if any failed.
10. **Spawn safety.** Gemini variants spawn a child (`gemini_phase`); the CLI entry is
    `python -m pipeline`, which is already spawn-safe. No module-level work in `audition.py`.

## Task 1: `pipeline/tts/audition.py` core + unit tests

**Files:** Create `pipeline/tts/audition.py`, `pipeline/tts/test_audition.py`.

Public API:
```python
@dataclass(frozen=True)
class Variant:
    requested: OpenAIConfig | GeminiConfig

def baseline_leaf(feed_slug: str) -> OpenAIConfig: ...
def build_variants(feed_slug, *, models, voices, style, include_openai) -> list[Variant]: ...
def excerpt(text: str, max_chars: int | None) -> str: ...   # ValueError when no boundary
def variant_filename(feed_slug: str, leaf) -> str: ...
def run_audition(text, variants, out_dir: Path, *, feed_slug, style, force=False,
                 render=None, echo=print) -> dict: ...      # returns the summary dict
```
`render` defaults to `pipeline.tts.render.render_episode` looked up at call time (tests inject a
fake). Tests (fake `render` writing a small file and returning a `RenderResult`, plus writing a
manifest dict into `manifest_dir` for the Gemini case) must cover, TDD:
- variant order/dedupe; baseline selection for an OpenAI-primary feed, a Gemini-primary feed
  with fallback (monkeypatch `FEED_VOICES`), and a Gemini-primary feed without fallback;
- OpenAI voice in voices -> `ValueError`;
- every render call gets `cache_dir=None`, `notify_fallback=False`, a `fallback=None` config,
  `manifest_dir=<out>/manifests`;
- filename from `rendered` and sanitization; `rendered_mismatch` -> FAILED, no mp3 left;
- `TTSRenderError` and a generic exception -> FAILED, next variant still runs, no mp3 left;
  `KeyboardInterrupt` propagates and leaves no partial;
- overwrite refusal (target exists, and summary.json exists) and `--force` semantics incl.
  stale-target deletion on failure;
- excerpt boundary rules (exact N, no boundary -> ValueError, None -> unchanged);
- `script.txt` content and `summary.json` rewritten after each variant (assert after a variant
  crash-through that the earlier variant is in the file);
- verify/tokens extraction from a real-shaped manifest (copy the shape of
  `gemini_phase.chunks[].attempts[].asr` from the plan's example below) and `null` on a
  malformed manifest;
- the import-isolation subprocess test (decision 1).

Real-shaped chunk record for fixtures:
```json
{"index": 0, "schema": 1, "attempts": [{"n": 1, "outcome": "verified",
  "synth": {"status": "ok", "audio_tokens": 5172, "prompt_tokens": 513, "elapsed_s": 32.3,
            "finish_reason": "STOP", "pcm_bytes": 7756800, "error": null, "kind": null},
  "asr": {"status": "pass", "recall": 0.9929, "reasons": ["ok"], "input_tokens": 4077,
          "output_tokens": 515, "thinking_tokens": 1814, "elapsed_s": 9.8, "detail": ""}}]}
```
Run: `uv run pytest pipeline/tts/test_audition.py -q`. Commit.

## Task 2: click command `tts-audition` in `pipeline/__main__.py` + CLI tests

Options: `--feed` (required, `click.Choice(sorted(FEED_VOICES))`), `--script` (required, existing
file), `--voices` (required), `--models` (default above), `--style` (default above),
`--max-chars` (int >= 200, optional), `--out-dir` (required), `--no-openai` flag, `--force` flag.
Import `pipeline.tts.audition` lazily inside the command (keeps `python -m pipeline --help`
cheap). Preflight keys (decision 4). Map `ValueError` from `build_variants`/`excerpt` to
`click.BadParameter`/`UsageError`. Exit code via `SystemExit(1)` when any variant failed.

Tests in `pipeline/test_tts_audition_cli.py` with `CliRunner`, monkeypatching the render function
(and setting dummy keys via `monkeypatch.setenv` — the suite deletes real keys): happy path
exit 0 and output lines; one failure -> exit 1 and `FAILED` line; missing key -> usage error
without the value; unknown feed; OpenAI voice in `--voices`; `--max-chars` without a boundary;
existing target without `--force`. Run the full suite: `uv run pytest -q` (~1 min) and
`uv run ruff check . && uv run ruff format --check .`. Commit.

## Task 3: docs

- `pipeline/AGENTS.md` "TTS renderer" section: a short `tts-audition` paragraph — what it does,
  local-only guarantee, FAILED-never-substituted, outputs, example command, cost note (Gemini
  variants also run per-chunk ASR verification, as production would; roughly $1/hour of Flash
  audio and $0.7/hour of Flash-Lite synthesis plus ASR).
- `AGENTS.md` Core Paths TTS bullet: mention `pipeline/tts/audition.py` (CLI `tts-audition`).
- Design doc Amendments: one bullet recording T4's decisions (style default, excerpt rule,
  labels from `rendered`).
Commit.

## Smoke (controller, not implementer)

Real run with keys on a ~3000-char Rundown excerpt: `--voices Kore --models
gemini-3.8-flash-lite-tts` plus the OpenAI baseline, out dir under
`/persist/my-podcasts/tts-eval/t4/smoke/`; and one forced failure (`--voices NoSuchVoiceXyz`)
showing FAILED, exit 1, no mp3.
