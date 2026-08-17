# My Podcasts Agent Guide

Quick start and navigation for humans and coding agents.

## Quick Start

1. Sync dependencies:
   - `uv sync`
2. Run tests:
   - `uv run pytest`
3. Check consumer service:
   - `sudo systemctl status my-podcasts-consumer --no-pager`
4. Restart consumer (after code changes):
   - `sudo systemctl restart my-podcasts-consumer`
5. Sync source caches:
   - `uv run python -m pipeline sync-sources`
6. Subscription URLs:
   - Main: `https://podcast.mohrbacher.dev/feed.xml`
   - Levine: `https://podcast.mohrbacher.dev/feeds/levine.xml`
   - Yglesias: `https://podcast.mohrbacher.dev/feeds/yglesias.xml`
   - Silver Bulletin: `https://podcast.mohrbacher.dev/feeds/silver.xml`
   - The Rundown: `https://podcast.mohrbacher.dev/feeds/the-rundown.xml` (created on first episode)
   - FP Digest: `https://podcast.mohrbacher.dev/feeds/fp-digest.xml`
   - Aaronson: `https://podcast.mohrbacher.dev/feeds/aaronson.xml`
   - ChinaTalk: `https://podcast.mohrbacher.dev/feeds/chinatalk.xml`
   - Papers (one-off arXiv reports): `https://podcast.mohrbacher.dev/feeds/papers.xml`

## Docs TOC

- **Roadmap (start here for "what's next"): `docs/ROADMAP.md`** — ordered work
  spine, the per-piece execution discipline, and the facts that must survive a
  context compaction. Beads hold issue detail; the roadmap holds order and
  rationale.
- Domain guides:
  - `pipeline/AGENTS.md`
- Agent skills:
  - `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
  - `.opencode/skills/monitoring-my-podcasts-pipeline/REFERENCE.md`
  - `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`
  - `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`
  - `.opencode/skills/shipping-my-podcasts-workers/SKILL.md`
  - `.opencode/skills/shipping-my-podcasts-workers/REFERENCE.md`
  - `.opencode/skills/operating-things-happen-digest/SKILL.md`
  - `.opencode/skills/operating-things-happen-digest/REFERENCE.md`

## Asset Locations

- Podcast artwork source files:
  - `assets/podcast/cover-general.jpg`
  - `assets/podcast/cover-levine.jpg`
  - `assets/podcast/cover-yglesias.jpg`
  - `assets/podcast/cover-silver.jpg`
  - `assets/podcast/cover-things-happen.jpg`
  - `assets/podcast/cover-dwarkesh.jpg`
- Artwork served via worker URLs:
  - `https://podcast.mohrbacher.dev/cover-general.jpg`
  - `https://podcast.mohrbacher.dev/cover-levine.jpg`
  - `https://podcast.mohrbacher.dev/cover-yglesias.jpg`
  - `https://podcast.mohrbacher.dev/cover-silver.jpg`
  - `https://podcast.mohrbacher.dev/cover-things-happen.jpg`
  - `https://podcast.mohrbacher.dev/cover-dwarkesh.jpg`

## Core Paths

- Email parser: `email_processor/`
- Pipeline + feed generation: `pipeline/`
- Email ingest worker: `workers/email-ingest/`
- Podcast serving worker: `workers/podcast-serve/`
- The Rundown pipeline: `pipeline/rundown_writer.py` (script generator), `pipeline/exa_client.py` (Exa wrapper)
- Directive→article matching (Rundown): `pipeline/article_resolver.py` (shared cascade + slugify), used by `pipeline/__main__.py:find_rundown_article_source`, `pipeline/things_happen_collector.py` (Exa trigger), and `pipeline/show_notes.py`
- ChinaTalk transcript report path: `pipeline/transcript_detect.py` (shared detector), `pipeline/chinatalk_writer.py`, `pipeline/chinatalk_report.py` (called from `pipeline/processor.py`)
- One-off episodes (source adapters): `pipeline/sources.py` (adapter registry + dispatch), `pipeline/document.py` (`Document` model), `pipeline/substack.py` (Substack API ingest + HTML normalization), `pipeline/arxiv.py` (arXiv paper adapter), `pipeline/report_writer.py` (style-keyed report writer)

