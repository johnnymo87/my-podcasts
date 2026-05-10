# Writer Output Persistence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist the model's raw assistant output to disk the moment it's available, so any retry that follows a successful generation reuses the persisted text instead of calling the model again. Applies to `pipeline/rundown_writer.py` and `pipeline/fp_writer.py`.

**Architecture:** Add a required `work_dir: Path` parameter to `generate_rundown_script` and `generate_fp_script`. Inside each, write `raw_writer_output.txt` to `work_dir` immediately after `get_last_assistant_text` returns, before any parsing. On retry, if the file already exists, reuse it and skip the model call entirely. If parsing the (possibly persisted) text fails, delete the file before re-raising so the next retry regenerates instead of looping on the same broken content.

**Tech Stack:** Python 3.14, pytest, opencode-serve HTTP API, existing `pipeline/opencode_client.py` primitives.

**Design doc:** `docs/plans/2026-05-10-writer-output-persistence-design.md`

---

## Pre-flight

Before starting Task 1, confirm baseline:

```bash
cd /home/dev/projects/my-podcasts && uv run pytest -q 2>&1 | tail -5
```
Expected: `306 passed` (or more, if other work has landed).

If you have uncommitted changes from prior work, commit or stash them first. The plan assumes a clean working tree.

---

## Task 1: Rundown writer — add `work_dir` param to existing tests (no behavior change yet)

**Why this first:** Existing tests call `generate_rundown_script(...)` without a `work_dir`. Adding the required param will break them. We update the call sites to pass `work_dir=tmp_path` first, while the implementation still ignores the new param. This keeps the test suite green between Task 1 and Task 2 and minimizes the diff in each.

**Files:**
- Modify: `pipeline/test_rundown_writer.py:67-71, 90-94, 118-122, 143-147, 218-222, 290-307` (every call to `generate_rundown_script`)

**Step 1: Audit existing call sites**

Run: `cd /home/dev/projects/my-podcasts && rg -n "generate_rundown_script\(" pipeline/test_rundown_writer.py`
Expected: list of every test that calls `generate_rundown_script`. There should be ~6 call sites (varies depending on test file evolution).

**Step 2: Update every test that calls `generate_rundown_script` to inject `work_dir`**

For each test that has `def test_xxx(...):` and calls `generate_rundown_script`, change the signature to accept `tmp_path` (pytest builtin fixture) and add `work_dir=tmp_path` to the call. Tests using `@patch` decorators look like this — note the new `tmp_path` arg comes AFTER all the mock args (pytest injects fixtures by name, not position):

```python
@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_generate_script(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    mock_create.return_value = "ses_123"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = "Generated podcast script here"

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )
    ...
```

Apply this same shape to every test that calls `generate_rundown_script`.

**Step 3: Run the updated tests — they should currently FAIL**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py -q`
Expected: `TypeError: generate_rundown_script() got an unexpected keyword argument 'work_dir'` for every modified test (~6 failures). This confirms the tests are wired correctly and the production code doesn't yet accept the param.

**Step 4: Add the `work_dir` param to `generate_rundown_script` (signature only, ignore it)**

Modify `pipeline/rundown_writer.py:158-163`:

```python
def generate_rundown_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
    work_dir: Path | None = None,
) -> WriterOutput:
    """Generate a Rundown podcast script via the shared opencode server."""
    # work_dir is accepted for API compatibility; persistence is added in
    # the next task.
    _ = work_dir
    prompt = build_rundown_prompt(themes, articles_by_theme, date_str, context_scripts)
    ...  # rest unchanged
```

Also add `from pathlib import Path` to the imports at the top of `pipeline/rundown_writer.py`.

(Yes, `work_dir` is `Path | None = None` here even though the design says required. Task 3 tightens this once the signature change has propagated through all callers.)

**Step 5: Run tests — they should now PASS**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py -q`
Expected: all green.

**Step 6: Commit**

```bash
cd /home/dev/projects/my-podcasts
git add pipeline/rundown_writer.py pipeline/test_rundown_writer.py
git commit -m "refactor(rundown_writer): accept optional work_dir param (no-op)

Plumb a work_dir argument into generate_rundown_script ahead of the
persistence change. The param is ignored for now; tests pass tmp_path."
```

---

## Task 2: Rundown writer — implement persistence (TDD)

**Files:**
- Modify: `pipeline/rundown_writer.py:158-200`
- Modify: `pipeline/test_rundown_writer.py` (add 4 new tests at the end of the file, before the `parse_*` tests if grouped, or at the end)

