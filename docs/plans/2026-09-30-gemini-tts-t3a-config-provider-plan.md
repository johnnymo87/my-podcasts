# T3a — typed render config, FEED_VOICES, GeminiProvider — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Give the renderer a typed primary/fallback config, one owner of per-feed voices
(`FEED_VOICES`), and a tested Gemini REST provider. **Production audio doesn't change**: every
feed stays OpenAI, and `render_episode` refuses a Gemini primary until T3b wires the bounded
Gemini phase.

**Bead:** `my-podcasts-9p3.3` (epic `my-podcasts-9p3`). T3b = `my-podcasts-9p3.11` (spawned
6-min Gemini phase, per-chunk verify, fallback) builds on this.
**Design:** `docs/plans/2026-09-30-gemini-tts-design.md` (with amendments in Task 6).
**REST facts:** `/persist/my-podcasts/tts-eval/T3-FACTS.md`.

**Tech stack:** Python 3.14, `uv`, pytest, `requests`, stdlib `wave`/`base64`/`threading`.

**Commands:** single file `uv run pytest pipeline/tts/test_config.py -q`; full suite
`uv run pytest -q -x` (~6-8 min, ~1000 tests; use a 900 s timeout); lint
`uv run ruff check . && uv run ruff format --check .`.

---

## Decisions (oracle-astra consult, 2026-09-30) — do not relitigate

1. **The T3 split.** This PR is config + provider only. T3b owns the child process, verification,
   fallback, alerts, and the Gemini render path.
2. **Config shape.** The config is split into provider-specific leaves plus a pair:
   - `OpenAIConfig(provider="openai", model, voice)`
   - `GeminiConfig(provider="gemini", model, voice, style)`
   - `RenderConfig(primary: OpenAIConfig | GeminiConfig, fallback: OpenAIConfig | None)`

   Valid combinations are OpenAI/None, Gemini/OpenAI and Gemini/None. OpenAI/OpenAI is
   rejected. `asdict()` serializes the tree.
3. **`FEED_VOICES` preserves today's values exactly.** Every model is `tts-1-hd`:

   | Feed | Voice |
   |---|---|
   | `general`, `levine` | ash |
   | `yglesias` | shimmer |
   | `silver` | echo |
   | `the-rundown` | nova |
   | `fp-digest` | onyx |
   | `aaronson` | fable |
   | `chinatalk` | alloy |
   | unknown slug | nova (`DEFAULT_OPENAI_VOICE`) |

4. **Overrides force OpenAI.** Either `voice_override` or `model_override` forces OpenAI. A field
   the override leaves unspecified comes from the feed's OpenAI config: its primary if that is
   OpenAI, else its fallback, else the default. An explicit `""` override is a `ValueError`.
   - The email path passes the raw `os.getenv("TTS_VOICE")` / `os.getenv("TTS_MODEL")`, which is
     `None` when unset.
   - **`publish_script(voice="nova")` and both CLI `--voice` defaults (`"nova"`) stay as they
     are.** Changing them is a T6 prerequisite, already noted on `my-podcasts-9p3.7`. Today's
     manual-publish voice is nova for every feed, and that must not change here.
5. **Gemini errors carry a `kind`.** `TTSProviderError` gains
   `kind: Literal["content", "infra", "fatal"]`, and `retryable` is derived as `kind != "fatal"`.
   OpenAI errors keep their current retryability; retryable ones get `kind="infra"`.
6. **Cache key** = `{text, primary, fallback, renderer_version, verifier_policy}`.
   - `verifier_policy` is `None` for an OpenAI primary. For a Gemini primary it is
     `verify.VERIFIER_POLICY`, imported lazily so the OpenAI path never imports `google-genai`.
   - `RENDERER_VERSION` goes from `"1"` to `"2"`. Accept one cold cache on deploy.
   - `result.json` records requested and rendered config plus `verification`. `lookup` validates
     provenance.
