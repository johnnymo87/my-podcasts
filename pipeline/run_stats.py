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
# "filtered" means Exa returned results but every one was rejected locally as
# the paywalled origin or a bypass mirror. It is deliberately distinct from
# "empty" (Exa found nothing), because the two have completely different
# causes and only one of them indicates the deny-list is doing work.
_EXA_BUCKETS = ("hit", "empty", "filtered", "no_key", "error")

# Buckets a writer_inputs.json entry can classify into. "live"/"paywalled"/
# "http_error"/"fetch_error" come from the tiers.json join (Levine articles,
# which were fetched); "cache" is a Semafor/Zvi path (copied, never fetched,
# so it has no tiers.json entry); "exa" is Exa-enriched text under
# enrichment/exa/ (also never fetched via tiers.json); "unknown" is the
# catch-all for anything that doesn't match a known shape (e.g. a work dir
# with no tiers.json to join against at all).
_WRITE_TIERS = (
    "live",
    "paywalled",
    "http_error",
    "fetch_error",
    "cache",
    "exa",
    "unknown",
)

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
    # Candidates routed away before further work (currently Semafor's
    # fp/skip routing filter, which sits between the candidates count and
    # the dedup check). Zero for sources with no such filter. Keeping this
    # separate from `deduped` preserves the distinction between "already
    # covered" and "not ours to cover" while still letting a reader account
    # for every candidate: candidates - routed_away - deduped.
    routed_away: dict[str, int] = Field(default_factory=dict)
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

    # Writer inputs that had open-access coverage appended to a stub
    # (consumer.py's exa_appended/exa_chars fields), and the total
    # characters of that appended text. Both zero on historical work dirs
    # written before this feature (writer_inputs.json entries with no
    # exa_appended key at all).
    writer_exa_appended: int = 0
    writer_exa_chars: int = 0

    # Entries where `reached_prompt is False` but `chars > 0` -- text was
    # resolved (has length) yet never landed in a rendered section. Per
    # consumer.py's `_assemble_writer_inputs`, `reached_prompt` is derived
    # from membership in the sections actually built, NOT from `bool(text)`
    # (that earlier form was tautological against `chars` and could never
    # fire). So this is a genuine cross-check: it goes non-zero if section
    # assembly ever loses a story that had text. Expect 0 on every healthy
    # run -- a regression canary, not a routine statistic. Uses
    # `is False`, never truthiness, so a missing key (historical
    # writer_inputs.json predating this field) reads as "unknown", not
    # "dropped" -- see the `exa_appended is True` precedent above.
    writer_dropped_before_prompt: int = 0

    # Histogram of `miss_reason` values (see find_rundown_article_source's
    # docstring for the taxonomy). Only entries carrying a real string
    # `miss_reason` are counted; a missing key (historical data predating
    # the field) is skipped rather than bucketed as "unknown", since we
    # have no evidence to attribute -- it simply does not appear here.
    writer_miss_reasons: dict[str, int] = Field(default_factory=dict)

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
    stats.routed_away = _int_counts(
        sentinel.get("routed_away"), ("levine", "semafor", "zvi")
    )

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
    writer_exa_appended = 0
    writer_exa_chars = 0
    dropped_before_prompt = 0
    miss_reasons: dict[str, int] = {}
    for item in writer_inputs:
        if not isinstance(item, dict):
            write_buckets["unknown"] = write_buckets.get("unknown", 0) + 1
            selected += 1
            continue
        selected += 1

        appended = item.get("exa_appended") is True
        if appended:
            writer_exa_appended += 1
            exa_chars = item.get("exa_chars")
            if isinstance(exa_chars, int) and not isinstance(exa_chars, bool):
                writer_exa_chars += exa_chars

        source_path = item.get("source_path")
        chars = item.get("chars")
        has_text = isinstance(chars, int) and not isinstance(chars, bool) and chars > 0

        # `is False`, never truthiness: a missing `reached_prompt` key
        # (historical writer_inputs.json) must read as "unknown", not
        # "dropped", or every historical work dir false-alarms.
        if item.get("reached_prompt") is False and has_text:
            dropped_before_prompt += 1

        miss_reason = item.get("miss_reason")
        if isinstance(miss_reason, str) and miss_reason:
            miss_reasons[miss_reason] = miss_reasons.get(miss_reason, 0) + 1

        if source_path is None:
            dropped += 1
            continue
        if not isinstance(source_path, str):
            key = "unknown+exa" if appended else "unknown"
            write_buckets[key] = write_buckets.get(key, 0) + 1
            if has_text:
                with_text += 1
            continue

        tier_entry = tiers.get(source_path)
        if isinstance(tier_entry, dict) and isinstance(tier_entry.get("tier"), str):
            tier = tier_entry["tier"]
            bucket = tier if tier in _FETCH_TIERS else "unknown"
        elif source_path.startswith("articles/semafor/") or source_path.startswith(
            "articles/zvi/"
        ):
            bucket = "cache"
        elif source_path.startswith("enrichment/exa/"):
            bucket = "exa"
        else:
            bucket = "unknown"
        key = f"{bucket}+exa" if appended else bucket
        write_buckets[key] = write_buckets.get(key, 0) + 1

        if has_text:
            with_text += 1

    stats.writer_selected = selected
    stats.writer_with_text = with_text
    stats.writer_dropped = dropped
    stats.writer_buckets = write_buckets
    stats.writer_exa_appended = writer_exa_appended
    stats.writer_exa_chars = writer_exa_chars
    stats.writer_dropped_before_prompt = dropped_before_prompt
    stats.writer_miss_reasons = miss_reasons

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

    date_token = f" {stats.date_str}" if stats.date_str else ""
    header = f"The Rundown{date_token} (job {stats.job_id}) - script stage"
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

    route_total = sum(stats.routed_away.values())
    if route_total:
        route_breakdown = ", ".join(
            f"{k} {v}" for k, v in stats.routed_away.items() if v
        )
        lines.append(f"ROUTE  -{route_total} ({route_breakdown}, fp/skip)")

    dedup_total = sum(stats.deduped.values())
    dedup_breakdown = ", ".join(f"{k} {v}" for k, v in stats.deduped.items() if v)
    dedup_sign = "-" if dedup_total else ""
    dedup_line = f"DEDUP  {dedup_sign}{dedup_total}"
    if dedup_breakdown:
        dedup_line += f" ({dedup_breakdown})"
    lines.append(dedup_line)

    fetch_count = stats.levine_articles
    if fetch_count is None:
        fetch_count = sum(stats.fetch_tiers.values())
    fetch_breakdown = ", ".join(f"{k} {v}" for k, v in stats.fetch_tiers.items() if v)
    fetch_line = f"FETCH  levine {fetch_count}"
    if fetch_breakdown:
        fetch_line += f": {fetch_breakdown}"
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
    if stats.writer_exa_appended:
        write_line += f", {stats.writer_exa_appended} +open-access"
    # Regression canary, not a routine statistic -- see RunStats docstring.
    # Only ever surfaced when non-zero, so a healthy run's report carries no
    # noise and this cannot cry wolf on the common case.
    if stats.writer_dropped_before_prompt:
        write_line += f", {stats.writer_dropped_before_prompt} DROPPED-AFTER-RESOLVE(!)"
    miss_breakdown = ", ".join(
        f"{k} {v}" for k, v in stats.writer_miss_reasons.items() if v
    )
    if miss_breakdown:
        write_line += f" [misses: {miss_breakdown}]"
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
