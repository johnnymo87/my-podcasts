---
name: operating-things-happen-digest
description: Use when debugging The Rundown writer and collection path, especially opencode timeouts, reused collection artifacts, empty script output, or delayed daily episodes.
---

# Operating The Rundown Writer Path

## How it works

The Rundown daily job collects Levine, Semafor, and Zvi content into a work dir, writes `plan.json`, enriches selected stories, then generates a script through the shared `opencode-serve` daemon. On success, the consumer writes `script.txt`, `summary.txt`, and optional `covered.json`, then hands the script to TTS + publish.

Recent hardening changed the operational behavior:
- Rundown writer timeout is now 900 seconds
- successful collection writes `collection_done.json`
- retries reuse prior collection when `collection_done.json` and `plan.json` exist
- empty script output is rejected at the writer boundary
- retries back off and eventually become `status='errored'` instead of retrying forever
- the writer prompt is built from an ordered list of `(theme, article_texts)`
  sections (`consumer._assemble_writer_inputs` builds it, `rundown_writer.build_rundown_prompt`
  renders it verbatim) — a theme with no resolved articles is never announced as a bare
  header, and a directive whose theme the editor invented (absent from `plan.json`'s
  `themes` list) still reaches the model, as its own trailing section under its own name
- `--dry-run` calls the same `_assemble_writer_inputs` the consumer uses (including the
  Exa open-access append) and now also writes `writer_inputs.json` into the dry-run work
  dir, so `run-stats --work-dir` works on dry-run dirs the same as production ones — a
  `--dry-run` + `publish-script` episode matches what the consumer would have produced

## Quick checks

```bash
# Service running?
sudo systemctl status my-podcasts-consumer --no-pager

# Pending Rundown jobs?
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, date_str, status, process_after, failure_count, last_error FROM pending_the_rundown ORDER BY created_at DESC LIMIT 10').fetchall()
for r in rows: print(dict(r))
"

# Rundown episodes?
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT title, pub_date, r2_key FROM episodes WHERE feed_slug = 'the-rundown' ORDER BY created_at DESC LIMIT 5\").fetchall()
for r in rows: print(dict(r))
"

# Feed live?
curl -sI https://podcast.mohrbacher.dev/feeds/the-rundown.xml | head -3
```

Expected healthy state:
- Service is `active (running)`
- Shared opencode-serve is healthy: `curl -s http://127.0.0.1:4096/global/health`
- Pending jobs transition from `pending` to `completed` after the writer + TTS path finishes
- Feed returns `200`

### Check if the writer is currently running

```bash
# Check for active sessions on the shared opencode server
curl -s http://127.0.0.1:4096/session | python3 -m json.tool

# Check current retry state for the latest job
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, date_str, status, process_after, failure_count, last_error FROM pending_the_rundown ORDER BY created_at DESC LIMIT 3').fetchall()
for r in rows: print(dict(r))
"
```

### Inspect the generated work dir

```bash
# Rundown work dirs
ls -ld /tmp/the-rundown-*

# View the latest script and collection sentinel
python3 - <<'PY'
from pathlib import Path
p = Path('/tmp/the-rundown-<job_id>')
for name in ['collection_done.json', 'plan.json', 'summary.txt', 'script.txt', 'covered.json']:
    f = p / name
    print(name, 'exists' if f.exists() else 'missing')
PY
```

### Read the content-acquisition funnel

Every script stage posts a funnel report to the Telegram General topic and
appends a line to `/persist/my-podcasts/run-stats.jsonl`. Re-render it for any
work dir without sending:

```bash
uv run python -m pipeline run-stats --work-dir /tmp/the-rundown-<job_id>
```

```
The Rundown 2026-08-15 (job 412) - script stage - collect 4m12s, lookback 3d

IN     47 = levine 21, semafor 19, zvi 7
DEDUP  -6 (levine)
FETCH  levine 15: live 6, paywalled 8, http_error 1, fetch_error 0
PLAN   14 directives = 9 episode, 5 fp-routed
EXA    2 flagged -> 1 hit, 1 empty
WRITE  9 selected -> 8 with text (3 live, 4 cache, 1 paywalled+exa), 1 dropped, 1 +open-access
OUT    1840 words, 4 themes, 9 headlines covered

paywalled: bloomberg.com 5, ft.com 2, wsj.com 1
```

What a healthy run looks like: `FETCH` shows a nonzero `live` count, `EXA`
shows more `hit` than `error`, and `WRITE`'s "with text" is close to "selected"
with few `dropped`.

What is currently normal but *not* healthy, and is expected: most Levine
articles land in `paywalled`, because 93% of *Levine files* are headline-only
stubs. That is the measured baseline this reporting exists to track, not a new
fault.

### Reading the `+exa` buckets and the `+open-access` fragment

Open-access substitution (`my-podcasts-85c`) fires Exa on stubbed Levine
stories that were actually *selected*, retrieves other outlets' coverage of
the same story, and appends it to the stub rather than replacing it — so the
stub's true headline still anchors the writer's section even if the search
matched the wrong story.

- A `WRITE` bucket suffixed `+exa` (e.g. `paywalled+exa` above) means that
  writer input's stub got open-access text appended. The plain bucket (bare
  `paywalled`) means it did not — either Exa wasn't triggered, or it was
  triggered and came back empty/error.
- The trailing `, N +open-access` on the `WRITE` line is the same count
  spelled out; it is present only when `N > 0`, so its absence on an older or
  quiet run is normal, not a sign the feature regressed.