7. **Gemini WAV parsing.** Parse with `wave`: 24 kHz, mono, 2-byte samples, nonzero frames, and
   `len(readframes(n)) == n*2`.
   - **Real responses carry a trailing `C2PA` chunk after `data`** (6 KB). A fixed 44-byte strip
     would splice it into the audio.
   - Decode base64 strictly (`validate=True`).
8. **`max_chars = 3000` for Gemini.** This is an application limit, not a vendor one. A real
   2399-char chunk took 33 s and produced 156.6 s of audio.

---

### Task 1: Typed configs, `FEED_VOICES`, `resolve_render_config`

**Files:**
- Modify: `pipeline/tts/config.py`, `pipeline/tts/__init__.py`
- Test: `pipeline/tts/test_config.py` (rewrite)

**Step 1: Write the failing tests** (replace the file). They must cover:
- `openai_config(model=, voice=)` returns `RenderConfig(OpenAIConfig("openai", m, v), None)`, and
  still rejects an empty model or voice.
- Every dataclass is frozen.
- `RenderConfig` validation:
  - An OpenAI primary with a non-None fallback raises `ValueError`.
  - A fallback that is not an `OpenAIConfig` raises.
  - A primary with the wrong type raises.
  - Gemini/OpenAI and Gemini/None are accepted.
- `GeminiConfig` validation:
  - Empty model or voice raises.
  - **The voice is not an OpenAI voice name**, checked case-insensitively against `OPENAI_VOICES`
    = {alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse}. The design
    requires that an OpenAI voice is never sent to Gemini.
  - `style` may be `""`.
- A parametrized golden table: `resolve_render_config(slug)` returns exactly the table in
  Decision 3, and an unknown slug returns nova.
- Overrides:
  - `resolve_render_config("levine", voice_override="shimmer")` gives OpenAI tts-1-hd shimmer.
  - `model_override="tts-1"` gives OpenAI tts-1 ash.
  - Both overrides together give both values.
  - `voice_override=""` raises `ValueError`, and so does `model_override=""`.
- An override on a Gemini-primary feed forces OpenAI and takes the missing field from the
  *fallback*. Test this by monkeypatching `config.FEED_VOICES` with a Gemini/OpenAI(echo) entry:
  `voice_override=None, model_override="tts-1"` gives OpenAI tts-1 echo. With a Gemini/None entry,
  the missing field comes from the default `tts-1-hd`/nova.
- `dataclasses.asdict(openai_config(...))` is
  `{"primary": {"provider": "openai", "model": ..., "voice": ...}, "fallback": None}`.

**Step 2:** Run `uv run pytest pipeline/tts/test_config.py -q`. Expected: FAIL (ImportError).

**Step 3: Implement** in `config.py`. Keep `DEFAULT_OPENAI_MODEL`, `PCM_SAMPLE_RATE` and
`PCM_BYTES_PER_SECOND`. Add:

```python
DEFAULT_OPENAI_VOICE = "nova"
OPENAI_VOICES = frozenset({"alloy","ash","ballad","coral","echo","fable","nova","onyx","sage","shimmer","verse"})

@dataclass(frozen=True)
class OpenAIConfig:
    model: str
    voice: str
    provider: Literal["openai"] = "openai"
    # __post_init__: non-empty model/voice; provider == "openai"

@dataclass(frozen=True)
class GeminiConfig:
    model: str
    voice: str
    style: str = ""
    provider: Literal["gemini"] = "gemini"
    # __post_init__: non-empty model/voice; voice.lower() not in OPENAI_VOICES

@dataclass(frozen=True)
class RenderConfig:
    primary: OpenAIConfig | GeminiConfig
    fallback: OpenAIConfig | None = None
    # __post_init__: type checks; OpenAI primary => fallback is None

def openai_config(*, model: str, voice: str) -> RenderConfig: ...

def _openai(voice: str) -> RenderConfig:
    return RenderConfig(OpenAIConfig(model=DEFAULT_OPENAI_MODEL, voice=voice))

# The ONE owner of per-feed TTS settings. Feed slugs are literals because
# pipeline.tts must not import call-site modules (cycle); a test in
# pipeline/test_feed_voices.py pins that every routable feed slug is a key.
FEED_VOICES: dict[str, RenderConfig] = {
    "general": _openai("ash"), "levine": _openai("ash"), "yglesias": _openai("shimmer"),
    "silver": _openai("echo"), "the-rundown": _openai("nova"), "fp-digest": _openai("onyx"),
    "aaronson": _openai("fable"), "chinatalk": _openai("alloy"),
}
DEFAULT_RENDER_CONFIG = _openai(DEFAULT_OPENAI_VOICE)

def resolve_render_config(feed_slug: str, *, voice_override: str | None = None,
                          model_override: str | None = None) -> RenderConfig: ...
```

