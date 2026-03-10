# The Rundown Pivot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rename "Things Happen" to "The Rundown", decouple from Levine email trigger, pivot to a timer-based daily digest covering everything except foreign policy.

**Architecture:** Levine emails cache links to disk instead of creating DB jobs. A systemd timer at 5 PM ET Mon-Fri creates the job, runs the collector (which reads from Levine, Semafor, and Zvi caches), and launches the agent. The consumer's role narrows to polling for completed scripts, running TTS, and publishing.

**Tech Stack:** Python, SQLite, Click CLI, systemd, NixOS, opencode-serve

**Design doc:** `docs/plans/2026-03-09-the-rundown-pivot-design.md`

---

### Task 1: Levine Link Cache

Replace `_maybe_queue_things_happen()` in `processor.py` with `_cache_levine_links()` that writes extracted links to `/persist/my-podcasts/levine-cache/{date}.json` instead of inserting a DB row.

**Files:**
- Modify: `pipeline/processor.py:56-88` (rename function, write to cache instead of DB)
- Test: `pipeline/test_things_happen_processor.py` (update `test_levine_email_creates_pending_things_happen_job`)

**Step 1: Write the test**

Update `test_levine_email_creates_pending_things_happen_job` → `test_levine_email_caches_links`. Instead of checking `store.list_due_things_happen()`, check that a JSON file was written to the levine cache directory with the expected link data.

```python
def test_levine_email_caches_links(tmp_path):
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    # ... existing email setup ...
    # Call process_email_bytes with levine_cache_dir=levine_cache
    cached_files = list(levine_cache.glob("*.json"))
    assert len(cached_files) == 1
    data = json.loads(cached_files[0].read_text())
    assert isinstance(data, list)
    assert all("link_text" in item for item in data)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_processor.py::test_levine_email_caches_links -v`

**Step 3: Implement**

In `processor.py`:
- Rename `_maybe_queue_things_happen` → `_cache_levine_links`
- Add `levine_cache_dir: Path` parameter
- Write links JSON to `{levine_cache_dir}/{date_str}.json` (overwrite if same date — Levine sometimes re-sends)
- Remove `store` parameter (no longer needed)
- Update the call site in `process_email_bytes` (~line 165)
- Add `levine_cache_dir` parameter to `process_email_bytes` and `process_r2_email_key` with default `/persist/my-podcasts/levine-cache`

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_processor.py -v`

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "refactor: cache Levine links to disk instead of creating DB jobs"
```

---

### Task 2: DB Schema — New `pending_the_rundown` Table

Add a `pending_the_rundown` table (mirrors `pending_fp_digest` — no `links_json` or `email_r2_key` columns) and corresponding StateStore methods.

**Files:**
- Modify: `pipeline/db.py:31-70` (add table to SCHEMA), `pipeline/db.py:265-324` (add new methods)
- Test: `pipeline/test_things_happen_db.py` (add tests for the new table)

**Step 1: Write tests**

```python
def test_insert_and_list_due_the_rundown(tmp_path):
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown("2026-03-09")
    assert job_id is not None
    due = store.list_due_the_rundown()
    assert len(due) == 1
    assert due[0]["date_str"] == "2026-03-09"
    store.close()

def test_no_duplicate_the_rundown(tmp_path):
    store = StateStore(tmp_path / "test.db")
    store.insert_pending_the_rundown("2026-03-09")
    assert store.insert_pending_the_rundown("2026-03-09") is None
    store.close()

def test_mark_the_rundown_completed(tmp_path):
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown("2026-03-09")
    store.mark_the_rundown_completed(job_id)
    assert store.list_due_the_rundown() == []
    store.close()

def test_the_rundown_session_id(tmp_path):
    store = StateStore(tmp_path / "test.db")
    job_id = store.insert_pending_the_rundown("2026-03-09")
    assert store.get_the_rundown_session_id(job_id) is None
    store.set_the_rundown_session_id(job_id, "ses_123")
    assert store.get_the_rundown_session_id(job_id) == "ses_123"
    store.clear_the_rundown_session_id(job_id)
    assert store.get_the_rundown_session_id(job_id) is None
    store.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_things_happen_db.py -v`

**Step 3: Implement**