- `EXA n flagged` is a much smaller number than the old editor-driven trigger
  produced, **by design**: it now fires per *selected* stubbed Levine story,
  not per directive the editor guessed was paywalled. Measured across 8 real
  runs, only ~1.2 of ~4.75 selected stories per episode are Levine stubs at
  all (`[0, 1, 0, 3, 1, 2, 1, 2]` stubs/run, max 3). **`EXA 2 flagged` is a
  healthy day. `EXA 0 flagged` is legitimate on a light one** — do not
  diagnose a broken trigger from a small number; compare it against that
  day's `FETCH` stub count (`paywalled` + `http_error` + `fetch_error`)
  instead, which is the fair denominator.
- A single run's report can neither confirm nor refute whether this feature
  is working, because the daily numbers are this small. Prefer a week of
  `/persist/my-podcasts/run-stats.jsonl` history over any one report.
- Alert thresholds for any of this remain deliberately unset
  (`my-podcasts-3qs`) until that history exists — don't invent a guessed
  threshold from one day's numbers.

### Reading `writer_inputs.json` directly: `reached_prompt` and `miss_reason`

Each entry in `work_dir/writer_inputs.json` carries `reached_prompt` (bool) and
`miss_reason` (`no_index` / `index_unreadable` / `index_no_overlap`, or `None` on a hit).
`reached_prompt` is true only if the directive both resolved to text AND its theme
survived into a section that actually got rendered into the prompt — so it is a genuine
check on section assembly, not a restatement of `chars > 0`.

If the funnel's `WRITE` line shows a trailing `, N DROPPED-AFTER-RESOLVE(!)`, that is
`dropped_before_prompt`: directives that resolved to real text but whose theme did not
make it into any section actually built. **This should always read 0.** If you see it
non-zero, section assembly lost a story that had text — treat it as a bug in
`consumer._assemble_writer_inputs`, not as a normal-variance statistic, and go read that
function's ordering rules before assuming anything else. A bracketed
`[misses: no_index 2, index_no_overlap 1]` on the same line is the `miss_reason`
histogram (only shown when non-empty) — `no_index` means `headline_index.json` was
missing from the work dir entirely, `index_unreadable` means it existed but failed to
parse, `index_no_overlap` means it parsed fine but neither exact-match nor word-overlap
found the article. Historical work dirs predate both fields; a missing key reads as
"unknown," not as a miss, so old dirs never falsely trip the canary.

Beware two false alarms:

- **`FETCH ... unknown N`** means the work dir predates the tier instrumentation,
  not that fetching failed.
- **A missing Telegram report** means the reporter or pigeon failed. The episode
  is unaffected — the reporter runs after the script is on disk and swallows all
  its own errors. Check `journalctl -u my-podcasts-consumer | grep 'run stats'`,
  and note the numbers are still in `work_dir/run_stats.json` and the JSONL
  regardless of whether the message was delivered.

The report is labeled **script stage** because TTS and publish happen on a later
consumer loop iteration. It never means an episode shipped.

## Manual operations

```bash
# Force-process a pending Rundown job immediately
uv run python -c "
import sqlite3
conn = sqlite3.connect('/persist/my-podcasts/state.sqlite3')
conn.execute(\"UPDATE pending_the_rundown SET process_after = datetime('now') WHERE status = 'pending'\")
conn.commit()
print('All pending jobs marked as due now')
"
```

## Failure modes

### Shared opencode-serve is down
- Check: `systemctl status opencode-serve.service --no-pager`
- Health: `curl -s http://127.0.0.1:4096/global/health`
- Restart: `sudo systemctl restart opencode-serve.service`
- The consumer will log errors and retry on the next poll cycle.

### Writer times out or returns empty output
- Check logs: `journalctl -u my-podcasts-consumer --since today --no-pager | rg "Failed Rundown job|retry #|empty script|900 seconds"`
- Check opencode-serve logs: `journalctl -u opencode-serve.service -n 50 --no-pager`
- Retries now back off automatically and preserve collection artifacts for reuse.

### Job stays pending
- Consumer service may be down: `sudo systemctl status my-podcasts-consumer`
- Compare `process_after` to current time before assuming it is stuck.
- If `status='errored'`, the retry budget is exhausted and manual reset is required.
- Force retry by restarting: `sudo systemctl restart my-podcasts-consumer`

## Deep checks

See `REFERENCE.md` for:
- Article fetcher fallback chain debugging
- Writer prompt location and customization
- Database schema details
- New module reference

For shared FP Digest / Rundown incident handling, also use `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`.

## A story resolved to the wrong article

Since 2026-08-17 this should be impossible by construction, but if you suspect it:

1. Look at `writer_inputs.json` in the work dir. Every entry carries `source_path`
   and `miss_reason`. A resolved entry names the exact file its text came from —
   open it and check the headline matches.
2. The resolver (`pipeline/article_resolver.py`) matches on **exact headline, then
   unique slug, and nothing else**. There is no fuzzy tier; if you find one, it has
   been reintroduced and should be removed. See AGENTS.md §"Directive→article
   matching" for the measurements (a wrong article scored ≥1 query word in 50 of 54
   real directives, and tied the correct one at a perfect score in one).
3. `miss_reason: slug_ambiguous` means two indexed headlines shared the directive's
   slug and the resolver **refused to guess**. That is working as designed — the
   story is dropped, visibly, rather than narrated from the wrong article. Fix it by
   looking at why two headlines collided (slugs truncate at 50 chars).
4. A `(N w/ shadow)` count on the funnel's `WRITE` line means N misses had a
   headline-similarity candidate. **This is a diagnostic, not a verdict.** The most
   common miss cause is the article being absent from the index entirely, in which
   case the shadow is necessarily a wrong headline that merely looks plausible. Do
   not "restore fuzzy matching" on the strength of it; check candidates against
   ground truth first.