### Sub-task 2a: Test — persists raw output before parsing

**Step 1: Write the failing test**

Add to `pipeline/test_rundown_writer.py` (place near the other `test_generate_*` tests):

```python
@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_persists_raw_output_before_parsing(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """The raw assistant text is written to raw_writer_output.txt the moment
    it's available, before any parsing happens."""
    mock_create.return_value = "ses_persist"
    mock_wait.return_value = True
    mock_messages.return_value = [{"role": "assistant", "parts": []}]
    mock_text.return_value = (
        "<summary>Today's summary.</summary>\n\n<script>Today's script.</script>"
    )

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    raw_path = tmp_path / "raw_writer_output.txt"
    assert raw_path.exists()
    assert raw_path.read_text(encoding="utf-8") == (
        "<summary>Today's summary.</summary>\n\n<script>Today's script.</script>"
    )
    assert result.script == "Today's script."
    assert result.summary == "Today's summary."
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py::test_persists_raw_output_before_parsing -v`
Expected: FAIL — `assert raw_path.exists()` fails because the writer doesn't yet persist anything.

**Step 3: Implement persistence**

Replace the body of `generate_rundown_script` in `pipeline/rundown_writer.py:158-200` with:

```python
def generate_rundown_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
    work_dir: Path | None = None,
) -> WriterOutput:
    """Generate a Rundown podcast script via the shared opencode server.

    If ``work_dir`` is provided, the model's raw output is persisted to
    ``work_dir/raw_writer_output.txt`` the moment it's available. Subsequent
    calls with the same ``work_dir`` skip the model call entirely and reuse
    the persisted text. If parsing the persisted text fails, the file is
    deleted so the next retry regenerates instead of looping on the same
    broken content.
    """
    raw_path = work_dir / "raw_writer_output.txt" if work_dir else None

    if raw_path is not None and raw_path.exists():
        full_text = raw_path.read_text(encoding="utf-8")
    else:
        prompt = build_rundown_prompt(
            themes, articles_by_theme, date_str, context_scripts
        )
        instruction = (
            "Read the following prompt and generate the podcast briefing script. "
            "First, write a 2-3 sentence summary of today's episode wrapped in "
            "<summary>...</summary> tags. "
            "Then list the headlines of the stories you actually cover in the script, "
            "wrapped in <covered>...</covered> tags, one headline per line prefixed "
            "with a dash. Use the exact headlines from the source material. "
            "Then write the full spoken script wrapped in "
            "<script>...</script> tags. Do NOT include any analysis, reasoning, or "
            "meta-commentary outside these tags — only the summary, covered list, "
            "and the script that will be read aloud.\n\n" + prompt
        )

        session_id = create_session()
        try:
            send_prompt_async(session_id, instruction)
            if not wait_for_idle(session_id, timeout=900):
                raise RuntimeError(
                    "opencode session did not complete within 900 seconds"
                )
            messages = get_messages(session_id)
            full_text = get_last_assistant_text(messages).strip()
            if raw_path is not None:
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(full_text, encoding="utf-8")
        finally:
            delete_session(session_id)

    try:
        covered = parse_covered(full_text)
        summary_result = parse_summary(full_text)
        script = _extract_script(summary_result.script)
        if not script.strip():
            raise RuntimeError("Rundown writer returned empty script")
    except RuntimeError:
        if raw_path is not None:
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

Remove the now-unused `_ = work_dir` placeholder line.

**Step 4: Run test to verify it passes**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py::test_persists_raw_output_before_parsing -v`
Expected: PASS.

### Sub-task 2b: Test — reuses persisted output when present

**Step 1: Write the failing test**

Add to `pipeline/test_rundown_writer.py`:

```python
@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_reuses_persisted_output_when_present(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """If raw_writer_output.txt already exists, the model is not called."""
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Cached summary.</summary>\n\n<script>Cached script.</script>",
        encoding="utf-8",
    )

    result = generate_rundown_script(
        themes=["Tech"],
        articles_by_theme={"Tech": ["Article"]},
        date_str="2026-03-10",
        work_dir=tmp_path,
    )

    assert result.script == "Cached script."
    assert result.summary == "Cached summary."
    mock_create.assert_not_called()
    mock_send.assert_not_called()
    mock_wait.assert_not_called()
    mock_messages.assert_not_called()
    mock_delete.assert_not_called()
```

