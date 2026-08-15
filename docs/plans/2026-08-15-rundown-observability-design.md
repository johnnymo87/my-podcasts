# The Rundown: Content-Acquisition Observability

Date: 2026-08-15
Status: Approved design, not yet implemented

## Motivation

The Rundown's source articles are frequently paywalled. The original request was
to investigate substituting open-access coverage of the same story, with
observability first to reveal where content gathering is weak.

Investigation found the observability question was answerable immediately from
artifacts already on disk. Measured across 107 work dirs in `/tmp` (~10 days of
runs), 106 Levine article files:

| Metric | Value |
| --- | --- |
| Article files | 106 |
| Headline-only or near-empty | 99 (93%) |
| bloomberg.com | 57 (all stubbed) |
| ft.com | 18 (all stubbed) |
| wsj.com | 18 (17 stubbed) |
| nytimes.com | 4 (all stubbed) |

The paywall question is therefore answered: the writer is composing most stories
from a headline and nothing else. Observability's remaining job is different and
narrower — prove that the Exa fix below actually delivers text, and catch future
silent degradation.

## Defects found during investigation

**Defect 1 — Exa output is written where nothing reads it.**
`things_happen_collector.py:278,288` writes `enrichment/exa/{i:02d}-{slug}.md`.
Both Rundown readers look for the bare `{slug}.md`: `__main__.py:101` and
`show_notes.py:78`. The FP path is correct (`fp_collector.py:392`), so this is
Rundown-specific. (`consumer.py:227` is the FP reader, not a Rundown one.)

**Defect 2 — Exa is a fallback that can never be reached.**
`__main__.py:_find_rundown_article_text` (lines 25-105) returns the first hit,
with Exa last at `:100-103`. A Levine article always has a stub file on disk
(`collector:99-101` writes `content=headline` when the fetch fails), so the
`headline_index` exact or fuzzy lookup at `:50-79` always resolves first. Exa
text has plausibly never reached a Rundown writer.

**Defect 3 — Directives with no text are silently dropped.**
`consumer.py:376-381` skips any directive whose lookup returns empty. No `else`
branch, no counter, no log.

**Defect 4 — Fetch outcome is discarded.**
`article_fetcher.py:84-101` drops `source_tier` when mapping `FetchedArticle` to
`Article`. Nothing downstream distinguishes a real article from an echoed
headline. This is why defects 1-3 went unnoticed.

**Defect 5 — Dedup runs after fetch.**
`collector:83` fetches, `collector:93` then discards already-covered URLs. The
dedup key (`resolved_url`) is available at `:82`, before the fetch.

**Defect 6 — CI has been red since at least 2026-06-15.**
Every push fails at `ruff check` in ~15s, so `mypy` and `pytest` have never run
in CI. Locally: ruff 71 errors, ruff format 12 files, mypy 111 errors in 32
files, pytest 405 passed. `ci.yaml:17-20` pins Python 3.11 while `pyproject.toml`
requires `>=3.14`.

## Constraints discovered

**`/tmp` is reaped at 10 days.** `systemd-tmpfiles` config is
`q /tmp 1777 root root 10d`. The 180-day `_cleanup_old_work_dirs`
(`consumer.py:132`) never gets a turn. Anything that must outlive ten days
cannot live in a work dir.

**One session-free HTTP path reaches the Telegram General topic.**
`POST http://127.0.0.1:4731/alert` on the pigeon daemon
(`notification-service.ts:386-432`) calls `sendMessage` with `{chat_id, text}`
only — no `message_thread_id`, therefore General; and no `parse_mode`, therefore
plain text. Body is `{"text", "severity": "info"|"warning"|"error"}`; returns
204/400/503/502. Auth is a bearer token required only when
`PIGEON_DAEMON_AUTH_TOKEN` is set, which it is not on devbox today.

Pigeon's swarm channel broadcast does **not** reach Telegram
(`telegram-notice.ts:17-20` early-returns for channel broadcasts), so it is not
an option. `/alert` is shared with pigeon's own ops alerts, so Rundown stats will
interleave with incident traffic.

A concurrent change to `pipeline/opencode_client.py` declares pipeline sessions
to pigeon with `notify_policy="none"`. That suppresses opencode-session
notifications only. The reporter posts direct HTTP and creates no session, so it
is unaffected.

## Design

Three pieces, shipped in order as separate commits.

### Piece 0 — Make CI functional

Separate PR, landed before the feature work, so that 12 reformatted files and a
mypy triage do not contaminate a feature diff.

