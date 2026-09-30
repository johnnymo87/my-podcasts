# T1: In-repo OpenAI renderer (replace `ttsjoin`) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Bead:** `my-podcasts-9p3.1` (epic `my-podcasts-9p3`). **Spec:** `docs/plans/2026-09-30-gemini-tts-design.md`
— sections "Components", "Config precedence", "Completed-render reuse", "Telemetry", "Rollout" item 1.

**Goal:** Replace the `ttsjoin` subprocess at all six TTS call sites with an in-repo Python renderer
(`pipeline/tts/`) that renders with OpenAI `tts-1-hd` exactly as configured today, reuses completed
renders across retries, and writes a per-attempt manifest.

**Architecture:** `pipeline/tts/` is a small package: `config` (what to render with), `chunker`
(lossless text partition), `providers` (OpenAI → raw PCM), `encode` (PCM → one mp3 via ffmpeg),
`cache` (completed-render reuse), `manifest` (diagnostics), `render` (orchestration). Call sites do
`from pipeline import tts` and call `tts.render_episode(...)`, so tests patch one attribute:
`pipeline.tts.render_episode`.

**Tech stack:** Python 3.14, `openai` 2.x SDK (`response_format="pcm"`), `ffmpeg` (libmp3lame),
pytest. No `pydub`, no `nltk`.

**Scope fence:** OpenAI only. No Gemini code, no verifier, no `FEED_VOICES` consolidation (those are
T2/T3). If you find work belonging elsewhere, file a bead with `--parent=my-podcasts-9p3` and leave it.

## Facts every task needs

- Work only in the worktree `/home/dev/projects/my-podcasts/.worktrees/tts-openai-renderer`
  (branch `tts-openai-renderer`). Run commands with `workdir` set there. Never touch the repo root.
- Test commands: `uv run pytest <path> -q`; lint: `uv run ruff check . && uv run ruff format --check .`.
  The full suite takes ~6 minutes (700 tests at baseline).
- OpenAI `tts-1-hd` with `response_format="pcm"` returns raw **24 kHz, mono, signed 16-bit
  little-endian** PCM. Published episodes today are **mp3, 24 kHz, mono, 32 kbps** (measured) — the
  encoder pins exactly that.
- Tests must never reach the real OpenAI API or real ffmpeg-dependent paths unintentionally. Task 3
  adds an autouse conftest guard (pattern: the existing `_block_real_telegram_posts` in
  `pipeline/conftest.py`).
- `pipeline/conftest.py` already exists and holds shared fixtures; extend it, don't create another.
- Commit after each task with a conventional message (`feat(tts): ...`, `test(tts): ...`,
  `refactor: ...`). Commit bare — never pass `-c user.email`.

---

### Task 1: Package skeleton, `RenderConfig`, `openai` dependency

**Files:**
- Modify: `pyproject.toml` (add `"openai>=2.0.0",` to `[project].dependencies`)
- Create: `pipeline/tts/__init__.py`, `pipeline/tts/config.py`
- Test: `pipeline/tts/test_config.py`

(Tests live next to code in this repo, e.g. `pipeline/test_*.py`; `pipeline/tts/test_*.py` is
collected the same way. Confirm with `grep -n testpaths -A3 pyproject.toml` — if `testpaths` is
set, make sure `pipeline/tts` is covered; an `__init__.py` in `pipeline/tts/` is required.)

**Step 1: Write the failing test** — `pipeline/tts/test_config.py`

```python
from __future__ import annotations

import dataclasses

import pytest

from pipeline.tts.config import DEFAULT_OPENAI_MODEL, RenderConfig, openai_config


def test_openai_config_builds_openai_render_config() -> None:
    config = openai_config(model="tts-1-hd", voice="onyx")
    assert config == RenderConfig(
        provider="openai", openai_model="tts-1-hd", openai_voice="onyx"
    )


def test_default_model_is_tts_1_hd() -> None:
    assert DEFAULT_OPENAI_MODEL == "tts-1-hd"


def test_render_config_is_frozen() -> None:
    config = openai_config(model="tts-1-hd", voice="nova")
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.openai_voice = "ash"  # type: ignore[misc]


@pytest.mark.parametrize("model,voice", [("", "nova"), ("tts-1-hd", "")])
def test_openai_config_rejects_empty_fields(model: str, voice: str) -> None:
    with pytest.raises(ValueError):
        openai_config(model=model, voice=voice)
```

**Step 2:** `uv run pytest pipeline/tts/test_config.py -q` → FAIL (module not found).

**Step 3: Implement** — `pipeline/tts/config.py`

```python
"""What an episode is rendered with.

T1 knows only OpenAI. T3 adds Gemini fields and per-feed resolution; keep this
dataclass the single description of a render so the cache key and manifest
can serialize it with ``dataclasses.asdict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_OPENAI_MODEL = "tts-1-hd"


@dataclass(frozen=True)
class RenderConfig:
    provider: Literal["openai"]
    openai_model: str
    openai_voice: str


def openai_config(*, model: str, voice: str) -> RenderConfig:
    if not model or not voice:
        raise ValueError(f"OpenAI render needs model and voice, got {model!r}/{voice!r}")
    return RenderConfig(provider="openai", openai_model=model, openai_voice=voice)
```

`pipeline/tts/__init__.py` (render bits are added in Task 6):

```python
"""In-repo TTS renderer. See docs/plans/2026-09-30-gemini-tts-design.md."""

from pipeline.tts.config import DEFAULT_OPENAI_MODEL, RenderConfig, openai_config

__all__ = ["DEFAULT_OPENAI_MODEL", "RenderConfig", "openai_config"]
```

Add `"openai>=2.0.0",` to `[project].dependencies` in `pyproject.toml`, then `uv lock` and
`uv sync`. (`openai` 2.3.0 is already installed transitively; this makes it a direct dependency.)

