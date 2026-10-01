"""The paid, I/O half of the T5 calibration harness.

``pipeline.tts.calibrate`` is the pure core (no network, no files). This module
is everything that touches the world: R2, OpenAI whisper-1, Gemini synthesis and
ASR, ``render_episode``, the ledger and the artifact tree. Run it as::

    uv run python -m pipeline tts-calibrate [--root R] [--budget 15] <step> ...

Plan: ``docs/plans/2026-09-30-gemini-tts-t5-calibration-plan.md`` (Task 3).

Rules every step obeys:

- **Resumable, never overwrites.** An artifact that exists is skipped; writes are
  atomic (temp file, then a no-clobber ``os.link``). Frozen files (``corpus.json``,
  ``cuts.json``, ``cuts-verified.json``) refuse to be regenerated.
- **Budget.** Before every paid call the ledger total plus every in-flight call's
  worst case plus this call's worst case must fit ``--budget``, else
  ``BudgetError`` (exit 2, nothing sent). Every paid call is appended to
  ``ledger.jsonl``; usage the API did not report is costed at its worst case, not
  zero.
- **Keys from the environment**, only variable names are ever printed.
- **No hidden network.** All services come through ``Services``; tests pass fakes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from pipeline.tts import calibrate as cal
from pipeline.tts.asr import ASR_MODEL, TranscriptionUnavailable, pcm_to_wav
from pipeline.tts.chunker import chunk_text
from pipeline.tts.config import PCM_SAMPLE_RATE, GeminiConfig, RenderConfig
from pipeline.tts.gemini_phase import (
    BACKOFF_SECONDS,
    MAX_TTS_CALLS,
    REQUEST_TIMEOUT_CAP_SECONDS,
)
from pipeline.tts.providers import GeminiProvider, TTSProviderError
from pipeline.tts.verify import DEFAULT_THRESHOLDS, analyze


DEFAULT_ROOT = Path("/persist/my-podcasts/tts-eval/t5")
DEFAULT_BUDGET_USD = 15.0
DEFAULT_SCRIPTS_ROOT = Path("/persist/my-podcasts/scripts")
DEFAULT_REPRO_SCRIPT = Path("/persist/my-podcasts/tts-eval/t4/set/levine/script.txt")
STYLE = "calm, measured news anchor"
MODELS = {"flash": "gemini-3.8-flash-tts", "lite": "gemini-3.8-flash-lite-tts"}
VOICE_BY_PARITY = ("Kore", "Charon")  # even corpus index -> Kore, odd -> Charon
FEED_SLUGS = {"rundown": "the-rundown", "fp": "fp-digest", "levine": "levine"}
ASR_POLICIES = ("default", "low")
ASR_TIMEOUT_S = 90.0
WHISPER_MODEL = "whisper-1"
WHISPER_MAX_BYTES = 24 * 1024 * 1024  # the API limit is 25 MB
WHISPER_BACKOFF_SECONDS = (2.0, 8.0)
WHISPER_ATTEMPTS = 3
CUTS_PER_BASE = 1.5
DEFAULT_CUT_SEED = "t5"
CLIP_WINDOW_S = 20.0
CLIP_WINDOWS = 3
WHISPER_DURATION_SLACK = 1.0

# Planning prices (plan decision 12). USD.
SYNTH_USD_PER_M_AUDIO_TOKENS = {
    "gemini-3.8-flash-tts": 9.2,
    "gemini-3.8-flash-lite-tts": 6.1,
}
ASR_USD_PER_M_INPUT = 1.0
ASR_USD_PER_M_OUTPUT = 5.0  # output AND thinking tokens
WHISPER_USD_PER_MINUTE = 0.006
AUDIO_TOKENS_PER_SECOND = 32
# Worst cases, used before a call and whenever usage is unknown. Speech is about
# 15 characters per second; assuming 10 over-estimates the audio (and so the
# bill) by 50%.
WORST_CHARS_PER_SECOND = 10
ASR_WORST_OUTPUT_TOKENS = 8000  # output + thinking
ASR_PROMPT_TOKENS = 100


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class CalibrateError(Exception):
    """A step could not do what was asked. Exit status 1."""

    exit_code = 1


class Refused(CalibrateError):
    """Refused before doing anything (bad input, frozen file, budget). Exit 2."""

    exit_code = 2


class BudgetError(Refused):
    """The next paid call would pass ``--budget``. Nothing was sent."""


class ServiceError(Exception):
    """A non-Gemini service failed. ``kind`` is ``transient`` or ``fatal``."""

    def __init__(self, message: str, kind: str = "transient") -> None:
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------- #
# small file helpers
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def write_new(path: Path, data: bytes) -> bool:
    """Atomically create ``path`` with ``data``; ``False`` if it already exists.

    Temp file in the same directory, then ``os.link`` (fails if the target
    exists, unlike ``os.replace``), so a concurrent or repeated run can never
    clobber an artifact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def write_json_new(path: Path, obj: Any) -> bool:
    return write_new(
        path, (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode("utf-8")
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def move_aside(path: Path, label: str) -> Path:
    """Keep an old artifact as evidence (``name.<label>-<n>``) before a retry."""
    n = 1
    while True:
        target = path.with_name(f"{path.stem}.{label}-{n}{path.suffix}")
        if not target.exists():
            os.rename(path, target)
            return target
        n += 1


# --------------------------------------------------------------------------- #
# prices and the ledger
# --------------------------------------------------------------------------- #


def synth_price(model: str) -> float:
    """USD per million audio tokens; an unknown model is priced as the dearest."""
    return SYNTH_USD_PER_M_AUDIO_TOKENS.get(
        model, max(SYNTH_USD_PER_M_AUDIO_TOKENS.values())
    )


def est_synth_worst(chars: int, model: str) -> float:
    seconds = chars / WORST_CHARS_PER_SECOND
    return seconds * AUDIO_TOKENS_PER_SECOND / 1e6 * synth_price(model)


def est_synth(audio_tokens: int | None, chars: int, model: str) -> tuple[float, bool]:
    """``(usd, worst_case)``: unknown audio tokens are costed at the worst case."""
    if audio_tokens is None:
        return est_synth_worst(chars, model), True
    return audio_tokens / 1e6 * synth_price(model), False


def est_asr_worst(duration_s: float) -> float:
    tokens_in = duration_s * AUDIO_TOKENS_PER_SECOND + ASR_PROMPT_TOKENS
    return (
        tokens_in / 1e6 * ASR_USD_PER_M_INPUT
        + ASR_WORST_OUTPUT_TOKENS / 1e6 * ASR_USD_PER_M_OUTPUT
    )


def est_asr(
    input_tokens: int | None,
    output_tokens: int | None,
    thinking_tokens: int | None,
    duration_s: float,
) -> tuple[float, bool]:
    """A completed call that reports no thinking count counted 0 (the model said
    none); input or output unknown is the worst case."""
    if input_tokens is None or output_tokens is None:
        return est_asr_worst(duration_s), True
    return (
        input_tokens / 1e6 * ASR_USD_PER_M_INPUT
        + (output_tokens + (thinking_tokens or 0)) / 1e6 * ASR_USD_PER_M_OUTPUT,
        False,
    )


def est_whisper(duration_s: float) -> float:
    return duration_s / 60.0 * WHISPER_USD_PER_MINUTE


def est_deadline_worst(chars: int, model: str) -> float:
    """Worst case of a whole ``render_episode``: every chunk synthesized
    ``MAX_TTS_CALLS`` times and verified every time."""
    chunks = chunk_text("x" * max(chars, 1), ceiling=GeminiProvider.max_chars)
    per_chunk_s = GeminiProvider.max_chars / WORST_CHARS_PER_SECOND
    total = est_synth_worst(chars, model) * MAX_TTS_CALLS
    total += len(chunks) * MAX_TTS_CALLS * est_asr_worst(per_chunk_s)
    return total


class Ledger:
    """Append-only ``ledger.jsonl`` plus the budget gate."""

    def __init__(self, path: Path, budget: float) -> None:
        self.path = path
        self.budget = budget
        self._lock = threading.Lock()
        self._reserved = 0.0

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                # Refuse to guess at spend: a corrupt ledger cannot gate a budget.
                raise CalibrateError(
                    f"{self.path}: line {i + 1} is not JSON ({exc}); fix it by hand"
                ) from exc
        return out

    def total(self) -> float:
        return sum(float(e.get("est_usd", 0.0)) for e in self.entries())

    def append(
        self,
        kind: str,
        model: str,
        ids: dict,
        usage: dict | None,
        est_usd: float,
        *,
        worst_case: bool = False,
        note: str | None = None,
    ) -> None:
        entry = {
            "ts": now_iso(),
            "kind": kind,
            "model": model,
            "ids": ids,
            "usage": usage,
            "est_usd": est_usd,
            "worst_case": worst_case,
        }
        if note:
            entry["note"] = note
        line = json.dumps(entry, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    @contextlib.contextmanager
    def reserve(self, worst_usd: float, what: str) -> Iterator[None]:
        """Hold ``worst_usd`` while a paid call is in flight.

        Raises ``BudgetError`` (before anything is sent) when the recorded spend,
        every other call's reservation and this call's worst case would pass the
        budget. The caller appends the real ledger entry before leaving the block.
        """
        with self._lock:
            spent = self.total()
            projected = spent + self._reserved + worst_usd
            if projected > self.budget + 1e-12:
                raise BudgetError(
                    f"budget: ${spent:.4f} spent + ${self._reserved:.4f} in flight "
                    f"+ ${worst_usd:.4f} worst case for {what} would pass "
                    f"--budget ${self.budget:.2f}; nothing was sent"
                )
            self._reserved += worst_usd
        try:
            yield
        finally:
            with self._lock:
                self._reserved -= worst_usd

    def summary(self) -> dict:
        by_kind: dict[str, dict] = {}
        for e in self.entries():
            row = by_kind.setdefault(
                e["kind"], {"calls": 0, "est_usd": 0.0, "worst_case_calls": 0}
            )
            row["calls"] += 1
            row["est_usd"] += float(e.get("est_usd", 0.0))
            row["worst_case_calls"] += 1 if e.get("worst_case") else 0
        total = sum(r["est_usd"] for r in by_kind.values())
        return {
            "total_usd": total,
            "budget_usd": self.budget,
            "remaining_usd": self.budget - total,
            "by_kind": by_kind,
        }


# --------------------------------------------------------------------------- #
# services and context
# --------------------------------------------------------------------------- #


@dataclass
class Services:
    """Everything that reaches outside the process, injectable for tests."""

    provider: Callable[[], Any]  # GeminiProvider-like: synthesize_detailed(...)
    whisper: Callable[[bytes, str], dict]  # (wav, filename) -> verbose_json dict
    transcriber: Callable[[str, float], Any]  # (thinking, timeout_s) -> transcriber
    r2_get: Callable[[str], bytes]
    render: Callable[..., Any]  # render_episode
    make_clip: Callable[[bytes, Path], None]  # raw PCM -> mp3 file
    sleep: Callable[[float], None] = time.sleep
    # True for the real services: steps then check the API keys are in the
    # environment (names only are ever printed) before the first paid call.
    check_env: bool = False


def _openai_whisper(wav: bytes, filename: str) -> dict:
    import openai

    from pipeline.tts import providers

    client = providers._make_openai_client(timeout=180.0)
    try:
        resp = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=(filename, wav, "audio/wav"),
            response_format="verbose_json",
            timestamp_granularities=["word"],
            language="en",
        )
    except openai.APIStatusError as exc:
        kind = "transient" if providers._status_is_retryable(exc) else "fatal"
        raise ServiceError(f"whisper HTTP {exc.status_code}", kind) from None
    except openai.APIError as exc:
        raise ServiceError(f"whisper {type(exc).__name__}", "transient") from None
    finally:
        client.close()
    return json.loads(resp.model_dump_json())


def default_services() -> Services:
    """The real services. Constructing this opens no connection."""
    from pipeline.r2 import R2Client
    from pipeline.tts.asr import GeminiTranscriber
    from pipeline.tts.encode import encode_mp3
    from pipeline.tts.render import render_episode

    r2: list[R2Client] = []

    def r2_get(key: str) -> bytes:
        if not r2:
            r2.append(R2Client())
        return r2[0].get_object_bytes(key)

    return Services(
        provider=GeminiProvider,
        whisper=_openai_whisper,
        transcriber=lambda thinking, timeout: GeminiTranscriber(
            thinking=thinking, timeout_s=timeout
        ),
        r2_get=r2_get,
        render=render_episode,
        make_clip=lambda pcm, out: encode_mp3(pcm, out),
        check_env=True,
    )


@dataclass
class Ctx:
    root: Path
    budget: float
    services: Services
    echo: Callable[[str], None] = click.echo
    ledger: Ledger = field(init=False)

    def __post_init__(self) -> None:
        self.ledger = Ledger(self.root / "ledger.jsonl", self.budget)

    # --- layout -----------------------------------------------------------
    def base_dir(self, base_id: str) -> Path:
        return self.root / "bases" / base_id

    def cut_dir(self, cut_id: str) -> Path:
        return self.root / "cuts" / cut_id

    @property
    def corpus_path(self) -> Path:
        return self.root / "corpus.json"

    @property
    def cuts_path(self) -> Path:
        return self.root / "cuts.json"

    @property
    def verified_path(self) -> Path:
        return self.root / "cuts-verified.json"

    @property
    def owner_path(self) -> Path:
        return self.root / "owner-calls.json"

    @property
    def repro_dir(self) -> Path:
        return self.root / "levine-repro"

    def need_env(self, *names: str) -> None:
        if not self.services.check_env:
            return
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise Refused(f"missing environment variable(s): {', '.join(missing)}")

    def corpus(self) -> dict:
        if not self.corpus_path.exists():
            raise Refused(f"{self.corpus_path} does not exist; run `corpus` first")
        return read_json(self.corpus_path)

    def cuts(self) -> dict:
        if not self.cuts_path.exists():
            raise Refused(f"{self.cuts_path} does not exist; run `cuts` first")
        return read_json(self.cuts_path)


def run_parallel(
    items: Sequence[Any], fn: Callable[[Any], Any], workers: int
) -> list[Any]:
    """Run ``fn`` over ``items``; completed work is never lost.

    A ``BudgetError`` cancels the queued items and is re-raised after the
    in-flight ones finish; any other exception is re-raised the same way.
    """
    results: list[Any] = []
    budget_error: BudgetError | None = None
    other: Exception | None = None
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(fn, it) for it in items]
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except BudgetError as exc:
                budget_error = budget_error or exc
                for g in futures:
                    g.cancel()
            except Exception as exc:  # noqa: BLE001
                other = other or exc
                for g in futures:
                    g.cancel()
    if budget_error is not None:
        raise budget_error
    if other is not None:
        raise other
    return results


# --------------------------------------------------------------------------- #
# corpus (decision 1)
# --------------------------------------------------------------------------- #


def levine_tts_text(raw_email: bytes) -> dict:
    """The exact TTS input the email path builds for a Levine email.

    Same calls, same order as ``pipeline.processor.process_email_bytes``:
    ``EmailProcessor.parse`` -> adapter ``format_title`` -> adapter ``clean_body``
    -> ``maybe_rewrite_transcript`` (a no-op for Levine) -> ``prepend_title``.
    ``test_calibrate_run`` pins this against ``process_email_bytes`` itself.
    """
    from email_processor.api import EmailProcessor
    from pipeline.presets import resolve_preset
    from pipeline.source_adapters import get_source_adapter
    from pipeline.title_prelude import prepend_title
    from pipeline.transcript_report import maybe_rewrite_transcript

    parsed = EmailProcessor(raw_email).parse()
    date_str = parsed["date"]
    subject_slug = parsed["subject"]
    subject_raw = parsed.get("subject_raw", "")
    preset = resolve_preset("levine")
    adapter = get_source_adapter(preset.feed_slug)
    title = adapter.format_title(
        date_str=date_str, subject_raw=subject_raw, subject_slug=subject_slug
    )
    body = adapter.clean_body(raw_email=raw_email, body=parsed["body"])
    body, title = maybe_rewrite_transcript(
        body=body, title=title, feed_slug=preset.feed_slug, subject_raw=subject_raw
    )
    return {
        "text": prepend_title(title, body),
        "date": date_str,
        "title": title,
        "slug": f"{date_str}-{subject_slug}",
        "feed_slug": preset.feed_slug,
    }


def lookup_published(state_db: Path, feed_slug: str, slug: str) -> str | None:
    """The published mp3 key from the episodes table, read-only; ``None`` if the
    database, the table or the row is missing."""
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT r2_key FROM episodes WHERE feed_slug = ? AND slug = ?",
            (feed_slug, slug),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _default_state_db() -> Path:
    return Path(os.getenv("MY_PODCASTS_STATE_DB", "/persist/my-podcasts/state.sqlite3"))


def select_chunks(n: int) -> list[int]:
    """Decision 1: chunk 0 and chunk ``n // 2`` (one chunk if there is one)."""
    return sorted({0, n // 2}) if n > 0 else []


def voice_for(corpus_index: int) -> str:
    return VOICE_BY_PARITY[corpus_index % 2]


def base_id_for(episode: str, chunk_index: int, model_short: str, voice: str) -> str:
    return f"{episode}--c{chunk_index}--{model_short}--{voice}"


def build_corpus(
    ctx: Ctx,
    *,
    rundown: Sequence[str],
    fp: Sequence[str],
    levine_keys: Sequence[str],
    dev: Sequence[str],
    scripts_root: Path = DEFAULT_SCRIPTS_ROOT,
    state_db: Path | None = None,
) -> dict:
    """Freeze ``corpus.json`` and ``texts/``. Validates everything first, so a
    refusal leaves no file behind."""
    if ctx.corpus_path.exists():
        raise Refused(f"{ctx.corpus_path} already exists; the corpus is frozen")
    for name, given in (
        ("--rundown", rundown),
        ("--fp", fp),
        ("--levine-key", levine_keys),
    ):
        if len(given) != 4 or len(set(given)) != 4:
            raise Refused(f"need exactly 4 distinct {name} values, got {list(given)}")
    if len(dev) != 6 or len(set(dev)) != 6:
        raise Refused(
            "the split must be fixed explicitly: give exactly 6 distinct --dev "
            f"episode ids (2 per feed), got {list(dev)}"
        )
    state_db = state_db or _default_state_db()

    episodes: list[dict] = []
    texts: dict[str, str] = {}
    for date in rundown:
        text = (scripts_root / "the-rundown" / f"{date}.txt").read_text(
            encoding="utf-8"
        )
        eid = f"rundown-{date}"
        slug = f"{date}-the-rundown"
        episodes.append(
            _episode(
                eid,
                "rundown",
                text,
                state_db,
                "the-rundown",
                slug,
                {"archive": f"the-rundown/{date}.txt", "date": date},
            )
        )
        texts[eid] = text
    for date in fp:
        # FP's processor strips the archived script before TTS.
        text = (
            (scripts_root / "fp-digest" / f"{date}.txt")
            .read_text(encoding="utf-8")
            .strip()
        )
        eid = f"fp-{date}"
        slug = f"{date}-fp-digest"
        episodes.append(
            _episode(
                eid,
                "fp",
                text,
                state_db,
                "fp-digest",
                slug,
                {"archive": f"fp-digest/{date}.txt", "date": date},
            )
        )
        texts[eid] = text
    ctx.need_env("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    for key in levine_keys:
        info = levine_tts_text(ctx.services.r2_get(key))
        eid = f"levine-{info['date']}"
        episodes.append(
            _episode(
                eid,
                "levine",
                info["text"],
                state_db,
                "levine",
                info["slug"],
                {"r2_email_key": key, "title": info["title"], "date": info["date"]},
                derived_key=f"episodes/levine/{info['slug']}.mp3",
            )
        )
        texts[eid] = info["text"]

    ids = [e["id"] for e in episodes]
    if len(set(ids)) != len(ids):
        raise Refused(
            f"duplicate episode ids: {sorted(i for i in ids if ids.count(i) > 1)}"
        )
    unknown = [d for d in dev if d not in ids]
    if unknown:
        raise Refused(f"--dev ids not among the 12 episodes: {unknown}; have {ids}")
    for feed in FEED_SLUGS:
        n = sum(1 for d in dev if d.startswith(f"{feed}-"))
        if n != 2:
            raise Refused(f"the dev split needs 2 episodes per feed; {feed} has {n}")
    bases = []
    for index, ep in enumerate(episodes):
        ep["index"] = index
        ep["split"] = "dev" if ep["id"] in dev else "holdout"
        ep["voice"] = voice_for(index)
        chunks = chunk_text(texts[ep["id"]], ceiling=GeminiProvider.max_chars)
        ep["n_chunks"] = len(chunks)
        ep["chunks"] = [
            {"index": i, "chars": len(c), "sha256": sha256_hex(c)}
            for i, c in enumerate(chunks)
        ]
        ep["selected_chunks"] = select_chunks(len(chunks))
        for ci in ep["selected_chunks"]:
            for short, model in MODELS.items():
                bases.append(
                    {
                        "base_id": base_id_for(ep["id"], ci, short, ep["voice"]),
                        "episode": ep["id"],
                        "feed": ep["feed"],
                        "split": ep["split"],
                        "chunk_index": ci,
                        "model_short": short,
                        "model": model,
                        "voice": ep["voice"],
                        "chars": len(chunks[ci]),
                        "chunk_sha256": sha256_hex(chunks[ci]),
                    }
                )
    corpus = {
        "version": 1,
        "created": now_iso(),
        "style": STYLE,
        "chunk_ceiling": GeminiProvider.max_chars,
        "episodes": episodes,
        "bases": bases,
    }
    for ep in episodes:
        path = ctx.root / "texts" / f"{ep['id']}.txt"
        if not write_new(path, texts[ep["id"]].encode("utf-8")):
            if path.read_text(encoding="utf-8") != texts[ep["id"]]:
                raise Refused(f"{path} exists with different text; not overwriting")
    if not write_json_new(ctx.corpus_path, corpus):
        raise Refused(f"{ctx.corpus_path} appeared while running; not overwriting")
    return corpus


def _episode(
    eid: str,
    feed: str,
    text: str,
    state_db: Path,
    feed_slug: str,
    slug: str,
    source: dict,
    derived_key: str | None = None,
) -> dict:
    key = lookup_published(state_db, feed_slug, slug)
    published = {
        "r2_key": key or derived_key or f"episodes/{feed_slug}/{slug}.mp3",
        "source": "episodes_table" if key else "derived",
    }
    return {
        "id": eid,
        "feed": feed,
        "feed_slug": feed_slug,
        "source": source,
        "text_path": f"texts/{eid}.txt",
        "text_sha256": sha256_hex(text),
        "chars": len(text),
        "published": published,
    }


# --------------------------------------------------------------------------- #
# corpus lookups
# --------------------------------------------------------------------------- #


def base_meta(ctx: Ctx) -> dict[str, dict]:
    return {b["base_id"]: b for b in ctx.corpus()["bases"]}


def chunk_for(ctx: Ctx, base: dict) -> str:
    text = (ctx.root / "texts" / f"{base['episode']}.txt").read_text(encoding="utf-8")
    chunks = chunk_text(text, ceiling=GeminiProvider.max_chars)
    chunk = chunks[base["chunk_index"]]
    if sha256_hex(chunk) != base["chunk_sha256"]:
        raise CalibrateError(
            f"{base['base_id']}: chunking no longer reproduces the corpus "
            "(chunker or text changed); do not mix runs"
        )
    return chunk


def owner_calls(ctx: Ctx) -> dict[str, dict]:
    return read_json(ctx.owner_path) if ctx.owner_path.exists() else {}


OWNER_CALLS = ("faithful", "natural_omission", "defect", "uncertain")


def record_owner_call(ctx: Ctx, base_id: str, call: str, note: str = "") -> None:
    if call not in OWNER_CALLS:
        raise Refused(f"call must be one of {OWNER_CALLS}")
    if base_id not in base_meta(ctx):
        raise Refused(f"unknown base id {base_id!r}")
    calls = owner_calls(ctx)
    existing = calls.get(base_id)
    if existing and existing["call"] != call:
        raise Refused(
            f"{base_id} already has the owner call {existing['call']!r}; "
            "calls are never overwritten"
        )
    if existing:
        return
    calls[base_id] = {"call": call, "note": note, "recorded": now_iso()}
    # the one file here that grows: rewrite atomically under a lock-free replace
    tmp = ctx.owner_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(calls, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, ctx.owner_path)


def effective_label(ctx: Ctx, base_id: str) -> cal.BaseLabel | None:
    """The base's label with the owner's call applied; ``None`` before ``label``."""
    path = ctx.base_dir(base_id) / "label.json"
    if not path.exists():
        return None
    label = cal.BaseLabel.from_dict(read_json(path))
    call = owner_calls(ctx).get(base_id, {}).get("call")
    if call == "faithful" and label.label != "faithful":
        return replace(label, label="faithful", reasons=())
    if call in ("natural_omission", "defect", "uncertain"):
        return replace(
            label, label="suspect", reasons=(*label.reasons, f"owner:{call}")
        )
    return label


def wav_info(wav: bytes) -> tuple[bytes, int, float]:
    pcm, rate = cal.read_wav(wav)
    return pcm, rate, len(pcm) / 2 / rate


# --------------------------------------------------------------------------- #
# synth
# --------------------------------------------------------------------------- #


def synth_one(
    ctx: Ctx,
    *,
    item_id: str,
    text: str,
    model: str,
    voice: str,
    out_dir: Path,
    retry_failed: bool = False,
) -> str:
    """Synthesize one chunk into ``out_dir`` (``pcm.wav`` then ``synth.json``).

    Production retry classification: a transient error is retried up to
    ``MAX_TTS_CALLS`` calls with backoff 2 s then 8 s; a fatal error is recorded
    and the step carries on. Returns ``skipped``, ``ok`` or ``failed``.
    """
    synth_path = out_dir / "synth.json"
    if synth_path.exists():
        if not (retry_failed and read_json(synth_path).get("status") == "failed"):
            return "skipped"
        move_aside(synth_path, "failed")
    cfg = GeminiConfig(model=model, voice=voice, style=STYLE)
    provider = ctx.services.provider()
    attempts: list[dict] = []
    cost = 0.0
    result = None
    kept_new_audio = True
    for n in range(1, MAX_TTS_CALLS + 1):
        if n > 1:
            ctx.services.sleep(BACKOFF_SECONDS[n - 2])
        worst = est_synth_worst(len(text), model)
        ids = {"id": item_id, "attempt": n}
        with ctx.ledger.reserve(worst, f"synth {item_id}"):
            try:
                result = provider.synthesize_detailed(
                    text, cfg, timeout=REQUEST_TIMEOUT_CAP_SECONDS
                )
            except TTSProviderError as exc:
                attempts.append(
                    {"n": n, "status": "error", "kind": exc.kind, "error": str(exc)}
                )
                cost += worst
                ctx.ledger.append(
                    "synth",
                    model,
                    ids,
                    None,
                    worst,
                    worst_case=True,
                    note=f"error:{exc.kind}",
                )
                if exc.kind == "fatal":
                    break
                continue
            # PCM first, before the ledger line or anything that could fail.
            pcm = result.pcm
            kept_new_audio = write_new(out_dir / "pcm.wav", pcm_to_wav(pcm))
            usd, worst_case = est_synth(result.audio_tokens, len(text), model)
            cost += usd
            attempts.append(
                {
                    "n": n,
                    "status": "ok",
                    "finish_reason": result.finish_reason,
                    "prompt_tokens": result.prompt_tokens,
                    "audio_tokens": result.audio_tokens,
                    "elapsed_s": result.elapsed_s,
                }
            )
            ctx.ledger.append(
                "synth",
                model,
                ids,
                {
                    "prompt_tokens": result.prompt_tokens,
                    "audio_tokens": result.audio_tokens,
                },
                usd,
                worst_case=worst_case,
            )
            break
        # (a transient error falls through to the next attempt)
    ok = bool(attempts) and attempts[-1]["status"] == "ok" and result is not None
    record = {
        "schema": 1,
        "id": item_id,
        "status": "ok" if ok else "failed",
        "model": model,
        "voice": voice,
        "style": STYLE,
        "chars": len(text),
        "chunk_sha256": sha256_hex(text),
        "attempts": attempts,
        "est_usd": cost,
        "created": now_iso(),
    }
    if ok:
        assert result is not None
        record["usage"] = attempts[-1]
        pcm_samples = len(result.pcm) // 2
        if not kept_new_audio:
            # A crash left pcm.wav without synth.json; the paid-for audio is
            # dropped rather than overwriting it. Describe what is on disk.
            pcm_on_disk, _, _ = wav_info((out_dir / "pcm.wav").read_bytes())
            pcm_samples = len(pcm_on_disk) // 2
            record["note"] = (
                "pcm.wav already existed; the new audio was not saved "
                "(artifacts are never overwritten)"
            )
        record["pcm_samples"] = pcm_samples
        record["duration_s"] = pcm_samples / PCM_SAMPLE_RATE
    else:
        record["error"] = attempts[-1]["error"] if attempts else "no attempt made"
    write_json_new(synth_path, record)
    return str(record["status"])


def step_synth(
    ctx: Ctx,
    *,
    workers: int = 4,
    split: str = "all",
    ids: Sequence[str] = (),
    retry_failed: bool = False,
) -> dict:
    ctx.need_env("GEMINI_API_KEY")
    bases = [
        b
        for b in ctx.corpus()["bases"]
        if (split == "all" or b["split"] == split) and (not ids or b["base_id"] in ids)
    ]

    def one(b: dict) -> tuple[str, str]:
        text = chunk_for(ctx, b)
        out = ctx.base_dir(b["base_id"])
        write_new(out / "chunk.txt", text.encode("utf-8"))
        return b["base_id"], synth_one(
            ctx,
            item_id=b["base_id"],
            text=text,
            model=b["model"],
            voice=b["voice"],
            out_dir=out,
            retry_failed=retry_failed,
        )

    results = run_parallel(bases, one, workers)
    counts = _count(r[1] for r in results)
    ctx.echo(f"synth: {len(bases)} bases: {_fmt(counts)}")
    for base_id, status in sorted(results):
        if status == "failed":
            ctx.echo(f"  FAILED {base_id}: see bases/{base_id}/synth.json")
    return counts


def _count(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it] = out.get(it, 0) + 1
    return out


def _fmt(counts: dict[str, int]) -> str:
    return ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing to do"


# --------------------------------------------------------------------------- #
# targets shared by whisper and asr
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Target:
    kind: str  # base | cut | removed | repro
    id: str
    audio: Path
    dir: Path
    split: str
    out_name: str = "whisper.json"  # whisper's output file name


def _synth_ok(directory: Path) -> bool:
    path = directory / "synth.json"
    return path.exists() and read_json(path).get("status") == "ok"


def repro_attempts(ctx: Ctx) -> list[Path]:
    if not ctx.repro_dir.exists():
        return []
    return sorted(
        p for p in ctx.repro_dir.iterdir() if p.is_dir() and (p / "synth.json").exists()
    )


def iter_targets(
    ctx: Ctx, kinds: Sequence[str], split: str, ids: Sequence[str]
) -> list[Target]:
    out: list[Target] = []

    def keep(item_id: str, item_split: str) -> bool:
        return (split == "all" or item_split == split) and (not ids or item_id in ids)

    if "base" in kinds:
        for b in ctx.corpus()["bases"]:
            d = ctx.base_dir(b["base_id"])
            if keep(b["base_id"], b["split"]) and _synth_ok(d):
                out.append(Target("base", b["base_id"], d / "pcm.wav", d, b["split"]))
    if "cut" in kinds or "removed" in kinds:
        for c in ctx.cuts()["cuts"]:
            d = ctx.cut_dir(c["cut_id"])
            if not keep(c["cut_id"], c["split"]):
                continue
            if "cut" in kinds:
                out.append(Target("cut", c["cut_id"], d / "cut.wav", d, c["split"]))
            if "removed" in kinds:
                for k in range(c["n_intervals"]):
                    out.append(
                        Target(
                            "removed",
                            f"{c['cut_id']}#{k}",
                            d / f"removed-{k}.wav",
                            d,
                            c["split"],
                            f"whisper-removed-{k}.json",
                        )
                    )
    if "repro" in kinds:
        for d in repro_attempts(ctx):
            if _synth_ok(d) and (not ids or d.name in ids):
                out.append(Target("repro", d.name, d / "pcm.wav", d, "dev"))
    return out


# --------------------------------------------------------------------------- #
# whisper
# --------------------------------------------------------------------------- #


def whisper_one(ctx: Ctx, t: Target) -> str:
    out = t.dir / t.out_name
    if out.exists():
        return "skipped"
    wav = t.audio.read_bytes()
    if len(wav) > WHISPER_MAX_BYTES:
        raise CalibrateError(
            f"{t.audio}: {len(wav)} bytes is over the {WHISPER_MAX_BYTES}-byte "
            "whisper limit; not sent"
        )
    _, _, duration = wav_info(wav)
    last: ServiceError | None = None
    for n in range(WHISPER_ATTEMPTS):
        if n:
            ctx.services.sleep(WHISPER_BACKOFF_SECONDS[n - 1])
        worst = est_whisper(duration)
        with ctx.ledger.reserve(worst, f"whisper {t.id}"):
            started = time.monotonic()
            try:
                resp = ctx.services.whisper(wav, f"{t.kind}.wav")
            except ServiceError as exc:
                last = exc
                if exc.kind == "fatal":
                    raise CalibrateError(f"whisper {t.id}: {exc}") from exc
                continue
            seconds = float((resp.get("usage") or {}).get("seconds") or duration)
            ctx.ledger.append(
                "whisper",
                WHISPER_MODEL,
                {"id": t.id, "kind": t.kind},
                {"seconds": seconds},
                est_whisper(seconds),
            )
            resp["_calibrate"] = {
                "model": WHISPER_MODEL,
                "kind": t.kind,
                "id": t.id,
                "audio_sha256": sha256_hex(wav),
                "audio_seconds": duration,
                "elapsed_s": time.monotonic() - started,
                "created": now_iso(),
            }
            write_json_new(out, resp)
            return "ok"
    raise CalibrateError(
        f"whisper {t.id}: gave up after {WHISPER_ATTEMPTS} attempts: {last}"
    )


def step_whisper(
    ctx: Ctx,
    *,
    kinds: Sequence[str] = ("base",),
    split: str = "all",
    ids: Sequence[str] = (),
    workers: int = 4,
) -> dict:
    ctx.need_env("OPENAI_API_KEY")
    targets = iter_targets(ctx, kinds, split, ids)
    failures: list[str] = []

    def one(t: Target) -> str:
        try:
            return whisper_one(ctx, t)
        except BudgetError:
            raise
        except CalibrateError as exc:
            failures.append(str(exc))
            return "failed"

    counts = _count(run_parallel(targets, one, workers))
    ctx.echo(f"whisper: {len(targets)} audio files: {_fmt(counts)}")
    for f in failures:
        ctx.echo(f"  FAILED {f}")
    return counts


# --------------------------------------------------------------------------- #
# label (decision 2)
# --------------------------------------------------------------------------- #


def lowest_recall_windows(
    label: cal.BaseLabel, window_s: float = CLIP_WINDOW_S, k: int = CLIP_WINDOWS
) -> list[tuple[float, float, float]]:
    """The ``k`` lowest-recall ``window_s`` windows, non-overlapping, as
    ``(start_s, end_s, recall)``. Token times come from the word map, with
    unmatched tokens interpolated between their matched neighbours."""
    n = len(label.word_map)
    duration = label.total_samples / label.sample_rate
    if n == 0 or duration <= 0:
        return []
    times: list[float | None] = [wm.start for wm in label.word_map]
    known = [i for i, t in enumerate(times) if t is not None]
    est: list[float] = []
    for i in range(n):
        t = times[i]
        if t is not None:
            est.append(t)
            continue
        prev = max((j for j in known if j < i), default=None)
        nxt = min((j for j in known if j > i), default=None)
        if prev is not None and nxt is not None:
            a, b = times[prev], times[nxt]
            assert a is not None and b is not None
            est.append(a + (b - a) * (i - prev) / (nxt - prev))
        elif prev is not None:
            est.append(times[prev] or 0.0)
        elif nxt is not None:
            est.append(times[nxt] or 0.0)
        else:
            est.append(duration * i / n)
    matched = [wm.word_index is not None for wm in label.word_map]
    scored = []
    step = window_s / 2
    start = 0.0
    while start < max(duration - step, 1e-9):
        end = min(start + window_s, duration)
        idx = [i for i in range(n) if start <= est[i] < end]
        if len(idx) >= 10:
            scored.append((sum(matched[i] for i in idx) / len(idx), start, end))
        start += step
    chosen: list[tuple[float, float, float]] = []
    for recall, s, e in sorted(scored):
        if all(e <= cs or s >= ce for cs, ce, _ in chosen):
            chosen.append((s, e, recall))
        if len(chosen) == k:
            break
    return chosen


def clip_path(ctx: Ctx, name: str) -> Path:
    return ctx.root / "clips" / f"{name}.mp3"


def write_clip(ctx: Ctx, pcm: bytes, start_s: float, end_s: float, out: Path) -> bool:
    if out.exists():
        return False
    a = max(0, round(start_s * PCM_SAMPLE_RATE))
    b = min(len(pcm) // 2, round(end_s * PCM_SAMPLE_RATE))
    if b <= a:
        raise CalibrateError(
            f"empty clip window {start_s:.2f}-{end_s:.2f}s for {out.name}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    ctx.services.make_clip(pcm[2 * a : 2 * b], out)
    return True


def step_label(ctx: Ctx, *, split: str = "all", ids: Sequence[str] = ()) -> list[dict]:
    rows = []
    for b in ctx.corpus()["bases"]:
        if (split != "all" and b["split"] != split) or (
            ids and b["base_id"] not in ids
        ):
            continue
        d = ctx.base_dir(b["base_id"])
        if not _synth_ok(d) or not (d / "whisper.json").exists():
            continue
        label_path = d / "label.json"
        pcm, _, _ = wav_info((d / "pcm.wav").read_bytes())
        if label_path.exists():
            label = cal.BaseLabel.from_dict(read_json(label_path))
        else:
            try:
                label = cal.screen_base(
                    (d / "chunk.txt").read_text(encoding="utf-8"),
                    read_json(d / "whisper.json"),
                    total_samples=len(pcm) // 2,
                    base_id=b["base_id"],
                )
            except ValueError as exc:
                rows.append(
                    {"base_id": b["base_id"], "label": "error", "error": str(exc)}
                )
                continue
            write_json_new(label_path, label.to_dict())
        clips: list[str] = []
        if label.label == "suspect":
            windows = [
                (s.clip_start_s, s.clip_end_s, f"span{k}")
                for k, s in enumerate(label.suspect_spans)
            ] or [
                (s, e, f"low{k}")
                for k, (s, e, _) in enumerate(lowest_recall_windows(label))
            ]
            for s, e, tag in windows:
                out = clip_path(ctx, f"{b['base_id']}--{tag}")
                write_clip(ctx, pcm, s, e, out)
                clips.append(str(out))
        rows.append(
            {
                "base_id": b["base_id"],
                "label": label.label,
                "recall": label.recall,
                "reasons": list(label.reasons),
                "spans": len(label.suspect_spans),
                "clips": clips,
            }
        )
    ctx.echo(f"{'base_id':<56} {'label':<9} {'recall':>6}  reasons / clips")
    for r in rows:
        if r["label"] == "error":
            ctx.echo(f"{r['base_id']:<56} {'ERROR':<9} {'':>6}  {r['error']}")
            continue
        recall = f"{r['recall']:.3f}" if r["recall"] is not None else "-"
        ctx.echo(
            f"{r['base_id']:<56} {r['label']:<9} {recall:>6}  "
            f"{','.join(r['reasons']) or '-'}"
        )
        for c in r["clips"]:
            ctx.echo(f"{'':<56} {'':<9} {'':>6}    clip: {c}")
    n_suspect = sum(1 for r in rows if r["label"] == "suspect")
    ctx.echo(
        f"label: {len(rows)} bases, {n_suspect} suspect "
        f"(owner clips in {ctx.root / 'clips'})"
    )
    return rows


def step_clips(
    ctx: Ctx, *, item_id: str, start_s: float, end_s: float, name: str | None
) -> Path:
    """An mp3 of ``[start_s, end_s]`` of a base or cut, for the owner."""
    if item_id in base_meta(ctx):
        wav = ctx.base_dir(item_id) / "pcm.wav"
    else:
        wav = ctx.cut_dir(item_id) / "cut.wav"
    if not wav.exists():
        raise Refused(f"no audio for {item_id!r}")
    pcm, _, _ = wav_info(wav.read_bytes())
    out = clip_path(ctx, name or f"{item_id}--{start_s:.0f}-{end_s:.0f}")
    if not write_clip(ctx, pcm, start_s, end_s, out):
        ctx.echo(f"clips: {out} already exists; not overwriting")
    else:
        ctx.echo(f"clips: wrote {out}")
    return out


# --------------------------------------------------------------------------- #
# cuts (decision 3)
# --------------------------------------------------------------------------- #

COMBOS: tuple[tuple[str, int], ...] = (
    *(
        (f, s)
        for f in ("start", "end", "mid_fluent", "sentence", "predictable")
        for s in cal.SIZE_BINS
    ),
    ("paragraph", 80),
    ("multi", cal.MULTI_TOTAL),
)


def plan_slots(n_cuts: int, seed: str, split: str) -> list[tuple[str, int]]:
    """``n_cuts`` (family, size) slots: every combination once per cycle, the
    order of each cycle shuffled by the seed."""
    rng = random.Random(f"{seed}|slots|{split}")
    slots: list[tuple[str, int]] = []
    while len(slots) < n_cuts:
        cycle = list(COMBOS)
        rng.shuffle(cycle)
        slots.extend(cycle)
    return slots[:n_cuts]


def step_cuts(ctx: Ctx, *, seed: str = DEFAULT_CUT_SEED) -> dict:
    """Choose, cut, snap and freeze the cuts (``cuts.json`` is written last and
    never changed). Deterministic given the seed and the labels."""
    if ctx.cuts_path.exists():
        raise Refused(f"{ctx.cuts_path} already exists; the cuts are frozen")
    meta = base_meta(ctx)
    faithful: dict[str, cal.BaseLabel] = {}
    for base_id in sorted(meta):
        label = effective_label(ctx, base_id)
        if label is not None and label.label == "faithful":
            faithful[base_id] = label
    if not faithful:
        raise Refused("no faithful bases yet; run synth, whisper and label first")

    entries: list[dict] = []
    skipped: list[dict] = []
    used: set[str] = set()
    for split in ("dev", "holdout"):
        ids = [b for b in faithful if meta[b]["split"] == split]
        if not ids:
            continue
        rng = random.Random(f"{seed}|bases|{split}")
        rng.shuffle(ids)
        slots = plan_slots(round(CUTS_PER_BASE * len(ids)), seed, split)
        for i, (family, size) in enumerate(slots):
            made = None
            for j in range(len(ids)):
                base_id = ids[(i + j) % len(ids)]
                cut_id = f"{base_id}--{family}-{size}"
                if cut_id in used:
                    continue
                spec = cal.choose_cuts(
                    faithful[base_id],
                    family,
                    size,
                    random.Random(f"{seed}|{cut_id}"),
                    seed=seed,
                )
                if spec is not None:
                    made = (base_id, spec)
                    break
            if made is None:
                skipped.append(
                    {
                        "split": split,
                        "family": family,
                        "size": size,
                        "reason": "no base has a valid interval",
                    }
                )
                continue
            base_id, spec = made
            used.add(spec.cut_id)
            pcm, _, _ = wav_info((ctx.base_dir(base_id) / "pcm.wav").read_bytes())
            if len(pcm) // 2 != faithful[base_id].total_samples:
                raise CalibrateError(f"{base_id}: label.json no longer matches pcm.wav")
            spec = cal.finalize_cut(spec, pcm)
            remaining, removed = spec.apply(pcm)
            d = ctx.cut_dir(spec.cut_id)
            b = meta[base_id]
            label_doc = {
                "spec": spec.to_dict(),
                "meta": {
                    k: b[k]
                    for k in ("episode", "feed", "split", "model_short", "voice")
                },
            }
            existing = d / "label.json"
            if existing.exists() and read_json(existing) != json.loads(
                json.dumps(label_doc)
            ):
                raise Refused(f"{existing} exists and differs; the seed/labels changed")
            write_new(d / "cut.wav", pcm_to_wav(remaining))
            for k, clip in enumerate(removed):
                write_new(d / f"removed-{k}.wav", pcm_to_wav(clip))
            write_json_new(existing, label_doc)
            entries.append(
                {
                    "cut_id": spec.cut_id,
                    "base_id": base_id,
                    "family": family,
                    "size_bin": size,
                    "split": split,
                    "feed": b["feed"],
                    "episode": b["episode"],
                    "model_short": b["model_short"],
                    "voice": b["voice"],
                    "total_tokens": spec.total_tokens,
                    "n_intervals": len(spec.intervals),
                    "label_sha256": sha256_hex(json.dumps(label_doc, sort_keys=True)),
                }
            )
    coverage = {
        split: {
            "families": sorted({e["family"] for e in entries if e["split"] == split}),
            "size_bins": sorted(
                {
                    e["size_bin"]
                    for e in entries
                    if e["split"] == split and e["family"] != "multi"
                }
            ),
            "n": sum(1 for e in entries if e["split"] == split),
        }
        for split in ("dev", "holdout")
    }
    doc = {
        "version": 1,
        "created": now_iso(),
        "seed": seed,
        "cuts": entries,
        "skipped": skipped,
        "coverage": coverage,
    }
    if not write_json_new(ctx.cuts_path, doc):
        raise Refused(f"{ctx.cuts_path} appeared while running; not overwriting")
    ctx.echo(
        f"cuts: {len(entries)} cuts frozen ({len(skipped)} slots had no valid interval)"
    )
    for split, c in coverage.items():
        ctx.echo(
            f"  {split}: {c['n']} cuts, families {c['families']}, bins {c['size_bins']}"
        )
    return doc


def step_cuts_verify(ctx: Ctx) -> dict:
    """After ``whisper --kind cut --kind removed``: run the post-cut checks and
    write ``cuts-verified.json`` (a separate file; ``cuts.json`` stays frozen)."""
    if ctx.verified_path.exists():
        raise Refused(f"{ctx.verified_path} already exists; not overwriting")
    results: dict[str, dict] = {}
    missing: list[str] = []
    for c in ctx.cuts()["cuts"]:
        d = ctx.cut_dir(c["cut_id"])
        doc = read_json(d / "label.json")
        spec = cal.CutSpec.from_dict(doc["spec"])
        need = [d / "whisper.json"] + [
            d / f"whisper-removed-{k}.json" for k in range(len(spec.intervals))
        ]
        gone = [str(p.relative_to(ctx.root)) for p in need if not p.exists()]
        if gone:
            missing.extend(gone)
            continue
        base = effective_label(ctx, c["base_id"])
        assert base is not None
        post = cal.verify_cut_audio(
            spec, base.script_text, read_json(d / "whisper.json"), base=base
        )
        sanity = [
            cal.removed_sanity(
                iv.normalized_tokens, read_json(d / f"whisper-removed-{k}.json")
            )
            for k, iv in enumerate(spec.intervals)
        ]
        reasons = []
        if not post["ok"]:
            reasons.append("post_cut_check")
        if not all(s["ok"] for s in sanity):
            reasons.append("removed_clip_sanity")
        results[c["cut_id"]] = {
            "discarded": bool(reasons),
            "reasons": reasons,
            "post_cut": post,
            "removed_sanity": sanity,
        }
    if missing:
        raise Refused(
            f"{len(missing)} whisper file(s) missing (first: {missing[0]}); run "
            "`whisper --kind cut --kind removed` first"
        )
    doc = {"version": 1, "created": now_iso(), "cuts": results}
    write_json_new(ctx.verified_path, doc)
    n_bad = sum(1 for r in results.values() if r["discarded"])
    ctx.echo(f"cuts --verify: {len(results)} cuts, {n_bad} discarded")
    for cid, r in sorted(results.items()):
        if r["discarded"]:
            ctx.echo(f"  DISCARD {cid}: {','.join(r['reasons'])}")
    return doc


def live_cuts(ctx: Ctx, *, allow_unverified: bool = False) -> list[dict]:
    """``cuts.json`` entries minus the ones ``cuts --verify`` discarded."""
    cuts = ctx.cuts()["cuts"]
    if not ctx.verified_path.exists():
        if not allow_unverified:
            raise Refused(
                "cuts-verified.json is missing; run `cuts --verify` (or pass "
                "--unverified to use every cut)"
            )
        return cuts
    verified = read_json(ctx.verified_path)["cuts"]
    return [
        c for c in cuts if not verified.get(c["cut_id"], {}).get("discarded", False)
    ]


# --------------------------------------------------------------------------- #
# asr
# --------------------------------------------------------------------------- #


def asr_path(t: Target, policy: str, n: int) -> Path:
    return t.dir / "asr" / f"{policy}-{n}.json"


def asr_one(
    ctx: Ctx,
    t: Target,
    policy: str,
    n: int,
    transcribers: dict[str, Any],
    retry_unavailable: bool,
) -> str:
    out = asr_path(t, policy, n)
    if out.exists():
        if not (retry_unavailable and read_json(out).get("status") == "unavailable"):
            return "skipped"
        move_aside(out, "unavailable")
    wav = t.audio.read_bytes()
    _, _, duration = wav_info(wav)
    tr = transcribers[policy]
    ids = {"id": t.id, "kind": t.kind, "policy": policy, "repeat": n}
    worst = est_asr_worst(duration)
    with ctx.ledger.reserve(worst, f"asr {t.id} {policy}-{n}"):
        started = time.monotonic()
        record: dict[str, Any] = {
            "schema": 1,
            "id": t.id,
            "kind": t.kind,
            "policy_name": policy,
            "repeat": n,
            "asr_policy": getattr(tr, "policy", None),
            "audio_sha256": sha256_hex(wav),
            "audio_seconds": duration,
        }
        try:
            res = tr(wav, "audio/wav")
        except TranscriptionUnavailable as exc:
            record.update(
                status="unavailable",
                unavailable_reason=exc.reason,
                detail=str(exc),
                elapsed_s=time.monotonic() - started,
            )
            ctx.ledger.append(
                "asr",
                getattr(tr, "model", ASR_MODEL),
                ids,
                None,
                worst,
                worst_case=True,
                note=f"unavailable:{exc.reason}",
            )
        else:
            usd, worst_case = est_asr(
                res.input_tokens, res.output_tokens, res.thinking_tokens, duration
            )
            record.update(
                status="ok",
                transcript=res.text,
                finish_reason=res.finish_reason,
                elapsed_s=res.elapsed_s,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                thinking_tokens=res.thinking_tokens,
                model=res.model,
                asr_policy=res.policy or record["asr_policy"],
            )
            ctx.ledger.append(
                "asr",
                res.model,
                ids,
                {
                    "input": res.input_tokens,
                    "output": res.output_tokens,
                    "thinking": res.thinking_tokens,
                },
                usd,
                worst_case=worst_case,
            )
    record["created"] = now_iso()
    write_json_new(out, record)
    return record["status"]


def step_asr(
    ctx: Ctx,
    *,
    kinds: Sequence[str],
    split: str = "all",
    policies: Sequence[str] = ("default",),
    repeat: int = 1,
    ids: Sequence[str] = (),
    workers: int = 4,
    retry_unavailable: bool = False,
    allow_unverified: bool = False,
    timeout_s: float = ASR_TIMEOUT_S,
) -> dict:
    ctx.need_env("GEMINI_API_KEY")
    for p in policies:
        if p not in ASR_POLICIES:
            raise Refused(f"unknown ASR policy {p!r}; choose from {ASR_POLICIES}")
    if repeat < 1:
        raise Refused("--repeat must be at least 1")
    targets: list[Target] = []
    for t in iter_targets(ctx, kinds, split, ids):
        if t.kind == "base":
            label = effective_label(ctx, t.id)
            if label is None or label.label != "faithful":
                continue  # only faithful bases are negative controls
        targets.append(t)
    if "cut" in kinds:
        live = {c["cut_id"] for c in live_cuts(ctx, allow_unverified=allow_unverified)}
        targets = [t for t in targets if t.kind != "cut" or t.id in live]
    # interleaved order: per target, repeats alternate between the policies
    tasks = [(t, p, n) for t in targets for n in range(repeat) for p in policies]
    transcribers = {p: ctx.services.transcriber(p, timeout_s) for p in policies}
    try:
        results = run_parallel(
            tasks,
            lambda task: asr_one(
                ctx, task[0], task[1], task[2], transcribers, retry_unavailable
            ),
            workers,
        )
    finally:
        for tr in transcribers.values():
            with contextlib.suppress(Exception):
                tr.close()
    counts = _count(results)
    ctx.echo(f"asr: {len(tasks)} transcriptions: {_fmt(counts)}")
    return counts


# --------------------------------------------------------------------------- #
# levine-repro (decision 10)
# --------------------------------------------------------------------------- #


def parse_voices(spec: str) -> list[tuple[str, int]]:
    out = []
    for part in spec.split(","):
        voice, _, count = part.partition(":")
        if not voice or not count.isdigit() or int(count) < 1:
            raise Refused(f"--voices wants Voice:count,Voice:count, got {spec!r}")
        out.append((voice.strip(), int(count)))
    return out


def step_levine_repro(
    ctx: Ctx,
    *,
    script: Path = DEFAULT_REPRO_SCRIPT,
    chunk_index: int = 0,
    voices: str = "Charon:3,Puck:3,Kore:2",
    model: str = MODELS["flash"],
    workers: int = 4,
    retry_failed: bool = False,
) -> dict:
    ctx.need_env("GEMINI_API_KEY")
    plan = parse_voices(voices)
    text = script.read_text(encoding="utf-8")
    chunks = chunk_text(text, ceiling=GeminiProvider.max_chars)
    if not 0 <= chunk_index < len(chunks):
        raise Refused(
            f"--chunk-index {chunk_index} but the script has {len(chunks)} chunk(s)"
        )
    chunk = chunks[chunk_index]
    source = {
        "script": str(script),
        "script_sha256": sha256_hex(text),
        "chunk_index": chunk_index,
        "chunk_sha256": sha256_hex(chunk),
        "model": model,
        "style": STYLE,
    }
    source_path = ctx.repro_dir / "source.json"
    if source_path.exists():
        if read_json(source_path) != source:
            raise Refused(f"{source_path} exists for a different script/chunk/model")
    else:
        write_json_new(source_path, source)
    attempts = [(f"{v}-{n}", v) for v, count in plan for n in range(count)]

    def one(item: tuple[str, str]) -> tuple[str, str]:
        name, voice = item
        d = ctx.repro_dir / name
        write_new(d / "chunk.txt", chunk.encode("utf-8"))
        return name, synth_one(
            ctx,
            item_id=f"levine-repro/{name}",
            text=chunk,
            model=model,
            voice=voice,
            out_dir=d,
            retry_failed=retry_failed,
        )

    results = run_parallel(attempts, one, workers)
    counts = _count(r[1] for r in results)
    ctx.echo(f"levine-repro: {len(attempts)} attempts: {_fmt(counts)}")
    return counts


# --------------------------------------------------------------------------- #
# deadline (decision 11)
# --------------------------------------------------------------------------- #


def step_deadline(
    ctx: Ctx,
    *,
    model: str,
    voice: str = "Kore",
    episode: str = "levine-2026-09-24",
    text_file: Path | None = None,
    attempt: int = 0,
) -> dict:
    """One full-episode ``render_episode`` under the production 6-minute budget,
    no fallback, no cache. Appends the outcome to ``deadline/summary.jsonl``."""
    ctx.need_env("GEMINI_API_KEY")
    from pipeline.tts import verify

    short = next((k for k, v in MODELS.items() if v == model), model)
    key = f"{episode}|{model}|{voice}|{attempt}"
    summary_path = ctx.root / "deadline" / "summary.jsonl"
    if summary_path.exists() and any(
        json.loads(line).get("key") == key
        for line in summary_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ):
        ctx.echo(f"deadline: {key} already recorded; pass --attempt N for another run")
        return {"status": "skipped"}
    path = text_file or (ctx.root / "texts" / f"{episode}.txt")
    if not path.exists():
        raise Refused(f"no text at {path}; give --episode of the corpus or --text-file")
    text = path.read_text(encoding="utf-8")
    worst = est_deadline_worst(len(text), model)
    cfg = RenderConfig(
        GeminiConfig(model=model, voice=voice, style=STYLE), fallback=None
    )
    deadline_dir = ctx.root / "deadline"
    out_mp3 = deadline_dir / f"{episode}--{short}--{voice}--{attempt}.mp3"
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    ids = {"key": key}
    with ctx.ledger.reserve(worst, f"deadline {key}"):
        started = time.monotonic()
        outcome, error, manifest_path = "ok", None, None
        try:
            result = ctx.services.render(
                text,
                cfg,
                out_mp3,
                feed_slug="tts-eval-t5",
                episode_id=f"deadline-{short}-{voice}-{attempt}",
                manifest_dir=deadline_dir / "manifests",
                cache_dir=None,
                notify_fallback=False,
            )
            manifest_path = getattr(result, "manifest_path", None)
        except Exception as exc:  # noqa: BLE001 -- recorded, not raised
            outcome, error = "failed", f"{type(exc).__name__}: {exc}"
        wall = time.monotonic() - started
        phase: dict[str, Any] = {}
        tokens: dict[str, Any] = {}
        if manifest_path is not None and Path(manifest_path).exists():
            phase = read_json(Path(manifest_path)).get("gemini_phase") or {}
            tokens = phase.get("tokens") or {}
        usd, worst_case = est_render_from_tokens(tokens, len(text), model)
        ctx.ledger.append(
            "deadline",
            model,
            ids,
            tokens or None,
            usd,
            worst_case=worst_case,
            note=None if tokens else "no manifest tokens",
        )
    retries = None
    chunks = phase.get("chunks")
    if isinstance(chunks, list):
        retries = sum(
            max(0, len(c.get("attempts") or []) - 1)
            for c in chunks
            if isinstance(c, dict)
        )
    record = {
        "key": key,
        "episode": episode,
        "model": model,
        "voice": voice,
        "attempt": attempt,
        "chars": len(text),
        "outcome": phase.get("outcome") if phase else outcome,
        "render_outcome": outcome,
        "error": error,
        "reason": phase.get("reason"),
        "detail": phase.get("detail"),
        "failed_chunk": phase.get("failed_chunk"),
        "phase_elapsed_s": phase.get("elapsed_s"),
        "wall_s": wall,
        "budget_s": phase.get("budget_s"),
        "retries": retries,
        "tokens": tokens or None,
        "est_usd": usd,
        "worst_case": worst_case,
        "verifier_policy": verify.VERIFIER_POLICY,
        "manifest": str(manifest_path) if manifest_path else None,
        "mp3": str(out_mp3) if out_mp3.exists() else None,
        "created": now_iso(),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    ctx.echo(
        f"deadline: {key}: {record['outcome']} reason={record['reason']} "
        f"phase={record['phase_elapsed_s']} s wall={wall:.1f} s retries={retries}"
    )
    return record


def est_render_from_tokens(tokens: dict, chars: int, model: str) -> tuple[float, bool]:
    """Spend of a ``render_episode`` from its manifest's token totals; any
    unknown total (or no manifest) is the whole render's worst case."""
    needed = ("synth_audio", "asr_input", "asr_output")
    if not tokens or any(tokens.get(k) is None for k in needed):
        return est_deadline_worst(chars, model), True
    usd = tokens["synth_audio"] / 1e6 * synth_price(model)
    usd += tokens["asr_input"] / 1e6 * ASR_USD_PER_M_INPUT
    usd += (
        (tokens["asr_output"] + (tokens.get("asr_thinking") or 0))
        / 1e6
        * ASR_USD_PER_M_OUTPUT
    )
    return usd, False


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def _asr_files(directory: Path, policies: Sequence[str]) -> list[tuple[str, int, dict]]:
    out: list[tuple[str, int, dict]] = []
    asr_dir = directory / "asr"
    if not asr_dir.exists():
        return out
    for p in policies:
        for f in sorted(asr_dir.glob(f"{p}-*.json")):
            tail = f.stem[len(p) + 1 :]
            if tail.isdigit():
                out.append((p, int(tail), read_json(f)))
    return out


def collect_records(
    ctx: Ctx, split: str, policies: Sequence[str], *, allow_unverified: bool = False
) -> tuple[list[cal.EvalRecord], list[dict]]:
    """``EvalRecord``s for every stored ASR run of faithful bases and live cuts,
    plus the inputs ``reconstruction`` needs per cut run."""
    records: list[cal.EvalRecord] = []
    recon: list[dict] = []
    meta = base_meta(ctx)
    clean: dict[tuple[str, str], str] = {}
    for base_id, b in sorted(meta.items()):
        label = effective_label(ctx, base_id)
        if label is None or label.label != "faithful":
            continue
        if split != "all" and b["split"] != split:
            continue
        for policy, n, rec in _asr_files(ctx.base_dir(base_id), policies):
            ok = rec.get("status") == "ok"
            if ok and n == 0:
                clean[(base_id, policy)] = rec["transcript"]
            records.append(
                cal.EvalRecord(
                    record_id=f"{base_id}:{policy}-{n}",
                    kind="base",
                    base_id=base_id,
                    script_text=label.script_text,
                    transcript=rec.get("transcript") if ok else None,
                    unavailable=None
                    if ok
                    else rec.get("unavailable_reason", "unavailable"),
                    split=b["split"],
                    feed=b["feed"],
                    model=b["model_short"],
                    voice=b["voice"],
                    policy=policy,
                    repeat=n,
                )
            )
    for c in live_cuts(ctx, allow_unverified=allow_unverified):
        if split != "all" and c["split"] != split:
            continue
        d = ctx.cut_dir(c["cut_id"])
        spec = cal.CutSpec.from_dict(read_json(d / "label.json")["spec"])
        label = effective_label(ctx, c["base_id"])
        assert label is not None
        for policy, n, rec in _asr_files(d, policies):
            ok = rec.get("status") == "ok"
            rid = f"{c['cut_id']}:{policy}-{n}"
            records.append(
                cal.EvalRecord(
                    record_id=rid,
                    kind="cut",
                    base_id=c["base_id"],
                    script_text=label.script_text,
                    transcript=rec.get("transcript") if ok else None,
                    unavailable=None
                    if ok
                    else rec.get("unavailable_reason", "unavailable"),
                    split=c["split"],
                    feed=c["feed"],
                    model=c["model_short"],
                    voice=c["voice"],
                    policy=policy,
                    repeat=n,
                    family=c["family"],
                    size_bin=c["size_bin"],
                    removed=tuple(spec.token_intervals),
                )
            )
            if ok:
                whisper = d / "whisper.json"
                recon.append(
                    {
                        "record_id": rid,
                        "spec": spec,
                        "script": label.script_text,
                        "clean": clean.get((c["base_id"], policy)),
                        "transcript": rec["transcript"],
                        "residue": read_json(whisper) if whisper.exists() else None,
                        "family": c["family"],
                        "size_bin": c["size_bin"],
                    }
                )
    return records, recon


def build_report(
    records: list[cal.EvalRecord], recon_inputs: list[dict], *, args: dict
) -> dict:
    grid = cal.default_grid()
    results = cal.evaluate(records, grid)
    by_id = {r.record_id: r for r in records}
    recon_rows = []
    for item in recon_inputs:
        out = cal.reconstruction(
            item["spec"],
            item["script"],
            item["clean"],
            item["transcript"],
            audio_residue=item["residue"],
        )
        verdicts = [cal.replay(by_id[item["record_id"]], th).status for th in grid]
        recon_rows.append(
            {
                "record_id": item["record_id"],
                "family": item["family"],
                "size_bin": item["size_bin"],
                "level": out["level"],
                "cut_hits": out["cut_hits"],
                "raw_cut_hits": out["raw_cut_hits"],
                "audio_residue_tokens": out["audio_residue_tokens"],
                "clean_hits": out["clean_hits"],
                "removed_tokens": out["removed_tokens"],
                "matched_text": out["matched_text"],
                "verdicts": verdicts,
            }
        )
    false_passes = [
        [
            r["record_id"]
            for r in recon_rows
            if r["level"] == "confirmed" and r["verdicts"][i] == "pass"
        ]
        for i in range(len(grid))
    ]
    for i, res in enumerate(results):
        res["reconstruction_false_passes"] = false_passes[i]
    return {
        "version": 1,
        "created": now_iso(),
        "args": args,
        "n_records": len(records),
        "n_bases": sum(1 for r in records if r.kind == "base"),
        "n_cuts": sum(1 for r in records if r.kind == "cut"),
        "grid": results,
        "reconstruction": recon_rows,
    }


def _tbl(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return lines


def render_markdown(report: dict) -> str:
    lines = [
        f"# T5 calibration report ({report['args'].get('name', '')})",
        "",
        f"records: {report['n_records']} ({report['n_bases']} base runs, "
        f"{report['n_cuts']} cut runs); "
        f"args: `{json.dumps(report['args'], sort_keys=True)}`",
        "",
        "Three things, reported separately: **detection** (cuts caught & localized), "
        "**safe rejection** (false alarms on faithful bases), **availability** "
        "(unavailable ASR). Unavailable is never a catch.",
        "",
        "## Grid summary",
        "",
    ]
    rows = []
    for i, r in enumerate(report["grid"]):
        t, c, b = r["thresholds"], r["cuts"]["overall"], r["bases"]["overall"]
        rows.append(
            [
                i,
                t["net_deficit_min"],
                t["recall_floor"],
                f"{c['caught_localized']}/{c['n']}",
                c["caught"],
                c["missed"],
                c["unavailable"],
                f"{b['false_alarms']}/{b['n']}",
                b["unavailable"],
                len(r["reconstruction_false_passes"]),
                "YES" if r["acceptance_ok"] else "no",
            ]
        )
    lines += _tbl(
        [
            "#",
            "M",
            "floor",
            "caught_localized",
            "caught",
            "missed",
            "cut unavail",
            "false alarms",
            "base unavail",
            "recon false-passes",
            "acceptance_ok",
        ],
        rows,
    )
    for i, r in enumerate(report["grid"]):
        t = r["thresholds"]
        lines += [
            "",
            f"## Grid {i}: M={t['net_deficit_min']} floor={t['recall_floor']}",
            "",
        ]
        for dim in ("by_family", "by_size_bin", "by_split", "by_model", "by_feed"):
            table = r["cuts"].get(dim) or {}
            if table:
                lines += [f"cuts {dim}:", ""]
                lines += _tbl(
                    ["key", "n", "caught", "caught_localized", "missed", "unavailable"],
                    [
                        [
                            k,
                            v["n"],
                            v["caught"],
                            v["caught_localized"],
                            v["missed"],
                            v["unavailable"],
                        ]
                        for k, v in sorted(table.items())
                    ],
                )
                lines.append("")
        for dim in ("by_split", "by_model", "by_feed"):
            table = r["bases"].get(dim) or {}
            if table:
                lines += [f"faithful bases {dim}:", ""]
                lines += _tbl(
                    ["key", "n", "passed", "false_alarms", "unavailable"],
                    [
                        [k, v["n"], v["passed"], v["false_alarms"], v["unavailable"]]
                        for k, v in sorted(table.items())
                    ],
                )
                lines.append("")
        lines.append(f"false alarms: {r['bases']['false_alarms'] or 'none'}")
        lines.append(f"missed cuts: {r['cuts']['missed'] or 'none'}")
        lines.append(f"caught but not localized: {r['cuts']['unlocalized'] or 'none'}")
        lines.append(
            "confirmed-reconstruction false passes: "
            f"{r['reconstruction_false_passes'] or 'none'}"
        )
        lines.append(
            f"acceptance: {r['acceptance']} -> "
            f"{'OK' if r['acceptance_ok'] else 'not ok'}"
        )
    lines += ["", "## Reconstruction (per cut run)", ""]
    lines += _tbl(
        [
            "record",
            "family",
            "bin",
            "level",
            "hits",
            "raw hits",
            "residue",
            "clean hits",
            "removed",
        ],
        [
            [
                r["record_id"],
                r["family"],
                r["size_bin"],
                r["level"],
                r["cut_hits"],
                r["raw_cut_hits"],
                r["audio_residue_tokens"],
                r["clean_hits"],
                r["removed_tokens"],
            ]
            for r in report["reconstruction"]
        ],
    )
    return "\n".join(lines) + "\n"


def step_report(
    ctx: Ctx,
    *,
    kind: str = "eval",
    split: str = "all",
    policies: Sequence[str] = (),
    name: str | None = None,
    allow_unverified: bool = False,
) -> dict:
    if not policies:
        raise Refused("--policy is required (default or low)")
    name = name or f"report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    json_path = ctx.root / "reports" / f"{name}.json"
    md_path = ctx.root / "reports" / f"{name}.md"
    if json_path.exists() or md_path.exists():
        raise Refused(f"report {name!r} already exists; pick another --name")
    if kind == "repro":
        report = build_repro_report(ctx, policies)
        text = render_repro_markdown(report)
    else:
        records, recon = collect_records(
            ctx, split, policies, allow_unverified=allow_unverified
        )
        report = build_report(
            records,
            recon,
            args={"name": name, "split": split, "policy": list(policies)},
        )
        text = render_markdown(report)
    write_json_new(json_path, report)
    write_new(md_path, text.encode("utf-8"))
    ctx.echo(f"report: wrote {json_path} and {md_path}")
    return report


# --- repro report ---------------------------------------------------------- #


def _spans(script: str, transcript: str, floor: int) -> list[dict]:
    a = analyze(script, transcript, DEFAULT_THRESHOLDS)
    return [
        {
            "start": s.script_start,
            "end": s.script_end,
            "net_missing": s.net_missing,
            "excerpt": s.excerpt,
        }
        for s in a.spans
        if s.flagged or s.net_missing >= floor
    ]


def build_repro_report(ctx: Ctx, policies: Sequence[str]) -> dict:
    """Per Levine-repro attempt: the spans Gemini-ASR shows and the spans
    whisper's word alignment shows, side by side. Both missing the same script
    span is a confirmed natural omission; only Gemini is an ASR false positive;
    only whisper is a Gemini ASR miss."""
    rows = []
    for d in repro_attempts(ctx):
        if not _synth_ok(d):
            continue
        script = (d / "chunk.txt").read_text(encoding="utf-8")
        pcm, _, _ = wav_info((d / "pcm.wav").read_bytes())
        whisper_spans: list[dict] | None = None
        if (d / "whisper.json").exists():
            label = cal.screen_base(
                script,
                read_json(d / "whisper.json"),
                total_samples=len(pcm) // 2,
                base_id=d.name,
            )
            whisper_spans = [
                {
                    "start": s.script_start,
                    "end": s.script_end,
                    "net_missing": s.net_missing,
                    "excerpt": s.excerpt,
                }
                for s in label.suspect_spans
            ]
        for policy, n, rec in _asr_files(d, policies):
            if rec.get("status") != "ok":
                rows.append(
                    {
                        "attempt": d.name,
                        "asr": f"{policy}-{n}",
                        "gemini_spans": None,
                        "whisper_spans": whisper_spans,
                        "classification": f"asr {rec.get('unavailable_reason')}",
                    }
                )
                continue
            g = _spans(script, rec["transcript"], cal.FAITHFUL_NET_MISSING)
            rows.append(
                {
                    "attempt": d.name,
                    "asr": f"{policy}-{n}",
                    "gemini_spans": g,
                    "whisper_spans": whisper_spans,
                    "classification": _classify(g, whisper_spans),
                }
            )
    return {
        "version": 1,
        "created": now_iso(),
        "policies": list(policies),
        "rows": rows,
    }


def _overlap(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def _classify(gemini: list[dict], whisper: list[dict] | None) -> str:
    if whisper is None:
        return "no whisper yet"
    if not gemini and not whisper:
        return "no span in either"
    both = [g for g in gemini if any(_overlap(g, w) for w in whisper)]
    if both:
        return "CONFIRMED natural omission (both ASRs miss the same span)"
    if gemini and not whisper:
        return "Gemini-only: ASR false positive"
    if whisper and not gemini:
        return "whisper-only: Gemini ASR missed it"
    return "disjoint spans: owner clip"


def render_repro_markdown(report: dict) -> str:
    def fmt(spans: list[dict] | None) -> str:
        if spans is None:
            return "-"
        return (
            "; ".join(
                f"[{s['start']},{s['end']}) -{s['net_missing']}: {s['excerpt'][:50]}"
                for s in spans
            )
            or "none"
        )

    lines = ["# Levine skip reproduction", ""]
    lines += _tbl(
        ["attempt", "asr", "Gemini-ASR spans", "whisper spans", "classification"],
        [
            [
                r["attempt"],
                r["asr"],
                fmt(r["gemini_spans"]),
                fmt(r["whisper_spans"]),
                r["classification"],
            ]
            for r in report["rows"]
        ],
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #


def step_ledger(ctx: Ctx) -> dict:
    s = ctx.ledger.summary()
    ctx.echo(
        f"ledger: ${s['total_usd']:.4f} of ${s['budget_usd']:.2f} spent "
        f"(${s['remaining_usd']:.4f} left)"
    )
    for kind, row in sorted(s["by_kind"].items()):
        ctx.echo(
            f"  {kind:<9} {row['calls']:>4} calls  ${row['est_usd']:.4f}  "
            f"({row['worst_case_calls']} at worst case)"
        )
    return s


# --------------------------------------------------------------------------- #
# click
# --------------------------------------------------------------------------- #


def _csv_ids(
    ctx: click.Context, param: click.Parameter, value: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(i for v in value for i in v.split(",") if i)


@click.group("tts-calibrate")
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_ROOT,
    show_default=True,
    help="Artifact tree.",
)
@click.option(
    "--budget",
    type=click.FloatRange(min=0),
    default=DEFAULT_BUDGET_USD,
    show_default=True,
    help="USD ceiling, enforced before every paid call.",
)
@click.pass_context
def calibrate_group(ctx: click.Context, root: Path, budget: float) -> None:
    """T5 omission-detector calibration harness (paid steps; resumable)."""
    ctx.obj = Ctx(root=root, budget=budget, services=default_services())


@contextlib.contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except CalibrateError as exc:
        click.echo(f"tts-calibrate: {exc}", err=True)
        raise SystemExit(exc.exit_code) from None


_SPLIT = click.Choice(["dev", "holdout", "all"])
_ids_option = click.option(
    "--ids",
    multiple=True,
    callback=_csv_ids,
    help="Only these ids (repeatable or comma-separated).",
)


@calibrate_group.command("corpus")
@click.option(
    "--rundown", "rundown", multiple=True, help="Rundown date YYYY-MM-DD (x4)."
)
@click.option("--fp", "fp", multiple=True, help="FP Digest date YYYY-MM-DD (x4).")
@click.option(
    "--levine-key",
    "levine_keys",
    multiple=True,
    help="R2 key of a Levine raw email (x4).",
)
@click.option(
    "--dev",
    "dev",
    multiple=True,
    callback=_csv_ids,
    help="Episode id in the dev split, e.g. rundown-2026-09-28 "
    "(x6; the rest are hold-out).",
)
@click.option(
    "--scripts-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_SCRIPTS_ROOT,
    show_default=True,
)
@click.option(
    "--state-db",
    type=click.Path(path_type=Path),
    default=None,
    help="Episodes table (opened read-only) for published mp3 keys.",
)
@click.pass_obj
def corpus_cmd(c: Ctx, rundown, fp, levine_keys, dev, scripts_root, state_db) -> None:
    """Freeze corpus.json and texts/ (refuses if corpus.json exists)."""
    with _guard():
        corpus = build_corpus(
            c,
            rundown=rundown,
            fp=fp,
            levine_keys=levine_keys,
            dev=dev,
            scripts_root=scripts_root,
            state_db=state_db,
        )
        c.echo(
            f"corpus: {len(corpus['episodes'])} episodes, "
            f"{len(corpus['bases'])} bases -> {c.corpus_path}"
        )


@calibrate_group.command("synth")
@click.option("--workers", default=4, show_default=True, type=click.IntRange(min=1))
@click.option("--split", type=_SPLIT, default="all", show_default=True)
@_ids_option
@click.option(
    "--retry-failed",
    is_flag=True,
    help="Retry bases whose synth.json says failed (the old record is kept).",
)
@click.pass_obj
def synth_cmd(c: Ctx, workers, split, ids, retry_failed) -> None:
    """Render the base chunks with Gemini (PCM saved first)."""
    with _guard():
        step_synth(c, workers=workers, split=split, ids=ids, retry_failed=retry_failed)


@calibrate_group.command("whisper")
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    type=click.Choice(["base", "cut", "removed", "repro"]),
    default=("base",),
    show_default=True,
)
@click.option("--split", type=_SPLIT, default="all", show_default=True)
@_ids_option
@click.option("--workers", default=4, show_default=True, type=click.IntRange(min=1))
@click.pass_obj
def whisper_cmd(c: Ctx, kinds, split, ids, workers) -> None:
    """whisper-1 word timestamps for bases, cut audio, removed clips, repro attempts."""
    with _guard():
        step_whisper(c, kinds=kinds, split=split, ids=ids, workers=workers)


@calibrate_group.command("label")
@click.option("--split", type=_SPLIT, default="all", show_default=True)
@_ids_option
@click.pass_obj
def label_cmd(c: Ctx, split, ids) -> None:
    """Screen bases with whisper; write owner clips for suspects."""
    with _guard():
        step_label(c, split=split, ids=ids)


@calibrate_group.command("owner-call")
@click.argument("base_id")
@click.argument("call", type=click.Choice(OWNER_CALLS))
@click.option("--note", default="")
@click.pass_obj
def owner_call_cmd(c: Ctx, base_id, call, note) -> None:
    """Record the owner's call on a suspect base (never overwritten)."""
    with _guard():
        record_owner_call(c, base_id, call, note)
        c.echo(f"owner-call: {base_id} = {call}")


@calibrate_group.command("clips")
@click.argument("item_id")
@click.option("--start", "start_s", type=float, required=True)
@click.option("--end", "end_s", type=float, required=True)
@click.option("--name", default=None)
@click.pass_obj
def clips_cmd(c: Ctx, item_id, start_s, end_s, name) -> None:
    """Write an mp3 of a time window of a base or cut for the owner."""
    with _guard():
        step_clips(c, item_id=item_id, start_s=start_s, end_s=end_s, name=name)


@calibrate_group.command("cuts")
@click.option("--seed", default=DEFAULT_CUT_SEED, show_default=True)
@click.option(
    "--verify",
    "verify",
    is_flag=True,
    help="After `whisper --kind cut --kind removed`: write cuts-verified.json.",
)
@click.pass_obj
def cuts_cmd(c: Ctx, seed, verify) -> None:
    """Choose, cut, snap and freeze cuts.json (or, with --verify, check them)."""
    with _guard():
        if verify:
            step_cuts_verify(c)
        else:
            step_cuts(c, seed=seed)


@calibrate_group.command("asr")
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    required=True,
    type=click.Choice(["base", "cut", "repro"]),
)
@click.option("--split", type=_SPLIT, default="all", show_default=True)
@click.option(
    "--policy",
    "policies",
    multiple=True,
    type=click.Choice(ASR_POLICIES),
    default=("default",),
    show_default=True,
)
@click.option("--repeat", default=1, show_default=True, type=click.IntRange(min=1))
@_ids_option
@click.option("--workers", default=4, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--retry-unavailable",
    is_flag=True,
    help="Redo runs recorded unavailable (the old record is kept).",
)
@click.option(
    "--unverified",
    "allow_unverified",
    is_flag=True,
    help="Use cuts even without cuts-verified.json.",
)
@click.pass_obj
def asr_cmd(
    c: Ctx,
    kinds,
    split,
    policies,
    repeat,
    ids,
    workers,
    retry_unavailable,
    allow_unverified,
) -> None:
    """Gemini ASR (audio only) over bases, cuts or repro attempts."""
    with _guard():
        step_asr(
            c,
            kinds=kinds,
            split=split,
            policies=policies,
            repeat=repeat,
            ids=ids,
            workers=workers,
            retry_unavailable=retry_unavailable,
            allow_unverified=allow_unverified,
        )


@calibrate_group.command("report")
@click.option(
    "--kind", type=click.Choice(["eval", "repro"]), default="eval", show_default=True
)
@click.option("--split", type=_SPLIT, default="all", show_default=True)
@click.option(
    "--policy",
    "policies",
    multiple=True,
    type=click.Choice(ASR_POLICIES),
    required=True,
)
@click.option("--name", default=None)
@click.option("--unverified", "allow_unverified", is_flag=True)
@click.pass_obj
def report_cmd(c: Ctx, kind, split, policies, name, allow_unverified) -> None:
    """Replay the verifier over stored ASR runs: reports/<name>.json and .md."""
    with _guard():
        step_report(
            c,
            kind=kind,
            split=split,
            policies=policies,
            name=name,
            allow_unverified=allow_unverified,
        )


@calibrate_group.command("levine-repro")
@click.option(
    "--script",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DEFAULT_REPRO_SCRIPT,
    show_default=True,
)
@click.option("--chunk-index", default=0, show_default=True, type=int)
@click.option("--voices", default="Charon:3,Puck:3,Kore:2", show_default=True)
@click.option("--model", default=MODELS["flash"], show_default=True)
@click.option("--workers", default=4, show_default=True, type=click.IntRange(min=1))
@click.option("--retry-failed", is_flag=True)
@click.pass_obj
def repro_cmd(
    c: Ctx, script, chunk_index, voices, model, workers, retry_failed
) -> None:
    """Re-render the Levine chunk that skipped in T4, several times."""
    with _guard():
        step_levine_repro(
            c,
            script=script,
            chunk_index=chunk_index,
            voices=voices,
            model=model,
            workers=workers,
            retry_failed=retry_failed,
        )


@calibrate_group.command("deadline")
@click.option("--model", required=True, type=click.Choice(list(MODELS.values())))
@click.option("--voice", default="Kore", show_default=True)
@click.option("--episode", default="levine-2026-09-24", show_default=True)
@click.option(
    "--text-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
@click.option("--attempt", default=0, show_default=True, type=int)
@click.pass_obj
def deadline_cmd(c: Ctx, model, voice, episode, text_file, attempt) -> None:
    """One full-episode render under the production 6-minute budget (no fallback)."""
    with _guard():
        step_deadline(
            c,
            model=model,
            voice=voice,
            episode=episode,
            text_file=text_file,
            attempt=attempt,
        )


@calibrate_group.command("ledger")
@click.pass_obj
def ledger_cmd(c: Ctx) -> None:
    """Print spend so far."""
    with _guard():
        step_ledger(c)


def main(argv: Sequence[str] | None = None) -> None:
    calibrate_group.main(
        args=list(argv) if argv is not None else None,
        prog_name="python -m pipeline tts-calibrate",
    )


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
