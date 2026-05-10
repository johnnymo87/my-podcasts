# Writer Output Persistence — Design

> **Status:** approved 2026-05-10. See companion plan `2026-05-10-writer-output-persistence-plan.md`.

## Problem

The Rundown and FP Digest writers (`pipeline/rundown_writer.generate_rundown_script`, `pipeline/fp_writer.generate_fp_script`) wrap the opencode-serve call in this shape:

```
create_session → send_prompt_async → wait_for_idle → get_messages → parse → validate → return
```

If anything between `wait_for_idle` and `return` fails, the session is deleted in `finally` and the next retry starts a fresh session, regenerating the entire ~26K-token reasoning. The model's output is discarded even when it was complete and well-formed.

The 2026-05-06 incident exposed this: opencode's `wait_for_idle` timed out (broken SSE detection — already fixed), so the writer raised, even though the model had already produced a valid 12.5KB script. Each retry re-ran the writer; nine retries before manual intervention.

## Goal

Persist the model's raw assistant output to disk the moment it's available, so any retry that follows a successful generation reuses the persisted text instead of calling the model again.

## Non-Goals

- Resuming an in-flight opencode session across retries (out of scope; YAGNI).
- Persisting opencode message JSON (concatenated assistant text is enough; YAGNI).
- Adding persistence to `pipeline/chinatalk_writer.generate_report` — that path is fail-soft (catches its own exceptions and falls back to standard reading), has no retry budget, so persistence would be inert code.
- Refactoring the post-script TTS/publish phase. The existing `script.txt` checkpoint already correctly skips the writer on retries.

## Design

Add a required `work_dir: Path` parameter to both writers and one persistence checkpoint:

```python
def generate_rundown_script(
    themes,
    articles_by_theme,
    date_str,
    context_scripts,
    work_dir: Path,
) -> WriterOutput:
    raw_path = work_dir / "raw_writer_output.txt"

    if raw_path.exists():
        full_text = raw_path.read_text(encoding="utf-8")
        # Skip the model call entirely
    else:
        prompt = build_rundown_prompt(...)
        instruction = ... + prompt
        session_id = create_session()
        try:
            send_prompt_async(session_id, instruction)
            if not wait_for_idle(session_id, timeout=900):
                raise RuntimeError("opencode session did not complete within 900 seconds")
            messages = get_messages(session_id)
            full_text = get_last_assistant_text(messages).strip()
            # Persist BEFORE parsing — survives any post-generation failure
            work_dir.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(full_text, encoding="utf-8")
        finally:
            delete_session(session_id)

    # Parse + validate. If anything fails, wipe the persisted file so the next
    # retry regenerates instead of looping on the same broken content.
    try:
        covered = parse_covered(full_text)
        summary_result = parse_summary(full_text)
        script = _extract_script(summary_result.script)
        if not script.strip():
            raise RuntimeError("Rundown writer returned empty script")
    except RuntimeError:
        try:
            raw_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return WriterOutput(
        script=script,
        summary=summary_result.summary,
        covered_headlines=covered,
    )
```

`generate_fp_script` gets the same shape, with FP-specific prompt and error string.

## Components Touched

**Modified:**

- `pipeline/rundown_writer.py` — `generate_rundown_script` signature + persistence + parse-failure cleanup.
- `pipeline/fp_writer.py` — `generate_fp_script` signature + persistence + parse-failure cleanup.
- `pipeline/consumer.py` — pass `work_dir=work_dir` at call sites (lines 388, 532).
- `pipeline/__main__.py` — pass `work_dir=work_dir` at call sites (lines 385, 479, 600, 693).
- `pipeline/test_rundown_writer.py` — add `work_dir` to existing `generate_rundown_script` calls + new persistence tests.
- `pipeline/test_fp_writer.py` — same for FP.
- `pipeline/AGENTS.md` — document `raw_writer_output.txt` alongside the existing `collection_done.json` description.

**Not touched:**

- `pipeline/chinatalk_writer.py` (out of scope).
- `pipeline/opencode_client.py` (the polling fix from 2026-05-06 morning is sufficient).
- TTS/publish stages.

## Data Flow

```
First attempt (clean):
  collect → plan.json → [model call] → raw_writer_output.txt → parse → script.txt → TTS → publish

Failure during model call (wait_for_idle returns False, network blip, etc.):
  raw_writer_output.txt NOT written → next retry → fresh model call

Success during model call, failure during parse/validate:
  raw_writer_output.txt written → parse fails → file deleted → raise → next retry → fresh model call

Success during model call, consumer crashes between persist and parse:
  raw_writer_output.txt persists across crash → next retry reuses it → parse → script.txt → TTS → publish

Success through script.txt, failure during TTS or publish (already today's behavior):
  next retry → script.txt exists → skip writer entirely → TTS → publish
```

The retry budget is now consumed by:

1. Fresh model calls that fail to even produce raw output.
2. Parse failures (which delete the bad output and force a fresh call next time).

A successful generation followed by a TTS or publish failure consumes zero additional model calls. A consumer crash between generation and parsing consumes zero additional model calls.

## Error Handling

| Failure point | Before | After |
|---|---|---|
| `create_session` raises | Retry creates fresh | Same |
| `send_prompt_async` raises | Retry creates fresh | Same |
| `wait_for_idle` returns False | Retry creates fresh | Same (raw output never persisted) |
| Model returns text | Continue to parse | **Persist `raw_writer_output.txt`, then parse** |
| Parse raises (missing tags) | Retry regenerates | **Persisted file deleted, retry regenerates** |
| Validation raises (empty script) | Retry regenerates | **Persisted file deleted, retry regenerates** |
| TTS fails after `script.txt` written | Already a no-op for the writer | Same |
| Consumer crashes between persist and parse | Retry re-calls model | **Retry reuses persisted output** |

## Testing

For each of `rundown_writer.py` and `fp_writer.py`:

1. `test_persists_raw_output_before_parsing` — model returns valid text → file exists with expected content, `WriterOutput` parsed correctly.
2. `test_reuses_persisted_output_when_present` — file pre-exists with valid content → no opencode session created, `WriterOutput` derived from file content.
3. `test_deletes_persisted_output_on_parse_failure` — file pre-exists with `<script>   </script>` (parses to empty) → `RuntimeError("empty script")` raised, file no longer exists.
4. `test_does_not_persist_when_wait_for_idle_times_out` — `wait_for_idle` returns False → no `raw_writer_output.txt` written, original RuntimeError raised.

Existing tests (`test_generate_rundown_script`, `test_generate_fp_script`, timeout tests, etc.) gain a `work_dir=tmp_path` argument. The `tmp_path` pytest fixture handles cleanup automatically.

## Risks

- **Stale persisted output across `--lookback` changes**: not a real risk, because the work_dir is keyed by job_id (e.g. `/tmp/the-rundown-{uuid}`) and a new job gets a fresh directory.
- **`/tmp` cleanup mid-retry**: `_cleanup_old_work_dirs` already removes work dirs older than the retention window. If `raw_writer_output.txt` is removed underneath us, the next retry just regenerates — same as today.
- **Disk space**: `raw_writer_output.txt` is ~12 KB per attempt. Negligible.