**Step 4:** `uv run pytest pipeline/tts/test_config.py -q` → PASS.

**Step 5:** Commit `feat(tts): add pipeline.tts package with RenderConfig; openai as main dep`.

---

### Task 2: Lossless chunker

**Files:** Create `pipeline/tts/chunker.py`; Test `pipeline/tts/test_chunker.py`.

Contract: `chunk_text(text, *, target=3000, ceiling=4096) -> list[str]`.
- Split into paragraphs on blank lines. Pack paragraphs greedily into chunks ≤ `target`, joined by
  `"\n\n"`.
- A paragraph longer than `target` is split into sentences (split after `.`, `!`, `?`, optionally
  followed by a closing quote/bracket, then whitespace); sentences of one paragraph are joined with
  `" "`.
- A sentence longer than `target` is split on whitespace into word runs ≤ `target`.
- A single word longer than `target` is hard-cut every `target` chars.
- **Lossless:** `" ".join(chunks).split() == text.split()`, except that hard-cut words are
  split into pieces.
- Every chunk ≤ `target` ≤ `ceiling`; `target > ceiling` → `ValueError`. Whitespace-only input → `[]`.

**Step 1: Write the failing tests**

```python
from __future__ import annotations

import pytest

from pipeline.tts.chunker import chunk_text


def _tokens(chunks: list[str]) -> list[str]:
    return " ".join(chunks).split()


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("Hello there.\n\nSecond paragraph.") == [
        "Hello there.\n\nSecond paragraph."
    ]


def test_whitespace_only_is_empty() -> None:
    assert chunk_text("  \n\n \n") == []


def test_packs_paragraphs_under_target_and_breaks_between_them() -> None:
    para = "word " * 39 + "end."  # 199 chars
    text = "\n\n".join([para] * 10)
    chunks = chunk_text(text, target=500, ceiling=4096)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) == 5  # two 199-char paragraphs (+2 sep) per chunk
    assert all(c.count("\n\n") == 1 for c in chunks)
    assert _tokens(chunks) == text.split()


def test_oversize_paragraph_splits_on_sentences() -> None:
    sentence = "This sentence is about forty characters. "
    para = (sentence * 30).strip()  # ~1230 chars, one paragraph
    chunks = chunk_text(para, target=300, ceiling=4096)
    assert all(len(c) <= 300 for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    assert _tokens(chunks) == para.split()


def test_sentence_split_keeps_closing_quote_with_sentence() -> None:
    para = ('He said "stop." ' * 40).strip()
    chunks = chunk_text(para, target=100, ceiling=4096)
    assert all(c.endswith('stop."') for c in chunks)


def test_oversize_sentence_splits_on_whitespace() -> None:
    sentence = " ".join(["word"] * 400) + "."  # ~2000 chars, no sentence break
    chunks = chunk_text(sentence, target=300, ceiling=4096)
    assert all(len(c) <= 300 for c in chunks)
    assert _tokens(chunks) == sentence.split()


def test_unbroken_string_is_hard_cut() -> None:
    blob = "x" * 1000
    chunks = chunk_text(blob, target=300, ceiling=4096)
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks) == blob


def test_target_above_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_text("hi", target=5000, ceiling=4096)


def test_real_shaped_text_respects_openai_ceiling() -> None:
    paras = [("Sentence number %d is here. " % i) * 60 for i in range(12)]
    text = "\n\n".join(p.strip() for p in paras)
    chunks = chunk_text(text)
    assert all(len(c) <= 3000 for c in chunks)
    assert _tokens(chunks) == text.split()
```

**Step 2:** run → FAIL.

**Step 3: Implement** — `pipeline/tts/chunker.py`

```python
"""Lossless, paragraph-aware text partition for TTS.

Replaces tts-joinery's nltk sentence packing. No nltk: its tokenizer data is
fetched from the network at runtime (bead my-podcasts-4ld).
"""

from __future__ import annotations

import re

DEFAULT_TARGET_CHARS = 3000
OPENAI_MAX_CHARS = 4096

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
# Fixed-width lookbehinds only (re has no variable-width lookbehind).
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'’”)\]])\s+")


def chunk_text(
    text: str, *, target: int = DEFAULT_TARGET_CHARS, ceiling: int = OPENAI_MAX_CHARS
) -> list[str]:
    if target > ceiling:
        raise ValueError(f"target {target} exceeds provider ceiling {ceiling}")
    units: list[tuple[str, str]] = []  # (separator before unit, unit text)
    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for i, piece in enumerate(_fit(paragraph, target)):
            units.append(("\n\n" if i == 0 else " ", piece))

    chunks: list[str] = []
    current = ""
    for sep, unit in units:
        if not current:
            current = unit
        elif len(current) + len(sep) + len(unit) <= target:
            current = f"{current}{sep}{unit}"
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    assert all(len(c) <= ceiling for c in chunks)
    return chunks


def _fit(paragraph: str, limit: int) -> list[str]:
    """Split one paragraph into pieces each <= limit (sentences, then words)."""
    if len(paragraph) <= limit:
        return [paragraph]
    pieces: list[str] = []
    for sentence in _SENTENCE_BREAK.split(paragraph):
        if len(sentence) <= limit:
            pieces.append(sentence)
        else:
            pieces.extend(_split_words(sentence, limit))
    return pieces


def _split_words(sentence: str, limit: int) -> list[str]:
    out: list[str] = []
    current = ""
    for word in sentence.split():
        while len(word) > limit:  # unbroken string: hard cut
            if current:
                out.append(current)
                current = ""
            out.append(word[:limit])
            word = word[limit:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= limit:
            current = f"{current} {word}"
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out
```

