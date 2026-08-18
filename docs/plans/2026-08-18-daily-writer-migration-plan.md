# Migrate the daily writers to `report_engine` — `my-podcasts-xlf`

Bead: `my-podcasts-xlf` (P2, spine piece 1). Branch:
`feat/migrate-daily-writers-to-report-engine`.

## Why

`pipeline/rundown_writer.py:_extract_script` (imported by `pipeline/fp_writer.py`)
is the last surviving copy of the extraction logic that PR #15 consolidated into
`pipeline/report_engine.py`. These are the two **daily** writers: they publish to
live feeds at 04:30 ET with no human in the loop, so they are the highest-stakes
carriers of the defect, not the lowest.

## Measured, not assumed

Every claim below was reproduced by running the current code against the engine
before any of it was written. `old` is
`_extract_script(parse_summary(t).script)`; `new` is `report_engine.extract_script(t)`.

| shape | old | new |
|---|---|---|
| A `<script>REAL…</scrip>` (mangled close, the `ne0` shape) | 6516 chars **including a literal `<script>` tag**, narrated aloud | 6499 clean |
| B `<SCRIPT>REAL…</SCRIPT>` | 6517 chars with literal tags | 6499 clean |
| C no tags at all | 6522 chars of raw model reasoning | 6521 chars of raw model reasoning |
| D `<script>...</script>` placeholder, then `<script>REAL…</scrip>` | **3 chars** (`...`) | 6499 clean |
| E `<summary>…</summary><covered>- h</covered>REAL…` (no script tags) | 6523 incl. `<covered>` | 6523 incl. `<covered>` |

Two of those results **contradict the bead and the roadmap**, which is why they
are recorded here rather than quietly worked around:

- **Finding 1 — migration alone does NOT fix the no-tag fallback.** The bead says
  the delta includes "replacing the `return text` no-match fallback." It does not.
  The engine's fallback is still "return the text," merely with the `<summary>`
  block and stray tags stripped (case C: 6522 → 6521). Raw model reasoning still
  reaches TTS, and `min_chars` cannot catch it because raw output is long. This
  defect is live in the already-migrated paths too. Closing it is **new work**
  (`require_tags`), not part of the migration.
- **Finding 2 — `<covered>` leaks through the guard.** The guard is
  `</?(?:script|summary)\b`; `<covered>` is absent. Only the daily writers emit a
  `<covered>` block, so the engine has never had to handle it (case E).

## The design question, settled

`run_report_prompt` fuses (i) session lifecycle → raw text with (ii) parse +
publish-boundary refusals. The daily writers must interpose between them, to
persist raw output to `work_dir/raw_writer_output.txt` and, on retry, skip the
900-second model call and re-parse the persisted file.

**Decision: decompose along that seam** (option B of three considered).

- `fetch_report_text(instruction, *, label) -> str` — session lifecycle only.
- `parse_report(text, *, label, min_chars, require_tags) -> ReportOutput` — parse
  plus **all** refusals.
- `run_report_prompt` becomes literally the composition of the two, signature
  unchanged, so existing callers are untouched.

Rejected: **(A) callback extension points** — `on_raw_text` cannot express "skip
the model call entirely," which is a *precondition*, not a post-hook; it would
need a callback plus a text-override parameter, which is decomposition wearing a
costume with the control flow hidden. **(C) a thin wrapper around the fused
function** — a wrapper cannot interpose *between* fetch and parse, so it means
re-implementing one half, which is the current buggy state.

The one real hazard of (B) is that a public `parse_report` invites a future
caller to parse *without* the refusals, recreating the drift that caused `78b`
and `ne0`. Mitigation: the refusals live **inside** `parse_report` and it raises;
there is no "parse without guard" entry point. Composing differently still goes
through the guarded parse.

`parse_covered` stays in `rundown_writer`. The engine's job is refusing to
narrate markup (universal); parsing a caller-specific block is the caller's.

## Summary-remainder vs full-text: a measured tradeoff, not an upgrade

Today rundown strips the `<summary>` block and extracts the script from the
**remainder**; the engine extracts from the **full text**. These diverge, and
neither is strictly better — both were run:

| shape | full-text (engine) | remainder-first (today) |
|---|---|---|
| stray `<script>` *mentioned inside* the summary prose | 6544 chars, **guard fires → refusal** | 6499 clean chars, correct |
| a real `<script>` block *nested inside* `<summary>` | 6499 clean chars, correct | 0 chars → empty → refusal |

Each shape fails loud in the case it handles badly, so this is a genuine
tradeoff. **Adopt full-text**, per the engine's existing behavior: it keeps the
blast radius off already-migrated, already-reviewed callers, and the cost of its
bad shape is one retry cycle with a fresh model call, not a wrong episode. Pin
the stray-mention shape as a test that documents the refusal as deliberate.

## Tasks

Ordering is dictated by one landmine: `fp_writer` imports `_extract_script`,
`parse_summary`, and `WriterOutput` from `rundown_writer`. Deleting any of them
before `fp_writer` stops importing is an `ImportError`, and the consumer runs
against the **live working tree** as a long-lived loop, so that is a crash-loop.

1. **Engine only, no caller changes.** Decompose into `fetch_report_text` +
   `parse_report`; `run_report_prompt` becomes their composition with an
   unchanged signature. Add `covered` to the leaked-markup guard. Add
   `require_tags`. The only behavior change for existing callers is that the
   guard refuses strictly more. Tests for shapes A-E plus stray-tag-in-summary.
2. **Migrate `rundown_writer`.** Compose fetch → persist → parse. **Leave**
   `_extract_script` and `parse_summary` in place — dead in rundown, still live
   in `fp_writer`'s imports.
3. **Migrate `fp_writer` and delete `_extract_script`/`parse_summary` in the
   same commit.** Same-commit deletion closes the window in which two
   implementations coexist without ever breaking an import.
4. **`require_tags=True` on the transcript path.** One line and one test at the
   same seam. Not scope creep: `transcript_report`'s own docstring commits to
   re-raise-over-degrade, and a no-tag fallback narrating raw reasoning
   contradicts it. `report_writer` (one-off, operator-reviewed) keeps the
   permissive fallback deliberately — operator review is the guard there.
5. **Docs, roadmap, beads.** Correct Finding 1 in the bead and roadmap.

`min_chars` for the daily writers is **500, re-derived rather than copied**: the
daily failure path is *bounded* (backoff → errored → alert), which is the regime
the original 500 was derived for ("far below the smallest plausible episode
(~5000 chars), so it only ever catches garbage"). That is the opposite of the
transcript path's unbounded-redelivery regime, which forced 2000. The
TTS-boundary `_validate_script_length` stays: it guards paths that bypass the
writer entirely (`--script-file`, `publish-script`), so it is load-bearing
independently and a double floor is not a problem.

Do **not** raise the floor to catch thin-day hallucination — that is `qd5`, a
different defect with a different fix.

## Verification

- `uv run python -c "import pipeline.consumer"` as an import smoke test on every
  commit — an import error here is a production crash-loop.
- `uv run pytest -q` green on every commit.
- Pin the raw-file-delete-on-parse-failure path with a test: the `except` catches
  `RuntimeError` only, and every engine refusal is a `RuntimeError` today. A
  future engine exception of another type would silently loop retries on the same
  broken persisted file.
- Deploy is a restart. Check `pending_the_rundown`/`pending_fp_digest` before
  restarting, and restart outside the 04:30 ET window.