**Step 2: Run — should already PASS** (the implementation in 2a handles this case already)

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py::test_reuses_persisted_output_when_present -v`
Expected: PASS. (If it fails, the implementation in 2a is wrong — debug before continuing.)

### Sub-task 2c: Test — deletes persisted output on parse failure

**Step 1: Write the failing test**

Add to `pipeline/test_rundown_writer.py`:

```python
@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.get_last_assistant_text")
@patch("pipeline.rundown_writer.get_messages")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_deletes_persisted_output_on_parse_failure(
    mock_create, mock_send, mock_wait, mock_messages, mock_text, mock_delete,
    tmp_path,
):
    """If a persisted file parses to an empty script, the file is deleted
    so the next retry regenerates."""
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Empty body.</summary>\n\n<script>   </script>",
        encoding="utf-8",
    )

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "empty script" in str(e)

    assert not raw_path.exists()
    mock_create.assert_not_called()
```

**Step 2: Run — should already PASS** (the implementation in 2a handles this too)

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py::test_deletes_persisted_output_on_parse_failure -v`
Expected: PASS.

### Sub-task 2d: Test — does not persist when wait_for_idle times out

**Step 1: Write the failing test**

Add to `pipeline/test_rundown_writer.py`:

```python
@patch("pipeline.rundown_writer.delete_session")
@patch("pipeline.rundown_writer.wait_for_idle")
@patch("pipeline.rundown_writer.send_prompt_async")
@patch("pipeline.rundown_writer.create_session")
def test_does_not_persist_when_wait_for_idle_times_out(
    mock_create, mock_send, mock_wait, mock_delete, tmp_path
):
    """If wait_for_idle returns False, no raw_writer_output.txt is written."""
    mock_create.return_value = "ses_timeout_persist"
    mock_wait.return_value = False

    try:
        generate_rundown_script(
            themes=["Tech"],
            articles_by_theme={"Tech": ["Article"]},
            date_str="2026-03-10",
            work_dir=tmp_path,
        )
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "900 seconds" in str(e)

    raw_path = tmp_path / "raw_writer_output.txt"
    assert not raw_path.exists()
    mock_delete.assert_called_once_with("ses_timeout_persist")
```

**Step 2: Run — should already PASS**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py::test_does_not_persist_when_wait_for_idle_times_out -v`
Expected: PASS.

### Sub-task 2e: Run full rundown writer test file

**Step 1: Run all rundown writer tests**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_rundown_writer.py -q`
Expected: all green (the 4 new tests plus all pre-existing tests).

**Step 2: Commit**

```bash
cd /home/dev/projects/my-podcasts
git add pipeline/rundown_writer.py pipeline/test_rundown_writer.py
git commit -m "feat(rundown_writer): persist raw model output for retry recovery

Write raw_writer_output.txt to work_dir the moment the model returns,
before parsing. On retry, reuse the persisted text and skip the model
call. On parse failure, delete the file so the next retry regenerates."
```

---

## Task 3: FP writer — same change

Mirror Task 1 + Task 2 for `pipeline/fp_writer.py`. Because `fp_writer.py` shares parsers and `WriterOutput` with `rundown_writer.py`, the changes are mechanical.

**Files:**
- Modify: `pipeline/fp_writer.py:107-149`
- Modify: `pipeline/test_fp_writer.py:50-194`

### Sub-task 3a: Plumb `work_dir` through existing tests + signature

**Step 1: Audit FP writer call sites in tests**

Run: `cd /home/dev/projects/my-podcasts && rg -n "generate_fp_script\(" pipeline/test_fp_writer.py`
Expected: list of ~4 call sites.

**Step 2: Add `tmp_path` and `work_dir=tmp_path` to every test that calls `generate_fp_script`**

The pattern matches Task 1, but `test_fp_writer.py` uses `monkeypatch` instead of `@patch` decorators:

```python
def test_generate_fp_script(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session",
        lambda directory=None: "sess-fp",
    )
    ...
    result = generate_fp_script(
        themes,
        articles_by_theme,
        date_str="2026-03-06",
        work_dir=tmp_path,
    )
```

Apply this to all four FP writer tests.

**Step 3: Run — tests should FAIL with `TypeError: unexpected keyword argument 'work_dir'`**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_fp_writer.py -q`
Expected: 4 failures.

**Step 4: Add `work_dir` param to `generate_fp_script` signature (no-op)**

Modify `pipeline/fp_writer.py:107-113`:

```python
def generate_fp_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
    work_dir: Path | None = None,
) -> WriterOutput:
    """Generate a FP podcast script via the shared opencode server."""
    _ = work_dir
    ...  # rest unchanged
