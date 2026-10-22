from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pipeline.article_fetcher import Article, fetch_all_articles
from pipeline.article_resolver import resolve_headline
from pipeline.article_resolver import slugify as _slugify
from pipeline.exa_client import exa_file_path, search_related_status
from pipeline.freshness import (
    annotate_headlines,
    classify_headlines,
    format_coverage_ledger,
)
from pipeline.rss_sources import semafor_routing
from pipeline.things_happen_editor import generate_rundown_research_plan
from pipeline.things_happen_extractor import resolve_redirect_url
from pipeline.zvi_cache import sync_zvi_cache


# Paywall-circumvention mirrors and front-ends. Never cite these: the show
# republishes what it reads, and laundering a paywalled article through an
# archive mirror is materially different from citing an outlet that published
# openly. archive.today rotates TLDs, so all known ones are listed.
BYPASS_DOMAINS = (
    "archive.ph",
    "archive.is",
    "archive.today",
    "archive.md",
    "archive.li",
    "archive.vn",
    "archive.fo",
    "12ft.io",
    "1ft.io",
    "freedium.cfd",
    "removepaywall.com",
)


def _host_banned(url: str, origin: str) -> bool:
    """True if a result URL is a bypass mirror or the paywalled origin itself.

    Suffix matching, so a subdomain (news.archive.ph) cannot slip through.
    `exclude_domains` is a request parameter honored by a third-party API;
    ethics policy must not depend on Exa's compliance, so results are
    filtered locally too.
    """
    host = (urlparse(url).hostname or "").removeprefix("www.").lower()
    if not host:
        return True
    if origin and (host == origin or host.endswith("." + origin)):
        return True
    return any(host == b or host.endswith("." + b) for b in BYPASS_DOMAINS)


# Levine fetch phase, persisted per work dir so a retry does not repeat it.
#
# Every retry of a failed job re-runs collection (collection_done.json is written
# last). Since the editors now raise on a Gemini error instead of persisting an
# empty plan, a 12 h outage means up to ~51 re-collections -- and without reuse,
# each one HEAD-resolves and GETs every Levine link (1 s apart) on the single
# consumer thread, thousands of requests at Bloomberg/WSJ/FT. (The same idea as
# fp_collector._prior_fetched_body.)
LEVINE_FETCH_FILE = "levine_fetch.json"
# Tiers that mean "the network failed", so a later attempt may do better. Every
# other tier (live, paywalled, ...) is a measurement of the page: re-asking
# returns the same answer.
_TRANSIENT_TIERS = frozenset({"http_error", "fetch_error"})


def _load_levine_fetch(work_dir: Path) -> dict[str, dict]:
    """Prior attempt's fetch results keyed by raw_url; ``{}`` if absent or unusable."""
    path = work_dir / LEVINE_FETCH_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 -- a bad cache file must never fail collection
        print(f"[collector] WARNING: ignoring unreadable {path.name}: {e}")
        return {}
    if not isinstance(data, dict):
        print(f"[collector] WARNING: ignoring {path.name}: not a JSON object")
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _reusable_resolved_url(entry: dict | None) -> str | None:
    """The resolved URL of an entry that does not need re-resolving.

    That is a fully fetched non-transient entry, or a resolved-only entry (a link
    that was deduped before fetching). A transient failure is re-resolved too:
    the redirect lookup is a network call that may be what failed.
    """
    if not entry:
        return None
    resolved = entry.get("resolved_url")
    if not isinstance(resolved, str) or not resolved:
        return None
    tier = entry.get("source_tier")
    if tier is not None and (not isinstance(tier, str) or tier in _TRANSIENT_TIERS):
        return None
    return resolved


