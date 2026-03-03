# Things Happen AI Agent — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the fixed Things Happen pipeline with an AI agent that enriches headlines via Exa/xAI, writes the briefing script, and communicates with the user via Telegram — then hands off to existing TTS+publish infrastructure.

**Architecture:** Consumer detects a due job → launches `opencode serve --port 5555` → sends initial prompt → AI enriches + writes script to `/tmp/things-happen-<job_id>.txt` → AI terminates → consumer's next poll picks up the script file → runs existing TTS/upload/feed pipeline. The 24-hour delay is removed; jobs process immediately.

**Tech Stack:** Python 3.14, opencode serve (TypeScript/Bun headless server), exa-py SDK, openai SDK (for xAI), pigeon (Telegram relay), existing pipeline modules (TTS, R2, feed).

**Design doc:** `docs/plans/2026-03-02-things-happen-agent-design.md`

---

### Task 1: Remove 24-hour delay

**Files:**
- Modify: `pipeline/db.py:222-240`
- Modify: `pipeline/test_things_happen_db.py:9-28`

**Step 1: Update test to expect immediate availability**

In `pipeline/test_things_happen_db.py`, change `test_insert_and_list_pending_things_happen` so that after inserting with default delay, the job IS immediately due (not 24h in the future):

```python
def test_insert_and_list_pending_things_happen(tmp_path) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    links_json = json.dumps(
        [
            {
                "link_text": "Test",
                "raw_url": "http://example.com",
                "headline_context": "Test story",
            }
        ]
    )
    store.insert_pending_things_happen(
        email_r2_key="inbox/raw/abc.eml",
        date_str="2026-02-26",
        links_json=links_json,
    )
    # Should be immediately due (no delay).
    due = store.list_due_things_happen()
    assert len(due) == 1
    assert due[0]["date_str"] == "2026-02-26"
    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_db.py::test_insert_and_list_pending_things_happen -v`
Expected: FAIL — `assert len(due) == 1` fails because `delay_hours=24` puts it in the future.

**Step 3: Change default delay to 0**

In `pipeline/db.py`, change line 227:

```python
    def insert_pending_things_happen(
        self,
        email_r2_key: str,
        date_str: str,
        links_json: str,
        delay_hours: int = 0,
    ) -> str:
```

**Step 4: Run all tests**

Run: `uv run pytest -v`
Expected: All pass.

**Step 5: Commit**

```
git add pipeline/db.py pipeline/test_things_happen_db.py
git commit -m "Remove 24h delay from Things Happen jobs — process immediately"
```

---

### Task 2: Refactor `process_things_happen_job()` to accept a pre-written script

**Files:**
- Modify: `pipeline/things_happen_processor.py:50-115`
- Modify: `pipeline/test_things_happen_processor.py`

**Step 1: Write a test for the script-file path**

Add to `pipeline/test_things_happen_processor.py`:

```python
def test_process_things_happen_job_with_script_file(tmp_path, monkeypatch) -> None:
    """When a script file is provided, skip fetch/summarize and go straight to TTS."""
    from pipeline.db import StateStore

    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()

    past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        """INSERT INTO pending_things_happen
           (id, email_r2_key, date_str, links_json, process_after, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "job-1",
            "inbox/raw/abc.eml",
            "2026-02-26",
            json.dumps([]),
            past,
            "pending",
        ),
    )
    store._conn.commit()

    # Write a pre-made script file.
    script_file = tmp_path / "script.txt"
    script_file.write_text("Here is the pre-written briefing script.")

    # Mock ttsjoin and ffprobe
    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ttsjoin":
            output_file = cmd[cmd.index("--output-file") + 1]
            Path(output_file).write_bytes(b"\xff\xfb\x90\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="60.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    monkeypatch.setattr(
        "pipeline.things_happen_processor.regenerate_and_upload_feed",
        lambda store, r2_client: None,
    )

    job = store.list_due_things_happen()[0]
    process_things_happen_job(job, store, r2_client, script_path=script_file)

    assert store.list_due_things_happen() == []
    episodes = store.list_episodes(feed_slug="things-happen")
    assert len(episodes) == 1
    r2_client.upload_file.assert_called_once()
    store.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_things_happen_processor.py::test_process_things_happen_job_with_script_file -v`
