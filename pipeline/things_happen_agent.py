from __future__ import annotations

from pathlib import Path

from pipeline.opencode_client import (
    create_session,
    delete_session,
    is_session_active,
    send_prompt_async,
)


def script_path_for_job(job_id: str) -> Path:
    """Return the path to the script file for a given job ID."""
    return Path(f"/tmp/things-happen-{job_id}.txt")


def is_agent_running(session_id: str | None) -> bool:
    """Check if the opencode session for the agent is still active."""
    if session_id is None:
        return False
    return is_session_active(session_id)


def stop_agent(session_id: str | None) -> None:
    """Delete the opencode session for the agent."""
    if session_id is None:
        return
    delete_session(session_id)


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


def launch_things_happen_agent(job: dict, work_dir: Path) -> str | None:
    """Launch a Things Happen agent session on the shared opencode server.

    Returns the session ID on success, or None if the script already exists.
    """
    job_id = job["id"]
    script_path = script_path_for_job(job_id)
    if script_path.exists():
        return None

    prompt = build_agent_prompt(job, work_dir)
    session_id = create_session()
    send_prompt_async(session_id, prompt)
    return session_id