def _reusable_article(entry: dict | None, headline: str | None) -> Article | None:
    """The prior attempt's Article for this link, if it can be trusted as-is.

    Requires a complete entry with a non-transient tier and the same headline
    (a paywalled article's content *is* its headline, so a changed headline
    invalidates it).
    """
    if _reusable_resolved_url(entry) is None or entry is None:
        return None
    url = entry.get("url")
    content = entry.get("content")
    tier = entry.get("source_tier")
    chars = entry.get("extracted_chars")
    if (
        not isinstance(tier, str)
        or not isinstance(url, str)
        or not isinstance(content, str)
        or not isinstance(chars, int)
        or isinstance(chars, bool)
        or entry.get("headline") != headline
    ):
        return None
    return Article(
        headline=entry["headline"],
        url=url,
        content=content,
        source_tier=tier,
        extracted_chars=chars,
    )


def _write_levine_fetch(work_dir: Path, record: dict[str, dict]) -> None:
    """Atomically persist ``record`` (tmp file + ``os.replace``).

    A torn write must not be able to hand the next attempt half an article, and
    a failed write only costs the retry its reuse, so it never fails collection.
    """
    path = work_dir / LEVINE_FETCH_FILE
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[collector] WARNING: could not persist {path.name}: {e}")


def collect_all_artifacts(
    job_id: str,
    work_dir: Path,
    levine_cache_dir: Path | None = None,
    scripts_source_dir: Path | None = None,
    fp_routed_dir: Path | None = None,
    zvi_cache_dir: Path | None = None,
    semafor_cache_dir: Path | None = None,
    lookback_days: int = 2,
    coverage_summary: list[dict] | None = None,
    prior_urls: set[str] | None = None,
) -> None:
    """
    Phase 1: Fetch articles and setup directories.
    Phase 2: Generate research plan via Editor AI.
    Phase 2b: Route FP-flagged directives to fp_routed_dir (if configured).
    Phase 3: Execute deep enrichment on non-FP directives only.
    """
    # Create directory structure
    articles_dir = work_dir / "articles"
    enrichment_dir = work_dir / "enrichment"
    exa_dir = enrichment_dir / "exa"
    context_dir = work_dir / "context"

    for d in (articles_dir, exa_dir, context_dir):
        d.mkdir(parents=True, exist_ok=True)

    _et = ZoneInfo("America/New_York")
    started_at = datetime.now(tz=_et).isoformat()
    lookback_dates = set()
    for i in range(lookback_days):
        d = (datetime.now(tz=_et) - timedelta(days=i)).strftime("%Y-%m-%d")
        lookback_dates.add(d)

    # Phase 1: Base Collection — read Levine links from cache
    _levine_cache = levine_cache_dir or Path("/persist/my-podcasts/levine-cache")
    links_raw: list[dict] = []
    if _levine_cache.exists():
        for cached in sorted(_levine_cache.glob("*.json")):
            date_stem = cached.stem  # e.g. "2026-03-09"
            if date_stem not in lookback_dates:
                continue
            try:
                links_raw.extend(json.loads(cached.read_text(encoding="utf-8")))
            except Exception as e:
                print(f"[collector] WARNING: Failed to read Levine cache {cached}: {e}")
    else:
        print(f"[collector] WARNING: Levine cache not found at {_levine_cache}")

    _prior = prior_urls or set()

    if links_raw:
        prior_fetch = _load_levine_fetch(work_dir)
        # raw_url -> Article reused from a prior attempt (no network at all).
        reused: dict[str, Article] = {}
        for link in links_raw:
            entry = prior_fetch.get(link["raw_url"])
            cached = _reusable_article(entry, link.get("headline_context"))
            if cached is not None:
                reused[link["raw_url"]] = cached
            resolved = _reusable_resolved_url(entry)
            link["resolved_url"] = (
                resolved if resolved else resolve_redirect_url(link["raw_url"])
            )
        # Dedup before fetching. The key is available pre-fetch, and every
        # skipped article we fetch anyway costs an HTTP round trip plus a
        # one-second politeness delay for a result we then throw away.
        levine_candidates = len(links_raw)
        all_links = links_raw
        links_raw = [
            link for link in links_raw if link.get("resolved_url") not in _prior
        ]
        levine_deduped = levine_candidates - len(links_raw)
        if not reused:
            articles = fetch_all_articles(links_raw, delay_between=1.0)
        else:
            to_fetch = [link for link in links_raw if link["raw_url"] not in reused]
            fetched = iter(
                fetch_all_articles(to_fetch, delay_between=1.0) if to_fetch else []
            )
            articles = [
                reused[link["raw_url"]] if link["raw_url"] in reused else next(fetched)
                for link in links_raw
            ]

        # Persist before the editor runs: the editor is the step that fails on a
        # Gemini outage, and the next attempt must not repeat this phase.
        record = dict(prior_fetch)
        kept = {id(link) for link in links_raw}
        for link in all_links:
            if id(link) not in kept:  # deduped: remember only where it resolved
                record[link["raw_url"]] = {
                    "resolved_url": link["resolved_url"],
                    "headline": link.get("headline_context"),
                }
        if len(articles) == len(links_raw):
            for link, art in zip(links_raw, articles, strict=True):
                record[link["raw_url"]] = {
                    "resolved_url": link["resolved_url"],
                    "headline": art.headline,
                    "url": art.url,
                    "content": art.content,
                    "source_tier": art.source_tier,
                    "extracted_chars": art.extracted_chars,
                }
        _write_levine_fetch(work_dir, record)
    else:
        levine_candidates = 0
        levine_deduped = 0
        articles = []

    headlines_with_snippets = []
    # Maps the headline text sent to the editor → file path (relative to work_dir)
    headline_index: dict[str, str] = {}
    # Fetch outcome per article, kept OUT of the article markdown: article file
    # contents are passed verbatim into the writer prompt, so a Source-Tier line
    # inside the file would be read by the model as part of the story.
    tiers: dict[str, dict] = {}

    for i, art in enumerate(articles):
        slug = f"{i:02d}-{_slugify(art.headline)}"
        art_path = articles_dir / f"{slug}.md"

        # Write article to disk
        content = f"# {art.headline}\n\nURL: {art.url}\n\n{art.content}"
        art_path.write_text(content, encoding="utf-8")

        rel = str(art_path.relative_to(work_dir))
        tiers[rel] = {
            "tier": art.source_tier,
            "extracted_chars": art.extracted_chars,
            "url": art.url,
        }

        # Build snippet for the Editor AI
        truncated = art.content[:300]
        suffix = "..." if len(art.content) > 300 else ""
        snippet = f"Headline: {art.headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)
        headline_index[art.headline] = rel

    # Phase 1b: Semafor articles from cache (TH categories)
    semafor_dir = articles_dir / "semafor"
    semafor_dir.mkdir(parents=True, exist_ok=True)
    _semafor_cache = semafor_cache_dir or Path("/persist/my-podcasts/semafor-cache")
    semafor_candidates = 0
    semafor_deduped = 0
    semafor_routed_away = 0
    if not _semafor_cache.exists():
        print(f"[collector] WARNING: Semafor cache not found at {_semafor_cache}")
    if _semafor_cache.exists():
        for cached in sorted(_semafor_cache.glob("*.md")):
            if not any(cached.name.startswith(d) for d in lookback_dates):
                continue
            semafor_candidates += 1
            text = cached.read_text(encoding="utf-8")
            lines = text.split("\n")
            headline = " ".join(lines[0].lstrip("# ").split())
            category = ""
            url = ""
            routing = ""
            for line in lines[1:8]:
                if line.startswith("Category: "):
                    category = line[10:].strip()
                elif line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Routing: "):
                    routing = line[9:].strip()
            # Prefer Routing header; fall back to category-based classification
            # when it is absent (legacy file) or still __PENDING__ (the
            # sync-time classification failed and awaits a retry).
            routing = semafor_routing(routing, category)
            if routing not in ("th", "both"):
                # Routed exclusively to FP Digest (or explicitly skipped) --
                # counted as a candidate above, so it must be accounted for
                # here rather than silently vanishing from the funnel.
                semafor_routed_away += 1
                continue
            if url and url in _prior:
                semafor_deduped += 1
                continue
            slug = _slugify(headline)
            art_path = semafor_dir / f"{slug}.md"
            if not art_path.exists():
                art_path.write_text(text, encoding="utf-8")

    # Semafor headlines
    for semafor_path in semafor_dir.glob("*.md"):
        text = semafor_path.read_text(encoding="utf-8")
        parts = text.split("\n\n", 2)
        headline = parts[0].lstrip("# ").strip() if parts else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        truncated = body[:300]
        suffix = "..." if len(body) > 300 else ""
        snippet = f"[semafor] {headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)
        headline_index[headline] = str(semafor_path.relative_to(work_dir))

    # Phase 1c: Zvi articles (day-of posts from persistent cache)
    zvi_cache = (
        zvi_cache_dir
        if zvi_cache_dir is not None
        else Path("/persist/my-podcasts/zvi-cache")
    )
    sync_zvi_cache(zvi_cache)
    zvi_dir = articles_dir / "zvi"
    zvi_dir.mkdir(parents=True, exist_ok=True)
    zvi_candidates = 0
    zvi_deduped = 0
    for cached_file in zvi_cache.glob("*.md"):
        if not any(cached_file.name.startswith(d) for d in lookback_dates):
            continue
        zvi_candidates += 1
        # Extract URL and skip if already used in prior episodes
        if _prior:
            zvi_text = cached_file.read_text(encoding="utf-8")
            zvi_url = ""
            for line in zvi_text.split("\n")[1:8]:
                if line.startswith("URL: "):
                    zvi_url = line[5:].strip()
                    break
            if zvi_url and zvi_url in _prior:
                zvi_deduped += 1
                continue
        target = zvi_dir / cached_file.name
        if not target.exists():
            target.write_text(cached_file.read_text(encoding="utf-8"), encoding="utf-8")

    # Zvi headlines
    for zvi_path in zvi_dir.glob("*.md"):
        text = zvi_path.read_text(encoding="utf-8")
        parts = text.split("\n\n", 2)
        headline = parts[0].lstrip("# ").strip() if parts else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        truncated = body[:300]
        suffix = "..." if len(body) > 300 else ""
        snippet = f"[zvi] {headline}\nContext: {truncated}{suffix}"
        headlines_with_snippets.append(snippet)
        headline_index[headline] = str(zvi_path.relative_to(work_dir))

    # Copy trailing window context (last 3 scripts)
    scripts_dir = (
        scripts_source_dir
        if scripts_source_dir is not None
        else Path("/persist/my-podcasts/scripts/the-rundown")
    )
    if scripts_dir.exists():
        scripts = sorted(scripts_dir.glob("*.txt"), reverse=True)[:3]
        for script in scripts:
            target = context_dir / script.name
            if not target.exists():
                # Read and write instead of symlink to avoid cross-device link issues
                target.write_text(script.read_text(encoding="utf-8"))

    # Write headline index for article lookup
    (work_dir / "headline_index.json").write_text(
        json.dumps(headline_index, indent=2), encoding="utf-8"
    )

    # Fetch-tier sidecar. Deliberately not a header inside the article
    # markdown: article file contents are passed verbatim into the writer
    # prompt, so an in-content Source-Tier line would be read as story text.
    (work_dir / "tiers.json").write_text(json.dumps(tiers, indent=2), encoding="utf-8")

    # Freshness annotation
    coverage_ledger: str | None = None
    if coverage_summary:
        classifications = classify_headlines(headlines_with_snippets, coverage_summary)
        headlines_with_snippets = annotate_headlines(
            headlines_with_snippets, classifications, coverage_summary
        )
        coverage_ledger = format_coverage_ledger(coverage_summary)
    else:
        # Fallback: extract themes from scripts when articles_json unavailable
        context_scripts = [
            p.read_text(encoding="utf-8") for p in sorted(context_dir.glob("*.txt"))
        ]
        if context_scripts:
            from pipeline.freshness import extract_themes_from_scripts

            fallback_coverage = extract_themes_from_scripts(context_scripts)
            if fallback_coverage:
                classifications = classify_headlines(
                    headlines_with_snippets, fallback_coverage
                )
                headlines_with_snippets = annotate_headlines(
                    headlines_with_snippets, classifications, fallback_coverage
                )
                coverage_ledger = format_coverage_ledger(fallback_coverage)

    # Phase 2: Editor AI
    plan = generate_rundown_research_plan(
        headlines_with_snippets,
        coverage_ledger=coverage_ledger,
    )

    # Write plan.json
    plan_path = work_dir / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    # Partition directives: FP links go to staging, non-FP get enriched
    fp_directives = [d for d in plan.directives if d.is_foreign_policy]
    non_fp_directives = [d for d in plan.directives if not d.is_foreign_policy]

    # Write FP directives to the staging directory
    if fp_directives and fp_routed_dir is None:
        print(
            f"[collector] {len(fp_directives)} FP directives not routed (fp_routed_dir not configured)"
        )
    if fp_directives and fp_routed_dir is not None:
        fp_routed_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(tz=_et).strftime("%Y-%m-%d")
        routed_path = fp_routed_dir / f"{today}-{job_id}.json"
        routed_data = []
        for d in fp_directives:
            # Find the matching article for context
            art = next((a for a in articles if a.headline == d.headline), None)
            routed_data.append(
                {
                    "headline": d.headline,
                    "url": art.url if art else "",
                    "snippet": (art.content[:500] if art else ""),
                }
            )
        routed_path.write_text(json.dumps(routed_data, indent=2), encoding="utf-8")

    # Phase 3: Deep Enrichment (non-FP only)
    # The filename must be the bare slug: __main__.find_rundown_article_source
    # and show_notes._find_article_file both look up `{slug}.md` exactly. An
    # index prefix here is how enrichment silently went undelivered for months.
    exa_outcomes: dict[str, str] = {}
    for directive in non_fp_directives:
        if not directive.include_in_episode:
            continue

        # Same join delivery uses: pipeline.article_resolver.resolve_headline
        # against the in-memory headline_index (exact match, then unique
        # slug -- absorbs the double-space reformulation Gemini introduces
        # when it echoes a headline back). Using a second, different join
        # here is exactly how the trigger and delivery paths drifted apart
        # (bead 3yb).
        rel, _reason = resolve_headline(directive.headline, headline_index)

        # A directive that does not resolve at all is an explicit no-fire,
        # NOT a fall-through into the "cache story" branch below (`None not
        # in tiers` would otherwise be True by accident). A search query
        # built from a headline we could not even find the article for is
        # exactly the wrong-story risk this unification exists to remove.
        if rel is None:
            continue

        # `tiers` is populated for Levine articles only (see the fetch loop
        # above). `rel not in tiers` means the resolved file is a Semafor/Zvi
        # story, read from cache with real body text already -- it needs no
        # substitution UNLESS the editor explicitly asked for one. Dropping
        # that union would silently regress today's behavior.
        #
        # Divergence from the old array-scan join: headline_index is
        # last-write-wins and Semafor/Zvi entries are written after Levine,
        # so a same-headline collision across sources (rare) now always
        # resolves to the cache copy and therefore fires only per the
        # editor's flag, never per the Levine tier the old join would have
        # matched. Arguably the better outcome (the cache copy has real body
        # text already) but it is a real behavior change from "the measured
        # fetch tier drives this" -- written down here rather than left
        # implicit.
        tier_info = tiers.get(rel)
        if tier_info is None and not directive.needs_exa:
            continue

        # The editor judges "paywalled" from headlines alone and sets the flag
        # on ~4% of directives while ~93% of articles arrive as stubs. The
        # fetch tier is the measured answer; the flag stays in the union
        # because it is real signal, just badly under-fired.
        if (
            tier_info is not None
            and tier_info["tier"] == "live"
            and not directive.needs_exa
        ):
            continue

        query = directive.exa_query or directive.headline
        # Origin must come from tiers[rel]["url"] (recorded from the fetched
        # Article), NOT from the resolved file's own `URL:` header -- using
        # the file header would make a Semafor needs_exa search start
        # excluding semafor.com, a behavior change nobody decided.
        origin = (
            (urlparse(tier_info["url"]).hostname or "").removeprefix("www.")
            if tier_info is not None
            else ""
        )
        exclude = [d for d in (origin, *BYPASS_DOMAINS) if d]

        slug = _slugify(directive.headline)
        exa_results, status = search_related_status(query, exclude_domains=exclude)
        # Defence in depth: exclude_domains is a request parameter honored by a
        # third-party API. Ethics policy must not depend on Exa's compliance,
        # so drop banned hosts from the results locally too.
        kept = [r for r in exa_results if not _host_banned(r.url, origin)]
        if status == "hit" and not kept and exa_results:
            # Distinct from "empty": Exa DID find coverage, we rejected all of
            # it as the paywalled origin or a bypass mirror. Collapsing this
            # into "empty" would hide a compliance failure -- if Exa ever stops
            # honouring exclude_domains, the funnel would show a mysterious
            # empty-rate spike with no recorded cause.
            status = "filtered"
        exa_results = kept
        exa_outcomes[slug] = status

        # Written unconditionally: an absent file cannot distinguish "we never
        # asked" from "we asked and got nothing", and that ambiguity is what the
        # funnel report exists to remove. Readers gate on `Result: hit`.
        out = (
            f"# Exa Results for: {directive.headline}\n"
            f"Result: {status}\n"
            f"Query: {query}\n\n"
        )
        for exa_r in exa_results:
            out += f"## [{exa_r.title}]({exa_r.url})\n{exa_r.text}\n\n"
        exa_file_path(work_dir, slug).write_text(out, encoding="utf-8")

    # Write sentinel — collection completed successfully
    sentinel = {
        "job_id": job_id,
        "started_at": started_at,
        "completed_at": datetime.now(tz=_et).isoformat(),
        "lookback_days": lookback_days,
        # levine_articles counts what survived BOTH the dedup and the fetch
        # below, so it is not comparable to sentinels written before the
        # dedup moved ahead of the fetch.
        "levine_articles": len(articles),
        "directives": len(plan.directives),
        "fp_routed": len(fp_directives),
        # "enriched" counts every non-FP directive, including ones that never
        # asked for Exa, so it does not equal the number of Exa files written.
        "enriched": len(non_fp_directives),
        # Candidates skipped are never written to disk and prior_urls comes from
        # the DB at run time, so these cannot be recovered from the work dir
        # afterwards. They must be emitted here or not at all.
        #
        # For each source, candidates is every item found in the cache window;
        # deduped is how many were dropped as already-covered before further
        # work (an HTTP fetch for Levine, a file copy for Semafor/Zvi).
        "candidates": {
            "levine": levine_candidates,
            "semafor": semafor_candidates,
            "zvi": zvi_candidates,
        },
        "deduped": {
            "levine": levine_deduped,
            "semafor": semafor_deduped,
            "zvi": zvi_deduped,
        },
        # Candidates routed away before further work -- currently only
        # Semafor, whose routing filter (fp/skip) sits between the
        # candidates count and the dedup check. Levine and Zvi have no such
        # filter, so they are always 0. Recorded so IN - ROUTE - DEDUP still
        # accounts for every candidate: this is the FP-flagged/skip-routed
        # slice that Semafor candidates counted but the funnel would
        # otherwise silently drop.
        "routed_away": {
            "levine": 0,
            "semafor": semafor_routed_away,
            "zvi": 0,
        },
        # Per-slug Exa status, so a miss is legible from the archived sentinel
        # rather than only from a work dir that /tmp reaps after 10 days. Note
        # a slug collision overwrites, so this can hold fewer entries than
        # there were Exa-flagged directives.
        "exa_outcomes": exa_outcomes,
    }
    (work_dir / "collection_done.json").write_text(
        json.dumps(sentinel, indent=2), encoding="utf-8"
    )