Field order note: `provider` goes last with a default, so `OpenAIConfig(model=..., voice=...)`
reads naturally and `asdict` still includes `provider`. `resolve_render_config` looks up
`FEED_VOICES` **at call time** (module global), so tests can monkeypatch it.

Export from `pipeline/tts/__init__.py`: `OpenAIConfig`, `GeminiConfig`, `RenderConfig`,
`FEED_VOICES`, `resolve_render_config`, `openai_config`, `DEFAULT_OPENAI_MODEL`,
`DEFAULT_OPENAI_VOICE`.

**Step 4:** Run the config tests. Expected: PASS. Other tests will break until Task 3; that's
expected.

**Step 5:** Commit with the message `tts: typed OpenAI/Gemini render configs and FEED_VOICES`.
Tasks 1 and 2 may be committed together if the suite can't be green in between. Say so in the
commit message.

---

### Task 2: Provider/render/cache migration to the typed config (OpenAI path unchanged)

**Files:**
- Modify: `pipeline/tts/providers.py`, `pipeline/tts/render.py`, `pipeline/tts/cache.py`,
  `pipeline/conftest.py` (the `_install_fake_render` fake: `provider=config.primary.provider`)
- Test: `pipeline/tts/test_providers.py`, `test_render.py`, `test_cache.py`

**Behavior:**

1. **Errors.** `TTSProviderError(message, *, retryable: bool | None = None, kind: str | None = None)`.
   Exactly one of the two is given. `kind` is one of content/infra/fatal. `retryable=True` maps to
   infra, and `retryable=False` maps to fatal. `.retryable` is a property: `kind != "fatal"`.
   Existing OpenAI call sites keep passing `retryable=`.
2. **OpenAI provider.** `OpenAIProvider.synthesize(text, cfg: OpenAIConfig)` uses `cfg.model` and
   `cfg.voice`.
3. **Render refuses Gemini.** `render._provider_for(leaf)` builds the provider from
   `config.primary`. At the very top of `render_episode`, after the empty-text check and before
   any cache or manifest work, a Gemini primary raises `ValueError`:
   `"Gemini rendering is not wired yet (T3b, my-podcasts-9p3.11)"`.
4. **`RenderResult`:**
   - `provider: str`: the rendered provider.
   - `config: RenderConfig`: the requested config.
   - A new field, **`rendered: OpenAIConfig | GeminiConfig`**: the leaf that actually produced the
     audio.
   - `cached`, `chunks` and `manifest_path` are unchanged.
5. **Cache key.** `cache.cache_key(text, config)` hashes
   `{"text", "primary": asdict(primary), "fallback": asdict-or-None, "renderer_version",
   "verifier_policy"}`. `verifier_policy` is `None` for an OpenAI primary. For a Gemini primary,
   use a local `from pipeline.tts.verify import VERIFIER_POLICY` inside the branch.
   `RENDERER_VERSION = "2"`.
6. **`result.json` shape.** When storing, `render.py` writes:

   ```json
   {"schema": 2, "provider": "openai", "requested": asdict(config),
    "rendered": asdict(config.primary), "verification": "not_run_openai",
    "fallback_reason": null, "renderer_version": "2", "rendered_at": ...,
    "chunks": N, "total_audio_seconds": S}
   ```