```

Add `from pathlib import Path` to imports at the top of `pipeline/fp_writer.py` if not already present.

**Step 5: Run — tests should PASS**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_fp_writer.py -q`
Expected: all green.

**Step 6: Commit**

```bash
cd /home/dev/projects/my-podcasts
git add pipeline/fp_writer.py pipeline/test_fp_writer.py
git commit -m "refactor(fp_writer): accept optional work_dir param (no-op)"
```

### Sub-task 3b: Implement FP persistence with TDD

Add the same four tests to `pipeline/test_fp_writer.py` (using `monkeypatch` style instead of `@patch`):

```python
def test_persists_fp_raw_output_before_parsing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session", lambda directory=None: "ses_persist"
    )
    monkeypatch.setattr("pipeline.fp_writer.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.fp_writer.wait_for_idle", lambda sid, timeout=900: True
    )
    monkeypatch.setattr(
        "pipeline.fp_writer.get_messages",
        lambda sid: [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": "<summary>FP summary.</summary>\n\n<script>FP script.</script>",
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr("pipeline.fp_writer.delete_session", lambda sid: None)

    result = generate_fp_script(
        themes=["Iran"],
        articles_by_theme={"Iran": ["Article"]},
        date_str="2026-03-06",
        work_dir=tmp_path,
    )

    raw_path = tmp_path / "raw_writer_output.txt"
    assert raw_path.exists()
    assert raw_path.read_text(encoding="utf-8") == (
        "<summary>FP summary.</summary>\n\n<script>FP script.</script>"
    )
    assert result.script == "FP script."
    assert result.summary == "FP summary."


def test_reuses_fp_persisted_output_when_present(monkeypatch, tmp_path) -> None:
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Cached.</summary>\n\n<script>Cached FP script.</script>",
        encoding="utf-8",
    )

    called = {"create": 0}
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session",
        lambda directory=None: (called.__setitem__("create", called["create"] + 1) or "ses"),
    )
    monkeypatch.setattr("pipeline.fp_writer.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.fp_writer.wait_for_idle", lambda sid, timeout=900: True
    )
    monkeypatch.setattr("pipeline.fp_writer.get_messages", lambda sid: [])
    monkeypatch.setattr("pipeline.fp_writer.delete_session", lambda sid: None)

    result = generate_fp_script(
        themes=["Iran"],
        articles_by_theme={"Iran": ["Article"]},
        date_str="2026-03-06",
        work_dir=tmp_path,
    )

    assert result.script == "Cached FP script."
    assert result.summary == "Cached."
    assert called["create"] == 0


def test_deletes_fp_persisted_output_on_parse_failure(monkeypatch, tmp_path) -> None:
    raw_path = tmp_path / "raw_writer_output.txt"
    raw_path.write_text(
        "<summary>Empty.</summary>\n\n<script>   </script>",
        encoding="utf-8",
    )

    called = {"create": 0}
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session",
        lambda directory=None: (called.__setitem__("create", called["create"] + 1) or "ses"),
    )

    with pytest.raises(RuntimeError, match="empty script"):
        generate_fp_script(
            themes=["Iran"],
            articles_by_theme={"Iran": ["Article"]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )

    assert not raw_path.exists()
    assert called["create"] == 0


def test_does_not_persist_fp_when_wait_for_idle_times_out(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "pipeline.fp_writer.create_session", lambda directory=None: "ses_timeout"
    )
    monkeypatch.setattr("pipeline.fp_writer.send_prompt_async", lambda sid, t: None)
    monkeypatch.setattr(
        "pipeline.fp_writer.wait_for_idle", lambda sid, timeout=900: False
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        "pipeline.fp_writer.delete_session", lambda sid: deleted.append(sid)
    )

    with pytest.raises(RuntimeError, match="900 seconds"):
        generate_fp_script(
            themes=["Iran"],
            articles_by_theme={"Iran": ["Article"]},
            date_str="2026-03-06",
            work_dir=tmp_path,
        )

    assert not (tmp_path / "raw_writer_output.txt").exists()
    assert "ses_timeout" in deleted
```

