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


def stop_agent() -> None:
    """Kill the opencode serve process and clean up the PID file."""
    if not AGENT_PID_FILE.exists():
        return
    try:
        pid = int(AGENT_PID_FILE.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    AGENT_PID_FILE.unlink(missing_ok=True)


def build_agent_prompt(job: dict, work_dir: Path) -> str:
    """Build the initial prompt for the Things Happen agent."""
    links_json = job["links_json"]

    prompt = f"""You are an AI assistant helping produce a daily podcast briefing for the "Things Happen" segment from Matt Levine's Money Stuff newsletter.

## Job Details
- Job ID: {job["id"]}
- Date: {job["date_str"]}

## Original Links
These were extracted from the newsletter:
{links_json}

## Your Task

All research for this episode has already been completed and collected for you on disk. You do NOT need to search the web or fetch articles yourself.

Your working directory is: `{work_dir}`

1. **Read the Articles:** Use your tools to read the markdown files in `{work_dir}/articles/`. These contain the full text (or headlines if paywalled) of the original newsletter links.
2. **Read the Enrichment Data:** Read the files in `{work_dir}/enrichment/`. This directory contains Exa web search results, xAI Twitter summaries, and RSS alternative perspectives (for foreign policy stories). Use this context to enrich the original articles.
3. **Check the Trailing Window:** Read the prior episode scripts in `{work_dir}/context/`. If a story is adequately covered in these prior scripts and has no new developments, **skip it**. Do not repeat background context we already explained to the listener yesterday.
4. **Draft a Plan:** Once you have read the files, write a short summary of what you found and your proposed script plan. Present this plan via Telegram and wait for the operator to reply.
5. **Write the Script:** Once approved by the operator, write your final podcast script to `{work_dir}/script.txt`. Make it conversational, engaging, and suitable for TTS.

Remember: Do not use the `bash` tool to run python scripts to fetch articles or search APIs. Just read the files that have already been gathered for you.
"""
    return prompt


def launch_things_happen_agent(job: dict, work_dir: Path) -> bool:
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
    prompt = build_agent_prompt(job, work_dir)

    # Start opencode server in the project directory so it picks up
    # AGENTS.md, skills, and .opencode/ configuration.
    # Auto-approve all tool permissions so the headless agent doesn't hang
    # waiting for user approval. Include explicit external_directory allow.
    project_dir = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "OPENCODE_PERMISSION": '{"*": "allow", "external_directory": "allow", "doom_loop": "allow"}',
    }
    proc = subprocess.Popen(
        [OPENCODE_BIN, "serve", "--port", str(AGENT_PORT)],
        cwd=project_dir,
        env=env,
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