Add to `SCHEMA`:
```sql
CREATE TABLE IF NOT EXISTS pending_the_rundown (
    id TEXT PRIMARY KEY,
    date_str TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    process_after TEXT NOT NULL,
    opencode_session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Add StateStore methods mirroring FP Digest: `insert_pending_the_rundown`, `list_due_the_rundown`, `mark_the_rundown_completed`, `set_the_rundown_session_id`, `get_the_rundown_session_id`, `clear_the_rundown_session_id`.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_db.py -v`

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "feat: add pending_the_rundown DB table and StateStore methods"
```

---

### Task 3: Collector Refactor — Read Levine from Cache

Refactor `collect_all_artifacts()` to read Levine links from the cache directory instead of receiving them as a parameter.

**Files:**
- Modify: `pipeline/things_happen_collector.py:25-78` (new signature, read from levine cache)
- Test: `pipeline/test_things_happen_collector.py` (update all tests)

**Step 1: Write test**

```python
def test_levine_links_read_from_cache(tmp_path):
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()
    today = _today_et()
    # Write a cached Levine links file
    links = [{"link_text": "Story", "raw_url": "http://example.com/story", "headline_context": "A story happened"}]
    (levine_cache / f"{today}.json").write_text(json.dumps(links))
    # ... call collect_all_artifacts with levine_cache_dir, no links_raw ...
    # Assert article file was written
```

Also add a test for when no Levine cache exists (Semafor + Zvi only):

```python
def test_collector_works_without_levine_links(tmp_path):
    levine_cache = tmp_path / "levine-cache"
    levine_cache.mkdir()  # empty
    # ... call collect_all_artifacts, verify it completes with Semafor/Zvi only
```

**Step 2: Run to verify failures**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`

**Step 3: Implement**

Change the `collect_all_artifacts` signature:
```python
def collect_all_artifacts(
    job_id: str,
    work_dir: Path,
    levine_cache_dir: Path | None = None,
    scripts_source_dir: Path | None = None,
    fp_routed_dir: Path | None = None,
    zvi_cache_dir: Path | None = None,
    semafor_cache_dir: Path | None = None,
    lookback_days: int = 2,
) -> None:
```

- Remove `links_raw` parameter
- Read Levine links from `{levine_cache_dir}/{date}.json` files within lookback window
- For each link file found, load JSON, resolve URLs, fetch articles (same as current Phase 1)
- If no Levine links found, skip Phase 1 Levine collection and proceed with Semafor + Zvi

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_collector.py -v`

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "refactor: collector reads Levine links from cache directory"
```

---

### Task 4: Agent Prompt Rewrite

Rewrite `build_agent_prompt()` to reflect The Rundown identity, co-equal sources, and smart-friend-at-a-bar writing ethos.

**Files:**
- Modify: `pipeline/things_happen_agent.py:32-60` (rewrite prompt)
- Test: `pipeline/test_things_happen_agent.py:51-65` (update prompt assertions)

**Step 1: Write test**

```python
def test_build_agent_prompt_reflects_rundown_identity():
    job = {"id": "test-id", "date_str": "2026-03-09"}
    work_dir = Path("/tmp/test")
    prompt = build_agent_prompt(job, work_dir)
    assert "The Rundown" in prompt
    assert "Matt Levine" not in prompt or "co-equal" in prompt  # Levine is not privileged
    assert "foreign policy" in prompt.lower()  # FP skip note
    assert "Semafor" in prompt
    assert "Zvi" in prompt or "zvi" in prompt
```

**Step 2: Run to verify failure**

Run: `uv run pytest pipeline/test_things_happen_agent.py::test_build_agent_prompt_reflects_rundown_identity -v`

**Step 3: Implement**

Rewrite the prompt in `build_agent_prompt()`:
- Identity: "The Rundown" daily current-affairs digest
- Scope: business, technology, AI, law, media, science, culture — everything except foreign policy
- Sources: three co-equal sources (Levine links, Semafor, Zvi) in `articles/`. Some days may have no Levine content.
- Writing ethos: conversational, opinionated, explains complexity without condescension. Treats the listener as smart. Has a point of view. Draws connections. Not just summarizing — add color.
- No specific negative rules (no "don't use the word X")
- Keep: FP skip note, context window dedup, plan approval step, script output path

