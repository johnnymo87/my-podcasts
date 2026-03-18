# Daily Podcast Hardening Docs Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Document the daily podcast hardening and incident-response workflow in `AGENTS.md`, `pipeline/AGENTS.md`, and repo-local skills before implementing the admin reset CLI.

**Architecture:** Keep `AGENTS.md` as the repo entrypoint and TOC, add `pipeline/AGENTS.md` as the domain-specific quick start for daily podcast operations, and store the detailed operational guidance in `.opencode/skills/`. Update the existing monitoring/Things Happen skills with the new hardening behavior, and add one shared daily-jobs operations skill for stuck or errored `fp-digest` / `the-rundown` jobs.

**Tech Stack:** Markdown, repo-local skills, AGENTS navigation docs

---

### Task 1: Add the domain TOC and quick start docs

**Files:**
- Modify: `AGENTS.md`
- Create: `pipeline/AGENTS.md`

**Step 1: Document the top-level structure**

Update `AGENTS.md` so it points readers to:
- `pipeline/AGENTS.md` for daily podcast operations
- `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- `.opencode/skills/operating-things-happen-digest/SKILL.md`
- the new shared daily-jobs operations skill

**Step 2: Add the pipeline domain guide**

Create `pipeline/AGENTS.md` with:
- quick start commands for checking today’s daily jobs
- where the hardening lives (timeouts, collection reuse, empty-script rejection, retry backoff, retry exhaustion)
- a short incident checklist for “Rundown or FP Digest is stuck / missing / errored”

**Step 3: Verify the docs are coherent**

Read both files and confirm the top-level doc points cleanly to the domain doc and skills.

### Task 2: Update existing operational skills

**Files:**
- Modify: `.opencode/skills/monitoring-my-podcasts-pipeline/SKILL.md`
- Modify: `.opencode/skills/operating-things-happen-digest/SKILL.md`

**Step 1: Expand monitoring skill**

Document:
- how to inspect `pending_fp_digest` and `pending_the_rundown`
- what `failure_count`, `last_error`, and `status='errored'` mean
- how collection reuse and retry backoff change incident expectations

**Step 2: Update Rundown/Things Happen skill**

Document the Rundown-side hardenings:
- 300-second writer timeout
- collection reuse via `collection_done.json`
- empty-script rejection at the writer boundary
- retry backoff and 12-hour exhaustion behavior

**Step 3: Verify skill content matches reality**

Check the documented commands and behaviors against the current code paths in `pipeline/consumer.py`, `pipeline/db.py`, `pipeline/fp_writer.py`, and `pipeline/rundown_writer.py`.

### Task 3: Add a shared daily-jobs operations skill

**Files:**
- Create: `.opencode/skills/operating-daily-podcast-jobs/SKILL.md`

**Step 1: Write the skill**

Create a concise operator skill covering:
- when to use it (stuck, missing, delayed, or errored FP Digest / The Rundown)
- quick checks for DB state, logs, and recent episodes
- how to interpret current hardening behavior
- the current manual recovery path until the admin CLI lands

**Step 2: Keep it CLI/ops oriented**

The skill should reference supported commands and state inspection, not raw architecture prose.

**Step 3: Verify discoverability**

Make sure the skill description includes the obvious search triggers: `errored`, `stuck`, `retry`, `fp-digest`, `the-rundown`, `missing episode`.

### Task 4: Final verification and tracking

**Files:**
- Modify: `.beads/issues.jsonl`

**Step 1: Read the final docs set**

Review:
- `AGENTS.md`
- `pipeline/AGENTS.md`
- updated skills
- new skill

Confirm they are consistent, non-duplicative, and point to the right places.

**Step 2: Run a lightweight verification**

At minimum, verify the documented operational commands still run:
- `uv run python -m pipeline --help`
- `sudo systemctl status my-podcasts-consumer --no-pager`

**Step 3: Update the beads issue and commit**

```bash
bd close <docs-issue> --reason "Documented daily podcast hardening and incident response flow."
bd sync --from-main
git add AGENTS.md pipeline/AGENTS.md .opencode/skills/... .beads/issues.jsonl docs/plans/2026-03-17-daily-podcast-hardening-docs-plan.md
git commit -m "docs: add daily podcast hardening runbooks"
```