7. **`cache.lookup` validates provenance.** An entry is a miss unless all of these hold:
   - `result["schema"] == 2`
   - `result["rendered"]` parses into a leaf config via a new
     `config.leaf_from_dict(d) -> OpenAIConfig | GeminiConfig`, which raises `ValueError` on
     anything else
   - `result["provider"] == rendered.provider`
   - `result["verification"] in {"passed", "not_run_openai"}`
   - `verification == "passed"` exactly when `rendered.provider == "gemini"`

   `CachedRender` gains `rendered: OpenAIConfig | GeminiConfig`. A cache hit's `RenderResult`
   reports `provider=rendered.provider` and `rendered=hit.rendered`, never the requested config.
   A hit whose `result.json` fails validation is a miss, so the next render overwrites it (the
   `store` corrupt-entry path already clears it).
8. **Manifest record.** `"config": asdict(config)` (requested) plus
   `"rendered_config": asdict(leaf) or None`. The cache-hit path also sets `rendered_config`.

**Tests to add or adjust:**
- Existing render, provider and cache tests are updated to the new shapes. Their behavior must not
  change. `data["config"]["openai_voice"]` becomes `data["config"]["primary"]["voice"]`.
- `render_episode` with `RenderConfig(GeminiConfig("gemini-3.8-flash-lite-tts","Kore"), fallback=None)`
  raises `ValueError`. It never calls `_provider_for`, never writes a manifest, and never touches
  the cache dir.
- Cache key:
  - It changes with the fallback. Build two Gemini configs that differ only in fallback; `cache_key`
    works on them even though render refuses them.
  - For a Gemini primary it changes with `VERIFIER_POLICY` (monkeypatch `verify.VERIFIER_POLICY`).
  - For an OpenAI primary it does *not* change with `VERIFIER_POLICY`.
  - It changes with `RENDERER_VERSION`.
- Import hygiene: a subprocess test runs
  `python -c "import sys, pipeline.tts; from pipeline.tts.cache import cache_key; cache_key('x', pipeline.tts.openai_config(model='tts-1-hd', voice='nova')); assert 'google.genai' not in sys.modules"`.
- `lookup` returns a miss for each of these:
  - an entry missing `schema`
  - `rendered` naming an unknown provider
  - `provider` disagreeing with `rendered`
  - a Gemini `rendered` with `verification="not_run_openai"`
  - an OpenAI `rendered` with `verification="passed"`
- A valid entry round-trips, and `hit.rendered` equals the stored leaf.
- A render whose cache hit came from an entry with `rendered` = OpenAI(onyx), under a request for
  OpenAI(onyx), reports `result.rendered == OpenAIConfig("tts-1-hd","onyx")`. Also write a
  direct-store case where `rendered` differs from the request: `result.provider` and
  `result.rendered` must come from the entry.
- The manifest has `rendered_config` on both the render path and the cache-hit path. It is `None`
  on a failed render.

**Step order:** write the failing tests, run them (FAIL), implement, run
`uv run pytest pipeline/tts -q` (PASS), then commit
`tts: render/cache/providers on typed config; provenance-checked cache (RENDERER_VERSION 2)`.

---

### Task 3: Call sites resolve through `FEED_VOICES` (behavior preserved)

**Files:**
- Modify:
  - `pipeline/presets.py`: drop `tts_model` and `tts_voice` from `NewsletterPreset` and every
    preset
  - `pipeline/blog_sources.py`: drop `tts_voice`
  - `pipeline/processor.py`
  - `pipeline/script_processor.py`: keep `DEFAULT_VOICE = "nova"`, drop `TTS_MODEL`
  - `pipeline/__main__.py`: publish-script `--dry-run`
  - `pipeline/things_happen_processor.py` and `pipeline/fp_processor.py`: drop `TTS_MODEL` and
    `TTS_VOICE`, keep `FEED_SLUG`
  - `pipeline/blog_poller.py`