Also remove `links_json` from the prompt since the job no longer carries it. The prompt should tell the agent to read all articles from the work dir rather than referencing "original newsletter links."

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_agent.py -v`

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "feat: rewrite agent prompt for The Rundown identity"
```

---

### Task 5: CLI Command — `the-rundown`

Add a `the-rundown` CLI command (mirroring `fp-digest`) that creates a job, runs the collector, and launches the agent.

**Files:**
- Modify: `pipeline/__main__.py` (add `the-rundown` command with `--date`, `--lookback`, `--dry-run`)
- Test: integration-test via existing test patterns

**Step 1: Write test**

Test the dry-run path (no DB, no agent launch):

```python
def test_the_rundown_cli_dry_run(tmp_path, monkeypatch):
    # ... mock collector, verify CLI creates work dir and runs collection
```

**Step 2: Run to verify failure**

Run: `uv run pytest pipeline/test_things_happen_processor.py -v` (or wherever the test lives)

**Step 3: Implement**

```python
@cli.command("the-rundown")
@click.option("--date", "date_str", default=None, type=str)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--lookback", "lookback_days", default=None, type=int)
def the_rundown_command(date_str, dry_run, lookback_days):
    """Create and launch a Rundown episode."""
    ...
```

The command:
1. Computes date (default: today ET)
2. Creates `pending_the_rundown` job in DB (or finds existing)
3. Computes lookback (adaptive or override)
4. Runs collector with all cache dirs
5. Launches agent on opencode-serve
6. Stores session ID in DB
7. Returns (consumer handles TTS)

Dry-run mode: runs collection only, no DB job, no agent launch.

**Step 4: Run tests**

Run: `uv run pytest pipeline/ -v`

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "feat: add the-rundown CLI command"
```

---

### Task 6: Consumer Simplification

Remove collection and agent launch from the consumer's Things Happen loop. Consumer now only handles: poll for script → TTS → publish, plus stuck-agent timeout.

**Files:**
- Modify: `pipeline/consumer.py:274-381` (simplify the Things Happen loop to use new DB table)
- Test: `pipeline/test_consumer.py` (update tests)

**Step 1: Update tests**

- Remove `test_consume_forever_launches_agent_for_due_job` (agent launch moves to CLI)
- Update remaining tests to use `pending_the_rundown` table instead of `pending_things_happen`
- Keep stuck-agent timeout test, update to use new table/methods

**Step 2: Run to verify failures**

Run: `uv run pytest pipeline/test_consumer.py -v`

**Step 3: Implement**

The consumer loop for The Rundown becomes:
```python
# Process any due Rundown jobs
due_jobs = store.list_due_the_rundown()
for job in due_jobs:
    work_dir = Path(f"/tmp/the-rundown-{job['id']}")
    script_file = work_dir / "script.txt"
    session_id = store.get_the_rundown_session_id(job["id"])

    if script_file.exists():
        # Script done — TTS + publish
        process_the_rundown_job(job, store, r2_client, script_path=script_file)
        # Copy to persistent scripts, cleanup, stop agent
    elif is_agent_running(session_id):
        # Agent still running — check timeout
        ...
    elif session_id:
        # Agent died without producing script — clear session, will be relaunched by next timer run
        store.clear_the_rundown_session_id(job["id"])
