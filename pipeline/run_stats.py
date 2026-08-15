"""Reconstruct the Rundown content-acquisition funnel from a work dir.

``collect_run_stats`` is a pure function of a directory: give it a work dir
and it returns a ``RunStats``, never an exception. This matters because most
work dirs on disk today are partial or empty -- a job that failed before
collection finished, or one that predates one of the sentinels this module
reads. Every read below is wrapped so a missing, malformed, or wrong-shaped
file yields a default instead of propagating.

See docs/plans/2026-08-15-rundown-observability-design.md ("Piece 2 -- The
funnel") for the message shape this renders and the reasoning behind each
join.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field


# Fetch-tier buckets a Levine article can land in, per article_fetcher.py.
_FETCH_TIERS = ("live", "paywalled", "http_error", "fetch_error")

# Outcome buckets for an Exa search, per exa_client.search_related_status.
# "error:{ExcClass}" collapses into "error"; anything unrecognized -> "other".
_EXA_BUCKETS = ("hit", "empty", "no_key", "error")

# Buckets a writer_inputs.json entry can classify into. "live"/"paywalled"/
# "http_error"/"fetch_error" come from the tiers.json join (Levine articles,
# which were fetched); "cache" is a Semafor/Zvi path (copied, never fetched,
# so it has no tiers.json entry); "unknown" is the catch-all for anything
# that doesn't match a known shape (e.g. an Exa-sourced path, or a work dir
# with no tiers.json to join against at all).
_WRITE_TIERS = ("live", "paywalled", "http_error", "fetch_error", "cache", "unknown")

_TOP_DOMAINS = 8
_MAX_REPORT_CHARS = 4000


class RunStats(BaseModel):
    """Funnel counts for one Rundown run, derived from its work dir."""

    job_id: str
    date_str: str
    reused_collection: bool = False

    # From collection_done.json. lookback_days is reported as written by the
    # collector, never recomputed -- on a retry with reused collection the
    # sentinel is the only record of what lookback was actually used.
    collect_started_at: str | None = None
    collect_completed_at: str | None = None
    collect_duration_seconds: float | None = None
    lookback_days: int | None = None
    candidates: dict[str, int] = Field(default_factory=dict)
    deduped: dict[str, int] = Field(default_factory=dict)
    levine_articles: int | None = None

    # From tiers.json (Levine articles only -- Semafor/Zvi are cache copies
    # and never get a tiers.json entry).
    fetch_tiers: dict[str, int] = Field(default_factory=dict)

    # From plan.json.
    directives_total: int = 0
    directives_episode: int = 0
    directives_fp_routed: int = 0
    themes_count: int = 0

    # From collection_done.json's exa_outcomes.
    exa_flagged: int = 0
    exa_outcomes: dict[str, int] = Field(default_factory=dict)

    # From writer_inputs.json, joined against tiers.json.
    writer_selected: int = 0
    writer_with_text: int = 0
    writer_dropped: int = 0
    writer_buckets: dict[str, int] = Field(default_factory=dict)

    # From script.txt / covered.json.
    script_words: int | None = None
    covered_headlines: int | None = None

    # Top-8 host histogram over tiers.json entries whose tier is "paywalled",
    # host taken from the entry's url with a leading "www." stripped.
    paywalled_domains: list[tuple[str, int]] = Field(default_factory=list)


def _read_json_dict(path: Path) -> dict:
    """Read a JSON file expected to hold an object. Never raises.

    Missing file, unreadable file, invalid JSON, or JSON that decodes to
    something other than a dict (e.g. a bare list) all yield {}.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_json_list(path: Path) -> list:
    """Read a JSON file expected to hold an array. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _int_counts(raw: object, keys: tuple[str, ...]) -> dict[str, int]:
    """Coerce ``raw`` into ``{key: int}`` for the given keys, defaulting to 0.

    ``raw`` may not even be a dict (e.g. a stale/malformed sentinel); any
    per-key value that is not int-like is dropped to 0 rather than raising.
    """
    out = dict.fromkeys(keys, 0)
    if not isinstance(raw, dict):
        return out
    for k in keys:
        v = raw.get(k, 0)
        if isinstance(v, bool):
            continue  # bool is an int subclass; not a legitimate count
        if isinstance(v, int):
            out[k] = v
    return out


def _duration_seconds(started_at: object, completed_at: object) -> float | None:
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at)
        return (end - start).total_seconds()
    except Exception:
        return None


def _domain(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        host = urlparse(url).netloc
    except Exception:
        return None
    host = host.split("@")[-1].split(":")[0]  # strip userinfo and port
    if host.startswith("www."):
        host = host[4:]
    return host or None


def collect_run_stats(
    work_dir: Path,
    job_id: str,
    date_str: str,
    reused_collection: bool = False,
) -> RunStats:
    """Reconstruct funnel counts from ``work_dir``. Never raises.

    ``work_dir`` may not exist, may be a file rather than a directory, and
    every artifact inside it may be missing or malformed -- all of that is
    the common case across real work dirs, not an edge case, so every read
    degrades to a default instead of propagating.
    """
    stats = RunStats(
        job_id=job_id, date_str=date_str, reused_collection=reused_collection
    )

    if not work_dir.is_dir():
        return stats

    # --- collection_done.json ---------------------------------------
    sentinel = _read_json_dict(work_dir / "collection_done.json")
    started_at = sentinel.get("started_at")
    completed_at = sentinel.get("completed_at")
    stats.collect_started_at = started_at if isinstance(started_at, str) else None
    stats.collect_completed_at = completed_at if isinstance(completed_at, str) else None
    stats.collect_duration_seconds = _duration_seconds(started_at, completed_at)

    lookback = sentinel.get("lookback_days")
    stats.lookback_days = (
        lookback
        if isinstance(lookback, int) and not isinstance(lookback, bool)
        else None
    )

    stats.candidates = _int_counts(
        sentinel.get("candidates"), ("levine", "semafor", "zvi")
    )
    stats.deduped = _int_counts(sentinel.get("deduped"), ("levine", "semafor", "zvi"))

    levine_articles = sentinel.get("levine_articles")
    stats.levine_articles = (
        levine_articles
        if isinstance(levine_articles, int) and not isinstance(levine_articles, bool)
        else None
    )

    exa_outcomes_raw = sentinel.get("exa_outcomes")
    exa_counts = dict.fromkeys(_EXA_BUCKETS, 0)
    exa_counts["other"] = 0
    flagged = 0
    if isinstance(exa_outcomes_raw, dict):
        for status in exa_outcomes_raw.values():
            if not isinstance(status, str):
                exa_counts["other"] += 1
                flagged += 1
                continue
            flagged += 1
            if status.startswith("error"):
                exa_counts["error"] += 1
            elif status in ("hit", "empty", "no_key"):
                exa_counts[status] += 1
            else:
                exa_counts["other"] += 1
    stats.exa_flagged = flagged
    stats.exa_outcomes = {k: v for k, v in exa_counts.items() if v or k in _EXA_BUCKETS}

    # --- tiers.json ----------------------------------------------------
    tiers_raw = _read_json_dict(work_dir / "tiers.json")
    tiers: dict[str, dict] = {
        path: entry for path, entry in tiers_raw.items() if isinstance(entry, dict)
    }

    fetch_tiers = dict.fromkeys(_FETCH_TIERS, 0)
    fetch_unknown = 0
    domain_counts: dict[str, int] = {}
    for entry in tiers.values():
        tier = entry.get("tier")
        if isinstance(tier, str) and tier in _FETCH_TIERS:
            fetch_tiers[tier] += 1
        else:
            fetch_unknown += 1
        if tier == "paywalled":
            domain = _domain(entry.get("url"))
            if domain:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1

    # Reconcile against the sentinel's levine_articles count when available:
    # any Levine article the sentinel counted but tiers.json has no entry
    # for (missing tiers.json entirely, or a partial/pre-instrumentation
    # sidecar) is real fetch activity we cannot classify, not zero activity.
    known_total = sum(fetch_tiers.values()) + fetch_unknown
    if stats.levine_articles is not None and stats.levine_articles > known_total:
        fetch_unknown += stats.levine_articles - known_total
    if fetch_unknown:
        fetch_tiers["unknown"] = fetch_unknown
    stats.fetch_tiers = fetch_tiers

    stats.paywalled_domains = sorted(
        domain_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )[:_TOP_DOMAINS]

    # --- plan.json -------------------------------------------------------
    plan = _read_json_dict(work_dir / "plan.json")
    directives = plan.get("directives")
    directives = directives if isinstance(directives, list) else []
    directives = [d for d in directives if isinstance(d, dict)]
    themes = plan.get("themes")
    stats.themes_count = len(themes) if isinstance(themes, list) else 0
    stats.directives_total = len(directives)
    stats.directives_episode = sum(
        1 for d in directives if d.get("include_in_episode") is True
    )
    stats.directives_fp_routed = sum(
        1 for d in directives if d.get("is_foreign_policy") is True
    )

    # --- writer_inputs.json, joined against tiers.json --------------------
    writer_inputs = _read_json_list(work_dir / "writer_inputs.json")
    write_buckets = dict.fromkeys(_WRITE_TIERS, 0)
    selected = 0
    with_text = 0
    dropped = 0
    for item in writer_inputs:
        if not isinstance(item, dict):
            write_buckets["unknown"] += 1
            selected += 1
            continue
        selected += 1
        source_path = item.get("source_path")
        chars = item.get("chars")
        has_text = isinstance(chars, int) and not isinstance(chars, bool) and chars > 0

        if source_path is None:
            dropped += 1
            continue
        if not isinstance(source_path, str):
            write_buckets["unknown"] += 1
            if has_text:
                with_text += 1
            continue

        tier_entry = tiers.get(source_path)
        if isinstance(tier_entry, dict) and isinstance(tier_entry.get("tier"), str):
            tier = tier_entry["tier"]
            if tier in _FETCH_TIERS:
                write_buckets[tier] += 1
            else:
                write_buckets["unknown"] += 1
        elif source_path.startswith("articles/semafor/") or source_path.startswith(
            "articles/zvi/"
        ):
            write_buckets["cache"] += 1
        else:
            write_buckets["unknown"] += 1

        if has_text:
            with_text += 1

    stats.writer_selected = selected
    stats.writer_with_text = with_text
    stats.writer_dropped = dropped
    stats.writer_buckets = write_buckets

    # --- script.txt / covered.json ----------------------------------------
    script_path = work_dir / "script.txt"
    if script_path.is_file():
        try:
            stats.script_words = len(script_path.read_text(encoding="utf-8").split())
        except Exception:
            stats.script_words = None

    covered = _read_json_list(work_dir / "covered.json")
    if (work_dir / "covered.json").is_file():
        stats.covered_headlines = len(covered)

    return stats


def _fmt_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def render_report(stats: RunStats) -> str:
    """Render ``stats`` as the plain-text funnel report.

    Plain text only -- the delivery endpoint sends no ``parse_mode``, so any
    markdown would render as literal asterisks/backticks. Bounded to stay
    under Telegram's 4096-character limit (the domain histogram is
    top-8, and a defensive truncation catches anything else pathological).
    """
    lines: list[str] = []

    header = f"The Rundown {stats.date_str} (job {stats.job_id}) - script stage"
    duration = _fmt_duration(stats.collect_duration_seconds)
    tail_bits = []
    if duration:
        tail_bits.append(f"collect {duration}")
    if stats.lookback_days is not None:
        tail_bits.append(f"lookback {stats.lookback_days}d")
    if stats.reused_collection:
        tail_bits.append("reused collection")
    if tail_bits:
        header += " - " + ", ".join(tail_bits)
    lines.append(header)
    lines.append("")

    in_total = sum(stats.candidates.values())
    in_parts = ", ".join(f"{k} {v}" for k, v in stats.candidates.items())
    lines.append(f"IN     {in_total} = {in_parts}")

    dedup_total = sum(stats.deduped.values())
    dedup_breakdown = ", ".join(f"{k} {v}" for k, v in stats.deduped.items() if v)
    dedup_line = f"DEDUP  -{dedup_total}"
    if dedup_breakdown:
        dedup_line += f" ({dedup_breakdown})"
    lines.append(dedup_line)

    fetch_count = stats.levine_articles
    if fetch_count is None:
        fetch_count = sum(stats.fetch_tiers.values())
    fetch_breakdown = ", ".join(f"{k} {v}" for k, v in stats.fetch_tiers.items() if v)
    fetch_line = f"FETCH  levine {fetch_count}:"
    if fetch_breakdown:
        fetch_line += f" {fetch_breakdown}"
    lines.append(fetch_line)

    lines.append(
        f"PLAN   {stats.directives_total} directives = "
        f"{stats.directives_episode} episode, {stats.directives_fp_routed} fp-routed"
    )

    exa_breakdown = ", ".join(f"{v} {k}" for k, v in stats.exa_outcomes.items() if v)
    exa_line = f"EXA    {stats.exa_flagged} flagged"
    if exa_breakdown:
        exa_line += f" -> {exa_breakdown}"
    lines.append(exa_line)

    write_breakdown = ", ".join(
        f"{v} {k}" for k, v in stats.writer_buckets.items() if v
    )
    write_line = (
        f"WRITE  {stats.writer_selected} selected -> {stats.writer_with_text} with text"
    )
    if write_breakdown:
        write_line += f" ({write_breakdown})"
    write_line += f", {stats.writer_dropped} dropped"
    lines.append(write_line)

    out_parts = []
    if stats.script_words is not None:
        out_parts.append(f"{stats.script_words} words")
    out_parts.append(f"{stats.themes_count} themes")
    if stats.covered_headlines is not None:
        out_parts.append(f"{stats.covered_headlines} headlines covered")
    lines.append(f"OUT    {', '.join(out_parts)}")

    if stats.paywalled_domains:
        lines.append("")
        histogram = ", ".join(
            f"{domain} {count}" for domain, count in stats.paywalled_domains
        )
        lines.append(f"paywalled: {histogram}")

    report = "\n".join(lines)
    if len(report) > _MAX_REPORT_CHARS:
        report = report[: _MAX_REPORT_CHARS - 3] + "..."
    return report


def append_jsonl(stats: RunStats, path: Path) -> None:
    """Append one JSON line for ``stats`` to ``path``.

    ``path`` is always caller-supplied (production passes
    /persist/my-podcasts/run-stats.jsonl) so tests can point this at
    tmp_path. Creates the parent directory if absent. A single
    open(..., "a") plus one write() call is atomic enough that a crash
    mid-write cannot corrupt a prior line -- it can only truncate the new
    one, which a downstream JSONL reader already has to tolerate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(stats.model_dump_json() + "\n")
