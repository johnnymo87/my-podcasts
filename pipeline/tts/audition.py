"""Local TTS audition: render one script through several voices, compare by ear.

Renders the feed's current OpenAI voice and every requested Gemini model x voice
through the production ``render_episode`` into local mp3s plus a ``summary.json``.

Local-only by construction: this module imports nothing but ``pipeline.tts.*`` (no
R2, DB, feed or alert code; a test pins it) and every render is made with
``cache_dir=None``, ``notify_fallback=False`` and -- for a Gemini variant -- no
OpenAI fallback. A Gemini variant that cannot render is therefore reported FAILED,
never silently replaced by the OpenAI voice it was being compared against, and a
file's name is derived from what ``render_episode`` says it *rendered*.

No module-level work: Gemini variants spawn a child process, which re-imports the
launching ``__main__`` (``python -m pipeline`` is spawn-safe).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.tts import config as tts_config
from pipeline.tts.config import GeminiConfig, OpenAIConfig, RenderConfig
from pipeline.tts.render import TTSRenderError


SUMMARY_SCHEMA = 1
DEFAULT_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
DEFAULT_STYLE = "calm, measured news anchor"
MAX_ERROR_CHARS = 500

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class Variant:
    requested: OpenAIConfig | GeminiConfig


def baseline_leaf(feed_slug: str) -> OpenAIConfig:
    """The feed's OpenAI voice: its primary if OpenAI, else its fallback, else the
    default."""
    if feed_slug not in tts_config.FEED_VOICES:
        raise ValueError(f"unknown feed {feed_slug!r}")
    entry = tts_config.FEED_VOICES[feed_slug]
    if isinstance(entry.primary, OpenAIConfig):
        return entry.primary
    if entry.fallback is not None:
        return entry.fallback
    default = tts_config.DEFAULT_RENDER_CONFIG.primary
    assert isinstance(default, OpenAIConfig)
    return default


def _dedupe(items) -> list[str]:
    return list(dict.fromkeys(items))


def build_variants(
    feed_slug: str,
    *,
    models,
    voices,
    style: str,
    include_openai: bool,
) -> list[Variant]:
    """Baseline first (if wanted), then models in order, voices in order.

    ``ValueError`` for an unknown feed, an OpenAI voice name given as a Gemini
    voice, or an empty result -- all before anything is rendered.
    """
    variants: list[Variant] = []
    if include_openai:
        variants.append(Variant(baseline_leaf(feed_slug)))
    elif feed_slug not in tts_config.FEED_VOICES:
        raise ValueError(f"unknown feed {feed_slug!r}")
    for model in _dedupe(models):
        for voice in _dedupe(voices):
            variants.append(
                Variant(GeminiConfig(model=model, voice=voice, style=style))
            )
    if not variants:
        raise ValueError("nothing to render: no variants requested")
    return variants


def excerpt(text: str, max_chars: int | None) -> str:
    """Cut ``text`` at the last paragraph boundary at or before ``max_chars``.

    Never cuts mid-paragraph: ``ValueError`` if there is no boundary that far in.
    ``None`` (or a text already short enough) returns the text unchanged.
    """
    if max_chars is None or len(text) <= max_chars:
        return text
    idx = text.rfind("\n\n", 0, max_chars + 2)  # boundary starting at or before N
    cut = text[:idx].rstrip() if idx >= 0 else ""
    if not cut:
        raise ValueError(
            f"no paragraph boundary at or before {max_chars} characters; "
            "raise --max-chars"
        )
    return cut


def _safe(value: str) -> str:
    return _UNSAFE.sub("_", value)


def variant_filename(feed_slug: str, leaf: OpenAIConfig | GeminiConfig) -> str:
    return (
        "--".join(
            (
                _safe(feed_slug),
                _safe(leaf.provider),
                _safe(leaf.model),
                _safe(leaf.voice),
            )
        )
        + ".mp3"
    )


# --- manifest reading (never raises) ---------------------------------------


def _read_json(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _verify_summary(manifest: dict | None) -> list[dict] | None:
    """Per chunk: attempts, the last attempt's ASR verdict (its outcome when ASR
    never ran) and recall. ``None`` for OpenAI or anything malformed."""
    try:
        phase = manifest.get("gemini_phase") if manifest else None
        if not isinstance(phase, dict):
            return None
        chunks = phase["chunks"]
        if not isinstance(chunks, list):
            return None
        out = []
        for chunk in chunks:
            attempts = chunk["attempts"]
            if not isinstance(attempts, list) or not attempts:
                return None
            last = attempts[-1]
            asr = last.get("asr")
            if isinstance(asr, dict) and isinstance(asr.get("status"), str):
                verdict = asr["status"]
                recall = _number(asr.get("recall"))
            else:
                verdict = last["outcome"]
                recall = None
            if not isinstance(verdict, str):
                return None
            out.append(
                {
                    "index": chunk["index"],
                    "attempts": len(attempts),
                    "verdict": verdict,
                    "recall": recall,
                }
            )
        return out
    except Exception:  # noqa: BLE001 -- a summary must never raise
        return None


def _tokens_summary(manifest: dict | None) -> dict | None:
    phase = manifest.get("gemini_phase") if manifest else None
    if not isinstance(phase, dict):
        return None
    tokens = phase.get("tokens")
    return tokens if isinstance(tokens, dict) else None


def _manifest_files(manifest_dir: Path) -> set[Path]:
    try:
        return set(manifest_dir.rglob("*.json"))
    except OSError:
        return set()


def _newest(paths) -> Path | None:
    def key(p: Path):
        try:
            return (p.stat().st_mtime_ns, p.name)
        except OSError:
            return (0, p.name)

    return max(paths, key=key, default=None)


# --- running ---------------------------------------------------------------


def _write_summary(path: Path, summary: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _error_text(exc: BaseException) -> str:
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001
        message = "<unprintable>"
    if not isinstance(exc, TTSRenderError):
        message = f"{type(exc).__name__}: {message}"
    return message[:MAX_ERROR_CHARS]


def _mmss(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def format_result_line(entry: dict) -> str:
    """One stdout line for a finished variant."""
    if entry["status"] == "ok":
        verify = entry["verify"]
        if verify is None:
            verdicts = "n/a"
        else:
            verdicts = ",".join(v["verdict"] for v in verify) or "-"
        return (
            f"OK      {entry['file']}  {_mmss(entry['audio_seconds'])}  "
            f"{entry['wall_seconds']:.0f}s  verify: {verdicts}"
        )
    req = entry["requested"]
    error = " ".join(str(entry["error"]).split())
    return f"FAILED  {req['provider']}/{req['model']}/{req['voice']}  {error}"


def run_audition(
    text: str,
    variants: list[Variant],
    out_dir: Path,
    *,
    feed_slug: str,
    style: str,
    force: bool = False,
    render: Callable | None = None,
    echo: Callable[[str], Any] = print,
) -> dict:
    """Render ``text`` once per variant into ``out_dir``; return the summary dict.

    ``ValueError`` (before anything is rendered) when two variants would write the
    same file, or when a target or ``summary.json`` exists and ``force`` is false.
    A variant that fails is recorded and the run continues; a failed variant never
    leaves an mp3 behind. ``KeyboardInterrupt``/``SystemExit`` propagate.
    """
    if render is None:
        from pipeline.tts import render as render_module

        render = render_module.render_episode  # looked up now so tests can patch it

    out_dir = Path(out_dir)
    requested_names = [variant_filename(feed_slug, v.requested) for v in variants]
    if len(set(requested_names)) != len(requested_names):
        dupes = sorted({n for n in requested_names if requested_names.count(n) > 1})
        raise ValueError(f"two variants would write the same file: {', '.join(dupes)}")
    summary_path = out_dir / "summary.json"
    if not force:
        existing = [
            name
            for name in (*requested_names, summary_path.name)
            if (out_dir / name).exists()
        ]
        if existing:
            raise ValueError(
                f"refusing to overwrite existing file(s) in {out_dir}: "
                f"{', '.join(existing)} (use --force)"
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = out_dir / "manifests"
    manifest_dir.mkdir(exist_ok=True)
    (out_dir / "script.txt").write_text(text, encoding="utf-8")

    started = datetime.now(UTC)
    episode_id = f"audition-{started:%Y%m%dT%H%M%S}"
    summary: dict = {
        "schema": SUMMARY_SCHEMA,
        "feed": feed_slug,
        "style": style,
        "episode_id": episode_id,
        "script_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "script_chars": len(text),
        "started_at": started.isoformat(),
        "variants": [],
    }
    _write_summary(summary_path, summary)

    for index, (variant, name) in enumerate(
        zip(variants, requested_names, strict=True)
    ):
        entry = _render_variant(
            text,
            variant,
            name,
            index,
            out_dir=out_dir,
            manifest_dir=manifest_dir,
            feed_slug=feed_slug,
            episode_id=episode_id,
            force=force,
            render=render,
        )
        summary["variants"].append(entry)
        _write_summary(summary_path, summary)
        echo(format_result_line(entry))
    return summary


def _render_variant(
    text: str,
    variant: Variant,
    name: str,
    index: int,
    *,
    out_dir: Path,
    manifest_dir: Path,
    feed_slug: str,
    episode_id: str,
    force: bool,
    render: Callable,
) -> dict:
    requested = variant.requested
    partial = out_dir / f".partial-{index}.mp3"
    target = out_dir / name
    entry: dict = {
        "label": None,
        "requested": asdict(requested),
        "rendered": None,
        "status": "failed",
        "error": None,
        "file": None,
        "audio_seconds": None,
        "wall_seconds": 0.0,
        "chunks": None,
        "manifest": None,
        "verify": None,
        "tokens": None,
    }
    config = RenderConfig(requested, None)  # no fallback: a failure stays a failure
    before = _manifest_files(manifest_dir)
    result = None
    started = time.monotonic()
    with contextlib.suppress(FileNotFoundError):
        partial.unlink()
    try:
        try:
            result = render(
                text,
                config,
                partial,
                feed_slug=feed_slug,
                episode_id=episode_id,
                manifest_dir=manifest_dir,
                cache_dir=None,
                notify_fallback=False,
            )
            entry["rendered"] = asdict(result.rendered)
            if result.rendered != requested:
                entry["error"] = (
                    f"rendered_mismatch: requested {asdict(requested)}, "
                    f"rendered {asdict(result.rendered)}"
                )
            else:
                # The label is earned by what rendered, not what was asked for.
                name = variant_filename(feed_slug, result.rendered)
                target = out_dir / name
                os.replace(partial, target)
                entry.update(
                    status="ok",
                    label=target.stem,
                    file=name,
                    chunks=result.chunks,
                )
        except Exception as exc:  # noqa: BLE001 -- one variant must not end the run
            entry["status"] = "failed"
            entry["file"] = None
            entry["label"] = None
            entry["error"] = _error_text(exc)
    finally:
        entry["wall_seconds"] = round(time.monotonic() - started, 3)
        with contextlib.suppress(OSError):
            partial.unlink()
        if entry["status"] != "ok" and force:
            # Never let an old file stand in for a variant that just failed.
            with contextlib.suppress(OSError):
                target.unlink()

    manifest_path = result.manifest_path if result is not None else None
    if manifest_path is None:
        manifest_path = _newest(_manifest_files(manifest_dir) - before)
    manifest = _read_json(manifest_path)
    if manifest_path is not None and manifest is not None:
        with contextlib.suppress(ValueError):
            entry["manifest"] = manifest_path.relative_to(out_dir).as_posix()
    entry["verify"] = _verify_summary(manifest)
    entry["tokens"] = _tokens_summary(manifest)
    if entry["status"] == "ok":
        entry["audio_seconds"] = _number((manifest or {}).get("total_audio_seconds"))
    return entry