## Where To Start

- Daily podcast operations and incident response: `pipeline/AGENTS.md`
- Whole-system health checks: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Stuck or errored Rundown / FP Digest jobs: `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`
- Resetting errored jobs via CLI: `.opencode/skills/resetting-errored-daily-jobs/SKILL.md`
- Rundown-specific writer / collection behavior: `.opencode/skills/operating-things-happen-digest/SKILL.md`

## The Rundown Pipeline

Daily current-affairs digest covering business, technology, AI, law, media, science, and culture (excluding foreign policy). Fully automated, no human-in-the-loop. Triggered by systemd timer Mon-Fri at 4:30 AM ET.

**Sources (all co-equal, read from persistent caches with adaptive lookback):**
- Matt Levine's "Things Happen" links (extracted from email, cached to `/persist/my-podcasts/levine-cache/`)
- Semafor RSS (`semafor.com/rss.xml`) — articles classified by Gemini Flash-Lite at cache sync time into `fp`, `th`, `both`, or `skip` via `Routing:` header
- Zvi Mowshowitz / "Don't Worry About the Vase" (`thezvi.substack.com/feed`) — AI roundup sections split by topic, essays kept whole. Persistent cache at `/persist/my-podcasts/zvi-cache/` (180-day retention).

**Subscription:** `https://podcast.mohrbacher.dev/feeds/the-rundown.xml`

**Category:** News

**Flow:**
1. Systemd timer triggers, or CLI: `uv run python -m pipeline the-rundown [--date YYYY-MM-DD] [--dry-run [--lookback N]]`. **The CLI only enqueues a job row; the consumer executes it.** See "Daily Job Execution Model" below.
2. `things_happen_collector.py` reads Levine links from cache, Semafor from cache, syncs Zvi cache — all within adaptive lookback window. Routes FP-flagged links to `/persist/my-podcasts/fp-routed-links/` for FP Digest.
3. `things_happen_editor.py` (Gemini Flash-Lite) triages into 3-5 themes, selects 8-12 stories, writes `plan.json`
4. Exa enrichment for selected stories whose Levine article measured a non-`live` `source_tier` (`paywalled`/`http_error`/`fetch_error`), unioned with any directive the editor separately flagged `needs_exa` — the measured fetch tier drives this, not the editor's headline-only guess. Semafor/Zvi stories have no Levine article to match, so the tier half cannot fire for them — they come from cache with real body text already — but they still fire when the editor flags `needs_exa`, which is the union preserving today's behavior. Matching a directive to its Levine article is by slug, not raw headline equality. The search excludes the article's own origin domain plus a fixed deny-list of paywall-circumvention mirrors (`archive.ph` and friends) so retrieved text never launders a paywalled read through a bypass site.
5. Open-access text found this way is **appended** to the stub, never used to replace it — the stub's true headline still anchors the section, so a wrong-story search result degrades the section rather than fabricating a confident narration under the wrong headline
6. `rundown_writer.py` generates script via opencode-serve (synchronous, no agent session); the writer prompt is told to name the outlet when it draws facts from that appended coverage
7. `things_happen_processor.py` runs TTS (`ttsjoin`) + publishes to R2 + updates feed

**Writer prompt assembly (structural, not just descriptive):** `_assemble_writer_inputs`
builds one ordered `list[tuple[theme, article_texts]]` — "sections" — and
`build_rundown_prompt` renders that list verbatim. Nothing downstream re-derives or
filters the theme set. Ordering: plan themes keep `plan.themes` order; a theme with zero
resolved articles is omitted entirely (no bare `## Theme` header can reach the model); a
directive whose `theme` is absent from `plan.themes` (an "orphan" — the editor invented a
near-miss name instead of using a listed one) is appended as its own trailing section,
under its own name, in first-seen order — **never** fuzzy-matched onto a similar plan
theme, because that would trade a visible drop for an invisible miscategorization.
`build_rundown_prompt` derives `TODAY'S THEMES` from the section names it is given, so the
announced theme list and the rendered `STORIES BY THEME` block are the same set by
construction; it also independently skips any section with no articles as a second guard.
Both `--dry-run` and the consumer call the same `_assemble_writer_inputs`, so a
`--dry-run` + `publish-script` episode matches what the consumer would have produced (this
was not always true — the dry-run path used to hand-roll its own assembly loop with no Exa
append, the same "second drifted implementation" shape as `my-podcasts-78b`).