**Step 1: Run new tests — they should FAIL**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_fp_writer.py -k "persist or reuse" -q`
Expected: 4 failures (the persistence isn't implemented yet — Task 3a only added the no-op signature).

**Step 2: Implement FP persistence**

Replace the body of `generate_fp_script` in `pipeline/fp_writer.py:107-149` with the same shape as the rundown writer (just with FP's instruction string and "FP writer returned empty script"). Pattern:

```python
def generate_fp_script(
    themes: list[str],
    articles_by_theme: dict[str, list[str]],
    date_str: str,
    context_scripts: list[str] | None = None,
    work_dir: Path | None = None,
) -> WriterOutput:
    """Generate a FP podcast script via the shared opencode server.

    See ``pipeline.rundown_writer.generate_rundown_script`` for the persistence
    contract — this writer follows the same pattern.
    """
    raw_path = work_dir / "raw_writer_output.txt" if work_dir else None

    if raw_path is not None and raw_path.exists():
        full_text = raw_path.read_text(encoding="utf-8")
    else:
        prompt = build_fp_prompt(
            themes, articles_by_theme, date_str, context_scripts
        )
        instruction = (
            "Read the following prompt and generate the podcast briefing script. "
            "First, write a 2-3 sentence summary of today's episode wrapped in "
            "<summary>...</summary> tags. "
            "Then list the headlines of the stories you actually cover in the script, "
            "wrapped in <covered>...</covered> tags, one headline per line prefixed "
            "with a dash. Use the exact headlines from the source material. "
            "Then write the full spoken script wrapped in "
            "<script>...</script> tags. Do NOT include any analysis, reasoning, or "
            "meta-commentary outside these tags — only the summary, covered list, "
            "and the script that will be read aloud.\n\n" + prompt
        )

        session_id = create_session()
        try:
            send_prompt_async(session_id, instruction)
            if not wait_for_idle(session_id, timeout=900):
                raise RuntimeError(
                    "opencode session did not complete within 900 seconds"
                )
            messages = get_messages(session_id)
            full_text = get_last_assistant_text(messages).strip()
            if raw_path is not None:
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(full_text, encoding="utf-8")
        finally:
            delete_session(session_id)

    try:
        covered = parse_covered(full_text)
        summary_result = parse_summary(full_text)
        script = _extract_script(summary_result.script)
        if not script.strip():
            raise RuntimeError("FP writer returned empty script")
    except RuntimeError:
        if raw_path is not None:
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

Remove the `_ = work_dir` placeholder added in Task 3a.

**Step 3: Run all FP writer tests**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest pipeline/test_fp_writer.py -q`
Expected: all green (existing 4 + new 4 = 8 generate-related tests, plus the build-prompt tests).

**Step 4: Commit**

```bash
cd /home/dev/projects/my-podcasts
git add pipeline/fp_writer.py pipeline/test_fp_writer.py
git commit -m "feat(fp_writer): persist raw model output for retry recovery

Mirror the rundown_writer change. Same persistence contract:
- raw_writer_output.txt written before parsing
- retry reuses persisted text, skipping model call
- parse failure deletes the file so retry regenerates"
```

---

## Task 4: Wire `work_dir` through real call sites

Both writers now accept `work_dir`. The consumer and CLIs need to actually pass it.

**Files:**
- Modify: `pipeline/consumer.py:388` (Rundown writer call) and `pipeline/consumer.py:532` (FP writer call)
- Modify: `pipeline/__main__.py:385, 479, 600, 693` (CLI call sites)

**Step 1: Locate consumer call sites**

Run: `cd /home/dev/projects/my-podcasts && rg -n -A 6 "generate_(rundown|fp)_script\(" pipeline/consumer.py`
Expected: two call sites, both inside blocks where `work_dir = Path(f"/tmp/the-rundown-{job['id']}")` (line ~250) or `Path(f"/tmp/fp-digest-{job['id']}")` (line ~430) is already in scope.

**Step 2: Add `work_dir=work_dir` to each consumer call**

Modify `pipeline/consumer.py:388-393`:

```python
writer_output = generate_rundown_script(
    themes=plan.themes,
    articles_by_theme=rundown_articles_by_theme,
    date_str=job["date_str"],
    context_scripts=context_scripts,
    work_dir=work_dir,
)
```

Modify `pipeline/consumer.py:532-537`:

```python
writer_output = generate_fp_script(
    themes=plan.themes,
    articles_by_theme=articles_by_theme,
    date_str=job["date_str"],
    context_scripts=context_scripts,
    work_dir=work_dir,
)
```

**Step 3: Locate CLI call sites**

Run: `cd /home/dev/projects/my-podcasts && rg -n -A 6 "generate_(rundown|fp)_script\(" pipeline/__main__.py`
Expected: four call sites.

**Step 4: Add `work_dir=work_dir` at each CLI call site**

Each call site already has `work_dir = Path(...)` in scope locally. Add `work_dir=work_dir` to the call, matching the pattern from Task 4 Step 2.

**Step 5: Run full test suite**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest -q`
Expected: all green. Pre-existing test count was 306; after Tasks 1-4 it should be 314 (8 new tests across the two writers).