Note on packing: sentence pieces from one oversize paragraph are packed back together (joined by
`" "`) up to `target`, which is why `test_oversize_paragraph_splits_on_sentences` expects chunks
ending in `.` rather than one sentence per chunk.

**Step 4:** run → PASS. If `test_packs_paragraphs_under_target_and_breaks_between_them` count is
off, recompute by hand; do not loosen the lossless assertions.

**Step 5:** Commit `feat(tts): lossless paragraph-aware chunker (no nltk)`.

---

### Task 3: OpenAI provider + test guard against real API calls

**Files:** Create `pipeline/tts/providers.py`; Modify `pipeline/conftest.py`; Test
`pipeline/tts/test_providers.py`.

Contract:
- `class TTSProviderError(Exception)` with attribute `retryable: bool`.
- `class OpenAIProvider` with `max_chars = 4096`, `__init__(self, *, timeout: float = 120.0)`,
  `synthesize(text: str, config: RenderConfig) -> bytes` returning raw PCM.
- The client is created lazily by module function `_make_openai_client(timeout)` →
  `openai.OpenAI(max_retries=0, timeout=timeout)`. **SDK retries are off: the renderer owns retries.**
- Error mapping: `openai.APIStatusError` → retryable iff `status_code >= 500 or == 429`;
  `openai.APIConnectionError` (includes `APITimeoutError`) → retryable. Empty or odd-length PCM →
  retryable `TTSProviderError`.

**Step 1: Failing tests** — `pipeline/tts/test_providers.py`

```python
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import openai
import pytest

from pipeline.tts import providers
from pipeline.tts.config import openai_config
from pipeline.tts.providers import OpenAIProvider, TTSProviderError

CONFIG = openai_config(model="tts-1-hd", voice="onyx")


def _client_returning(data: bytes) -> MagicMock:
    client = MagicMock()
    client.audio.speech.create.return_value.read.return_value = data
    return client


def test_synthesize_requests_pcm_with_config(monkeypatch) -> None:
    client = _client_returning(b"\x01\x00" * 10)
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    pcm = OpenAIProvider().synthesize("Hello.", CONFIG)
    assert pcm == b"\x01\x00" * 10
    client.audio.speech.create.assert_called_once_with(
        model="tts-1-hd", voice="onyx", input="Hello.", response_format="pcm"
    )


def test_client_is_built_with_sdk_retries_off(monkeypatch) -> None:
    built = {}

    def fake_openai(**kwargs):
        built.update(kwargs)
        return _client_returning(b"\x00\x00")

    monkeypatch.setattr(providers.openai, "OpenAI", fake_openai)
    providers._make_openai_client_unguarded(timeout=42.0)
    assert built == {"max_retries": 0, "timeout": 42.0}


@pytest.mark.parametrize("data", [b"", b"\x00"])
def test_empty_or_odd_pcm_is_retryable(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr(
        providers, "_make_openai_client", lambda timeout: _client_returning(data)
    )
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable


def _status_error(code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    response = httpx.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


@pytest.mark.parametrize("code,retryable", [(500, True), (503, True), (429, True), (400, False), (401, False)])
def test_status_errors_map_retryability(monkeypatch, code: int, retryable: bool) -> None:
    client = MagicMock()
    client.audio.speech.create.side_effect = _status_error(code)
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable is retryable


def test_connection_error_is_retryable(monkeypatch) -> None:
    client = MagicMock()
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    client.audio.speech.create.side_effect = openai.APITimeoutError(request=request)
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: client)
    with pytest.raises(TTSProviderError) as exc:
        OpenAIProvider().synthesize("Hello.", CONFIG)
    assert exc.value.retryable


def test_autouse_guard_blocks_real_client() -> None:
    with pytest.raises(AssertionError, match="real OpenAI"):
        providers._make_openai_client(timeout=1.0)
```

Implementation detail the tests rely on: `_make_openai_client` is a thin module-level wrapper that
calls `_make_openai_client_unguarded`; the conftest guard patches `_make_openai_client` only, so the
constructor-args test can call the unguarded one directly.

**Step 2:** run → FAIL.

**Step 3: Implement** — `pipeline/tts/providers.py`

```python
"""TTS providers. Each returns raw 24 kHz mono s16le PCM for one chunk."""

from __future__ import annotations

import openai

from pipeline.tts.config import RenderConfig

PCM_SAMPLE_RATE = 24_000
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2  # mono, 16-bit


class TTSProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _make_openai_client_unguarded(*, timeout: float) -> openai.OpenAI:
    # max_retries=0: the renderer owns retries, so the SDK's hidden 2 retries x
    # 600 s default timeout cannot silently multiply a render's wall time.
    return openai.OpenAI(max_retries=0, timeout=timeout)


def _make_openai_client(timeout: float) -> openai.OpenAI:
    return _make_openai_client_unguarded(timeout=timeout)


class OpenAIProvider:
    max_chars = 4096

    def __init__(self, *, timeout: float = 120.0) -> None:
        self._timeout = timeout
        self._client: openai.OpenAI | None = None

    def synthesize(self, text: str, config: RenderConfig) -> bytes:
        if self._client is None:
            self._client = _make_openai_client(self._timeout)
        try:
            response = self._client.audio.speech.create(
                model=config.openai_model,
                voice=config.openai_voice,
                input=text,
                response_format="pcm",
            )
            data = response.read()
        except openai.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code == 429
            raise TTSProviderError(
                f"OpenAI HTTP {exc.status_code}: {exc}", retryable=retryable
            ) from exc
        except openai.APIConnectionError as exc:
            raise TTSProviderError(f"OpenAI connection: {exc}", retryable=True) from exc
        if not data or len(data) % 2:
            raise TTSProviderError(
                f"OpenAI returned {len(data)} bytes of PCM", retryable=True
            )
        return data
```