This mattered in practice, not just in theory: replaying 8 historical work dirs through
the new assembler (pre/post diff) recovered a genuine Semafor story — "White House's AI
framework close-hold fuels industry concerns," and a sibling piece the next day — that had
been silently dropped under the invented theme `'AI Safety & Regulation'` in two separate
episodes. 3 of the 8 plans rendered byte-identical; the other 5 differed only by a removed
empty header and, in two of them, that recovered story.

**Key modules:**
- `pipeline/opencode_client.py` — shared HTTP client for the opencode-serve API
- `pipeline/rundown_writer.py` — synchronous script generator via opencode-serve; `build_rundown_prompt(sections, date_str, context_scripts)` renders the ordered sections built by `_assemble_writer_inputs`
- `pipeline/things_happen_collector.py` — article collection, Semafor integration, Zvi integration, FP routing
- `pipeline/things_happen_editor.py` — Gemini AI for themed research plan (story selection, priority, FP flagging)
- `pipeline/zvi_cache.py` — Zvi RSS fetch, roundup splitting, persistent cache
- `pipeline/exa_client.py` — Exa search API wrapper (bounded with a 30s timeout, `exclude_domains` support), plus `exa_file_path`/`exa_text_if_hit` (the `Result: hit` gate) and `exa_result_sections` (header-stripped view fed to the writer)
- `pipeline/consumer.py` — `_assemble_writer_inputs` resolves each selected directive to article text via `__main__.find_rundown_article_source`, appends open-access coverage to stubs, and builds the ordered `sections` list described above; also used by the `--dry-run` path
- `pipeline/rss_sources.py` — RSS source definitions, `SEMAFOR`, `categorize_semafor_article()` (legacy fallback)
- `pipeline/source_cache.py` — Persistent cache sync for Semafor (with LLM routing), Antiwar RSS, and Antiwar homepage
- `pipeline/run_stats.py` — content-acquisition funnel: `collect_run_stats`, `render_report`, `append_jsonl`
- `pipeline/alerts.py` — `send_alert`, posts plain text to the Telegram General topic via pigeon
- `pipeline/pigeon.py` — pigeon daemon URL + auth headers, shared by `alerts` and `opencode_client`

**Content-acquisition funnel:** every Rundown script stage emits a funnel report
to the Telegram General topic and appends a line to
`/persist/my-podcasts/run-stats.jsonl`. It answers "how much real article text
actually reached the writer" — the question that went unanswered for months while
93% of Levine articles arrived as bare headlines.

Render the funnel for any existing work dir:

```bash
uv run python -m pipeline run-stats --work-dir /tmp/the-rundown-<job_id> [--send]
```

Reading the report: `IN` is candidates found per source, `DEDUP` how many were
already covered, `FETCH` the per-tier outcome of fetching Levine links
(`live` means real body text; `paywalled`/`http_error`/`fetch_error` all mean the
writer got a headline), `EXA` the enrichment hit rate, `WRITE` what reached the
writer, `OUT` the resulting script. The `paywalled:` domain histogram names the
publishers worth routing around.