**Step 6: Commit**

```bash
cd /home/dev/projects/my-podcasts
git add pipeline/consumer.py pipeline/__main__.py
git commit -m "feat(consumer): pass work_dir to writers for retry recovery

Both daily writers now persist their raw model output to work_dir
the moment it's available. The consumer and CLIs were already keying
off work_dir for collection sentinels and script.txt; now the writer
itself participates in that artifact convention."
```

---

## Task 5: Documentation

**Files:**
- Modify: `pipeline/AGENTS.md`

**Step 1: Update the "Current Hardening" section**

Find the block in `pipeline/AGENTS.md:23-30` (or thereabouts):

```markdown
- `pipeline/rundown_writer.py`
  - 900-second opencode writer timeout
  - empty script output rejected at writer boundary
- `pipeline/fp_writer.py`
  - 900-second opencode writer timeout
  - empty script output rejected at writer boundary
```

Add a third bullet to each:

```markdown
- `pipeline/rundown_writer.py`
  - 900-second opencode writer timeout
  - empty script output rejected at writer boundary
  - raw model output persisted to `work_dir/raw_writer_output.txt` before parsing; retries reuse this file and skip the model call
- `pipeline/fp_writer.py`
  - 900-second opencode writer timeout
  - empty script output rejected at writer boundary
  - raw model output persisted to `work_dir/raw_writer_output.txt` before parsing; retries reuse this file and skip the model call
```

**Step 2: Verify there are no other docs that describe the writer's retry behavior in a way that's now inaccurate**

Run: `cd /home/dev/projects/my-podcasts && rg -n "writer.*retry|retry.*writer|raw_writer_output" --type md`
Expected: matches in `pipeline/AGENTS.md` only. If there are stale references in `.opencode/skills/` or `AGENTS.md`, update them to match.

**Step 3: Commit**

```bash
cd /home/dev/projects/my-podcasts
git add pipeline/AGENTS.md
git commit -m "docs(pipeline): document raw_writer_output.txt persistence"
```

---

## Task 6: End-to-end verification

This is a manual gate before considering the work complete.

**Step 1: Confirm full suite passes**

Run: `cd /home/dev/projects/my-podcasts && uv run pytest -q 2>&1 | tail -5`
Expected: 314 passed (or more, accounting for any other work merged since the baseline). No warnings related to your changes.

**Step 2: Confirm the consumer service can still import + start cleanly**

Run: `cd /home/dev/projects/my-podcasts && uv run python -c "from pipeline.consumer import main; print('imports OK')"`
Expected: `imports OK`. (Don't actually start the loop — just confirm the module imports.)

**Step 3: Restart the consumer**

```bash
sudo systemctl restart my-podcasts-consumer
sleep 3
sudo systemctl status my-podcasts-consumer --no-pager | head -10
```
Expected: `Active: active (running)`.

**Step 4: Confirm no immediate startup errors**

Run: `journalctl -u my-podcasts-consumer --since "1 minute ago" --no-pager | tail -20`
Expected: normal poll messages, no tracebacks, no `TypeError` about unexpected keyword arguments.

**Step 5: Push**

Per `AGENTS.md` "Landing the Plane":

```bash
cd /home/dev/projects/my-podcasts
git pull --rebase
bd sync
git push
git status  # MUST show "up to date with origin"
```

---

## Verification Checklist

Before claiming the work is complete:

- [ ] `uv run pytest -q` shows all green (≥314 tests)
- [ ] `pipeline/rundown_writer.py:generate_rundown_script` accepts `work_dir`
- [ ] `pipeline/fp_writer.py:generate_fp_script` accepts `work_dir`
- [ ] `pipeline/consumer.py` passes `work_dir` at both writer call sites
- [ ] `pipeline/__main__.py` passes `work_dir` at all four CLI call sites
- [ ] `pipeline/AGENTS.md` documents the new artifact
- [ ] Consumer service restarts cleanly after the change
- [ ] Changes are committed AND pushed

If any box is unchecked, the work is not done.