**Conftest guard** — append to `pipeline/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _block_real_openai_tts(request):
    """No test may build a real OpenAI client (a real TTS call costs money).

    Tests exercising the provider patch ``pipeline.tts.providers._make_openai_client``
    themselves; their patch nests inside this one and wins.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    def _refuse(timeout: float):
        raise AssertionError(
            "A test tried to build a real OpenAI client. Patch "
            "pipeline.tts.providers._make_openai_client or pipeline.tts.render_episode."
        )

    with patch("pipeline.tts.providers._make_openai_client", _refuse):
        yield
```

The message must contain "real OpenAI" (the guard test matches on it).

**Step 4:** `uv run pytest pipeline/tts -q` → PASS.

**Step 5:** Commit `feat(tts): OpenAI PCM provider with renderer-owned retries; block real client in tests`.

---

### Task 4: PCM → mp3 encoder

**Files:** Create `pipeline/tts/encode.py`; Test `pipeline/tts/test_encode.py`.

**Step 1: Failing tests**

```python
from __future__ import annotations

import shutil
import subprocess

import pytest

from pipeline.tts import encode


def test_encode_invokes_ffmpeg_with_pinned_format(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(encode.subprocess, "run", fake_run)
    out = tmp_path / "x.mp3"
    encode.encode_mp3(b"\x00\x00" * 10, out)
    cmd = seen["cmd"]
    assert cmd[0] == "ffmpeg"
    for flag, value in [("-f", "s16le"), ("-codec:a", "libmp3lame"), ("-b:a", "32k")]:
        assert cmd[cmd.index(flag) + 1] == value
    assert cmd.count("24000") == 2 and cmd.count("1") >= 2
    assert cmd[-1] == str(out)
    assert seen["kwargs"]["input"] == b"\x00\x00" * 10
    assert seen["kwargs"]["check"] is True
    assert seen["kwargs"]["timeout"] > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_encode_real_ffmpeg_produces_24k_mono_32kbps(tmp_path) -> None:
    out = tmp_path / "tone.mp3"
    encode.encode_mp3(b"\x00\x10" * 24_000 * 2, out)  # 2 s of constant signal
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,sample_rate,channels", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    assert probe.stdout.strip() == "mp3,24000,1"
```

**Step 2:** run → FAIL.

**Step 3: Implement**

```python
"""Encode the joined PCM once, matching today's published format exactly:
mp3, 24 kHz, mono, 32 kbps (measured on published episodes 2026-09-30)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.tts.providers import PCM_SAMPLE_RATE

ENCODE_TIMEOUT_SECONDS = 600


def encode_mp3(pcm: bytes, out_mp3: Path) -> None:
    rate = str(PCM_SAMPLE_RATE)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "s16le", "-ar", rate, "-ac", "1", "-i", "pipe:0",
            "-codec:a", "libmp3lame", "-b:a", "32k", "-ar", rate, "-ac", "1",
            str(out_mp3),
        ],
        input=pcm,
        check=True,
        capture_output=True,
        timeout=ENCODE_TIMEOUT_SECONDS,
    )
```

**Step 4:** run → PASS (real-ffmpeg test runs locally; CI skips it if no ffmpeg).

**Step 5:** Commit `feat(tts): encode PCM to pinned 24k mono 32kbps mp3`.

---

### Task 5: Completed-render cache

**Files:** Create `pipeline/tts/cache.py`; Test `pipeline/tts/test_cache.py`.

Contract (spec "Completed-render reuse"):
- `RENDERER_VERSION = "1"` — bump on any change to chunking, encoding, provider request shape.
- `DEFAULT_CACHE_DIR = Path("/persist/my-podcasts/tts-cache")`; `RETENTION_DAYS = 14`.
- `cache_key(text, config) -> str`: sha256 hex of `json.dumps({"text": text, "primary":
  asdict(config), "fallback": None, "renderer_version": RENDERER_VERSION}, sort_keys=True,
  ensure_ascii=False)`. (`fallback` is `None` until T3.)
- `lookup(cache_dir, key) -> CachedRender | None` — `CachedRender(audio: Path, result: dict)`. Miss
  when the dir, `result.json`, or non-empty `audio.mp3` is absent, or `result.json` is not valid JSON.
  **Never raises.**
- `store(cache_dir, key, mp3, result) -> bool` — writes `audio.mp3` + `result.json` into a temp dir
  inside `cache_dir`, then one `os.rename` to `cache_dir/key`. If the target already exists, discard
  the temp dir and return True. Any other error: log a warning containing "reduced retry
  protection", clean up, return False. **Never raises.**
- `prune(cache_dir, *, max_age_days=RETENTION_DAYS, now=None) -> None` — remove entries older than
  the cutoff by directory mtime; also stale `.tmp-*` dirs. Never raises.

**Step 1: Failing tests**

