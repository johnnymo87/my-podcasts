from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import requests


AGENT_PORT = 5555
AGENT_PID_FILE = Path("/tmp/things-happen-opencode.pid")
OPENCODE_BIN = os.environ.get(
    "OPENCODE_BIN", str(Path.home() / ".nix-profile" / "bin" / "opencode")
)


def script_path_for_job(job_id: str) -> Path:
    """Return the path to the script file for a given job ID."""
    return Path(f"/tmp/things-happen-{job_id}.txt")


def is_agent_running() -> bool:
    """Check if PID file exists and process is alive."""
    if not AGENT_PID_FILE.exists():
        return False
    try:
        pid = int(AGENT_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def build_agent_prompt(job: dict) -> str:
    """Build the initial prompt for the Things Happen agent."""
    job_id = job["id"]
    date_str = job["date_str"]
    links = json.loads(job["links_json"])

    links_section_lines = []
    for i, link in enumerate(links, 1):
        links_section_lines.append(f"  {i}. {link['link_text']}")
        links_section_lines.append(f"     Headline context: {link['headline_context']}")
        links_section_lines.append(f"     URL: {link['raw_url']}")
    links_section = "\n".join(links_section_lines)

    script_path = script_path_for_job(job_id)

    prompt = f"""You are an AI assistant helping produce a daily podcast briefing for the "Things Happen" segment from Matt Levine's Money Stuff newsletter.

## Job Details

- Job ID: {job_id}
- Date: {date_str}
- Script output path: {script_path}

## Links to Research

{links_section}

## Step-by-Step Instructions

Work through the following steps to produce the briefing script:

### Step 1: Resolve redirect URLs

For each link above, resolve the redirect URL using:

```python
from pipeline.things_happen_extractor import resolve_redirect_url
resolved_url = resolve_redirect_url(raw_url)
```

### Step 2: Fetch articles

Fetch each article using:

```python
from pipeline.article_fetcher import fetch_article
article = fetch_article(resolved_url)
```

### Step 3: Enrich with additional context

For each article:

- Search for related articles using:
  ```python
  from pipeline.exa_client import search_related
  results = search_related(headline_context)
  ```

- Search Twitter/X for discussion using:
  ```python
  from pipeline.xai_client import search_twitter
  twitter_result = search_twitter(headline_context)
  ```

### Step 4: Write briefing script

Write the final briefing script to: `{script_path}`

### Step 5: Terminate this agent

Once the script has been written, terminate the agent server by running:

```bash
fuser -k {AGENT_PORT}/tcp
kill $(cat {AGENT_PID_FILE})
```

## Enrichment Guidelines

- **Paywalled content**: If an article is behind a paywall and you cannot access the full text, use `search_related()` via Exa to find related open-access articles.
- **Foreign policy topics**: Search antiwar.com and caitlinjohnstone.com for alternative perspectives using `search_related()` with `include_domains=["antiwar.com", "caitlinjohnstone.com"]`.
- **Twitter/X discussion**: Use `search_twitter()` via xAI to find notable takes from journalists and experts.

## Script Writing Rules

- Write in a conversational, spoken style — this will be read aloud by a text-to-speech system.
- No markdown formatting (no headers, bullet points, bold, italics, etc.).
- No the word "delve" — ever.
- Use plain spoken English suitable for podcast TTS.
- Keep each story segment focused and digestible.
- Connect the stories naturally with brief transitions.
- Aim for 5-8 minutes of spoken content total.

## Pigeon Communication Note

Messages you write while idle will be relayed to Telegram so the operator can follow your progress. Use the question tool when you need to make a decision that requires human input.
"""
    return prompt


def launch_things_happen_agent(job: dict) -> bool:
    """Orchestrate the launch of the Things Happen agent.

    Returns True on success, False if already running or script already written.
    Cleans up (kills process, removes PID file) on failure.
    """
    # Check if already running
    if is_agent_running():
        return False

    # Check if script already written
    job_id = job["id"]
    script_path = script_path_for_job(job_id)
    if script_path.exists():
        return False

    # Build the prompt
    prompt = build_agent_prompt(job)

    # Start opencode server in the project directory so it picks up
    # AGENTS.md, skills, and .opencode/ configuration.
    project_dir = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [OPENCODE_BIN, "serve", "--port", str(AGENT_PORT)],
        cwd=project_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Write PID file
    AGENT_PID_FILE.write_text(str(proc.pid))

    try:
        # Wait for health check
        base_url = f"http://localhost:{AGENT_PORT}"
        health_url = f"{base_url}/global/health"
        deadline = time.time() + 30
        healthy = False
        while time.time() < deadline:
            try:
                resp = requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    healthy = True
                    break
            except requests.RequestException:
                pass
            time.sleep(1)

        if not healthy:
            raise RuntimeError("Agent server did not become healthy within 30 seconds")

        # Create session
        session_resp = requests.post(f"{base_url}/session", timeout=10)
        session_resp.raise_for_status()
        session_id = session_resp.json()["id"]

        # Send prompt (opencode API expects {parts: [{type:"text", text:"..."}]})
        prompt_resp = requests.post(
            f"{base_url}/session/{session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": prompt}]},
            timeout=10,
        )
        prompt_resp.raise_for_status()

        return True

    except Exception:
        # Clean up on failure
        try:
            proc.kill()
        except Exception:
            pass
        try:
            AGENT_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        raise