- Test: new `pipeline/test_feed_voices.py`; adjust `pipeline/test_blog_poller.py:70`
  (`src.tts_voice`) and any test that reads the removed constants. Grep for `tts_voice`,
  `tts_model`, `TTS_VOICE`, `TTS_MODEL` and `DEFAULT_VOICE`.

**Wiring:**
- `processor.py`: `tts.resolve_render_config(preset.feed_slug, voice_override=os.getenv("TTS_VOICE"), model_override=os.getenv("TTS_MODEL"))`.
- `script_processor.publish_script`: `tts.resolve_render_config(feed_slug, voice_override=voice)`.
  `voice` still defaults to `DEFAULT_VOICE`, so the behavior is identical.
- `__main__` publish-script `--dry-run`: the same call with `voice_override=voice`, so dry-run and
  live resolve identically.
- `things_happen_processor` / `fp_processor`: `tts.resolve_render_config(FEED_SLUG)`.
- `blog_poller`: `tts.resolve_render_config(source.feed_slug)`.

**Tests (`pipeline/test_feed_voices.py`):**
- Every routable slug is a `FEED_VOICES` key: each `PRESETS` slug, `DEFAULT_PRESET.feed_slug`,
  each `BLOG_SOURCES` slug, `fp_processor.FEED_SLUG` and `things_happen_processor.FEED_SLUG`. This
  ensures no configured feed silently falls through to the default.
- **Golden preservation:** a table of (call path, expected `openai_config`) matching the values
  *before* this PR. Hard-code the Decision 3 table here, not derived from `FEED_VOICES`. It covers:
  - email levine → ash
  - email with an unknown route tag → general → ash
  - the Rundown processor → nova
  - FP → onyx
  - aaronson blog → fable
  - `publish_script` with the default voice on the fp-digest feed → **nova**. This pins that
    manual publish is unchanged.
- Existing call-site tests (`test_processor_tts_render.py`, `test_fp_processor.py`,
  `test_things_happen_processor.py`, `test_script_processor.py`, `test_publish_script_cli.py`,
  `test_blog_poller.py`) must pass **unmodified** except where they read removed attributes. Their
  `openai_config(...)` equality assertions are the regression net.
- The email path with `TTS_VOICE=""` raises `ValueError`. Today `openai_config` raises the same.

Run `uv run pytest -q -x` (full, 900 s timeout), then ruff. Commit
`tts: FEED_VOICES is the single owner of per-feed voices; call sites resolve through it`.

---

### Task 4: `GeminiProvider` (REST) + test guard

**Files:**
- Modify: `pipeline/tts/providers.py`, `pipeline/conftest.py`
- Test: `pipeline/tts/test_gemini_provider.py` (new)

**Interface:**

```python
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_TIMEOUT_SECONDS = 90.0

@dataclass(frozen=True)
class Synthesis:
    pcm: bytes
    finish_reason: str
    prompt_tokens: int | None
    audio_tokens: int | None   # usageMetadata.candidatesTokenCount
    elapsed_s: float

def _make_gemini_session_unguarded() -> requests.Session: ...   # plain Session, no adapter retries
def _make_gemini_session() -> requests.Session: return _make_gemini_session_unguarded()

class GeminiProvider:
    max_chars = 3000   # application limit (not a documented vendor limit); bump RENDERER_VERSION if changed
    def __init__(self, *, timeout: float = DEFAULT_GEMINI_TIMEOUT_SECONDS) -> None
    def synthesize(self, text: str, cfg: GeminiConfig, *, timeout: float | None = None) -> bytes
    def synthesize_detailed(self, text: str, cfg: GeminiConfig, *, timeout: float | None = None) -> Synthesis
    def close(self) -> None     # closes every session this provider created; idempotent
    __enter__/__exit__
```

- **One `requests.Session` per thread.** Keep a `threading.local` plus a lock-guarded list of every
  session created, so `close()` closes all of them. T3b runs up to 4 worker threads on one
  provider.
