# The Argument Audio Reports — Phase 2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (implementer + spec review + code review per task) to implement this plan.

**Goal:** The consumer discovers new *The Argument* episodes on audioboom, turns
each into a "Report:" briefing (Phase 1 pipeline), and publishes it to the
`yglesias` feed — unattended, with bounded retries, alerts, and a CLI to
backfill/reset.

**Architecture:** Design `docs/plans/2026-10-18-argument-audio-report-design.md`
(read "State and failure handling" first). Phase 1 plan + handoff state:
`docs/plans/2026-10-18-argument-audio-report.md`. Pilot measurements:
`docs/plans/2026-10-18-argument-audio-report-pilot.md`. Phase 2 adds: a killable
child that runs Phase 1's `run_episode_to_script`; a `podcast_episodes` job table
keyed by `(source, guid)`; a shared publish identity + reconciling publish; a
discovery poller with health alerts; an executor; consumer wiring; and an
enqueue/reset/list CLI. **The consumer is the only executor and the only
publisher.** The CLI only writes job rows.

**Tech stack:** Python 3 / uv / pytest, sqlite3, `multiprocessing` (`spawn`),
click. Run tests with `uv run pytest <path> -q`; full suite `uv run pytest -q`.

**Worktree:** `/home/dev/projects/my-podcasts/.worktrees/argument-audio-phase2`,
branch `argument-audio-phase2`, stacked on `argument-audio-report` (PR #37).

**Ground rules (repo AGENTS.md; violating these has caused real incidents):**
- Tests never touch the network, `/persist`, or real API keys (autouse guards in
  `pipeline/conftest.py`; `MY_PODCASTS_STATE_DB`, `SCRIPT_ARCHIVE_ROOT` are
  redirected to `tmp_path`). A spawned child does not inherit conftest
  monkeypatches: give it `pipeline.tts._phase_testing.deny_network` as bootstrap
  and fake runners.
- Never edit `pipeline/tts/asr.py:ASR_PROMPT`/its config, nor the email-path
  prompts in `pipeline/transcript_report.py`.
- Persist files that a later run reads atomically
  (`report_engine.persist_raw_output`).
- Bare `git commit`, one commit per task. No destructive git in shared checkouts.

## Decisions taken while expanding (binding for implementers)

1. **The child runs the whole `run_episode_to_script`** (download, transcribe,
   write), not only download+transcribe. Both the Gemini SDK (per-read timeout)
   and opencode can trickle; one wall-clock bound covers both. Deadline
   `CHILD_DEADLINE_S = 1500` (25 min): pilot measured ~4.5 min end to end for
   80-85 min episodes; Sep 10 (97 min) is ~4 min of ASR; the writer's own
   timeout is 900 s. TTS + publish run in the parent (yglesias is OpenAI
   `shimmer`; the TTS path has its own bounds).
2. **Retries are cheap by construction.** A retry re-runs the child against the
   same work dir: `download_enclosure` skips an existing file, the transcript and
   piece checkpoints are reused, the raw writer reply is reused (keyed by the
   full instruction). Only the failed stage is re-paid.
3. **Retry policy:** every failure counts against one budget: backoff 6 h, the
   4th failure -> `errored` + one `send_alert` attempt. **Terminal at once**
   (`errored` on the first failure): stage `claim` (work dir owned by another
   episode — a bug, retrying cannot help) and `TranscriptionError` with HTTP
   `code == 400` (invalid request/key; Gemini reports a bad key as 400). An
   operator fixes the cause and runs `podcast-report reset`.
4. **Publish identity** is computed once at admission and stored on the row:
   `display_title = f"Report: {date} - {source.title_label} - {item.title}"`,
   `date = item.pub_date (UTC) %Y-%m-%d`, `(slug, r2_key) =
   script_processor.episode_identity(display_title, feed_slug, date)` (the exact
   formula `publish_script` already uses, extracted, output unchanged).
   `r2_key` is `UNIQUE` in the table.
5. **Episode `<pubDate>` is publish time** (what `publish_script` already does
   for every feed); the title carries the original date. Backfills therefore
   appear as new items, which is what a listener needs to get them.
6. **Show notes:** `work_dir/show_notes.md` = `## Episode Summary` + the writer's
   summary, then a line saying this is an unofficial AI briefing from the public
   audio with the original episode link. `source_url` = the item's `link`.
   Category `News` (matches the feed's existing episodes).
7. **Work dirs:** production `PODCAST_WORK_ROOT =
   /persist/my-podcasts/podcast-work/<source>/<sha256(guid)[:16]>` (dry runs keep
   their separate `.../dry-run/...` root). On completion the executor deletes
   `audio.mp3` (~70 MB/episode; `acquisition.json` keeps its sha256); transcript,
   checkpoints, raw replies and script stay.
8. **Admission metadata vs acquisition:** `item.json` is written once and never
   rewritten (drop the per-run `last_requested_at` write). A fresh download
   writes `acquisition.json` `{url, downloaded_at, bytes, sha256}` — the URL
   actually fetched, which may differ from `item.json`'s by tracking params.
9. **Duration headroom tracking:** on completion record
   `decoded_minus_rss_s` and the allowed tolerance in the row's `stats_json` and
   log it; if it exceeds 75% of the allowed tolerance, send one `info` alert
   (dynamic ad insertion already uses ~40%; this is the early warning before
   refusals start).
10. **Discovery health:** consecutive fetch/parse failures **or** a feed that
    parses to zero items count as one failed tick; the 4th consecutive failed
    tick sends one `error` alert (not again until a success resets the count).
    Discovery runs on its own 6 h timer, separate from `_last_blog_poll`.
11. **Quiet signed URLs:** set `httpx`/`httpcore` loggers to WARNING wherever we
    configure INFO logging (dry-run CLI, the child).

---

### Task 8: Killable child for one episode

**Files:**
- Modify: `pipeline/podcast_report.py` (`run_episode_to_script`: `on_stage`
  callback; `acquisition.json`; `item.json` written once)
- Modify: `pipeline/__main__.py` (dry-run: quiet `httpx`/`httpcore` loggers)
- Create: `pipeline/podcast_child.py`
- Test: `pipeline/test_podcast_report.py` (existing file if present, else the
  existing `run_episode_to_script` tests' file), `pipeline/test_podcast_child.py`,
  `pipeline/test_podcast_child_spawn.py`

**Step 1: failing tests for the `podcast_report` changes**
- `on_stage` receives `"claim"`, `"download"`, `"transcribe"`, `"write"` in
  order (fakes for download/transcriber/writer as the existing tests do).
- A fresh download writes `acquisition.json` with `url == item.enclosure_url`,
  `bytes`, `sha256` of `audio.mp3`, ISO `downloaded_at`; a run where
  `audio.mp3` already exists does not rewrite it.
- `item.json` bytes are identical before and after a second run (no
  `last_requested_at` churn); owner check still refuses a different guid.
- Dry-run CLI: after invoking, `logging.getLogger("httpx").level ==
  logging.WARNING` (and `httpcore`).

**Step 2:** run, see them fail. **Step 3:** implement:
```python
def run_episode_to_script(source, item, work_dir, *, carry_context=True,
                          transcriber_factory=None, progress=None,
                          on_stage: Callable[[str], None] | None = None) -> DryRunResult:
    stage = on_stage or (lambda _s: None)
    stage("claim"); _claim_work_dir(...)
    stage("download"); fresh = not audio_path.exists() or audio_path.stat().st_size == 0
    download_enclosure(...)
    if fresh: persist_raw_output(work_dir / ACQUISITION_NAME, json.dumps({...}))
    stage("transcribe"); ...
    stage("write"); ...
```
`_claim_work_dir`: when `item.json` exists and the owner matches, write nothing.

**Step 4: failing tests for `pipeline/podcast_child.py`** (in-process, no spawn):
`_child_body(source_key, work_dir, runner)` — the function the child runs:
- reads `job.json` (`item_metadata` shape) from `work_dir`, rebuilds the
  `PodcastItem`, calls `runner(source, item, work_dir, on_stage=...)`, writes
  `child_result.json` `{"ok": true, "script_sha256", "piece_count",
  "input_tokens", "output_tokens"}` atomically;
- `on_stage` writes `child_stage.json` `{"stage": ...}` atomically;
- a `TranscriptionError` -> `{"ok": false, "stage": <last stage>, "reason":
  exc.reason, "code": exc.code, "detail": str(exc)}`; any other exception ->
  same with `reason = type(exc).__name__`; a missing/invalid `job.json` ->
  stage `"job"`, reason `"bad_job"`.

**Step 5:** implement. Public API:
```python
CHILD_DEADLINE_S = 1500.0
JOB_NAME, RESULT_NAME, STAGE_NAME = "job.json", "child_result.json", "child_stage.json"

@dataclass(frozen=True)
class ChildOutcome:
    ok: bool
    stage: str          # last stage reached, or "child"
    reason: str         # "" when ok; else e.g. asr_timeout, deadline, child_no_result,
                        # invalid_result, spawn_failed, <ExceptionName>
    code: int | None
    detail: str
    result: dict[str, Any] | None
    elapsed_s: float

def run_episode_in_child(source: PodcastSource, item: PodcastItem, work_dir: Path, *,
                         deadline_s: float = CHILD_DEADLINE_S,
                         _runner: str = "pipeline.podcast_report:run_episode_to_script",
                         _bootstrap: Callable[[], Any] | None = None) -> ChildOutcome
```
Parent: `mkdir`; unlink stale `child_result.json`/`child_stage.json`; write
`job.json` = `item_metadata(source, item)` atomically; compute an absolute
`time.monotonic()` deadline; spawn via `multiprocessing.get_context("spawn")`
with args `(source.key, str(work_dir), deadline, os.getpid(), _runner,
_bootstrap)` only (small, picklable; never the transcript); poll every 0.5 s for
`child_result.json` or process exit; at the deadline `kill()` + bounded
`join(5)`, then read the result ONCE more (a result that landed as the clock
expired wins). Reuse the shape of `pipeline/tts/gemini_phase.py`
(`_make_process`, `_Reaper`, `_await_child`, `_watchdog_loop`, `_child_main`
with `os._exit` in `finally`) — copy the idea, do not import its private names.
The child: starts a watchdog thread (exit 3 if `getppid()` changes or the
deadline + 30 s grace passes), runs `_bootstrap`, configures
`logging.basicConfig(level=INFO)` with `httpx`/`httpcore` at WARNING, resolves
`_runner` (`module:attr` via importlib) and calls `_child_body`. **Parent
validation of an ok result:** `script.txt` exists, is non-empty, and its sha256
equals `result["script_sha256"]`; otherwise `invalid_result`. A not-ok result
keeps its stage/reason/code. A dead child without a result is
`child_no_result` (stage from `child_stage.json` when readable, else `"child"`).
Deadline -> reason `deadline`, stage from `child_stage.json`. `Process.start()`
raising -> `spawn_failed`. `KeyboardInterrupt`/`SystemExit` kill the child and
propagate.

**Step 6: spawn tests** (`test_podcast_child_spawn.py`, real spawn, fake runners
defined at module level in a non-test helper `pipeline/_podcast_child_testing.py`
so the child can import them, bootstrap `deny_network`):
- happy runner writes `script.txt` and returns a stub result -> `ok`, result
  validated;
- runner that blocks forever after `on_stage("transcribe")` with
  `deadline_s=<measured startup*3+2, min 4>` -> `ok False`, `reason
  "deadline"`, `stage "transcribe"`, child pid dead (`os.kill(pid,0)` raises),
  wall time < deadline + 7 s;
- runner raising `TranscriptionError("x", reason="asr_error", code=400)` ->
  reason/code carried;
- runner that `os._exit(1)`s -> `child_no_result`;
- runner returning but writing a script whose sha differs from what it reports
  -> `invalid_result`.

**Step 7:** run all, then `uv run pytest pipeline/test_podcast_*.py -q`.
**Step 8:** commit `Podcast reports: killable child for one episode`.

---

### Task 9: `podcast_episodes` job table

**Files:** Modify `pipeline/db.py`; Test `pipeline/test_db_podcast_episodes.py`.

**Schema** (append to `SCHEMA`; `CREATE TABLE IF NOT EXISTS`, so existing DBs
gain it on next open):
```sql
CREATE TABLE IF NOT EXISTS podcast_episodes (
    source TEXT NOT NULL,
    guid TEXT NOT NULL,
    item_json TEXT NOT NULL,          -- frozen admission metadata (item_metadata shape)
    pub_date TEXT NOT NULL,           -- ISO-8601 UTC, ordering key
    display_title TEXT NOT NULL,
    episode_slug TEXT NOT NULL,
    r2_key TEXT NOT NULL UNIQUE,
    admitted_via TEXT NOT NULL,       -- 'poll' | 'enqueue'
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|in_progress|completed|errored
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at TEXT NOT NULL,    -- ISO-8601 UTC
    stats_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, guid)
);
CREATE TABLE IF NOT EXISTS podcast_source_health (
    source TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_success_at TEXT,
    alerted INTEGER NOT NULL DEFAULT 0
);
```
**Constants:** `PODCAST_MAX_FAILURES = 4`, `PODCAST_RETRY_BACKOFF =
timedelta(hours=6)`, `PODCAST_HEALTH_ALERT_AFTER = 4`.

**Methods** (all take an explicit `now: datetime` where time matters, so tests
need no clock patching; every write commits):
- `admit_podcast_episode(*, source, guid, item_json, pub_date, display_title,
  episode_slug, r2_key, admitted_via, now) -> bool` — `INSERT ... ON CONFLICT
  (source, guid) DO NOTHING`; True if inserted. A *different* `(source, guid)`
  with the same `r2_key` raises `sqlite3.IntegrityError` (do not swallow).
- `get_podcast_episode(source, guid) -> dict | None`
- `list_podcast_episodes(source: str | None = None) -> list[dict]` (pub_date desc)
- `next_due_podcast_episode(now) -> dict | None` — `status='pending' AND
  next_attempt_at <= now`, `ORDER BY pub_date ASC LIMIT 1`.
- `claim_podcast_episode(source, guid) -> bool` — `UPDATE ... SET
  status='in_progress' WHERE ... AND status='pending'`; True iff 1 row changed.
- `complete_podcast_episode(source, guid, *, stats: dict | None)` — status
  completed, `last_error NULL`, `stats_json`.
- `fail_podcast_episode(source, guid, error, *, now, terminal=False) ->
  RetryUpdate` — `failure_count+1`; `terminal` or count >= 4 -> `errored`
  (`process_after=None`, `exhausted=True`); else `pending`, `next_attempt_at =
  now + 6h`. `error[:500]`.
- `reset_podcast_episode(source, guid, *, now) -> str` — returns the prior
  status; `errored`/`pending` -> `pending`, `failure_count 0`, `last_error NULL`,
  `next_attempt_at now`; `completed`/`in_progress` -> `ValueError` (re-publishing
  is not supported; in_progress means the consumer is on it); unknown ->
  `KeyError`.
- `recover_interrupted_podcast_episodes() -> list[dict]` — `in_progress ->
  pending`, counting the interruption as a failure (`failure_count + 1`,
  `last_error "interrupted: consumer stopped mid-run"`; the
  `PODCAST_MAX_FAILURES`-th makes it `errored`, so a job that kills the
  consumer cannot crash-loop). Keeps `next_attempt_at` (runs at once). Returns
  `{source, guid, display_title, failure_count, status}` per recovered row.
- `record_podcast_discovery(source, *, ok: bool, error: str | None, now) ->
  tuple[int, bool]` — ok: count 0, `alerted 0`, `last_success_at now`; failure:
  count+1, `last_error`. Returns `(consecutive_failures, should_alert)` where
  `should_alert` is True exactly once: when count reaches
  `PODCAST_HEALTH_ALERT_AFTER` and `alerted` was 0 (then set `alerted 1`).

**Tests (write first, see fail, implement, see pass):** admission
idempotent + frozen (second admit with a changed title returns False and leaves
row unchanged); r2_key collision raises; due ordering oldest-first and
not-before `next_attempt_at`; claim only from pending (second claim False);
fail x3 -> pending with +6h each, 4th -> errored/exhausted; terminal -> errored
on first; reset semantics incl. refusals; recovery flips only in_progress;
health: alert exactly once at 4, success resets, then alerts again after 4 more;
opening a StateStore on a DB created by the old schema (create one with the
`episodes` table only) adds both tables.

**Commit:** `Podcast reports: podcast_episodes job table`.

---

### Task 10: Publish identity, admission helper, reconciling publish

**Files:**
- Modify: `pipeline/script_processor.py` — extract
  ```python
  @dataclass(frozen=True)
  class EpisodeIdentity:
      slug: str
      r2_key: str

  def episode_identity(title: str, feed_slug: str, date_str: str) -> EpisodeIdentity:
      slug = f"{date_str}-{_slugify(title)}"
      return EpisodeIdentity(slug=slug, r2_key=f"episodes/{feed_slug}/{slug}.mp3")
  ```
  and use it in `publish_script` (behavior byte-identical).
- Modify: `pipeline/db.py` — `get_episode_by_r2_key(r2_key) -> Episode | None`
  (reuse the `list_episodes` row mapping).
- Create: `pipeline/podcast_jobs.py`:
  ```python
  PODCAST_WORK_ROOT = Path("/persist/my-podcasts/podcast-work")
  @dataclass(frozen=True)
  class PodcastIdentity: display_title: str; date_str: str; slug: str; r2_key: str
  def podcast_identity(source, item) -> PodcastIdentity
  def work_dir_for(source_key: str, guid: str, root: Path = PODCAST_WORK_ROOT) -> Path
  def admit_item(store, source, item, *, via: str, now: datetime) -> bool
      # refuses (ValueError) an item with pub_date > now; stores
      # json.dumps(item_metadata(source, item)) as item_json
  def item_from_row(row: dict) -> PodcastItem   # rebuild from item_json
  ```
- Create: `pipeline/podcast_publish.py`:
  ```python
  def publish_podcast_episode(row: dict, work_dir: Path, *, store, r2_client) -> str
      # returns "published" or "reconciled"
  ```
  1. Recompute `episode_identity(row["display_title"], source.feed_slug,
     row date)`; if it differs from the row's `episode_slug`/`r2_key`, raise
     `RuntimeError` **before** any TTS (identity drift = bug).
  2. If `store.get_episode_by_r2_key(row["r2_key"])` exists: call
     `regenerate_and_upload_feed(store, r2_client)` and return `"reconciled"`
     (no TTS, no insert).
  3. Else write `work_dir/show_notes.md` (Decision 6; summary from
     `summary.txt` when present) atomically and call `publish_script(
     script_file=work_dir/"script.txt", title=row["display_title"],
     feed_slug=source.feed_slug, store=store, r2_client=r2_client,
     show_notes_file=..., category="News", date_str=row date,
     source_url=item.link or None)`; assert the returned `r2_key ==
     row["r2_key"]`; return `"published"`.
- Tests: `pipeline/test_podcast_jobs.py`, `pipeline/test_podcast_publish.py`,
  plus a pin in the existing `publish_script` tests that slug/r2_key are
  unchanged for a sample title.

**Publish tests** (patch `pipeline.script_processor.tts.render_episode` to write
a small file, a fake R2 recording `upload_file`/`put_object_bytes`; follow the
existing `publish_script` tests' fakes):
- happy path: one upload to `row["r2_key"]`, one `episodes` row with that key,
  title, `source_url`, category `News`, summary from show notes; feeds uploaded.
- failure **after audio upload** (fake `store.insert_episode` raises once): the
  retry re-renders/re-uploads the same key and inserts exactly one row.
- failure **after DB commit** (fake `regenerate_and_upload_feed` raises once):
  retry -> `"reconciled"`, no second TTS call, no second row, feeds uploaded.
- failure **during feed upload** (fake R2 `put_object_bytes` raises once):
  same as above.
- identity drift -> RuntimeError and zero TTS calls.

**Identity tests:** `display_title` format; date from UTC pub date (an item at
`2026-10-15T23:30-05:00` is `2026-10-16`); `admit_item` refuses future items,
stores the frozen JSON, returns False on second call.

**Commit:** `Podcast reports: publish identity and reconciling publish`.

---

### Task 11: Discovery poller

**Files:** Create `pipeline/podcast_poller.py`; Test
`pipeline/test_podcast_poller.py` (fixture `pipeline/fixtures/argument_feed.xml`).

```python
def discover(store, *, now: datetime,
             fetch: Callable[[str], str] = podcast_report.fetch_feed_xml,
             alert: Callable[..., bool] = send_alert,
             sources: Iterable[PodcastSource] = PODCAST_SOURCES) -> dict[str, int]:
    """Per source: fetch + parse; admit eligible_items(since=source.since, now=now)
    via admit_item(via="poll"). Returns {source.key: admitted_count}."""
```
Each source in its own try/except: an exception or zero parsed items ->
`record_podcast_discovery(ok=False, error=...)`; if `should_alert`, one
`alert(f"Podcast discovery for {source.name} failed {n} times in a row: {err}",
severity="error")` and log its boolean result. Success -> `ok=True`. An
`IntegrityError` on admission (r2_key collision) is logged and alerted once per
tick but does not abort the other items. Tests: admits only items in `[since,
now]` (fixture items vs a `since` between them); idempotent across two ticks;
fetch raising 4x -> exactly one alert; empty feed counts as failure; success
resets; one source failing does not stop another (two fake sources).

**Commit:** `Podcast reports: discovery poller with health alerts`.

---

### Task 12: Executor

**Files:** Create `pipeline/podcast_executor.py`; Test
`pipeline/test_podcast_executor.py`.

```python
def run_one_podcast_episode(store, r2_client, *, now: datetime | None = None,
        run_child=podcast_child.run_episode_in_child,
        publish=podcast_publish.publish_podcast_episode,
        alert=send_alert, work_root: Path = PODCAST_WORK_ROOT) -> str | None:
    """Run at most one due podcast job. Returns "<source>/<guid>: <outcome>" or None."""
```
1. `row = store.next_due_podcast_episode(now)`; none -> `None`.
2. `claim_podcast_episode`; False -> `None` (someone else changed it).
3. `source = get_podcast_source(row["source"])`, `item = item_from_row(row)`,
   `work_dir = work_dir_for(...)`.
4. `outcome = run_child(source, item, work_dir)`; not ok -> failure with
   `error = f"{outcome.stage}: {outcome.reason}: {outcome.detail}"`,
   `terminal = outcome.stage == "claim" or outcome.code == 400`.
5. `publish(row, work_dir, store=store, r2_client=r2_client)`; an exception ->
   failure `f"publish: {type(exc).__name__}: {exc}"` (not terminal).
6. Stats from `work_dir/transcript.json`: `decoded_duration_s`,
   `rss_duration_s`, `decoded_minus_rss_s`, `allowed_s` (same formula as
   `podcast_transcribe._check_duration`: import the constants, do not
   re-literal), pieces, tokens, child `elapsed_s`, publish mode. Unreadable
   manifest -> stats with `None`s (never fails the job). If
   `decoded_minus_rss_s > 0.75 * allowed_s` -> one `alert(..., severity="info")`.
7. `complete_podcast_episode(stats=...)`; delete `work_dir/audio.mp3`
   (`missing_ok`); print one summary line.
8. On failure: `retry = store.fail_podcast_episode(..., terminal=...)`; if
   `retry.exhausted`: `sent = alert(f"Podcast report {row['display_title']} gave
   up after {retry.failure_count} failure(s). Last error: {error}",
   severity="error")` and print `sent`. Never raises for a job failure.

**Tests** (fake `run_child`/`publish`/`alert`; real StateStore in tmp_path):
nothing due -> None, no child; happy path -> completed, stats stored, audio
deleted, no alert; child deadline -> pending +6h, `last_error` starts
`"transcribe: deadline"`; 4th failure -> errored + exactly one error alert;
`stage="claim"` -> errored on first failure + alert; `code=400` -> same;
publish raising -> pending, error starts `"publish:"`; headroom 80% -> one info
alert and still completed; two due rows -> one call runs only the oldest.

**Commit:** `Podcast reports: executor`.

---

### Task 13: Consumer wiring

**Files:** Modify `pipeline/consumer.py`; Test `pipeline/test_consumer.py` (or
`pipeline/test_consumer_podcast.py`).

- Module globals `_PODCAST_POLL_INTERVAL = 6 * 3600`, `_last_podcast_poll = 0.0`.
- Extract the loop body's new part into
  `_podcast_tick(store, r2_client, now_monotonic: float) -> None`: discovery when
  due (own try/except, prints errors, updates `_last_podcast_poll` only on
  completion of the attempt), then `run_one_podcast_episode` (own try/except).
  Called once per loop iteration after the blog poll.
- At the top of `consume_forever` (before `while True`): `n =
  store.recover_interrupted_podcast_episodes()` in try/except; print when the
  list is non-empty, and send one `error` alert naming each row that came back
  `status == "errored"` (the interruption exhausted its budget).
- Tests: `_podcast_tick` calls discovery on the first tick and not again within
  6 h, again after; a discovery exception does not prevent the executor call;
  an executor exception does not propagate; `consume_forever` calls recovery
  once before the first pull (patch `CloudflareQueueConsumer` to raise
  `KeyboardInterrupt` on `pull`, as existing consumer tests do — check
  `pipeline/test_consumer.py` for the established pattern).

**Commit:** `Podcast reports: wire discovery and executor into the consumer`.

---

### Task 14: CLI — enqueue, reset, list

**Files:** Modify `pipeline/__main__.py` (`podcast_report_group`); Test
`pipeline/test_podcast_report_cli.py`.

- `podcast-report enqueue --source S --guid G`: fetch the feed, `find_item`,
  `admit_item(via="enqueue", now=utcnow)` (bypasses `since`). Prints
  `enqueued <source>/<guid> as "<display_title>"` or, if the row exists,
  `already <status>` (+ for `errored`: "run podcast-report reset"). Exit 0 in
  both; exit 1 for item not found / future item / fetch error / r2_key
  collision. Opens `StateStore(get_state_db_path())` the way other CLI
  commands do (find the helper used by `the-rundown`).
- `podcast-report reset --source S --guid G`: `reset_podcast_episode`; prints
  `reset <source>/<guid> (was <status>)`; exit 1 with a message for
  completed/in_progress/unknown.
- `podcast-report list [--source S]`: one line per row: status, pub date,
  failures, title, last_error (truncated 120).
- Never runs the pipeline, never touches R2. Tests use CliRunner with patched
  `fetch_feed_xml` returning the fixture and `MY_PODCASTS_STATE_DB` already
  redirected by conftest.

**Commit:** `Podcast reports: enqueue/reset/list CLI`.

---

### Task 15: Docs

- `AGENTS.md`: new section "Podcast Audio Reports (The Argument)": flow,
  modules, state table + how to read it, CLI, work dirs, retry/terminal policy,
  alerts (discovery, give-up, headroom), cost, accepted gaps (duplicate Report
  if Slow Boring resumes; ad sentence may leak; `<pubDate>` = publish time).
  Add the new modules to "Core Paths". Update the Transcript Report Path
  "Registered feeds" note: the yglesias email transcript path is dormant for
  The Argument since Slow Boring stopped cross-posting after 2026-09-24.
- `pipeline/AGENTS.md`: a short "Podcast report jobs" entry under How To Read
  Job State (statuses, `next_attempt_at`, reset command).
- Update the Phase 1 plan's Handoff state.
- **Commit:** `Docs: podcast audio reports`.

---

### Task 16: Ship (after owner merges #37 and this PR; ask before restarting)

1. Pre-PR: full `uv run pytest -q`; adversarial-reviewer-astra on
   `git diff origin/argument-audio-report...HEAD` (or `origin/main...HEAD` once
   #37 is merged); fix; PR via `shepherding-pull-requests` (base
   `argument-audio-report` until #37 merges, then rebase onto main).
2. After merge, **with owner OK**: pull on host,
   `sudo systemctl restart my-podcasts-consumer`; confirm startup log.
3. `uv run python -m pipeline podcast-report enqueue --source argument --guid
   <G>` for Sep 10, Oct 1, Oct 8, Oct 15 (GUIDs in the Phase 1 plan's Handoff
   state). Watch `journalctl -u my-podcasts-consumer -f`: one per iteration,
   oldest first; verify `feeds/yglesias.xml` gains four "Report: ... The
   Argument ..." items, each playable.
4. Delete R2 previews `episodes/preview/argument-report-2026-10-{15,08}-1853dabc.mp3`.
5. Thursday 2026-10-22: confirm discovery admitted and published unattended
   (expected ~07:00-13:00 UTC). Schedule a wake for it.