```python
from __future__ import annotations

import json
import os
import time

from pipeline.tts import cache
from pipeline.tts.config import openai_config

CFG = openai_config(model="tts-1-hd", voice="nova")


def _mp3(tmp_path, data=b"ID3fake"):
    p = tmp_path / "in.mp3"
    p.write_bytes(data)
    return p


def test_key_is_stable_and_sensitive(monkeypatch) -> None:
    k = cache.cache_key("hello", CFG)
    assert k == cache.cache_key("hello", CFG)
    assert k != cache.cache_key("hello!", CFG)
    assert k != cache.cache_key("hello", openai_config(model="tts-1-hd", voice="ash"))
    monkeypatch.setattr(cache, "RENDERER_VERSION", "999")
    assert k != cache.cache_key("hello", CFG)


def test_store_then_lookup_round_trips(tmp_path) -> None:
    d = tmp_path / "c"
    assert cache.store(d, "k1", _mp3(tmp_path), {"provider": "openai"})
    hit = cache.lookup(d, "k1")
    assert hit is not None
    assert hit.audio.read_bytes() == b"ID3fake"
    assert hit.result == {"provider": "openai"}


def test_lookup_misses(tmp_path) -> None:
    d = tmp_path / "c"
    assert cache.lookup(d, "absent") is None
    (d / "half").mkdir(parents=True)
    (d / "half" / "audio.mp3").write_bytes(b"x")  # no result.json: half-written
    assert cache.lookup(d, "half") is None
    (d / "badjson").mkdir()
    (d / "badjson" / "audio.mp3").write_bytes(b"x")
    (d / "badjson" / "result.json").write_text("{not json")
    assert cache.lookup(d, "badjson") is None
    (d / "empty").mkdir()
    (d / "empty" / "audio.mp3").write_bytes(b"")
    (d / "empty" / "result.json").write_text("{}")
    assert cache.lookup(d, "empty") is None


def test_store_existing_key_is_success_and_keeps_first(tmp_path) -> None:
    d = tmp_path / "c"
    assert cache.store(d, "k", _mp3(tmp_path, b"first"), {"n": 1})
    assert cache.store(d, "k", _mp3(tmp_path, b"second"), {"n": 2})
    assert cache.lookup(d, "k").audio.read_bytes() == b"first"
    assert not [p for p in d.iterdir() if p.name.startswith(".tmp-")]


def test_store_failure_returns_false_and_warns(tmp_path, caplog) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert cache.store(blocker, "k", _mp3(tmp_path), {}) is False
    assert "reduced retry protection" in caplog.text


def test_lookup_never_raises_on_unreadable_root(tmp_path) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert cache.lookup(blocker, "k") is None


def test_prune_removes_old_entries_only(tmp_path) -> None:
    d = tmp_path / "c"
    cache.store(d, "old", _mp3(tmp_path), {})
    cache.store(d, "new", _mp3(tmp_path), {})
    old_time = time.time() - 20 * 86400
    os.utime(d / "old", (old_time, old_time))
    cache.prune(d, max_age_days=14)
    assert not (d / "old").exists()
    assert (d / "new").exists()
    json.loads((d / "new" / "result.json").read_text())
```

**Step 2:** run → FAIL.

**Step 3: Implement**

```python
"""Completed-render reuse: a retry after an upload/DB failure costs nothing.

Replaces tts-joinery's per-chunk cache. Only completed renders are stored.
Every function is best-effort and never raises -- a cache problem must never
discard valid audio (design doc, "Completed-render reuse").
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline.tts.config import RenderConfig

log = logging.getLogger(__name__)

RENDERER_VERSION = "1"
DEFAULT_CACHE_DIR = Path("/persist/my-podcasts/tts-cache")
RETENTION_DAYS = 14


@dataclass(frozen=True)
class CachedRender:
    audio: Path
    result: dict


def cache_key(text: str, config: RenderConfig) -> str:
    payload = json.dumps(
        {
            "text": text,
            "primary": asdict(config),
            "fallback": None,
            "renderer_version": RENDERER_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lookup(cache_dir: Path, key: str) -> CachedRender | None:
    try:
        entry = cache_dir / key
        audio = entry / "audio.mp3"
        result = json.loads((entry / "result.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not audio.is_file() or audio.stat().st_size == 0:
            return None
        return CachedRender(audio=audio, result=result)
    except Exception:  # noqa: BLE001 -- a cache miss is always safe
        return None


def store(cache_dir: Path, key: str, mp3: Path, result: dict) -> bool:
    tmp: Path | None = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=cache_dir, prefix=".tmp-"))
        shutil.copyfile(mp3, tmp / "audio.mp3")
        (tmp / "result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
        target = cache_dir / key
        if target.exists():
            return True
        try:
            os.rename(tmp, target)
            tmp = None
        except OSError:
            if target.exists():  # lost a race; the first writer's entry stands
                return True
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("TTS cache store failed (%s); reduced retry protection", exc)
        return False
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def prune(cache_dir: Path, *, max_age_days: int = RETENTION_DAYS, now: float | None = None) -> None:
    try:
        cutoff = (now if now is not None else time.time()) - max_age_days * 86400
        for entry in cache_dir.iterdir():
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return
```

Note `prune` also catches stale `.tmp-*` dirs because they are dirs older than the cutoff.

**Step 4:** run → PASS.

**Step 5:** Commit `feat(tts): best-effort completed-render cache`.

---

### Task 6: Manifest + `render_episode`

**Files:** Create `pipeline/tts/manifest.py`, `pipeline/tts/render.py`; Modify
`pipeline/tts/__init__.py`; Test `pipeline/tts/test_render.py`.

Contracts:

`manifest.py`:
- `DEFAULT_MANIFEST_DIR = Path("/persist/my-podcasts/tts-renders")`, `RETENTION_DAYS = 60`.
- `write_manifest(manifest_dir, *, feed_slug, episode_id, record: dict) -> Path | None` — path
  `manifest_dir/feed_slug/{episode_id}-{UTC %Y%m%dT%H%M%S%fZ}.json`; write `.tmp` then
  `os.replace`; on any error log a warning and return None. Never raises.
- `prune_manifests(manifest_dir, *, max_age_days=RETENTION_DAYS)` — remove `*.json` files older
  than cutoff under `manifest_dir/*/`. Never raises.

`render.py`:

```python
@dataclass(frozen=True)
class RenderResult:
    provider: str          # provider that RENDERED the audio (not "published": caller's concern)
    config: RenderConfig
    cached: bool
    chunks: int
    manifest_path: Path | None

class TTSRenderError(RuntimeError): ...

def render_episode(
    text: str,
    config: RenderConfig,
    out_mp3: Path,
    *,
    feed_slug: str,
    episode_id: str,
    manifest_dir: Path | None = DEFAULT_MANIFEST_DIR,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
) -> RenderResult
```