A `WRITE` bucket suffixed `+exa` (e.g. `paywalled+exa`) means that stub got
open-access coverage appended before reaching the writer; the plain bucket
(bare `paywalled`) means it did not. When any appending happened, the `WRITE`
line also grows a trailing `, N +open-access` count — its absence means zero
appends that run, not that the field doesn't exist. **Scale expectations:**
measured across 8 real runs, only ~1.2 of ~4.75 selected stories per episode
are Levine stubs (`[0, 1, 0, 3, 1, 2, 1, 2]` stubs/run) — `EXA 2 flagged` is a
healthy day, and `EXA 0 flagged` is legitimate on a light one. A single run's
report cannot confirm or refute whether this feature is working; only a
week of `run-stats.jsonl` history can.

Each `writer_inputs.json` entry also carries `reached_prompt` (bool) and
`miss_reason` (`no_index` / `index_unreadable` / `index_no_match` / `slug_ambiguous`,
or `None` on a hit — see `find_rundown_article_source`'s docstring for why a miss has
exactly one reason, not one per lookup stage it cascades through). `reached_prompt` is derived
from whether the directive's theme survived into the section list that
`_assemble_writer_inputs` built — deliberately not from `bool(text)`, which would be
tautological against `chars` and could never catch section assembly dropping a story.
Note the one hop it does *not* cover: it trusts that `build_rundown_prompt` renders
every non-empty section verbatim (true today, and pinned by the legacy-equivalence
test), so a future filter added inside the renderer would not show up here. If `WRITE`
shows a trailing `, N DROPPED-AFTER-RESOLVE(!)`, that is `dropped_before_prompt`:
entries with real resolved text (`chars > 0`) whose theme did not make it into any
assembled section. **This should always be 0.** Non-zero means section assembly lost
a story that had text — a bug to investigate, not a statistic to shrug at. A
bracketed `[misses: reason N, ...]` on the same line is the `miss_reason` histogram,
shown only when non-empty. Both fields are backward-compatible: historical
`writer_inputs.json` files predate them, and a missing key is treated as "unknown,"
never as `False` — so old work dirs never false-alarm on the new canary.

**Trending misses across the rename:** `run-stats.jsonl` lines written before
2026-08-17 carry the retired reason `index_no_overlap`; later lines carry
`index_no_match`. Union them when trending, or a chart will show one reason
vanishing and another appearing on the same day for no real reason.

## Directive→article matching

One resolver decides which article file a directive from the editor's `plan.json`
refers to: `pipeline/article_resolver.py`. It is a **leaf module** (imports nothing
from `pipeline`), so the collector, the consumer, `__main__`, and show notes can all
share it without an import cycle. Its cascade is **exact headline match, then
*unique* slug match** — and nothing else.

**There is deliberately no fuzzy tier, and restoring one would be a regression.**
A word-overlap fallback used to sit at the end of the cascade, scoring each indexed
file by how many of the headline's >3-char words appeared *anywhere in its body* and
accepting any score above zero. Measured across 54 real directives: a **wrong**
article scored at least one query word in **50 of 54** cases, reached half the query
words in 12, and in one case **tied the correct article at a perfect 4/4**, where the
winner was decided by dict iteration order. No threshold can separate those
distributions. Meanwhile exact+slug covered **54/54**.

Its worst property was structural: the tier only ran when exact *and* slug both
missed, and a common cause of that is *the correct article not being in the index at
all* (FP-routed, deduped, file gone). In that regime `best_score > 0` **guarantees** a
wrong match. Replaying real work dirs found exactly this — four directives resolving
to grotesquely unrelated articles, including 17.6 KB of a Mets ETF story under the
headline "Trade tensions mount ahead of Trump-Xi summit". All four were
`include_in_episode=False`, so none reached the writer, but the mechanism was live.

Two rules follow, and both are enforced in code:

- **Ambiguity is a miss, not a coin flip.** If two indexed headlines share a
  directive's slug (they can: slugs truncate at 50 chars), the resolver returns
  `slug_ambiguous` and the cascade **stops** — it does not fall through to the
  filesystem tiers, which would happily return `sorted()[0]` and undo the refusal.