- `ruff check --fix` and `ruff format` across the repo. Most of the 71 errors
  auto-fix; the remainder are hand-fixed or added to the existing per-file
  ignore list in `pyproject.toml`.
- `mypy` gets `continue-on-error: true` until the 111 errors are triaged. An
  honest amber gate beats a red one everybody ignores, and beats deleting the
  step outright.
- Fix the `setup-python` 3.11 pin (`ci.yaml:17-20`) to match
  `requires-python >=3.14`, and bump `actions/setup-python@v3`.
- Run `pytest` before `mypy`, so the gate that currently passes reports first.

### Piece 1 — Repair the Exa path

- Write Exa output to the bare `{slug}.md` that both readers expect. No
  collision suffix: a suffixed filename would be unreachable by the exact-match
  readers, reintroducing defect 1 inside its own fix. Identical 50-character
  slugs mean duplicate stories.
- Write the Exa file unconditionally with a `Result: hit|empty|error` header
  (plus `Query:` and, on error, the exception class), so a miss is observed
  rather than inferred from an absent file.
- Gate **both** readers on `Result: hit` — `__main__.py:101` and
  `show_notes.py:78`. Without this, the unconditional write would feed
  `Result: empty` stubs to the writer and to show notes.
- Move dedup before fetch in the collector, aligning the funnel with reality and
  eliminating polite-delay fetches for articles that are then discarded.

Exa **append** semantics (making Exa augment article text rather than substitute
for a missing file, resolving defect 2) is deliberately deferred to its own later
commit. It changes writer input, so landing it here would pollute the before/after
read on the filename fix.

### Piece 2 — The funnel

Purpose: verify the Exa repair delivers text, and detect future degradation.

Collection is *mostly* derived from work-dir artifacts by a pure function, with
two deliberate exceptions where reconstruction would lie:

- **Stages 1-2 (candidates in, deduped out) cannot be derived.** Skipped articles
  are never written to disk (`collector:93-94` skips before the write at `:101`;
  Semafor at `:117-139` and Zvi at `:166-178` likewise). `prior_urls` comes from
  the database at run time (`consumer.py:341-343`) and changes daily, so it is not
  reconstructable later either. Instead, extend the existing
  `collection_done.json` sentinel (`collector:293-301`) with per-source candidate,
  dedup, and fetch-tier counts. The sentinel is already threaded state that is
  already written; extending it costs nothing new.

- **Stage 7 (reached writer) must not be re-derived.** Writer assembly resolves a
  directive to text through fuzzy word-overlap (`__main__.py:55-79`). A reporter
  that re-ran that match would be a parallel implementation, guaranteed to drift
  the moment the resolver changes, and would miscount when two directives resolve
  to the same file. Instead, `_find_rundown_article_text` returns
  `(text, source_path, exa_appended)` and the consumer writes `writer_inputs.json`
  at `:372-381`. Roughly 15 lines; makes stage 7 exact and converts defect 3's
  silent skip into a recorded event.

**Fetch tier lives in a `tiers.json` sidecar, not in the article markdown.**
A `Source-Tier:` header inside the article file would be fed verbatim into the
writer prompt — the writer would read `Source-Tier: headline_only` as content —
and suppressing it during fuzzy scoring would silently change existing match
behavior. The sidecar sits next to `headline_index.json`, keyed by relative path,
and records both the tier and `extracted_chars` per article.

`_try_live_url` (`article_fetcher.py:56-72`) returns a reason instead of a bare
`None`: `live`, `paywalled`, `http_error`, `fetch_error`. `paywalled` means HTTP
200 with under 200 characters extracted. This is a weak proxy — FT and WSJ
teasers plus a subscribe pitch can clear 200 characters through the `<body>`
fallback at `:50` — which is exactly why `extracted_chars` is recorded. The
threshold can then be retuned against historical data instead of by re-fetching.

**Durability.** One line appended per run to
`/persist/my-podcasts/run-stats.jsonl`. No schema, no sqlite table, survives the
10-day `/tmp` reaper, and is greppable for trends. `run_stats.json` in the work
dir remains as a per-run convenience.

**Delivery.** New `pipeline/alerts.py` exposes `send_alert(text, severity) -> bool`:
`POST {PIGEON_DAEMON_URL}/alert`, 10-second timeout, bearer token only when
configured. It never raises — connection error, timeout, 502, 503 all return
`False` and print, with the rendered text logged so journald retains it when
pigeon is down. The token-reading logic is extracted from
`opencode_client.py:61-79` rather than duplicated; it already handles the
`/run/secrets/pigeon_daemon_auth_token` fallback, so auth appearing later is
pre-solved.

