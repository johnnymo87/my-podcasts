# Things Happen Multi-Stage Architecture Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the Things Happen pipeline into a 5-phase deterministic architecture with local file caching, an Editor AI phase (Gemini 2.0 Flash), and a trailing context window.

**Architecture:** Python coordinates fetching and disk I/O. A direct Gemini API call generates the research plan. Python executes the plan and writes Exa/xAI/RSS artifacts to disk. Opencode Agent reads the disk and writes the final script.

**Tech Stack:** Python, `google-genai`, `exa_py`, `xai_sdk`, `opencode serve`.

---

### Task 1: Add `google-genai` dependency and configure environment

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv add`)
- Modify: `/home/dev/projects/workstation/hosts/devbox/configuration.nix`

**Step 1: Install google-genai**
```bash
cd ~/projects/my-podcasts
uv add google-genai
```

**Step 2: Add GEMINI_API_KEY to nix config**
Modify `/home/dev/projects/workstation/hosts/devbox/configuration.nix`.
Find the `my-podcasts-consumer-start` script definition. Add `export GEMINI_API_KEY="$(cat /run/secrets/gemini_api_key)"`.

**Step 3: Apply Nix configuration**
```bash
cd ~/projects/workstation
sudo nixos-rebuild switch --flake .#devbox
```

**Step 4: Commit**
Commit changes in both `my-podcasts` and `workstation` repos.

---

### Task 2: Implement `pipeline.things_happen_editor` (Phase 2)

Create the module that calls Gemini to generate the research directives.

**Files:**
- Create: `pipeline/things_happen_editor.py`
- Create: `pipeline/test_things_happen_editor.py`

**Step 1: Write the failing test**
Write a test that mocks `google.genai.Client` and verifies it parses structured JSON into `ResearchDirective` objects.

**Step 2: Run test to verify it fails**
```bash
uv run pytest pipeline/test_things_happen_editor.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
Implement `generate_research_plan(headlines_with_snippets: list[str]) -> list[dict]`.
Use `response_schema` with `gemini-2.5-flash` (or 2.0 if 2.5 isn't available in SDK) to enforce the JSON structure (`needs_exa`, `exa_query`, etc.).

**Step 4: Run test to verify it passes**
```bash
uv run pytest pipeline/test_things_happen_editor.py -v
```
Expected: PASS

**Step 5: Commit**

---

### Task 3: Implement `pipeline.things_happen_collector` (Phases 1 & 3)

Create the module that orchestrates writing to the `/tmp` directory.

**Files:**
- Create: `pipeline/things_happen_collector.py`
- Create: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing test**
Mock the APIs (`fetch_all_articles`, `generate_research_plan`, `exa_client`, etc.) and test `collect_all_artifacts(job_id, links)`. Assert the directory structure is created correctly and symlinks to prior scripts are made.

**Step 2: Run test to verify it fails**
```bash
uv run pytest pipeline/test_things_happen_collector.py -v
```

**Step 3: Write implementation**
Implement `collect_all_artifacts(job_id: str, links_raw: list[dict], work_dir: Path)`.
- Phase 1: Fetch articles, write to `work_dir/articles/`. Find up to 3 recent `.txt` files in `/persist/my-podcasts/scripts/things-happen/` and symlink them to `work_dir/context/`.
- Phase 2: Call `generate_research_plan()`.
- Phase 3: For each directive, call Exa/xAI/RSS and write to `work_dir/enrichment/{source}/`.

**Step 4: Run tests**
```bash
uv run pytest pipeline/test_things_happen_collector.py -v
```

**Step 5: Commit**

---

### Task 4: Refactor Agent Launcher (Phase 4)

Update the agent prompt to read from the disk instead of using tools.

**Files:**
- Modify: `pipeline/things_happen_agent.py`
- Modify: `pipeline/test_things_happen_agent.py`

**Step 1: Update tests**
Update `test_build_agent_prompt` to expect the new directory-based instructions.

**Step 2: Write implementation**
Remove the tool instructions (Exa, xAI, RSS).
New prompt: "Your research has been collected in `{work_dir}`. Read `articles/`, `enrichment/`, and the prior scripts in `context/`. If a story is adequately covered in the prior scripts with no new developments, skip it. Present your plan via Telegram. When approved, write `script.txt` to the root of the directory."

**Step 3: Run tests**
```bash
uv run pytest pipeline/test_things_happen_agent.py -v
```

**Step 4: Commit**

---

### Task 5: Refactor Consumer & Finalization (Phases 1-5 orchestration)

Wire it all together in the consumer.

**Files:**
- Modify: `pipeline/consumer.py`
- Modify: `pipeline/test_consumer.py`

**Step 1: Update tests**
Update consumer tests to expect the new flow.

**Step 2: Write implementation**
In `consumer.py`:
- Generate `work_dir = Path(f"/tmp/things-happen-{job['id']}")`
- Check if `script.txt` exists in `work_dir`.
- If NO script AND agent is not running:
  - Call `collect_all_artifacts(job['id'], links, work_dir)`
  - Call `launch_things_happen_agent(job, work_dir)`
- If script EXISTS:
  - Run TTS and upload (or skip if dry run)
  - `shutil.copy(script, "/persist/my-podcasts/scripts/things-happen/YYYY-MM-DD.txt")`
  - `shutil.rmtree(work_dir)`
  - Stop agent, mark job completed.

**Step 3: Run tests**
```bash
uv run pytest pipeline/test_consumer.py -v
```

**Step 4: Commit**

---

### Task 6: Dry Run

**Step 1:** Set `THINGS_HAPPEN_DRY_RUN=1` in DB/env if needed, or rely on existing config.
**Step 2:** Reset a job to `pending`.
**Step 3:** Restart consumer, tail logs, verify the directory structure is created, Gemini is called, and the agent writes the script based on local files.