Behavior:
1. `text.strip()` empty → `ValueError`.
2. If `cache_dir`: `prune(cache_dir)`; `lookup(cache_dir, cache_key(text, config))`. Hit → copy
   audio to `out_mp3`, write manifest (`status="cache_hit"`), return `cached=True`.
3. Provider: `_provider_for(config)` returns `OpenAIProvider()` for `"openai"`
   (module-level function so tests patch it).
4. `chunk_text(text, ceiling=provider.max_chars)`.
5. Chunks rendered **sequentially** (as today). Per chunk up to `MAX_ATTEMPTS = 3`; on retryable
   `TTSProviderError`, sleep `BACKOFF_SECONDS[attempt]` (`(2.0, 8.0)`) via module-level `_sleep`;
   non-retryable → stop immediately. Exhausted/non-retryable → write manifest
   (`status="failed"`, `error`) then raise `TTSRenderError(f"chunk {i+1}/{n} failed after
   {attempts} attempt(s): {err}")`.
6. `encode_mp3(b"".join(pcm_parts), out_mp3)` (module-level import, patched in tests).
7. If `cache_dir`: `store(cache_dir, key, out_mp3, result_record)` where `result_record =
   {"provider": config.provider, "config": asdict(config), "renderer_version": RENDERER_VERSION,
   "rendered_at": <iso>}`.
8. Manifest (`status="rendered"`) with: `renderer_version, feed_slug, episode_id, started_at,
   wall_seconds, input_sha256, input_chars, cache_key, config, rendered_provider, cached,
   chunks: [{index, chars, attempts, errors: [str], audio_seconds}], total_audio_seconds,
   cache_stored: bool, status, error`.
9. `prune_manifests(manifest_dir)` once per call when `manifest_dir` is set.
10. Log one INFO line: provider, chunk count, audio seconds, wall seconds, cached.

Update `pipeline/tts/__init__.py` to also export `RenderResult`, `TTSRenderError`,
`render_episode`.

**Step 1: Failing tests** — `pipeline/tts/test_render.py`

```python
from __future__ import annotations

import json

import pytest

from pipeline.tts import render
from pipeline.tts.config import openai_config
from pipeline.tts.providers import TTSProviderError

CFG = openai_config(model="tts-1-hd", voice="onyx")
TEXT = "First paragraph.\n\n" + ("Second paragraph sentence. " * 200)


class FakeProvider:
    max_chars = 4096

    def __init__(self, script=None):
        self.calls: list[str] = []
        self.script = list(script or [])  # exceptions to raise, in order

    def synthesize(self, text, config):
        self.calls.append(text)
        if self.script:
            exc = self.script.pop(0)
            if exc is not None:
                raise exc
        return b"\x01\x00" * 24_000  # 1 s


@pytest.fixture
def harness(monkeypatch, tmp_path):
    provider = FakeProvider()
    encoded: list[bytes] = []
    monkeypatch.setattr(render, "_provider_for", lambda config: provider)
    monkeypatch.setattr(render, "_sleep", lambda s: None)

    def fake_encode(pcm, out):
        encoded.append(pcm)
        out.write_bytes(b"ID3" + pcm[:10])

    monkeypatch.setattr(render, "encode_mp3", fake_encode)
    return provider, encoded, tmp_path


def _run(tmp_path, **kw):
    kw.setdefault("manifest_dir", tmp_path / "m")
    kw.setdefault("cache_dir", tmp_path / "c")
    return render.render_episode(
        TEXT, CFG, tmp_path / "out.mp3", feed_slug="fp-digest", episode_id="2026-09-30-fp", **kw
    )


def test_renders_all_chunks_in_order_and_encodes_once(harness) -> None:
    provider, encoded, tmp = harness
    result = _run(tmp)
    assert len(provider.calls) == result.chunks >= 2
    assert " ".join(provider.calls).split() == TEXT.split()
    assert len(encoded) == 1 and len(encoded[0]) == 48_000 * result.chunks
    assert result.provider == "openai" and not result.cached
    assert (tmp / "out.mp3").exists()


def test_manifest_records_render(harness) -> None:
    _, _, tmp = harness
    result = _run(tmp)
    data = json.loads(result.manifest_path.read_text())
    assert result.manifest_path.parent == tmp / "m" / "fp-digest"
    assert result.manifest_path.name.startswith("2026-09-30-fp-")
    assert data["status"] == "rendered"
    assert data["rendered_provider"] == "openai"
    assert data["config"]["openai_voice"] == "onyx"
    assert data["total_audio_seconds"] == pytest.approx(result.chunks * 1.0)
    assert [c["attempts"] for c in data["chunks"]] == [1] * result.chunks


def test_second_render_is_a_cache_hit_with_no_provider_calls(harness) -> None:
    provider, _, tmp = harness
    _run(tmp)
    first_calls = len(provider.calls)
    (tmp / "out.mp3").unlink()
    result = _run(tmp)
    assert result.cached and len(provider.calls) == first_calls
    assert (tmp / "out.mp3").exists()
    assert json.loads(result.manifest_path.read_text())["status"] == "cache_hit"


def test_retryable_error_is_retried(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("500", retryable=True), None]
    result = _run(tmp)
    data = json.loads(result.manifest_path.read_text())
    assert data["chunks"][0]["attempts"] == 2
    assert "500" in data["chunks"][0]["errors"][0]


def test_non_retryable_error_fails_immediately_and_writes_manifest(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("401", retryable=False)]
    with pytest.raises(render.TTSRenderError, match=r"chunk 1/\d+ failed after 1 attempt"):
        _run(tmp)
    assert len(provider.calls) == 1
    [manifest] = (tmp / "m" / "fp-digest").glob("*.json")
    assert json.loads(manifest.read_text())["status"] == "failed"


def test_retry_exhaustion_raises_after_three_attempts(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("503", retryable=True)] * 3
    with pytest.raises(render.TTSRenderError, match="after 3 attempt"):
        _run(tmp)
    assert len(provider.calls) == 3


def test_failed_render_is_not_cached(harness) -> None:
    provider, _, tmp = harness
    provider.script = [TTSProviderError("401", retryable=False)]
    with pytest.raises(render.TTSRenderError):
        _run(tmp)
    assert not (tmp / "c").exists() or not any(
        p for p in (tmp / "c").iterdir() if not p.name.startswith(".")
    )


def test_manifest_and_cache_can_be_disabled(harness) -> None:
    _, _, tmp = harness
    result = _run(tmp, manifest_dir=None, cache_dir=None)
    assert result.manifest_path is None
    assert not (tmp / "m").exists() and not (tmp / "c").exists()


def test_unwritable_manifest_and_cache_do_not_fail_render(harness) -> None:
    _, _, tmp = harness
    blocker = tmp / "blocker"
    blocker.write_text("x")
    result = _run(tmp, manifest_dir=blocker, cache_dir=blocker)
    assert result.manifest_path is None
    assert (tmp / "out.mp3").exists()


def test_empty_text_rejected(harness) -> None:
    _, _, tmp = harness
    with pytest.raises(ValueError):
        render.render_episode(
            "  \n", CFG, tmp / "o.mp3", feed_slug="x", episode_id="y",
            manifest_dir=None, cache_dir=None,
        )
```