- **API key.** `os.environ["GEMINI_API_KEY"]` is read per call. If it is missing or empty, raise
  `TTSProviderError(kind="fatal")` with the message `"GEMINI_API_KEY is not set"`. It goes in the
  `x-goog-api-key` header, never in the URL, and **no error message ever includes it**. Add a test
  that sets the key to a sentinel and asserts the sentinel appears in no raised message.
- **Request.** POST `f"{GEMINI_API_BASE}/models/{cfg.model}:generateContent"` with
  `timeout=(min(10.0, t), t)`, where `t = timeout if timeout is not None else self._timeout`.
  `t <= 0` raises `TTSProviderError(kind="infra", "no time budget left")` without any request.
  Body:

  ```json
  {"contents":[{"role":"user","parts":[{"text": TEXT, "speech_metadata": {"style": STYLE}}]}],
   "generationConfig":{"responseModalities":["AUDIO"],
     "speechConfig":{"voiceConfig":{"prebuiltVoiceConfig":{"voiceName": VOICE}}}}}
  ```

  Omit `speech_metadata` entirely when `cfg.style == ""`. **Never put the style into `text`**: a
  preamble in the text gets read aloud.
- **Classification.** Error messages include the HTTP status, `error.status`, `error.message`
  truncated to 300 chars, and the `ErrorInfo.reason` if present.

  | Response | Kind |
  |---|---|
  | `requests.Timeout` / `ConnectionError` / other `RequestException` | infra |
  | 5xx, 408 | infra |
  | 429 whose `details` has a `type.googleapis.com/google.rpc.QuotaFailure` violation whose `quotaId` or `quotaMetric` matches `(?i)per.?day\|daily` | fatal (confirmed nonrenewable quota) |
  | any other 429 | infra (ambiguous is treated as bounded transient) |
  | 400 (includes `API_KEY_INVALID` and unknown voice), 401, 403, 404, any other 4xx | fatal |
  | 200 with a non-JSON body | infra |
  | 200 with no `candidates` | content (include `promptFeedback.blockReason` if present) |
  | 200 with `finishReason` != `STOP` | content (include the finish reason) |
  | 200, STOP, zero `inlineData` parts | content |
  | 200, STOP, more than one `inlineData` part | fatal (unexpected shape; never concatenate) |
  | `mimeType` not starting with `audio/wav` (case-insensitive) | fatal (format drift is systemic) |
  | base64 invalid (`b64decode(validate=True)`), `wave.Error`, zero frames, `readframes` shorter than declared | infra (corrupt transfer) |
  | WAV rate != 24000, channels != 1, or sampwidth != 2 | fatal |

- **Success.** Returns `readframes(nframes)` exactly. Trailing RIFF chunks (C2PA) are ignored by
  construction.

**Guard** (`pipeline/conftest.py`): add an autouse `_block_real_gemini_tts` that patches
`pipeline.tts.providers._make_gemini_session` to raise `AssertionError`, mirroring
`_block_real_openai_tts`. Tests inject a fake session via
`monkeypatch.setattr(providers, "_make_gemini_session", lambda: fake)`.

**Tests** (fake session whose `.post` records kwargs and returns a fake response with
`status_code`, `.json()` and `.text`; build WAVs with stdlib `wave` in a helper):
- Request shape:
  - URL
  - header key
  - JSON body exactly as above, with and without style
  - timeout tuple
  - the key is not in the URL
- Success returns PCM, and `Synthesis` has the finish reason and tokens.
- **A WAV with a trailing `C2PA` chunk after `data`**: append `b"C2PA" + struct.pack("<I", n) + b"\0"*n`
  and fix up the RIFF size. The result equals just the frames. Use the real header layout from
  `T3-FACTS.md`.
- One test per row of the classification table, including:
  - a 400 `API_KEY_INVALID` body copied from T3-FACTS (fatal)
  - a 400 unknown voice (fatal)
  - a 429 with a `QuotaFailure` `...PerDay...` (fatal) vs a 429 with only `RetryInfo` (infra)
  - `finishReason: OTHER` with no content (content)
  - truncated data (infra)
  - 44.1 kHz (fatal)
  - stereo (fatal)