```

Key change: the consumer does NOT launch agents or run collection. If a job has no script and no agent, it simply waits for the next timer run to launch the agent.

Also update imports and references: `process_things_happen_job` → `process_the_rundown_job`.

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_consumer.py -v`

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "refactor: simplify consumer to poll-only for The Rundown"
```

---

### Task 7: Rename Processor & Metadata

Rename `things_happen_processor.py` metadata constants and episode fields to match The Rundown.

**Files:**
- Modify: `pipeline/things_happen_processor.py:24-28,104-118` (FEED_SLUG, CATEGORY, preset_name, source_tag, title)
- Test: `pipeline/test_things_happen_processor.py` (update assertions)

**Step 1: Update tests**

Change assertions:
- `feed_slug="the-rundown"` (was `"things-happen"`)
- `category="News"` (was `"Business"`)
- Episode title format: `"{date} - The Rundown"` (was `"Things Happen"`)
- `source_tag="the-rundown"` (was `"things-happen"`)
- `preset_name="The Rundown"` (was `"Things Happen - Money Stuff Links"`)

**Step 2: Run to verify failures**

Run: `uv run pytest pipeline/test_things_happen_processor.py -v`

**Step 3: Implement**

```python
FEED_SLUG = "the-rundown"
CATEGORY = "News"
...
episode_title = f"{date_str} - The Rundown"
episode_slug = f"{date_str}-the-rundown"
episode_r2_key = f"episodes/{FEED_SLUG}/{episode_slug}.mp3"
...
source_tag="the-rundown",
preset_name="The Rundown",
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_processor.py -v`

**Step 5: Commit**

```bash
git -c commit.gpgsign=false commit -am "refactor: rename Things Happen metadata to The Rundown"
```

---

### Task 8: Update All References & Paths

Update remaining references across the codebase: work dir paths, persistent script paths, cleanup patterns, AGENTS.md.

**Files:**
- Modify: `pipeline/consumer.py` (work dir pattern `/tmp/the-rundown-*`, scripts path)
- Modify: `pipeline/things_happen_collector.py:136` (scripts source dir default)
- Modify: `pipeline/__main__.py` (imports if needed)
- Modify: `AGENTS.md` (Things Happen → The Rundown throughout)

**Step 1: Search and update**

Use grep to find all remaining `things-happen` and `Things Happen` references (excluding test files, design docs, and git history).

**Step 2: Update each reference**

- `/persist/my-podcasts/scripts/things-happen/` → `/persist/my-podcasts/scripts/the-rundown/`
- `/tmp/things-happen-*` → `/tmp/the-rundown-*` (cleanup pattern in `_cleanup_old_work_dirs`)
- Work dir: `Path(f"/tmp/things-happen-{job['id']}")` → `Path(f"/tmp/the-rundown-{job['id']}")`
- AGENTS.md: full rewrite of the Things Happen section

**Step 3: Run full test suite**

Run: `uv run pytest -v`

**Step 4: Commit**

```bash
git -c commit.gpgsign=false commit -am "refactor: update all path references from things-happen to the-rundown"
```

---

### Task 9: Systemd Timer & Service

Add `the-rundown.timer` and `the-rundown.service` to the workstation NixOS config. Remove the consumer's role in creating Things Happen jobs.

**Files:**
- Modify: `~/projects/workstation/hosts/devbox/configuration.nix` (add timer + service)

**Step 1: Add service**

Mirror the `fp-digest` service pattern:
```nix
systemd.services.the-rundown = {
    description = "The Rundown daily podcast generation";
    wants = [ "network-online.target" "opencode-serve.service" ];
    after = [ "network-online.target" "opencode-serve.service" ];
    # ... same path, env as fp-digest ...
    serviceConfig = {
        Type = "oneshot";
        ExecStart = "... uv run python -m pipeline the-rundown";
        TimeoutStartSec = 600;
    };
};
```

**Step 2: Add timer**

```nix
systemd.timers.the-rundown = {
    description = "Run The Rundown daily at 5 PM ET Mon-Fri";
    wantedBy = [ "timers.target" ];
    timerConfig = {
        OnCalendar = "Mon..Fri *-*-* 17:00:00";
        Persistent = true;
        RandomizedDelaySec = "5min";
    };
};
```

**Step 3: Deploy**

```bash
cd ~/projects/workstation
git -c commit.gpgsign=false commit -am "feat: add The Rundown systemd timer (5 PM ET Mon-Fri)"
sudo nixos-rebuild switch --flake .#devbox
```

**Step 4: Verify**

```bash
systemctl list-timers | grep rundown
sudo systemctl status the-rundown.timer
```

**Step 5: Push**

```bash
git push  # workstation
cd ~/projects/my-podcasts && git push  # my-podcasts
```

---

### Task 10: Restart Consumer & Verify

Restart the consumer service to pick up all code changes, verify everything works.

**Step 1: Run full test suite**

```bash
cd ~/projects/my-podcasts
uv run pytest -v
```

**Step 2: Restart consumer**

```bash
sudo systemctl restart my-podcasts-consumer
```

**Step 3: Verify consumer is healthy**

```bash
sudo journalctl -u my-podcasts-consumer --since "1 minute ago" --no-pager
```

**Step 4: Verify timer is scheduled**

```bash
systemctl list-timers | grep rundown
```

**Step 5: Optional — dry run**

```bash
uv run python -m pipeline the-rundown --dry-run
```
