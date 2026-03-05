# Shared opencode-serve Refactor

**Date:** 2026-03-05
**Status:** Approved
**Scope:** `pipeline/things_happen_agent.py`, `pipeline/summarizer.py`, `pipeline/consumer.py`, `pipeline/db.py`, new `pipeline/opencode_client.py`

## Problem

The my-podcasts pipeline manages its own `opencode serve` process on port 5555 -- spawning it via `subprocess.Popen`, writing PID files, polling for health, and killing it when done. Meanwhile, pigeon now runs a shared `opencode-serve.service` on port 4096 as a persistent systemd daemon with the pigeon plugin pre-loaded.

This means:
- ~80 lines of process lifecycle code (PID files, SIGTERM, health polling) that shouldn't exist
- No pigeon/Telegram integration for agent sessions (pigeon plugin isn't loaded in the ad-hoc process)
- Port conflict risk between the ad-hoc server and any other opencode usage
- Agent sessions invisible to the unified opencode session list

## Goal

Eliminate all process lifecycle management from my-podcasts. Both the Things Happen agent and the summarizer talk to the shared opencode server at `http://127.0.0.1:4096`. This gives us pigeon/Telegram integration for free, removes subprocess/PID code, and consolidates all sessions into one server.

## Architecture

### Shared client module: `pipeline/opencode_client.py`

Both the agent and summarizer need the same HTTP primitives. A small shared module provides:

- `create_session(directory) -> session_id` -- `POST /session` with `x-opencode-directory` header
- `send_prompt_async(session_id, text)` -- `POST /session/{id}/prompt_async`
- `wait_for_idle(session_id, timeout) -> bool` -- SSE listener on `/event`, filters for `session.status` with `status.type == "idle"` matching our session ID
- `get_messages(session_id) -> list[dict]` -- `GET /session/{id}/message`
- `delete_session(session_id)` -- `DELETE /session/{id}`
- `is_session_active(session_id) -> bool` -- `GET /session/{id}`, returns False on 404

Configuration:
- `OPENCODE_URL` env var, default `http://127.0.0.1:4096`
- Project directory derived from `Path(__file__).resolve().parent.parent` (repo root), passed as `x-opencode-directory` header

### Things Happen agent changes

**Current flow:**
```
consumer.py -> things_happen_agent.py -> subprocess.Popen("opencode serve --port 5555")
                                      -> PID file write
                                      -> health poll loop (30s timeout)
                                      -> POST /session (port 5555)
                                      -> POST /session/{id}/prompt_async (port 5555)
consumer.py -> is_agent_running() checks PID file + os.kill(pid, 0)
consumer.py -> stop_agent() sends SIGTERM + deletes PID file
```

**New flow:**
```
consumer.py -> things_happen_agent.py -> create_session(project_dir)
                                      -> send_prompt_async(session_id, prompt)
                                      -> store session_id in SQLite
consumer.py -> is_agent_running(session_id) queries opencode API
consumer.py -> stop_agent(session_id) DELETEs via opencode API
```

**Deleted:** `AGENT_PORT`, `AGENT_PID_FILE`, `OPENCODE_BIN`, the `subprocess.Popen` block, health-check poll loop, PID file I/O, SIGTERM cleanup.

**Added:** Calls to `opencode_client` functions. `launch_things_happen_agent` returns the session ID. `is_agent_running` and `stop_agent` take a session ID parameter.

### Summarizer changes

**Current flow:**
```
summarizer.py -> write prompt to tempfile
              -> subprocess.run(["opencode", "run", "--file", ...], timeout=120)
              -> parse stdout, strip header lines
              -> return script text
```

**New flow:**
```
summarizer.py -> create_session(project_dir)
              -> send_prompt_async(session_id, prompt)
              -> wait_for_idle(session_id, timeout=120)
              -> get_messages(session_id) -> extract last assistant message text
              -> delete_session(session_id)
              -> return script text
```

**Deleted:** Tempfile creation, `subprocess.run`, stdout header stripping.

### Consumer changes

- `is_agent_running()` call changes from no-arg (PID-based) to `is_agent_running(session_id)` using the ID stored in SQLite
- `stop_agent()` changes from no-arg (SIGTERM) to `stop_agent(session_id)` using `DELETE /session/{id}`
- On startup, check for stale `opencode_session_id` values: if the session no longer exists on the server (404), clear the ID so the job can re-launch

### DB schema change

Add `opencode_session_id TEXT` column to `pending_things_happen`, nullable. Populated when the agent session is created, cleared when the job completes or is cleaned up. New methods on `StateStore`:
- `set_things_happen_session_id(job_id, session_id)`
- `get_things_happen_session_id(job_id) -> str | None`

### Permissions

Add permission config to the project-level `opencode.json` so the shared server auto-approves tool use when working in the my-podcasts directory:

```json
{
  "permission": {
    "*": "allow",
    "external_directory": "allow",
    "doom_loop": "allow"
  }
}
```

## Error Handling

- **Shared server unavailable:** Log error, skip, retry next poll cycle (10s). No crash.
- **Session creation fails:** Log and raise; consumer's existing `except` block catches it. Job stays `pending`, retried next cycle.
- **SSE connection drops (summarizer):** Fall back to polling `GET /session/status` every 2 seconds until idle or 120s timeout.
- **Stale session IDs:** On consumer startup, validate any stored `opencode_session_id` against the server. Clear on 404.

## Testing

- **`opencode_client.py`:** Unit tests mocking HTTP calls. Cover each method: create, prompt, wait_for_idle (mock SSE stream), get_messages, delete, is_active.
- **`things_happen_agent.py`:** Mock `opencode_client` functions instead of `subprocess.Popen`.
- **`summarizer.py`:** Mock `opencode_client` functions instead of `subprocess.run`.
- **`consumer.py`:** Update mocks for session-ID-based `is_agent_running` / `stop_agent`.
- **`db.py`:** Test migration adds column, test getter/setter methods.

## Out of Scope

- Changes to pigeon daemon or worker
- Changes to the `opencode-serve.service` systemd unit
- Changes to the opencode codebase
- Monitoring/alerting for the shared server (already handled by pigeon ops)

## Files Changed

| File | Change |
|------|--------|
| `pipeline/opencode_client.py` | **New.** Shared HTTP client for opencode serve API. |
| `pipeline/things_happen_agent.py` | Remove process lifecycle, use `opencode_client`. |
| `pipeline/summarizer.py` | Replace `subprocess.run` with `opencode_client`. |
| `pipeline/consumer.py` | Session-ID-based agent tracking, stale session cleanup. |
| `pipeline/db.py` | Add `opencode_session_id` column + migration + accessors. |
| `opencode.json` | Add permission config for auto-approval. |
| `pipeline/test_opencode_client.py` | **New.** Tests for client module. |
| `pipeline/test_things_happen_agent.py` | Update mocks. |
| `pipeline/test_summarizer.py` | Update mocks. |
| `AGENTS.md` | Update "port 5555" reference. |
| `.opencode/skills/operating-things-happen-digest/SKILL.md` | Update operational docs. |
| `.opencode/skills/operating-things-happen-digest/REFERENCE.md` | Update references. |