- A missing key is fatal, and no session is created.
- `timeout=0` is infra and makes no request.
- Threads: two threads each call `synthesize` once with a fake factory that counts sessions. There
  are 2 sessions, and `close()` closes both. A second `close()` is a no-op.
- The autouse guard fires: calling `providers._make_gemini_session()` raises `AssertionError`.

Commit `tts: GeminiProvider (REST, validated WAV, classified errors) + test guard`.

---

### Task 5: Docs and design amendments

**Files:** `docs/plans/2026-09-30-gemini-tts-design.md`, `pipeline/AGENTS.md` ("TTS Renderer"),
`AGENTS.md` (Core Paths TTS bullet).

- **Design doc amendments.** Add a short "Amendments (T3 consult, 2026-09-30)" section after
  "Render flow". The inline text stays, but these override it:
  - The "infra-transient exhausting retries on 2 distinct chunks" rule is dropped, because any
    exhausted chunk already triggers whole-episode fallback.
  - Each chunk gets a single 3-call counter covering transient retries and the one omission
    re-render: at most 3 TTS calls and 2 ASR calls.
  - A 429 is fatal only with confirmed daily/billing `QuotaFailure` detail; `RESOURCE_EXHAUSTED`
    alone is transient.
  - The bound is the 6-min Gemini budget plus reap margin. `send_alert` is synchronous.
  - The config shape is the primary/fallback pair, not flat fields.
  - Gemini WAVs carry a trailing C2PA chunk.
  - The PR split: T3a is this PR, T3b is `my-podcasts-9p3.11`.
  - Manual-publish defaults stay `nova` until T6.
- **`pipeline/AGENTS.md`:**
  - `FEED_VOICES` in `pipeline/tts/config.py` is now the only place a feed's voice lives.
  - Overrides force OpenAI.
  - `publish_script`/CLI still default to nova (T6 changes it).
  - `GeminiProvider` exists but `render_episode` refuses a Gemini primary until T3b.
  - Cache entries now carry provenance, and `RENDERER_VERSION` 2 means the cache is cold on
    deploy.
  - The guard `_block_real_gemini_tts`.
- **`AGENTS.md` Core Paths:** mention `FEED_VOICES`.

Commit `docs: T3a config/provider notes and design amendments`.

---

### Task 6: Real smoke (paid, well under $1)

The controller runs this, not a subagent. The script lives at
`/persist/my-podcasts/tts-eval/t3/smoke_t3a.py` (outside the repo) and is run with
`uv run --project <worktree> python ...`. Record the results in
`/persist/my-podcasts/tts-eval/t3/SMOKE-T3a.md`.

1. `GeminiProvider().synthesize_detailed(chunk, GeminiConfig("gemini-3.8-flash-lite-tts","Kore","calm, measured news anchor"))`
   on a `chunk_text(in-rundown.txt, ceiling=3000)` chunk. Check:
   - PCM length is even and nonzero
   - seconds ≈ chars / 16
   - tokens are recorded
   - wrap it with `asr.pcm_to_wav` and write it out
   - ffprobe the duration
2. Voice `"NotAVoice"` gives a fatal `TTSProviderError`.
3. Env `GEMINI_API_KEY=bogus` gives a fatal error (400 `API_KEY_INVALID`), and the message
   contains no `bogus`.
4. Real OpenAI `render_episode` of one short paragraph with `resolve_render_config("fp-digest")`,
   `cache_dir` and `manifest_dir` under the t3 dir. The OpenAI key is readable via sudo; export it
   in-process and never echo it. Then check:
   - the manifest has `rendered_config`
   - a second call is a cache hit with `rendered == OpenAIConfig("tts-1-hd","onyx")`

Then open the PR per `shepherding-pull-requests`: pre-PR adversarial-reviewer-astra first.