Expected: FAIL — `process_things_happen_job()` doesn't accept `script_path`.

**Step 3: Add `script_path` parameter to `process_things_happen_job()`**

In `pipeline/things_happen_processor.py`, modify the function signature and add an early branch:

```python
def process_things_happen_job(
    job: dict,
    store: StateStore,
    r2_client: R2Client,
    script_path: Path | None = None,
) -> None:
    """Process a single pending Things Happen job.

    If script_path is provided, skip fetch/summarize and use the pre-written
    script directly (agent handoff path). Otherwise run the full pipeline.
    """
    job_id = job["id"]
    date_str = job["date_str"]

    if script_path is not None:
        script = script_path.read_text(encoding="utf-8")
    else:
        links_raw = json.loads(job["links_json"])

        # Step 1: Resolve redirect URLs.
        for link in links_raw:
            link["resolved_url"] = resolve_redirect_url(link["raw_url"])

        # Step 2: Fetch articles with fallback chain.
        articles = fetch_all_articles(links_raw, delay_between=3.0)

        # Step 3: Generate briefing script via LLM.
        script = generate_briefing_script(articles, date_str=date_str)

    # Step 4: TTS.
    episode_slug = f"{date_str}-things-happen"
    episode_r2_key = f"episodes/{FEED_SLUG}/{episode_slug}.mp3"

    # ... rest stays exactly the same from line 74 onward ...
```

**Step 4: Run all tests**

Run: `uv run pytest -v`
Expected: All pass (both existing test and new test).

**Step 5: Commit**

```
git add pipeline/things_happen_processor.py pipeline/test_things_happen_processor.py
git commit -m "Support pre-written script in Things Happen processor (agent handoff)"
```

---

### Task 3: Add exa-py and openai dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add dependencies**

Add `exa-py` and `openai` to the `dependencies` list in `pyproject.toml`:

```toml
dependencies = [
    "click (>=8.1.8,<9.0.0)",
    "beautifulsoup4 (>=4.12.3,<5.0.0)",
    "boto3 (>=1.35.0)",
    "requests (>=2.32.0)",
    "exa-py (>=2.0.0)",
    "openai (>=1.0.0)",
]
```

**Step 2: Sync**

Run: `uv sync`
Expected: Dependencies install successfully.

**Step 3: Verify import**

Run: `uv run python -c "from exa_py import Exa; from openai import OpenAI; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```
git add pyproject.toml uv.lock
git commit -m "Add exa-py and openai dependencies for Things Happen enrichment"
```

---

### Task 4: Create Exa client wrapper

**Files:**
- Create: `pipeline/exa_client.py`
- Create: `pipeline/test_exa_client.py`

**Step 1: Write tests**

```python
# pipeline/test_exa_client.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.exa_client import search_related, ExaResult


def test_search_related_returns_results(monkeypatch) -> None:
    mock_exa = MagicMock()
    mock_result = MagicMock()
    mock_result.title = "Related Article"
    mock_result.url = "https://example.com/article"
    mock_result.text = "Article content here."
    mock_response = MagicMock()
    mock_response.results = [mock_result]
    mock_exa.search_and_contents.return_value = mock_response

    monkeypatch.setenv("EXA_API_KEY", "test-key")

    with patch("pipeline.exa_client.Exa", return_value=mock_exa):
        results = search_related("Blue Owl battles sparked a chill")

    assert len(results) == 1
    assert results[0].title == "Related Article"
    assert results[0].url == "https://example.com/article"
    assert results[0].text == "Article content here."
    mock_exa.search_and_contents.assert_called_once()