- **Filesystem globs are anchored or unique.** Flat Levine articles match
  `\d+-{slug}\.md` exactly, because the old `*{slug}.md` was a *suffix* match (slug
  `ai` matched `00-openai.md`). Both halves matter: narrowing the glob to `*-{slug}.md`
  still matches `00-open-ai.md`, and only the fullmatch rejects it. The Zvi tier must
  substring-match by nature, so it returns a file only when exactly one matches. An
  empty slug (punctuation-only headline) skips the filesystem tiers entirely, since it
  would otherwise turn every glob into a wildcard.

  These rules hold in **both** `find_rundown_article_source` and
  `show_notes._find_article_file`. They were fixed in delivery first and the same
  defect was left standing in show notes until review caught it — if you change one,
  change both.

**The shadow candidate.** On a miss, `shadow_candidate` records what a
*headline-vs-headline* Jaccard matcher would have chosen — never bodies, never used
for resolution — into `writer_inputs.json` and the funnel's
`(N w/ shadow)` count. It exists because the corpus (10 work dirs) proved exact+slug
*sufficient over 10 days* but cannot bound how often the editor reformulates a
headline by **word substitution**; all three observed reformulations were
whitespace-only, which slug matching absorbs.

**Read the shadow log carefully — a non-zero score is NOT evidence to restore fuzzy
matching.** The dominant miss cause is the correct article being absent from the
index entirely, and in that regime the shadow will by construction score some
plausible but *wrong* headline. Escalating requires checking candidates against
ground truth and separating a reformulation-shaped miss from an absent-article miss.
Read naively, the log would recreate the original bug with logged ammunition.

**Two slugify families, deliberately not unified.** The article family
(`article_resolver.slugify`, re-exported as `_slugify` by `things_happen_collector`,
`fp_collector`, `show_notes`, `zvi_cache`, `source_cache`) keeps non-ASCII
alphanumerics, because `str.isalnum()` is True for `é`. The R2-key family
(`script_processor`, `blog_poller`) strips them via regex. They name different things
— article files versus episode keys — and a test pins the difference.

**`show_notes._find_article_file` resolves through the same cascade**, so delivery,
the Exa trigger, and show notes agree on the index tiers by construction, and their
filesystem tiers now enforce the same anchoring and uniqueness rules. Its fallback is
**permanent, not legacy**: `show_notes` is shared with FP Digest, and `fp_collector`
writes no `headline_index.json` at all, so for every FP work dir that path is the
only one. `show_notes._headlines_match` is a separate, lower-stakes word-overlap join
(it filters show notes by coverage, never selects prompt text) and is intentionally
left alone.

Two cautions. It is labeled **script stage** because TTS and publish happen on a
later consumer loop iteration — the report says nothing about whether an episode
shipped. And severity is always `info`: thresholds are deliberately unset until
`run-stats.jsonl` has real history (`my-podcasts-3qs`), because
`include_in_episode` measured 4-5 on every one of the last 10 runs, so any
guessed threshold fires on normal days.

