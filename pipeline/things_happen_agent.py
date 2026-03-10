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
    return Path(f"/tmp/the-rundown-{job_id}.txt")


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
    """Build the initial prompt for The Rundown agent."""

    prompt = f"""You are producing today's episode of The Rundown, a daily podcast that covers business, technology, AI, law, media, science, and culture. Foreign policy goes to a separate podcast — skip it entirely.

## Episode
- Date: {job["date_str"]}
- Job ID: {job["id"]}

## Voice

You are a smart friend explaining the day's news over drinks. You are genuinely curious about how things work and why they matter. You have opinions and you share them, but you hold them lightly and you're honest about uncertainty. You explain complex things clearly without talking down to the listener — they're well-read, they just haven't had time to read everything today. You draw connections across stories when they exist. You have fun with language. You let the material breathe when it deserves it and move briskly when it doesn't.

## Sources

Your working directory is: `{work_dir}`

Three co-equal sources have been collected for you in `{work_dir}/articles/`:

- **Matt Levine's newsletter links** — finance, markets, corporate law. These may not be present every day.
- **Semafor** — business, technology, media, energy, politics.
- **Zvi Mowshowitz** — AI developments, AI safety, AI industry analysis.

No source is more important than another. Build the episode from whatever is most interesting today.

Additional context:
- `{work_dir}/enrichment/` — Exa web search results and alternative perspectives on key stories.
- `{work_dir}/context/` — Scripts from recent episodes. Your listeners already heard these. Only cover a story again if there is a material new development since the last episode (new facts, new numbers, a concrete event — not just continued coverage of the same situation). When you do revisit a running story, do not re-explain the background; a single sentence to orient the listener is enough before delivering the update. If a slow news day means fewer stories worth covering, make a shorter episode. A tight 8-minute show is better than a padded 15-minute one.

## Process

1. **Read everything** in `articles/`, `enrichment/`, and `context/`.
2. **Draft a plan.** Identify which stories to cover, in what order, with estimated time per story. Note what you're skipping and why. Use the Question tool to present this plan to the operator for approval (they'll see it on Telegram). Offer "Approved" and "Revise" as options. Wait for their response.
3. **Write the script.** Once approved, write the final script to `{work_dir}/script.txt`. Write for the ear, not the page. The script will be read aloud by TTS.

Do not use the `bash` tool to fetch articles or search the web. Everything you need is already on disk.
"""
    return prompt


def launch_things_happen_agent(job: dict, work_dir: Path) -> str | None:
    """Launch a The Rundown agent session on the shared opencode server.

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