**Severity is always `info` for now.** Measured across the last 10 successful
runs, `include_in_episode` is 4-5 every time, so a proposed "warn if fewer than 5
directives reached the writer" rule would have fired on normal days. Setting
alert thresholds inside a project premised on not yet having numbers is
self-refuting; thresholds get set after two weeks of real data.

**The report is labeled script-stage.** It fires at `consumer:396`, after the
script is written but before TTS and publish (which happen on the next loop
iteration). It must not imply an episode shipped.

**Message shape** — plain text, no markdown, roughly 450 characters:

```
The Rundown 2026-08-15 (job 412) - script stage - collect 4m12s, lookback 3d

IN     47 = levine 21, semafor 19, zvi 7
DEDUP  -6 (levine)
FETCH  levine 15: live 6, paywalled 8, http_error 1, fetch_error 0
PLAN   14 directives = 9 episode, 5 fp-routed
EXA    7 flagged -> 3 hit, 3 empty, 1 error
WRITE  9 selected -> 8 with text (3 live, 4 cache, 1 stub), 1 dropped
OUT    1840 words, 4 themes, 9 headlines covered

paywalled: bloomberg.com 5, ft.com 2, wsj.com 1
```

The `paywalled:` domain histogram (top 8) is the single most useful line for
designing open-access substitution later: it names which publishers to route
around.

**Idempotence and failure isolation.** The reporter runs after `script.txt`,
`summary.txt`, and `covered.json` are on disk, inside its own `try/except` that
only prints, so it can never fail a job or burn retry budget. A
`work_dir/run_stats_sent` marker prevents a second send when the consumer retries
with reused collection (the retry window is far shorter than the 10-day reap, so
a `/tmp` marker is safe here). Manual sends ignore the marker. On a retry with
reused collection the report states `(reused collection)` and reports
`lookback_days` from the sentinel rather than recomputing it.

**New CLI.** `uv run python -m pipeline run-stats --work-dir /tmp/the-rundown-N [--send]`
re-renders any existing work dir, enabling manual verification against real data
and a manual re-send after a failed delivery.

### Scope

The Rundown only. FP Digest is not a useful control group — it has a different
fetch profile and its Exa path is already correct (`fp_collector.py:392`).
`run_stats.py` will be named feed-agnostically, but nothing more is built for FP.

## Testing

- `test_article_fetcher.py` — four tier-classification cases (200 with long body
  to `live`; 200 with under 200 characters to `paywalled`; 404 to `http_error`;
  `requests.get` raising to `fetch_error`), plus tier and `extracted_chars`
  propagation. This is the test that would have caught defect 4.
- `test_things_happen_collector.py` — `tiers.json` written with tier and char
  count; Exa file written when `search_related` returns `[]` (`Result: empty`)
  and when it raises (`Result: error`); filename asserted against the exact path
  the readers use, so the two halves cannot drift apart again; dedup happens
  before fetch.
- `test_run_stats.py` — fixture work dir with known artifacts, asserting every
  funnel number. Degenerate shapes that exist on disk today must be covered:
  `/tmp/the-rundown-74a2d34d` has `collection_done.json` equal to `{}` and a plan
  with `directives: []`. Also: missing `plan.json` yields partial stats rather
  than an exception; a work dir with no `tiers.json` yields an `unknown` bucket
  without crashing.
- `test_alerts.py` — monkeypatched `requests.post`: URL, body shape, severity
  passthrough; auth header present only when a token is configured; returns
  `False` rather than raising on timeout, connection error, and non-2xx.
- Existing `test_find_rundown_article_text_*`
  (`test_things_happen_collector.py:502-571`) re-checked, plus a case asserting
  `Result: empty` Exa files are not returned.
- Manual: run the new CLI without `--send` against the 107 existing
  `/tmp/the-rundown-*` dirs, exercising the degradation path against real data
  and yielding a historical baseline for free.

The consumer wiring itself is not unit-tested. The logic goes in a small
`_report_run_stats(work_dir, ...)` helper that swallows exceptions; the helper is
tested with a deliberately broken work dir and the consumer call site stays one
line.

## Deferred

- Exa append semantics (defect 2's full resolution).
- Alert thresholds, after two weeks of `run-stats.jsonl`.
- Open-access substitution — the actual feature this investigation was for. The
  93% stub rate and the domain histogram are its inputs.
- mypy's 111 errors, tracked separately once CI is amber.