Work dirs live under `/tmp` by default; `MY_PODCASTS_WORK_DIR_BASE` overrides
the base (used by tests so the suite does not litter the host's `/tmp`).

## FP Digest Pipeline

Daily foreign policy podcast. Fully automated, no human-in-the-loop.

**Sources (all read from persistent caches with adaptive lookback):**
- antiwar.com homepage (~49 curated external links across 13 regions)
- 3 antiwar.com RSS feeds + Caitlin Johnstone feed
- Semafor RSS — articles with `Routing: fp` or `Routing: both` (classified by Gemini at cache sync time)
- Routed FP links from The Rundown (via `/persist/my-podcasts/fp-routed-links/`)

**Flow:**
1. Systemd timer triggers daily at 4:30 AM ET (08:30 UTC)
2. `fp_collector.py` reads homepage articles, RSS articles, Semafor FP articles from persistent caches (with adaptive lookback window) + routed Levine FP links
4. `fp_editor.py` (Gemini Flash-Lite) triages into 3-5 themes, selects 8-12 stories
5. Exa enrichment for paywalled articles
6. `fp_writer.py` generates script via opencode-serve
7. `fp_processor.py` runs TTS + publishes to R2

**Key modules:**
- `pipeline/fp_homepage_scraper.py` — antiwar.com homepage parser
- `pipeline/fp_editor.py` — story triage and theme identification
- `pipeline/fp_collector.py` — multi-source collection orchestrator (homepage, RSS, Semafor, routed links)
- `pipeline/fp_writer.py` — script generation via opencode-serve
- `pipeline/fp_processor.py` — TTS + publish
- `pipeline/rss_sources.py` — RSS source definitions, Semafor category routing (legacy fallback)
- `pipeline/source_cache.py` — Persistent cache sync for all sources

**CLI:** `uv run python -m pipeline fp-digest [--date YYYY-MM-DD] [--dry-run [--lookback N]]` — enqueue-only, same as The Rundown. See "Daily Job Execution Model".

## Daily Job Execution Model

**The daily CLI enqueues; the consumer executes. There is exactly one executor.**

`python -m pipeline the-rundown` (and `fp-digest`) inserts a pending job row,
prints the job id, and exits in well under a second. The long-lived
`my-podcasts-consumer` picks it up within ~10s and runs collection, the writer,
TTS, and publish.

It did not always work this way, and the history is the reason for the rule.
The CLI used to run the whole pipeline inline while holding the row at
`status='pending'` — so the consumer, which polls that status with no claim,
started a *second* pipeline on the same job ~10s later. Two TTS renders, two
PUTs to one `r2_key`, two `episodes` rows: the feed carried two items for one
day and the stale one declared the wrong length. That produced 16 duplicated
keys across 9 weekdays before it was fixed by deleting the inline path
(`my-podcasts-78b`).

Consequences worth knowing:

- **A green timer unit means "enqueued", not "published."** The unit finishes in
  seconds now. Two alerts cover the gap: an enqueue-time audit (each run checks
  whether any *earlier* run is still `pending`/`errored` and alerts if so) and a
  retry-exhaustion alert from the consumer.
- **`--lookback` applies to `--dry-run` only** and is a hard error otherwise —
  the consumer computes the window itself via `_compute_lookback`. It errors
  rather than being ignored, deliberately.
- **`--dry-run` still runs collection and generation inline** and touches no DB.
- **Re-running an already-`completed` date is not supported.** The command
  reports `status=completed` and does nothing. An `errored` date reports
  `status=errored` and tells you to `jobs reset`, because the consumer only
  executes `pending` rows.
- **Never run a second consumer by hand.** It reintroduces exactly this race.
- **Manual publish while the consumer is down:** `--dry-run`, then
  `publish-script`, then **`jobs complete --feed <slug> --date <date>`**.
  `publish-script` does not touch the job row, so skipping the last step leaves
  it `pending` and the returning consumer publishes a duplicate.

**Accepted tradeoff:** the old inline run was accidental redundancy — it
published even when the consumer was dead. Now a dead consumer means no
episode. That is accepted because `Restart=on-failure` covers crashes, a dead
consumer also stops the email-driven feeds so it gets noticed, and the
"redundancy" was corrupting a shared work dir anyway. The honest gap: the
enqueue-time audit only runs at the *next* weekday 04:30, so a consumer that
dies Friday morning yields no episode and no alert until Monday (~72h).

## Content Routing

Foreign policy content is exclusively routed to FP Digest, not The Rundown:

- **The Rundown editor** classifies each link with `is_foreign_policy: bool`
- FP-flagged links are written to `/persist/my-podcasts/fp-routed-links/{date}-{job_id}.json`
- FP Digest collector reads routed files within the lookback window
- **Semafor** articles are classified by Gemini Flash-Lite at cache sync time via a `Routing:` header (`fp`, `th`, `both`, or `skip`). Legacy cache files without `Routing:` fall back to category-based routing via `categorize_semafor_article()`
- Routed link files are cleaned up after 7 days

## Source Caching

All external sources are cached daily to persistent storage by the `sync-sources` timer (4:00 AM ET daily). Podcasts read from caches instead of fetching live, with an adaptive lookback window based on days since the last episode (min 2, max 14 days).

**Caches:**
- Zvi: `/persist/my-podcasts/zvi-cache/` (also synced on-demand by The Rundown collector)
- Semafor: `/persist/my-podcasts/semafor-cache/`
- Antiwar RSS: `/persist/my-podcasts/antiwar-rss-cache/`
- Antiwar Homepage: `/persist/my-podcasts/antiwar-homepage-cache/`

All caches use 180-day retention (cleaned up by `_cleanup_old_work_dirs` in consumer). Files are markdown with metadata headers (URL, Published, Source, Category/Region). Semafor cache files also include a `Routing:` header (`fp`, `th`, `both`, or `skip`) set by Gemini Flash-Lite at sync time.

**Key module:** `pipeline/source_cache.py` — `sync_semafor_cache`, `sync_antiwar_rss_cache`, `sync_antiwar_homepage_cache`

**CLI:** `uv run python -m pipeline sync-sources`

**Timer:** `sync-sources.timer` — daily at 4:00 AM ET

**Adaptive lookback:** `pipeline/db.py:days_since_last_episode()` queries the latest episode date per feed. `pipeline/consumer.py:_compute_lookback()` computes `min(max(2, days_since + 1), 14)`.

## Blog Polling

WordPress and other blog sources are polled via RSS for new posts. Each new post is adapted for audio (via Gemini Flash), converted to speech, and published as a standalone episode.

**Polling interval:** Every 6 hours (inside the consumer loop)

**CLI:** `uv run python -m pipeline poll-blogs [--dry-run]`

**Sources:**
- Scott Aaronson / Shtetl-Optimized: `https://scottaaronson.blog/?feed=rss2` -> feed slug `aaronson`

**Key module:** `pipeline/blog_poller.py` -- RSS fetch, AI adaptation, TTS + publish

**Source definitions:** `pipeline/blog_sources.py` -- `BlogSource` dataclass, `BLOG_SOURCES` tuple

## One-Off Episodes (Source Adapters)

Turn any supported source URL (Substack post, arXiv paper, ...) into a one-off episode via the generic `episode` command. The command resolves the URL through a source-adapter registry into a normalized `Document`, then either generates a spoken **report** (briefing, default) or a faithful **read** (full reading via Gemini adaptation). Manual/operator-run, not automated. This **replaces** the old `substack` command — Substack URLs and bare numeric Substack post ids are auto-detected and routed to the substack adapter.

**CLI:** `uv run python -m pipeline episode --url <url-or-id> [--source {arxiv,substack}] --mode {report|read} --feed-slug <slug> [--style {interview,paper}] [--title ...] [--voice nova] [--category ...] [--date YYYY-MM-DD] [--script-file PATH] [--dry-run]`

**Adapter model:** `pipeline/sources.py:resolve_document(url, source=None)` dispatches to the first adapter whose `matches(url)` returns True (or to an explicitly forced `--source`). Each adapter returns a `pipeline/document.py:Document` carrying `report_text`, `read_html`, `style`, `byline`, `default_category`, etc.
- **substack adapter** (`pipeline/substack.py`): style `interview`, **read supported**, default category `Technology`. Ingests via the Substack JSON API (numeric id, short link `.../p-<id>`, or canonical slug URL `.../p/<slug>`); paywalled/empty posts rejected. Auto-matches `substack.com`, `/p/`, `/p-`, `.dwarkesh.com`, and bare numeric ids.
- **arXiv adapter** (`pipeline/arxiv.py`): style `paper`, **report-only** (`read_html=None`; `--mode read` errors), default category `Science`. Metadata via the arXiv Atom API using the **versioned** id derived from the Atom entry; body via the `/html` LaTeXML rendering (drops references/math/image bodies and footnotes, collapses figures/tables to their captions). Auto-matches `arxiv.org` hosts and bare/`arXiv:`-prefixed modern ids.

**Report writer:** `pipeline/report_writer.py:generate_report(body, subject, style, byline)` selects a prompt by `style` (`interview` vs `paper`); `--style` overrides the source default. opencode-serve, 900s timeout, rejects empty output; mirrors `chinatalk_writer.py` / `yglesias_writer.py`.

**Publishing a reviewed script:** `--script-file PATH` publishes a pre-written script verbatim, skipping generation. Metadata (title prefix, source `<link>`, show notes) still comes from the resolved `Document`. This is the first-class version of the dry-run-then-publish workflow: review the `--dry-run` artifact, then publish that exact text with `--script-file`.

**Key modules:**
- `pipeline/sources.py` — adapter registry (`Adapter`, `ADAPTERS`) + `resolve_document` dispatch
- `pipeline/document.py` — `Document` model (`byline`, `default_category`, `style`, `report_text`, `read_html`, ...)
- `pipeline/arxiv.py` — arXiv adapter (`matches`, `parse_arxiv_id`, `resolve`)
- `pipeline/substack.py` — `resolve_post` (Substack API), `html_to_clean_text` (HTML normalization)
- `pipeline/report_writer.py` — style-keyed report writer (interview + paper, with byline)
- Read mode reuses `pipeline/blog_poller.py:adapt_for_audio` (Gemini); publishes via `pipeline/script_processor.py:publish_script` (extended with `source_url`)

## ChinaTalk Transcript Report Path

ChinaTalk newsletters are sometimes podcast transcripts rather than essays. For transcripts, the pipeline replaces the TTS body with an AI-written spoken briefing about the conversation, and prefixes the episode title with "Report: ". Essays and articles are read normally.

**Detection:** `pipeline/transcript_detect.looks_like_transcript` — a deterministic, content-only transcript-shape detector (shared with the Yglesias path). It counts line-start speaker labels and fires only when at least two distinct speakers each take five or more turns. No LLM call, so detection never depends on a remote API (the previous Gemini classifier silently returned NO during a 2026-05-26 endpoint outage, shipping an 80-minute transcript as a literal read).

**Generation:** `pipeline/chinatalk_writer.py` (opencode-serve, mirrors `rundown_writer.py`, 900-second timeout, rejects empty output).

**Wiring:** `pipeline/chinatalk_report.maybe_rewrite_chinatalk` is called from `pipeline/processor.process_email_bytes` between body cleaning and TTS. Essays (detection returns False) are read normally. For a *confirmed* transcript, a report-generation failure is logged and re-raised rather than silently degrading to a literal read — it propagates out of `process_email_bytes`, the consumer leaves the queue message unacked, and the email is reprocessed on redelivery.

## Yglesias Argument Transcript Reports

Slow Boring publishes the newsletter form of Matt Yglesias's *The Argument* podcast with Jerusalem Demsas (regular weekly episodes, plus one-off live events with guests). For paying subscribers the email body carries the full verbatim transcript (~80 minutes of TTS). Rather than read that aloud — or drop it, as the pipeline used to — these posts are rewritten into a spoken briefing, mirroring the ChinaTalk transcript report path, and published to the `yglesias` feed with a `Report: ` title prefix.

**Detection:** `pipeline/yglesias_filter.is_argument_transcript` is a deterministic, content-only transcript-shape detector. It counts line-start speaker labels and fires only when at least two distinct speakers each take five or more turns. No LLM call. This survives Substack footer/boilerplate changes (the old marker-based detector missed live-event posts entirely) and is near-impossible to trigger on a normal essay.

**Generation:** `pipeline/yglesias_writer.generate_report` (opencode-serve, mirrors `chinatalk_writer.py`, 900-second timeout, rejects empty output) with a prompt tuned for *The Argument*.

**Wiring:** `pipeline/yglesias_report.maybe_rewrite_yglesias` is called from `pipeline/processor.process_email_bytes` after body cleaning, before TTS (alongside the ChinaTalk hook; the two are mutually exclusive by `feed_slug`). It is yglesias-only and fails safe: any exception in detection or generation falls back to the standard reading, so the listener always gets an episode (a long reading rather than a silently dropped essay).

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push   # sync bead DB to DoltHub (git-free; beads is NOT in git)
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