**Step 2:** run → FAIL.

**Step 3: Implement** `manifest.py` and `render.py` to the contracts above. Keep `render.py`
free of call-site knowledge (no feed names, no voices).

**Step 4:** `uv run pytest pipeline/tts -q` → PASS.

**Step 5:** Commit `feat(tts): render_episode with retries, cache reuse and per-attempt manifest`.

---

### Task 7: Shared test fixtures for call sites

**Files:** Modify `pipeline/conftest.py`; Modify `pipeline/test_publish_script_cli.py` (delete its
duplicate `captured_tts_input` fixture — that duplication is bead `my-podcasts-4sc`, close it with
this PR).

Replace the body of `captured_tts_input` and add `fake_tts_render`, both backed by one helper:

```python
def _install_fake_render(monkeypatch) -> tuple[list[dict], list[str]]:
    """Stub ``pipeline.tts.render_episode``; return (calls, texts).

    Each call records ``text``, ``config`` and the keyword args, and writes a
    small fake mp3 to ``out_mp3`` so callers can stat/upload it. Call sites
    must invoke ``tts.render_episode`` through the ``pipeline.tts`` module
    attribute for this patch to reach them.
    """
    from pipeline.tts import RenderResult

    calls: list[dict] = []
    texts: list[str] = []

    def fake(text, config, out_mp3, **kwargs):
        calls.append({"text": text, "config": config, "out_mp3": out_mp3, **kwargs})
        texts.append(text)
        Path(out_mp3).write_bytes(b"\xff\xfb\x90\x00" * 100)
        return RenderResult(
            provider=config.provider, config=config, cached=False, chunks=1,
            manifest_path=None,
        )

    monkeypatch.setattr("pipeline.tts.render_episode", fake)
    return calls, texts


@pytest.fixture
def fake_tts_render(monkeypatch) -> list[dict]:
    """Stub the renderer; return the list of recorded calls."""
    calls, _ = _install_fake_render(monkeypatch)
    return calls


@pytest.fixture
def captured_tts_input(monkeypatch) -> list[str]:
    """Stub the renderer and ``ffprobe`` (60 s); return texts handed to TTS."""
    _, texts = _install_fake_render(monkeypatch)

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    return texts
```

Keep the existing docstring intent (flag-name lookups no longer apply; say the capture is at the
`render_episode` boundary). A test must use one of the two fixtures, not both.

No test step of its own: this fixture is exercised by Task 8's migrations. Commit together with
Task 8's first call site.

---

### Task 8: Migrate the six call sites

For each site: `from pipeline import tts`; delete the `input_txt` temp file and the `ttsjoin`
command; call

```python
tts.render_episode(
    <text>, tts.openai_config(model=<model>, voice=<voice>), output_mp3,
    feed_slug=<feed slug>, episode_id=<episode_slug>,
)
```

Keep everything else (upload, ffprobe duration, DB insert) unchanged. **Add `timeout=60` to every
`ffprobe` `subprocess.run`** (five places: `processor.py:41`, `things_happen_processor.py:37`,
`script_processor.py:143`, `fp_processor.py:34`, `blog_poller.py:~179`).

| Site | Text | Model / voice (unchanged semantics) | feed_slug / episode_id |
|---|---|---|---|
| `pipeline/processor.py:131-151` | `body` after `prepend_title` | `tts_model`/`tts_voice` (env `TTS_MODEL`/`TTS_VOICE` override, else preset) | `preset.feed_slug` / `episode_slug` |
| `pipeline/script_processor.py:210-227` | `tts_text` | `TTS_MODEL` / `voice` kwarg (default `DEFAULT_VOICE="nova"`) | `feed_slug` / `episode_slug` |
| `pipeline/__main__.py:~840-880` (publish-script `--dry-run`) | `tts_text` | `TTS_MODEL` / `voice` | `feed_slug` / `"dry-run"`, **`manifest_dir=None, cache_dir=None`** (dry run touches no state) |
| `pipeline/things_happen_processor.py:91-108` | `script` | `TTS_MODEL` / `TTS_VOICE` | `FEED_SLUG` / `episode_slug` |
| `pipeline/fp_processor.py:75-92` | `script` | `TTS_MODEL` / `TTS_VOICE` | `FEED_SLUG` / `episode_slug` |
| `pipeline/blog_poller.py:146-170` | `adapted_text` after `prepend_title` | `TTS_MODEL` / `source.tts_voice` | `source.feed_slug` / `episode_slug` |