def test_search_related_with_include_domains(monkeypatch) -> None:
    mock_exa = MagicMock()
    mock_response = MagicMock()
    mock_response.results = []
    mock_exa.search_and_contents.return_value = mock_response

    monkeypatch.setenv("EXA_API_KEY", "test-key")

    with patch("pipeline.exa_client.Exa", return_value=mock_exa):
        results = search_related(
            "US bombs Yemen",
            include_domains=["antiwar.com", "caitlinjohnstone.com"],
        )

    call_kwargs = mock_exa.search_and_contents.call_args
    assert call_kwargs[1]["include_domains"] == ["antiwar.com", "caitlinjohnstone.com"]


def test_search_related_returns_empty_on_missing_key(monkeypatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    results = search_related("any headline")
    assert results == []


def test_search_related_returns_empty_on_error(monkeypatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    with patch("pipeline.exa_client.Exa", side_effect=Exception("API error")):
        results = search_related("any headline")
    assert results == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_exa_client.py -v`
Expected: FAIL — `pipeline.exa_client` doesn't exist.

**Step 3: Implement**

```python
# pipeline/exa_client.py
from __future__ import annotations

import os
from dataclasses import dataclass

from exa_py import Exa


@dataclass(frozen=True)
class ExaResult:
    title: str
    url: str
    text: str


def search_related(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    num_results: int = 3,
) -> list[ExaResult]:
    """Search Exa for articles related to a headline.

    Returns empty list if EXA_API_KEY is not set or on any error.
    """
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return []

    try:
        exa = Exa(api_key=api_key)
        kwargs: dict = {
            "num_results": num_results,
            "type": "auto",
            "contents": {"text": {"max_characters": 3000}},
        }
        if include_domains:
            kwargs["include_domains"] = include_domains

        response = exa.search_and_contents(headline, **kwargs)

        return [
            ExaResult(
                title=r.title or "",
                url=r.url or "",
                text=r.text or "",
            )
            for r in response.results
        ]
    except Exception:
        return []
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_exa_client.py -v`
Expected: All pass.

**Step 5: Commit**

```
git add pipeline/exa_client.py pipeline/test_exa_client.py
git commit -m "Add Exa client wrapper for headline enrichment"
```

---

### Task 5: Create xAI/Grok Twitter search wrapper

**Files:**
- Create: `pipeline/xai_client.py`
- Create: `pipeline/test_xai_client.py`

**Step 1: Write tests**

```python
# pipeline/test_xai_client.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.xai_client import search_twitter, XaiResult


def test_search_twitter_returns_result(monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    mock_client = MagicMock()
    mock_text_content = MagicMock()
    mock_text_content.type = "output_text"
    mock_text_content.text = "Summary of Twitter discussion."
    mock_message = MagicMock()
    mock_message.type = "message"
    mock_message.content = [mock_text_content]
    mock_response = MagicMock()
    mock_response.output = [mock_message]

    mock_client.responses.create.return_value = mock_response

    with patch("pipeline.xai_client.OpenAI", return_value=mock_client):
        result = search_twitter("Blue Owl battles sparked a chill")

    assert result is not None
    assert result.summary == "Summary of Twitter discussion."
    mock_client.responses.create.assert_called_once()


def test_search_twitter_returns_none_on_missing_key(monkeypatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    result = search_twitter("any headline")
    assert result is None


def test_search_twitter_returns_none_on_error(monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    with patch("pipeline.xai_client.OpenAI", side_effect=Exception("API error")):
        result = search_twitter("any headline")
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_xai_client.py -v`
Expected: FAIL — `pipeline.xai_client` doesn't exist.

**Step 3: Implement**

```python
# pipeline/xai_client.py
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openai import OpenAI


@dataclass(frozen=True)
class XaiResult:
    summary: str


def search_twitter(
    headline: str,
    *,
    lookback_days: int = 3,
) -> XaiResult | None:
    """Search Twitter/X via xAI Grok for discussion about a headline.

    Returns None if XAI_API_KEY is not set or on any error.
    """
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return None

    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
        )

        now = datetime.now(tz=UTC)
        from_date = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")

        response = client.responses.create(
            model="grok-3-fast",
            input=[
                {
                    "role": "user",
                    "content": (
                        f"Search X/Twitter for discussion about this news headline:\n"
                        f'"{headline}"\n\n'
                        f"Summarize the key points of what people are saying. "
                        f"Include any notable takes from journalists or experts. "
                        f"Be concise — 2-3 short paragraphs max."
                    ),
                }
            ],
            tools=[
                {
                    "type": "x_search",
                    "x_search": {
                        "from_date": from_date,
                        "to_date": to_date,
                    },
                },
            ],
        )

        text_parts = []
        for item in response.output:
            if item.type == "message":
                for c in item.content:
                    if c.type == "output_text":
                        text_parts.append(c.text)

        summary = "\n".join(text_parts).strip()
        if not summary:
            return None

        return XaiResult(summary=summary)
    except Exception:
        return None
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_xai_client.py -v`
Expected: All pass.

**Step 5: Commit**

```
git add pipeline/xai_client.py pipeline/test_xai_client.py
git commit -m "Add xAI/Grok Twitter search wrapper for headline enrichment"
```

---

### Task 6: Create agent launcher module

**Files:**
- Create: `pipeline/things_happen_agent.py`
- Create: `pipeline/test_things_happen_agent.py`

**Step 1: Write tests**

```python
# pipeline/test_things_happen_agent.py
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.things_happen_agent import (
    AGENT_PID_FILE,
    AGENT_PORT,
    build_agent_prompt,
    is_agent_running,
    script_path_for_job,
)


def test_script_path_for_job() -> None:
    path = script_path_for_job("abc-123")
    assert path == Path("/tmp/things-happen-abc-123.txt")


def test_is_agent_running_false_when_no_pid_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.things_happen_agent.AGENT_PID_FILE",
        tmp_path / "nonexistent.pid",
    )
    assert is_agent_running() is False


def test_is_agent_running_false_when_pid_dead(tmp_path, monkeypatch) -> None:
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("999999")
    monkeypatch.setattr("pipeline.things_happen_agent.AGENT_PID_FILE", pid_file)
    # PID 999999 almost certainly doesn't exist
    assert is_agent_running() is False


def test_build_agent_prompt_contains_job_data() -> None:
    job = {
        "id": "job-abc",
        "date_str": "2026-03-02",
        "links_json": json.dumps([
            {
                "link_text": "Blue Owl",
                "raw_url": "https://example.com/1",
                "headline_context": "Blue Owl battles",
            }
        ]),
    }
    prompt = build_agent_prompt(job)
    assert "job-abc" in prompt
    assert "2026-03-02" in prompt
    assert "Blue Owl" in prompt
    assert str(AGENT_PORT) in prompt
    assert str(AGENT_PID_FILE) in prompt
    assert "fuser" in prompt
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/test_things_happen_agent.py -v`
Expected: FAIL — module doesn't exist.

**Step 3: Implement the launcher module**

```python
# pipeline/things_happen_agent.py
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import requests


AGENT_PORT = 5555
AGENT_PID_FILE = Path("/tmp/things-happen-opencode.pid")
AGENT_HEALTH_URL = f"http://localhost:{AGENT_PORT}/global/health"
AGENT_SESSION_URL = f"http://localhost:{AGENT_PORT}/session"


def script_path_for_job(job_id: str) -> Path:
    """Return the expected script output path for a given job."""
    return Path(f"/tmp/things-happen-{job_id}.txt")


def is_agent_running() -> bool:
    """Check if a Things Happen agent is already running."""
    if not AGENT_PID_FILE.exists():
        return False
    try:
        pid = int(AGENT_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # signal 0 = check if process exists
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _wait_for_health(timeout: int = 30) -> bool:
    """Wait for the opencode server to become healthy."""
    for _ in range(timeout):
        try:
            r = requests.get(AGENT_HEALTH_URL, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def build_agent_prompt(job: dict) -> str:
    """Build the initial prompt for the AI agent."""
    job_id = job["id"]
    date_str = job["date_str"]
    links = json.loads(job["links_json"])

    links_block = ""
    for i, link in enumerate(links, 1):
        links_block += (
            f"{i}. {link['headline_context']}\n"
            f"   URL: {link['raw_url']}\n"
        )

    return f"""\
You are the Things Happen enrichment agent. A new Matt Levine "Money Stuff" newsletter has arrived with a "Things Happen" section. Your job is to research these headlines, enrich them with context, and write a podcast briefing script.

## Job Details
- Job ID: {job_id}
- Date: {date_str}
- Output file: /tmp/things-happen-{job_id}.txt

## Headlines
{links_block}

## Your Mission

### Step 1: Fetch and Assess
For each headline, run the existing pipeline code to resolve redirect URLs and fetch article content:

```python
from pipeline.things_happen_extractor import resolve_redirect_url
from pipeline.article_fetcher import fetch_article

url = resolve_redirect_url("<raw_url>")
article = fetch_article(url, "<headline>")
print(f"Source: {{article.source_tier}}, Content length: {{len(article.content)}}")
```

### Step 2: Enrich (use your judgment)
For each headline, decide what enrichment it needs. You have two tools:

**Exa web search** — for finding related non-paywalled coverage:
```python
from pipeline.exa_client import search_related
results = search_related("headline text")
# For foreign policy topics, also cross-check:
fp_results = search_related("headline text", include_domains=["antiwar.com", "caitlinjohnstone.com"])
```

**xAI/Grok Twitter search** — for finding Twitter/X discussion:
```python
from pipeline.xai_client import search_twitter
result = search_twitter("headline text")
if result:
    print(result.summary)
```

Use your judgment:
- Paywalled articles (headline_only) → definitely use Exa to find related coverage
- Foreign policy topics → also search antiwar.com and caitlinjohnstone.com
- Topics with likely Twitter discussion → use xAI
- Articles where you already have full content → enrichment is optional, use if it adds value
- Not every headline needs every tool — be selective

### Step 3: Write the Briefing Script
Write a podcast briefing script and save it to the output file. Rules:
- Conversational and concise — this will be read aloud by TTS
- For each story, explain what happened and why it matters
- Be upfront about your source quality (full article vs. headline + enrichment)
- Flag the 2-3 biggest stories early
- Start with: "Here are today's Things Happen stories from Money Stuff."
- End with a brief sign-off
- Plain spoken English only — NO markdown, bullet points, or special characters
- Do NOT use the word "delve"

Save with:
```python
from pathlib import Path
Path("/tmp/things-happen-{job_id}.txt").write_text(script, encoding="utf-8")
```

### Step 4: Communicate
Your messages relay to the user's Telegram automatically when you pause. Use the question tool at decision points if you want explicit guidance. The user can reply from Telegram and you'll receive their response.

### Step 5: Terminate
Once you've written the script file, terminate this opencode process:
```bash
fuser -k {AGENT_PORT}/tcp 2>/dev/null; kill $(cat {AGENT_PID_FILE}) 2>/dev/null
```
This is your final action. The existing pipeline will handle TTS, upload, and publishing.
"""


def launch_things_happen_agent(job: dict) -> bool:
    """Launch an opencode serve agent for a Things Happen job.

    Returns True if the agent was successfully launched, False otherwise.
    """
    if is_agent_running():
        print("Things Happen agent already running, skipping launch.")
        return False

    job_id = job["id"]
    script_out = script_path_for_job(job_id)
    if script_out.exists():
        print(f"Script file already exists for job {job_id}, skipping agent launch.")
        return False

    prompt = build_agent_prompt(job)

    # Start opencode serve as a background process.
    proc = subprocess.Popen(
        ["opencode", "serve", "--port", str(AGENT_PORT)],
        cwd="/home/dev/projects/my-podcasts",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    AGENT_PID_FILE.write_text(str(proc.pid))
    print(f"Started opencode serve (PID {proc.pid}, port {AGENT_PORT})")

    # Wait for server health.
    if not _wait_for_health():
        print("opencode serve failed to become healthy, killing.")
        proc.kill()
        AGENT_PID_FILE.unlink(missing_ok=True)
        return False

    # Create session and send prompt.
    try:
        session_resp = requests.post(AGENT_SESSION_URL, timeout=10)
        session_resp.raise_for_status()
        session_id = session_resp.json()["id"]

        prompt_url = f"{AGENT_SESSION_URL}/{session_id}/prompt_async"
        prompt_resp = requests.post(
            prompt_url,
            json={"parts": [{"type": "text", "text": prompt}]},
            timeout=10,
        )
        prompt_resp.raise_for_status()

        print(
            f"Things Happen agent launched: session={session_id}, "
            f"job={job_id}, port={AGENT_PORT}"
        )
        return True
    except Exception as exc:
        print(f"Failed to create session/send prompt: {exc}")
        proc.kill()
        AGENT_PID_FILE.unlink(missing_ok=True)
        return False
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_things_happen_agent.py -v`
Expected: All pass.

**Step 5: Run full test suite**

Run: `uv run pytest -v`
Expected: All pass.

**Step 6: Commit**

```
git add pipeline/things_happen_agent.py pipeline/test_things_happen_agent.py
git commit -m "Add Things Happen agent launcher (opencode serve + prompt)"
```

---

### Task 7: Update consumer to use agent launcher

**Files:**
- Modify: `pipeline/consumer.py:144-157`
- Modify: `pipeline/test_consumer.py`

**Step 1: Write test for agent-based consumer flow**

Add to `pipeline/test_consumer.py`:

```python
def test_consume_forever_launches_agent_for_due_job(monkeypatch) -> None:
    """When a due Things Happen job exists, consumer launches agent instead of
    processing directly. When a script file exists, consumer processes it."""
    store = MagicMock()
    r2_client = MagicMock()

    launched = []

    def fake_launch(job):
        launched.append(job["id"])
        return True

    monkeypatch.setattr(
        "pipeline.consumer.launch_things_happen_agent", fake_launch
    )
    monkeypatch.setattr(
        "pipeline.consumer.is_agent_running", lambda: False
    )
    monkeypatch.setattr(
        "pipeline.consumer.script_path_for_job",
        lambda job_id: Path("/nonexistent/script.txt"),
    )

    store.list_due_things_happen.return_value = [
        {"id": "job-1", "date_str": "2026-03-02", "links_json": "[]"}
    ]

    call_count = 0

    def flaky_pull(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        raise _Done("done")

    mock_consumer = MagicMock()
    mock_consumer.pull.side_effect = flaky_pull

    slept = []
    monkeypatch.setattr(time, "sleep", lambda n: slept.append(n))

    with patch("pipeline.consumer.CloudflareQueueConsumer", return_value=mock_consumer):
        try:
            consume_forever(store, r2_client, poll_interval=5)
        except _Done:
            pass

    assert launched == ["job-1"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/test_consumer.py::test_consume_forever_launches_agent_for_due_job -v`
Expected: FAIL — consumer still calls `process_things_happen_job`.

**Step 3: Update consumer**

Replace the Things Happen section in `pipeline/consumer.py` (lines 144-157):

```python
from pipeline.things_happen_agent import (
    is_agent_running,
    launch_things_happen_agent,
    script_path_for_job,
)
from pipeline.things_happen_processor import process_things_happen_job
```

And update the job processing block:

```python
        # Process any due Things Happen jobs.
        try:
            due_jobs = store.list_due_things_happen()
            for job in due_jobs:
                try:
                    script_file = script_path_for_job(job["id"])
                    if script_file.exists():
                        # Agent finished — run TTS + publish with the script.
                        print(
                            f"Processing Things Happen job with agent script: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        process_things_happen_job(
                            job, store, r2_client, script_path=script_file
                        )
                        script_file.unlink(missing_ok=True)
                        print(f"Completed Things Happen job: {job['id']}")
                    elif not is_agent_running():
                        # No script, no agent — launch one.
                        print(
                            f"Launching Things Happen agent for job: "
                            f"{job['id']} ({job['date_str']})"
                        )
                        launch_things_happen_agent(job)
                    # else: agent is running, wait for it to finish.
                except Exception as exc:
                    print(f"Failed Things Happen job {job['id']}: {exc}")
        except Exception as exc:
            print(f"Error checking Things Happen jobs: {exc}")
```

**Step 4: Run all tests**

Run: `uv run pytest -v`
Expected: All pass.

**Step 5: Commit**

```
git add pipeline/consumer.py pipeline/test_consumer.py
git commit -m "Consumer launches AI agent for Things Happen instead of processing directly"
```

---

### Task 8: Wire EXA_API_KEY in NixOS config

**Files:**
- Modify: `/home/dev/projects/workstation/hosts/devbox/configuration.nix`

**Note:** This task requires an Exa API key. If the user hasn't provided one yet, skip and return to this later.

**Step 1: Add EXA_API_KEY to SOPS secrets**

```bash
cd ~/projects/workstation
sops secrets/devbox.yaml
# Add: exa_api_key: <the key>
```

**Step 2: Add sops.secrets block in configuration.nix**

Add after the xai_api_key block (~line 80):

```nix
      exa_api_key = {
        owner = "dev";
        group = "dev";
        mode = "0400";
      };
```

**Step 3: Add env var export to consumer ExecStart**

Add after the XAI_API_KEY export (~line 183):

```
        export EXA_API_KEY="$(cat /run/secrets/exa_api_key)"
```

**Step 4: Commit and rebuild**

```bash
cd ~/projects/workstation
git add hosts/devbox/configuration.nix secrets/devbox.yaml
git commit -m "Add Exa API key secret for Things Happen enrichment"
git push
sudo nixos-rebuild switch --flake .#devbox
```

---

### Task 9: Update docs and skill

**Files:**
- Modify: `AGENTS.md`
- Modify: `.opencode/skills/operating-things-happen-digest/SKILL.md`
- Modify: `.opencode/skills/operating-things-happen-digest/REFERENCE.md`

**Step 1: Update AGENTS.md**

Add to the Things Happen section: mention the AI agent flow, the opencode serve launcher, port 5555, and the script-file handoff.

**Step 2: Update the operational skill**

Update SKILL.md with:
- New flow: agent launches on email arrival (no 24h delay)
- How to check if agent is running: `ls /tmp/things-happen-opencode.pid`
- How to kill a stuck agent: `fuser -k 5555/tcp`
- Script file location: `/tmp/things-happen-<job_id>.txt`

Update REFERENCE.md with:
- New modules: `things_happen_agent.py`, `exa_client.py`, `xai_client.py`
- Agent prompt location (embedded in `things_happen_agent.py:build_agent_prompt()`)
- Consumer flow diagram update

**Step 3: Commit**

```
git add AGENTS.md .opencode/skills/operating-things-happen-digest/
git commit -m "Update docs for AI agent-based Things Happen pipeline"
```

---

### Task 10: End-to-end verification

**Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass.

**Step 2: Verify consumer service starts cleanly**

```bash
sudo systemctl restart my-podcasts-consumer
sudo systemctl status my-podcasts-consumer --no-pager
sudo journalctl -u my-podcasts-consumer -n 20 --no-pager
```

**Step 3: Check pending job in production database**

```bash
uv run python -c "
from pathlib import Path
from pipeline.db import StateStore
store = StateStore(Path('/persist/my-podcasts/state.sqlite3'))
jobs = store.list_due_things_happen()
print(f'Due jobs: {len(jobs)}')
for j in jobs:
    print(f'  {j[\"id\"]}: {j[\"date_str\"]}')
store.close()
"
```

**Step 4: Push**

```bash
git push
```