Do one site per commit, TDD-style:

**Step 1:** In the site's test file, replace every fake-`ttsjoin` branch (`if cmd[0] == "ttsjoin":`)
with the `fake_tts_render` fixture (keep the `ffprobe` branch). Add one test asserting the exact
config and ids passed, e.g. for FP:

```python
def test_fp_digest_renders_with_onyx(fake_tts_render, ...):
    ...  # existing setup that runs process_fp_digest_job
    [call] = fake_tts_render
    assert call["config"] == openai_config(model="tts-1-hd", voice="onyx")
    assert call["feed_slug"] == "fp-digest"
    assert call["episode_id"] == "<date>-fp-digest"
```

Equivalent assertions: Rundown → `nova`, `the-rundown`; processor → preset voice, and with
`monkeypatch.setenv("TTS_VOICE", "shimmer")` → `shimmer`; `publish_script` default → `nova`, and
explicit `voice="ash"` → `ash`; blog → `fable`, `aaronson`; dry-run → `manifest_dir is None and
cache_dir is None`.

**Step 2:** Run that test file → the new test FAILS (site still shells out to `ttsjoin`).

**Step 3:** Migrate the site.

**Step 4:** `uv run pytest <that test file> pipeline/test_processor_prelude.py pipeline/test_publish_script_cli.py -q` → PASS.

**Step 5:** Commit `refactor(<module>): render via pipeline.tts instead of ttsjoin`.

Test files to update (from `rg -n 'ttsjoin' pipeline/test_*.py`): `test_things_happen_processor.py`,
`test_fp_processor.py`, `test_blog_poller.py`, `test_script_processor.py`,
`test_publish_script_cli.py`, plus anything using `captured_tts_input`
(`test_processor_prelude.py`, `test_blog_poller.py`). After all six sites:
`rg -n "ttsjoin" pipeline/` must return only comments you intend to keep (ideally none).

---

### Task 9: Remove tts-joinery; docs

**Files:** `pyproject.toml`, `uv.lock`, `tts-joinery/README.md` (delete the directory), `AGENTS.md`,
`pipeline/title_prelude.py` (docstring), `.opencode/skills/operating-things-happen-digest/REFERENCE.md`,
`.opencode/skills/monitoring-my-podcasts-pipeline/REFERENCE.md`.

1. Remove `"tts-joinery>=1.0.4"` from `[dependency-groups].dev`. Check whether anything else needs
   `audioop-lts` (`rg -n "audioop|pydub" --glob '*.py' .` excluding `.venv`); it existed only for
   pydub on 3.14 — remove it if unused. `uv lock && uv sync`. Confirm
   `uv run python -c "import pydub"` now fails and `import openai` works.
2. `title_prelude.prepend_title` docstring: the terminating period still matters (an unterminated
   title runs into the first sentence's prosody), but the reason is no longer nltk — reword without
   naming `ttsjoin`.
3. `AGENTS.md`: line ~103 `runs TTS (\`ttsjoin\`)` → `runs TTS (\`pipeline/tts\`)`; add to "Core
   Paths": `- TTS renderer: \`pipeline/tts/\` (\`render_episode\`: chunk → OpenAI PCM → one mp3 encode;
  completed-render cache \`/persist/my-podcasts/tts-cache/\`, per-attempt manifests
  \`/persist/my-podcasts/tts-renders/<feed>/\`). Design: \`docs/plans/2026-09-30-gemini-tts-design.md\`.`
4. Skills: things-happen REFERENCE line 25 → describe `pipeline.tts.render_episode` (tts-1-hd,
   nova); monitoring REFERENCE line 23 NLTK check → note NLTK is no longer used by the pipeline once
   this is deployed (the workstation unit cleanup is bead `my-podcasts-9p3.9`).
5. Full gate: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` → all pass
   (700 + new tests).

Commit `chore: drop tts-joinery; document pipeline.tts`.

---

### Task 10: Real-render comparison (controller does this, not a subagent)

Paid (~$0.50 of OpenAI). Artifacts go under the worktree in `.tts-compare/` (add to
`.git/info/exclude`), **not `/tmp`**.

1. Inputs: newest `/tmp/the-rundown-*/script.txt` and `/tmp/fp-digest-*/script.txt` if still present,
   else `/persist/my-podcasts/scripts/` or an R2 email; a Levine body rebuilt from R2 email
   `inbox/raw/d9dead7b-c34e-4c82-b3cd-f079c7446a9f.eml` via `EmailProcessor(raw).parse()` +
   `get_source_adapter('levine').clean_body` + `prepend_title` (see `pipeline/processor.py:96-137`).
   Fetch published mp3s from `https://podcast.mohrbacher.dev/episodes/<feed>/<slug>.mp3`.
2. Render each with `OPENAI_API_KEY="$(sudo cat /run/secrets/openai_api_key)"` (never echo it),
   `manifest_dir=None, cache_dir=None`, same voices as production.
3. Compare to published: duration (expect within a few %), `ffprobe` codec/rate/channels/bitrate
   (must be mp3/24000/1/32 kbps). Cut 60 s clips around 2-3 chunk joins per file and tell the owner
   where they are to listen. Record numbers with `bd note my-podcasts-9p3.1`.

---

## After the plan

Adversarial review of the full diff (`adversarial-reviewer-astra`, fall back to `-opus`), then PR via
`shepherding-pull-requests`. PR body must say: not behavior-neutral (chunk boundaries, PCM concat),
the Task 10 numbers, deploy = consumer restart (owner), and that it closes `my-podcasts-4ld` and
`my-podcasts-4sc`. Close `my-podcasts-9p3.1` at **merge**.
