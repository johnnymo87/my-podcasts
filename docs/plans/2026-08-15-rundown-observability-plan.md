# The Rundown Content-Acquisition Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Repair The Rundown's dead Exa enrichment path and add a per-run content-acquisition funnel report delivered to the Telegram General topic, so that future degradation in article gathering is visible instead of silent.

**Architecture:** Three independent pieces landed in order. Piece 0 makes CI functional (it has been red since June, failing at `ruff check` before `mypy` or `pytest` ever run). Piece 1 repairs the Exa file path so enrichment can reach the writer. Piece 2 adds a funnel reporter that reads work-dir artifacts plus two small collector-emitted sentinels, appends a line to a durable JSONL, and posts a plain-text summary via the pigeon daemon's `/alert` endpoint.

**Tech Stack:** Python 3.14, uv, pytest, ruff, mypy, click, requests, pydantic. Delivery via `POST http://127.0.0.1:4731/alert` on the local pigeon daemon.

**Design doc:** `docs/plans/2026-08-15-rundown-observability-design.md`

**Beads:**

| Bead | Piece | Blocked by |
| --- | --- | --- |
| `my-podcasts-llm` | Piece 0 — unblock CI | ready |
| `my-podcasts-6yo` | Piece 1 — repair the Exa path | `llm` |
| `my-podcasts-vxd` | Piece 2 — the funnel | `6yo` |
| `my-podcasts-85c` | Open-access substitution (the actual feature) | `vxd` |
| `my-podcasts-kyk` | Exa augment-not-replace | `vxd` |
| `my-podcasts-3qs` | Alert thresholds from real data | `vxd` |
| `my-podcasts-cgn` | Triage 111 mypy errors, make the step blocking | `llm` |

---

## Background you need before touching anything

Read the design doc first. The essentials:

- The Rundown runs as a systemd timer Mon-Fri 04:30 ET. The consumer
  (`pipeline/consumer.py`) picks up a pending job, calls
  `collect_all_artifacts` (`pipeline/things_happen_collector.py`), then generates
  a script, then on a *later loop iteration* runs TTS and publishes.
- Work dirs are `/tmp/the-rundown-{job_id}`. **systemd-tmpfiles deletes `/tmp`
  entries after 10 days.** Anything that must survive longer goes in
  `/persist/my-podcasts/`.
- Only Levine links get a live HTTP fetch. Semafor and Zvi articles are copied
  from persistent markdown caches and are always "cache" tier.
- Measured baseline: across 107 work dirs, 99 of 106 Levine article files (93%)
  are headline-only stubs. bloomberg.com is 57 of them. This is the problem the
  observability exists to keep watch over — you are not expected to fix it here.

Run the full test suite before you start so you know the baseline is green:

```bash
cd /home/dev/projects/my-podcasts
uv run pytest -q
```

Expected: `405 passed`.

---

# Piece 0: Make CI functional

This lands as its own PR, before any feature work. Do not mix it with Pieces 1-2
— it reformats 12 files and would drown a feature diff.

### Task 0.1: Auto-fix the mechanical ruff violations

**Files:**
- Modify: many, mechanically

**Step 1: Record the starting state**

```bash
uv run ruff check . 2>&1 | tail -3
```

Expected: `Found 71 errors.` with a note that 23 are fixable.

**Step 2: Apply the safe auto-fixes**

```bash
uv run ruff check --fix .
uv run ruff check . 2>&1 | tail -3
```

Expected: error count drops. Whatever remains is hand-work for Task 0.2.

**Step 3: Verify tests still pass**

```bash
uv run pytest -q
```

Expected: `405 passed`. Auto-fixes are safe fixes only; if this fails, revert
and investigate rather than pressing on.

**Step 4: Commit**

```bash
git add -A
git commit -m "style: apply ruff safe auto-fixes"
```

### Task 0.2: Hand-fix the remaining ruff violations

**Step 1: List what is left, grouped by rule**

```bash
uv run ruff check . --output-format=concise | sed 's/.*\] //' | sort | uniq -c | sort -rn
```

**Step 2: Fix each**

Two judgment calls, applied consistently:

- Unused function arguments that exist to satisfy an interface (e.g. `date_str`
  in a signature that must match a sibling): rename with a leading underscore, as
  ruff's help suggests.
- Long lines in prompt or fixture strings: add the file to the existing
  `[tool.ruff.lint.per-file-ignores]` block in `pyproject.toml` with an `E501`
  ignore and a comment saying why, matching the eleven entries already there.
  Do **not** reflow prompt strings; the line breaks are semantically meaningful
  to the model reading them.

Everything else gets a real fix, not an ignore.

**Step 3: Verify**

```bash
uv run ruff check .
```

Expected: `All checks passed!`

**Step 4: Commit**

```bash
git add -A
git commit -m "style: clear remaining ruff violations"
```

### Task 0.3: Apply ruff format

**Step 1: See the scope**

```bash
uv run ruff format --check . 2>&1 | tail -3
```

Expected: `12 files would be reformatted, 78 files already formatted`.

**Step 2: Apply**

```bash
uv run ruff format .
uv run ruff format --check .
```

Expected: `90 files already formatted`.

**Step 3: Verify tests**

```bash
uv run pytest -q
```

Expected: `405 passed`.

**Step 4: Commit**

```bash
git add -A
git commit -m "style: apply ruff format"
```

### Task 0.4: Fix the workflow itself

**Files:**
- Modify: `.github/workflows/ci.yaml`

**Step 1: Rewrite the job steps**

Three changes. The Python pin is wrong (`3.11` against a `requires-python
>=3.14`); `uv` supplies its own interpreter so it was merely misleading, but
misleading config in a file nobody trusts is how this rotted. `pytest` moves
ahead of `mypy` so the gate that actually passes reports first. `mypy` becomes
non-blocking until its 111 errors are triaged — an honest amber beats a red
gate everybody has learned to ignore, and beats deleting the step.

```yaml
name: CI

on:
  push: # Will run on all branch pushes
  pull_request: # Will run on all PRs

permissions:
  contents: read

jobs:
  build:

    runs-on: ubuntu-latest

    steps:
    - uses: actions/checkout@v4
    - name: Set up Python 3.14
      uses: actions/setup-python@v5
      with:
        python-version: "3.14"

    - name: Install uv
      run: |
        curl -LsSf https://astral.sh/uv/install.sh | sh

    - name: Install dependencies
      run: |
        uv sync

    - name: Run ruff linter
      run: |
        uv run ruff check .

    - name: Run ruff formatter
      run: |
        uv run ruff format --check .

    - name: Run tests
      run: |
        uv run pytest --cov --cov-report=term-missing

    # Non-blocking until the pre-existing 111 errors are triaged.
    # Tracked separately; see docs/plans/2026-08-15-rundown-observability-design.md
    - name: Run mypy type checker
      continue-on-error: true
      run: |
        uv run mypy .
```

**Step 2: Commit and push, then watch the run**

```bash
git add .github/workflows/ci.yaml
git commit -m "ci: unblock the pipeline

Red since at least June 15, always failing at ruff before mypy or
pytest could run. Clears the lint and format gates, moves tests ahead
of mypy, and makes mypy non-blocking until its 111 pre-existing errors
are triaged. Also corrects the Python pin, which claimed 3.11 against a
requires-python of >=3.14."
git push
```

**Step 3: Verify CI is green**

```bash
gh run list --limit 1
```

Expected: `completed  success`. If it is not, fix forward — this piece is not
done until CI passes.

---

# Piece 1: Repair the Exa path

Three defects, one commit each. All are in the Rundown path only; the FP path is
already correct and must not be touched.

### Task 1.1: Make Exa report why it returned nothing

**Files:**
- Modify: `pipeline/exa_client.py`
- Test: `pipeline/test_exa_client.py` (create if absent)

`search_related` swallows every exception and returns `[]`
(`exa_client.py:48-49`), so a caller cannot distinguish "no results" from "the
API blew up" from "no API key". We need that distinction to instrument the stage.
`search_related` keeps its exact current behavior because `fp_collector.py:391`
depends on it; we add a sibling that reports status and reimplement the old one
on top.

**Step 1: Write the failing tests**

```python
# pipeline/test_exa_client.py
from __future__ import annotations

import pytest

from pipeline import exa_client


def test_search_related_status_reports_no_key(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    results, status = exa_client.search_related_status("anything")
    assert results == []
    assert status == "no_key"


def test_search_related_status_reports_empty(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "fake")

    class _Response:
        results: list = []

    class _Exa:
        def __init__(self, api_key):
            pass

        def search(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(exa_client, "Exa", _Exa)
    results, status = exa_client.search_related_status("anything")
    assert results == []
    assert status == "empty"


def test_search_related_status_reports_error_class(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "fake")

    class _Exa:
        def __init__(self, api_key):
            pass

        def search(self, *a, **kw):
            raise TimeoutError("boom")

    monkeypatch.setattr(exa_client, "Exa", _Exa)
    results, status = exa_client.search_related_status("anything")
    assert results == []
    assert status == "error:TimeoutError"


def test_search_related_still_swallows_errors(monkeypatch):
    """The FP path depends on this contract; it must not change."""
    monkeypatch.setenv("EXA_API_KEY", "fake")

    class _Exa:
        def __init__(self, api_key):
            pass

        def search(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(exa_client, "Exa", _Exa)
    assert exa_client.search_related("anything") == []
```

**Step 2: Run to verify failure**

```bash
uv run pytest pipeline/test_exa_client.py -v
```

Expected: FAIL, `module 'pipeline.exa_client' has no attribute 'search_related_status'`.

**Step 3: Implement**

Replace the body of `pipeline/exa_client.py` below the `ExaResult` dataclass:

```python
def search_related_status(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    num_results: int = 3,
) -> tuple[list[ExaResult], str]:
    """Search Exa, reporting why the result list is empty when it is.

    Status is one of ``hit``, ``empty``, ``no_key``, or ``error:{ExcClass}``.
    ``search_related`` is the status-free view of this, kept because the FP
    collector relies on errors being swallowed.
    """
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return [], "no_key"

    try:
        exa = Exa(api_key=api_key)
        response = exa.search(
            headline,
            num_results=num_results,
            type="auto",
            contents={"text": {"max_characters": 3000}},
            include_domains=include_domains,
        )
        results = [
            ExaResult(
                title=r.title or "",
                url=r.url or "",
                text=r.text or "",
            )
            for r in response.results
        ]
    except Exception as exc:
        return [], f"error:{type(exc).__name__}"

    return results, ("hit" if results else "empty")


def search_related(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    num_results: int = 3,
) -> list[ExaResult]:
    """Search Exa for articles related to a headline.

    Returns empty list if EXA_API_KEY is not set or on any error.
    """
    results, _status = search_related_status(
        headline,
        include_domains=include_domains,
        num_results=num_results,
    )
    return results
```

**Step 4: Run tests**

```bash
uv run pytest pipeline/test_exa_client.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
git add pipeline/exa_client.py pipeline/test_exa_client.py
git commit -m "feat(exa): report why a search returned nothing

search_related collapsed no-API-key, no-results, and exception into a
bare empty list, so a caller could not tell a genuine miss from a
broken client. Adds search_related_status returning (results, status)
and reimplements search_related on top of it, leaving the FP
collector's contract untouched."
```

### Task 1.2: Write Exa output where the readers look, unconditionally

**Files:**
- Modify: `pipeline/things_happen_collector.py:276-290`
- Test: `pipeline/test_things_happen_collector.py`

The collector writes `enrichment/exa/{i:02d}-{slug}.md`; both Rundown readers
(`__main__.py:101`, `show_notes.py:78`) look for the bare `{slug}.md`. Enrichment
has therefore never reached a Rundown writer.

**Do not add a collision suffix.** A suffixed filename is unreachable by the
exact-match readers, which is precisely the bug being fixed. Two directives whose
first 50 slug characters collide are duplicate stories, and the second overwriting
the first is the correct outcome.

**Step 1: Write the failing tests**

Add to `pipeline/test_things_happen_collector.py`:

```python
def test_exa_file_uses_bare_slug_readers_expect(tmp_path, monkeypatch):
    """The write path and the read path must agree on the filename."""
    from pipeline.things_happen_collector import _slugify

    work_dir = _run_collector_with_exa(
        tmp_path, monkeypatch, exa_status="hit", headline="Meta Releases Coding Agent"
    )
    slug = _slugify("Meta Releases Coding Agent")
    assert (work_dir / "enrichment" / "exa" / f"{slug}.md").exists()

    from pipeline.__main__ import _find_rundown_article_text

    exa_dir_files = list((work_dir / "enrichment" / "exa").glob("*.md"))
    assert len(exa_dir_files) == 1
    assert not exa_dir_files[0].name[0].isdigit(), "no index prefix"


def test_exa_file_written_when_search_returns_empty(tmp_path, monkeypatch):
    work_dir = _run_collector_with_exa(tmp_path, monkeypatch, exa_status="empty")
    files = list((work_dir / "enrichment" / "exa").glob("*.md"))
    assert len(files) == 1
    assert "Result: empty" in files[0].read_text(encoding="utf-8")


def test_exa_file_written_when_search_raises(tmp_path, monkeypatch):
    work_dir = _run_collector_with_exa(tmp_path, monkeypatch, exa_status="error")
    files = list((work_dir / "enrichment" / "exa").glob("*.md"))
    assert len(files) == 1
    assert "Result: error:RuntimeError" in files[0].read_text(encoding="utf-8")
```

Write `_run_collector_with_exa` as a module-level helper in that test file,
following the monkeypatching style of the existing tests there (they already stub
`generate_rundown_research_plan`, `fetch_all_articles`, and `sync_zvi_cache` —
reuse those patterns rather than inventing a new harness). It must stub
`things_happen_collector.search_related_status` to return the requested outcome
and produce a plan containing one non-FP directive with `needs_exa=True` and a
non-empty `exa_query`.

**Step 2: Run to verify failure**

```bash
uv run pytest pipeline/test_things_happen_collector.py -k exa -v
```

Expected: FAIL — the file is written with an index prefix, and no file at all is
written for the empty and error cases.

**Step 3: Implement**

Change the import at `things_happen_collector.py:9`:

```python
from pipeline.exa_client import search_related_status
```

Replace the Phase 3 block (`things_happen_collector.py:276-290`):

```python
    # Phase 3: Deep Enrichment (non-FP only)
    # The filename must be the bare slug: __main__._find_rundown_article_text
    # and show_notes._find_article_file both look up `{slug}.md` exactly. An
    # index prefix here is how enrichment silently went undelivered for months.
    exa_outcomes: dict[str, str] = {}
    for directive in non_fp_directives:
        if not (directive.needs_exa and directive.exa_query):
            continue

        slug = _slugify(directive.headline)
        exa_results, status = search_related_status(directive.exa_query)
        exa_outcomes[slug] = status

        # Written unconditionally: an absent file cannot distinguish "we never
        # asked" from "we asked and got nothing", and that ambiguity is what the
        # funnel report exists to remove. Readers gate on `Result: hit`.
        out = (
            f"# Exa Results for: {directive.headline}\n"
            f"Result: {status}\n"
            f"Query: {directive.exa_query}\n\n"
        )
        for exa_r in exa_results:
            out += f"## [{exa_r.title}]({exa_r.url})\n{exa_r.text}\n\n"
        (exa_dir / f"{slug}.md").write_text(out, encoding="utf-8")
```

Note `search_related_status` never raises, so the `try/except` is gone; the error
is now data in the file rather than a print.

**Step 4: Run tests**

```bash
uv run pytest pipeline/test_things_happen_collector.py -v
```

Expected: all pass, including the pre-existing ones.

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "fix(rundown): write Exa output where the readers look

The collector wrote enrichment/exa/{i:02d}-{slug}.md while both Rundown
readers look up the bare {slug}.md, so Exa enrichment has plausibly
never reached a writer. The FP path was always correct. Also writes the
file unconditionally with a Result header, so a miss is observable
rather than inferred from an absent file."
```

### Task 1.3: Gate both readers on `Result: hit`

**Files:**
- Modify: `pipeline/__main__.py:100-103`
- Modify: `pipeline/show_notes.py:77-80`
- Test: `pipeline/test_things_happen_collector.py`

Task 1.2 now writes `Result: empty` and `Result: error:...` stubs. Without gating,
those stubs become writer input and show-note sources — a new way to feed the
writer garbage, created by the fix.

**Step 1: Write the failing tests**

```python
def test_exa_fallback_used_when_result_hit(tmp_path):
    from pipeline.__main__ import _find_rundown_article_text

    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    (exa_dir / "some-headline.md").write_text(
        "# Exa Results for: Some Headline\nResult: hit\nQuery: q\n\n## [T](u)\nbody\n",
        encoding="utf-8",
    )

    class FakeDirective:
        headline = "Some Headline"
        source = ""

    text = _find_rundown_article_text(FakeDirective(), tmp_path)
    assert "body" in text


def test_exa_fallback_skipped_when_result_empty(tmp_path):
    from pipeline.__main__ import _find_rundown_article_text

    exa_dir = tmp_path / "enrichment" / "exa"
    exa_dir.mkdir(parents=True)
    (exa_dir / "some-headline.md").write_text(
        "# Exa Results for: Some Headline\nResult: empty\nQuery: q\n\n",
        encoding="utf-8",
    )

    class FakeDirective:
        headline = "Some Headline"
        source = ""

    assert _find_rundown_article_text(FakeDirective(), tmp_path) == ""
```

**Step 2: Run to verify failure**

```bash
uv run pytest pipeline/test_things_happen_collector.py -k exa_fallback -v
```

Expected: the `empty` case FAILs — the stub is returned as article text.

**Step 3: Implement**

Add a shared helper. Put it in `pipeline/things_happen_collector.py` next to
`_slugify`, since both readers already import from there:

```python
def exa_text_if_hit(exa_file: Path) -> str:
    """Exa file contents, but only when the search actually returned results.

    Task 1.2 writes this file unconditionally so that misses are observable.
    That makes gating the readers mandatory: a `Result: empty` stub is not
    article text and must never reach the writer or the show notes.
    """
    if not exa_file.exists():
        return ""
    text = exa_file.read_text(encoding="utf-8")
    for line in text.split("\n")[:5]:
        if line.startswith("Result: "):
            return text if line[8:].strip() == "hit" else ""
    # No Result header: a file from before this change. Trust it.
    return text
```

In `pipeline/__main__.py`, replace lines 100-103:

```python
    # Exa enrichment
    from pipeline.things_happen_collector import exa_text_if_hit

    exa_text = exa_text_if_hit(work_dir / "enrichment" / "exa" / f"{slug}.md")
    if exa_text:
        return exa_text
```

In `pipeline/show_notes.py`, replace lines 77-80:

```python
    # Exa enrichment
    from pipeline.things_happen_collector import exa_text_if_hit

    exa_file = work_dir / "enrichment" / "exa" / f"{slug}.md"
    if exa_text_if_hit(exa_file):
        return exa_file
```

**Step 4: Run tests**

```bash
uv run pytest pipeline/ -q
```

Expected: all pass.

**Step 5: Commit**

```bash
git add pipeline/__main__.py pipeline/show_notes.py pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "fix(rundown): only treat Exa output as content when it has results

Writing the Exa file unconditionally means empty and error stubs now
exist on disk. Both readers gate on Result: hit so a stub cannot become
writer input or a show-note source."
```

### Task 1.4: Dedup before fetching

**Files:**
- Modify: `pipeline/things_happen_collector.py:80-94`
- Test: `pipeline/test_things_happen_collector.py`

The collector fetches every Levine link (`:83`), then discards the
already-covered ones (`:93-94`). The dedup key is `resolved_url`, computed at
`:82` — before the fetch. Each wasted fetch also costs a one-second politeness
delay.

**Step 1: Write the failing test**

```python
def test_prior_urls_are_not_fetched(tmp_path, monkeypatch):
    """Dedup must happen before the HTTP fetch, not after."""
    fetched_urls = []

    def _fake_fetch_all(links, delay_between=1.0):
        fetched_urls.extend(link["resolved_url"] for link in links)
        return []

    monkeypatch.setattr(
        things_happen_collector, "fetch_all_articles", _fake_fetch_all
    )
    # ... build a levine cache with two links, pass prior_urls={one of them} ...
    # (follow the existing cache-fixture helpers in this file)

    assert fetched_urls == ["https://example.com/fresh"]
```

**Step 2: Run to verify failure**

Expected: FAIL — both URLs appear in `fetched_urls`.

**Step 3: Implement**

Replace `things_happen_collector.py:78-94`:

```python
    _prior = prior_urls or set()

    if links_raw:
        for link in links_raw:
            link["resolved_url"] = resolve_redirect_url(link["raw_url"])
        # Dedup before fetching. The key is available pre-fetch, and every
        # skipped article we fetch anyway costs an HTTP round trip plus a
        # one-second politeness delay for a result we then throw away.
        levine_candidates = len(links_raw)
        links_raw = [
            link for link in links_raw if link.get("resolved_url") not in _prior
        ]
        levine_deduped = levine_candidates - len(links_raw)
        articles = fetch_all_articles(links_raw, delay_between=1.0)
    else:
        levine_candidates = 0
        levine_deduped = 0
        articles = []

    headlines_with_snippets = []
    # Maps the headline text sent to the editor → file path (relative to work_dir)
    headline_index: dict[str, str] = {}

    for i, art in enumerate(articles):
        slug = f"{i:02d}-{_slugify(art.headline)}"
```

The `if art.url and art.url in _prior: continue` guard at `:93-94` is now dead
and is removed by the above. `levine_candidates` and `levine_deduped` are
consumed in Task 2.2.

**Step 4: Run tests**

```bash
uv run pytest pipeline/test_things_happen_collector.py -q
```

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "perf(rundown): drop already-covered links before fetching them

Dedup ran after fetch_all_articles, so every article already used in a
prior episode still cost an HTTP round trip and a one-second politeness
delay before being discarded. The dedup key is resolved before the
fetch, so the filter simply moves up."
```

---

# Piece 2: The funnel

### Task 2.1: Classify and record fetch outcomes

**Files:**
- Modify: `pipeline/article_fetcher.py:18-101`
- Test: `pipeline/test_article_fetcher.py`

`_try_live_url` collapses non-200, short-extraction, and exceptions into a bare
`None`, and `fetch_all_articles` then drops `source_tier` entirely. That discard
is why the 93% stub rate went unnoticed for months — this is the test that would
have caught it.

`extracted_chars` is recorded alongside the tier because the `<200` paywall proxy
is weak: an FT or WSJ teaser plus a subscribe pitch can clear 200 characters
through the `<body>` fallback at `:50`. With the character counts on record the
threshold can be retuned against history instead of by re-fetching.

**Step 1: Write the failing tests**

```python
# pipeline/test_article_fetcher.py
from __future__ import annotations

import pytest

from pipeline import article_fetcher


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def _long_page(n=500):
    return "<html><body><article>" + ("word " * n) + "</article></body></html>"


def test_live_fetch_is_tier_live(monkeypatch):
    monkeypatch.setattr(
        article_fetcher.requests, "get", lambda *a, **kw: _Resp(200, _long_page())
    )
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "live"
    assert result.extracted_chars > 200
    assert "word" in result.content


def test_short_body_on_200_is_paywalled(monkeypatch):
    monkeypatch.setattr(
        article_fetcher.requests,
        "get",
        lambda *a, **kw: _Resp(200, "<html><body>Subscribe to read.</body></html>"),
    )
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "paywalled"
    assert result.content == "Headline"
    assert 0 < result.extracted_chars < 200


def test_non_200_is_http_error(monkeypatch):
    monkeypatch.setattr(
        article_fetcher.requests, "get", lambda *a, **kw: _Resp(404, "nope")
    )
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "http_error"
    assert result.extracted_chars == 0


def test_exception_is_fetch_error(monkeypatch):
    def _boom(*a, **kw):
        raise TimeoutError("boom")

    monkeypatch.setattr(article_fetcher.requests, "get", _boom)
    result = article_fetcher.fetch_article("https://x.test/a", "Headline")
    assert result.source_tier == "fetch_error"


def test_fetch_all_articles_preserves_tier(monkeypatch):
    """The discard of source_tier here is what hid a 93% stub rate."""
    monkeypatch.setattr(
        article_fetcher.requests, "get", lambda *a, **kw: _Resp(404, "nope")
    )
    articles = article_fetcher.fetch_all_articles(
        [{"resolved_url": "https://x.test/a", "headline_context": "Headline"}],
        delay_between=0,
    )
    assert articles[0].source_tier == "http_error"
    assert articles[0].extracted_chars == 0
```

**Step 2: Run to verify failure**

```bash
uv run pytest pipeline/test_article_fetcher.py -v
```

Expected: FAIL — `FetchedArticle` has no `extracted_chars`, and tiers are only
`live`/`headline_only`.

**Step 3: Implement**

In `pipeline/article_fetcher.py`, replace `SOURCE_LABELS` and the two dataclasses
(lines 18-41):

```python
SOURCE_LABELS = {
    "live": "Based on the publicly available portion of the article",
    "paywalled": "Based on the headline alone",
    "http_error": "Based on the headline alone",
    "fetch_error": "Based on the headline alone",
    # Retained: pre-existing artifacts and the legacy summarizer path use it.
    "headline_only": "Based on the headline alone",
}


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    content: str
    # "live" | "paywalled" | "http_error" | "fetch_error"
    source_tier: str
    # Characters extracted from the page body, before the headline fallback.
    # Recorded because the <200 paywall threshold is a weak proxy and needs to
    # stay retunable against history rather than by re-fetching.
    extracted_chars: int = 0

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_tier, self.source_tier)


@dataclass(frozen=True)
class Article:
    """An article with its headline, URL, and fetched content."""

    headline: str
    url: str
    content: str
    source_tier: str = "unknown"
    extracted_chars: int = 0
```

Replace `_try_live_url` and `fetch_article` (lines 56-81):

```python
_MIN_ARTICLE_CHARS = 200


def _try_live_url(url: str) -> tuple[str | None, str, int]:
    """Fetch the article, reporting the outcome rather than a bare None.

    Returns ``(text_or_None, tier, extracted_chars)``.
    """
    try:
        response = requests.get(
            url,
            headers=_BROWSER_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return None, "http_error", 0
        text = _extract_article_text(response.text)
        if len(text) < _MIN_ARTICLE_CHARS:
            return None, "paywalled", len(text)
        return text, "live", len(text)
    except Exception:
        return None, "fetch_error", 0


def fetch_article(url: str, headline: str) -> FetchedArticle:
    """Fetch article content with fallback: live URL -> headline only."""
    content, tier, chars = _try_live_url(url)
    if content:
        return FetchedArticle(
            url=url, content=content, source_tier=tier, extracted_chars=chars
        )

    return FetchedArticle(
        url=url, content=headline, source_tier=tier, extracted_chars=chars
    )
```

And in `fetch_all_articles` (lines 94-100), stop discarding:

```python
        results.append(
            Article(
                headline=link["headline_context"],
                url=fetched.url,
                content=fetched.content,
                source_tier=fetched.source_tier,
                extracted_chars=fetched.extracted_chars,
            )
        )
```

**Step 4: Run tests**

```bash
uv run pytest pipeline/test_article_fetcher.py pipeline/test_summarizer.py -v
```

Expected: all pass. `test_summarizer.py` is checked because `source_label` is
consumed at `summarizer.py:42`.

**Step 5: Commit**

```bash
git add pipeline/article_fetcher.py pipeline/test_article_fetcher.py
git commit -m "feat(fetcher): classify fetch outcomes and stop discarding them

_try_live_url collapsed non-200, short-extraction, and exceptions into
one None, and fetch_all_articles then dropped source_tier on the way to
Article, so nothing downstream could tell a real article from an echoed
headline. Measured consequence: 93% of Levine articles reach the writer
as a headline and nothing else, unnoticed for months.

Also records extracted_chars, because '200 chars' is a weak paywall
proxy and needs to stay retunable against history."
```

### Task 2.2: Emit collection-side counts

**Files:**
- Modify: `pipeline/things_happen_collector.py`
- Test: `pipeline/test_things_happen_collector.py`

Two facts cannot be recovered from the work dir later: candidates that were
skipped are never written to disk, and `prior_urls` comes from the database at
run time and changes daily. So the collector must emit them. It already writes a
sentinel; we extend it rather than inventing a new channel.

`tiers.json` is a **sidecar**, deliberately not a header inside the article
markdown: `_find_rundown_article_text` returns whole file contents straight into
the writer prompt, so an in-content `Source-Tier: paywalled` line would be read
by the model as article text.

**Step 1: Write the failing tests**

```python
def test_tiers_sidecar_records_tier_and_chars(tmp_path, monkeypatch):
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    tiers = json.loads((work_dir / "tiers.json").read_text(encoding="utf-8"))
    entry = next(iter(tiers.values()))
    assert entry["tier"] in {"live", "paywalled", "http_error", "fetch_error"}
    assert isinstance(entry["extracted_chars"], int)
    assert entry["url"]


def test_tiers_sidecar_is_keyed_by_relative_article_path(tmp_path, monkeypatch):
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    tiers = json.loads((work_dir / "tiers.json").read_text(encoding="utf-8"))
    for rel_path in tiers:
        assert (work_dir / rel_path).exists()


def test_sentinel_records_per_source_counts(tmp_path, monkeypatch):
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    sentinel = json.loads(
        (work_dir / "collection_done.json").read_text(encoding="utf-8")
    )
    assert "started_at" in sentinel
    for key in ("levine", "semafor", "zvi"):
        assert key in sentinel["candidates"]
        assert key in sentinel["deduped"]


def test_article_files_have_no_tier_header(tmp_path, monkeypatch):
    """Tier metadata must not leak into the writer prompt."""
    work_dir = _run_collector_basic(tmp_path, monkeypatch)
    for f in (work_dir / "articles").glob("*.md"):
        assert "Source-Tier" not in f.read_text(encoding="utf-8")
```

**Step 2: Run to verify failure**

Expected: FAIL — no `tiers.json`, sentinel lacks the new keys.

**Step 3: Implement**

At the top of `collect_all_artifacts`, capture the start time:

```python
    started_at = datetime.now(tz=_et).isoformat()
```

placed immediately after `_et = ZoneInfo("America/New_York")` (`:57`).

In the Levine article loop (`:91-108`), accumulate the sidecar:

```python
    # Fetch outcome per article, kept OUT of the article markdown: article file
    # contents are passed verbatim into the writer prompt, so a Source-Tier line
    # inside the file would be read by the model as part of the story.
    tiers: dict[str, dict] = {}

    for i, art in enumerate(articles):
        slug = f"{i:02d}-{_slugify(art.headline)}"
        art_path = articles_dir / f"{slug}.md"

        content = f"# {art.headline}\n\nURL: {art.url}\n\n{art.content}"
        art_path.write_text(content, encoding="utf-8")

        rel = str(art_path.relative_to(work_dir))
        tiers[rel] = {
            "tier": art.source_tier,
            "extracted_chars": art.extracted_chars,
            "url": art.url,
        }
```

(the snippet-building lines below it are unchanged).

Count the Semafor and Zvi sources. In the Semafor cache loop, increment a
`semafor_candidates` counter for every file inside the lookback window and a
`semafor_deduped` counter at the `if url and url in _prior: continue` at `:138`.
Do the same for Zvi around `:177`. Both counters are plain ints initialised to
zero before their loops.

Write the sidecar next to `headline_index.json` (`:209-212`):

```python
    (work_dir / "tiers.json").write_text(json.dumps(tiers, indent=2), encoding="utf-8")
```

And extend the sentinel (`:292-304`):

```python
    sentinel = {
        "job_id": job_id,
        "started_at": started_at,
        "completed_at": datetime.now(tz=_et).isoformat(),
        "lookback_days": lookback_days,
        "levine_articles": len(articles),
        "directives": len(plan.directives),
        "fp_routed": len(fp_directives),
        "enriched": len(non_fp_directives),
        # Candidates skipped are never written to disk and prior_urls comes from
        # the DB at run time, so these cannot be recovered from the work dir
        # afterwards. They must be emitted here or not at all.
        "candidates": {
            "levine": levine_candidates,
            "semafor": semafor_candidates,
            "zvi": zvi_candidates,
        },
        "deduped": {
            "levine": levine_deduped,
            "semafor": semafor_deduped,
            "zvi": zvi_deduped,
        },
        "exa_outcomes": exa_outcomes,
    }
```

**Step 4: Run tests**

```bash
uv run pytest pipeline/test_things_happen_collector.py -q
```

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "feat(rundown): emit collection counts and a fetch-tier sidecar

Skipped candidates are never written to disk and prior_urls comes from
the database at run time, so neither is recoverable from the work dir
later. The existing collection_done.json sentinel carries them instead.

Fetch tiers go in a tiers.json sidecar rather than an article header:
article files are passed verbatim into the writer prompt, so in-content
metadata would be read by the model as story text."
```

### Task 2.3: Record what actually reached the writer

**Files:**
- Modify: `pipeline/__main__.py:25-105`
- Modify: `pipeline/consumer.py:372-381`
- Test: `pipeline/test_things_happen_collector.py`

Writer assembly resolves a directive to text by fuzzy word-overlap
(`__main__.py:55-79`). A reporter that re-ran that match would be a second
implementation of it, drifting the moment either changes, and would miscount when
two directives resolve to the same file. So the consumer records the resolution
as it happens. This also converts the silent directive-drop at `consumer.py:377`
into a recorded event.

**Step 1: Write the failing test**

```python
def test_find_rundown_article_text_reports_source_path(tmp_path):
    from pipeline.__main__ import find_rundown_article_source

    index = {"Some Headline": "articles/00-some-headline.md"}
    (tmp_path / "articles").mkdir()
    (tmp_path / "articles" / "00-some-headline.md").write_text(
        "# Some Headline\n\nURL: u\n\nbody text here", encoding="utf-8"
    )
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    class FakeDirective:
        headline = "Some Headline"
        source = ""

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert "body text here" in text
    assert path == "articles/00-some-headline.md"


def test_find_rundown_article_source_reports_miss(tmp_path):
    from pipeline.__main__ import find_rundown_article_source

    class FakeDirective:
        headline = "Nothing Matches This"
        source = ""

    text, path = find_rundown_article_source(FakeDirective(), tmp_path)
    assert text == ""
    assert path is None
```

**Step 2: Run to verify failure**

Expected: FAIL, no `find_rundown_article_source`.

**Step 3: Implement**

Rename the body of `_find_rundown_article_text` to
`find_rundown_article_source(directive, work_dir) -> tuple[str, str | None]`,
returning the work-dir-relative path alongside the text at each of its four
return sites. Keep `_find_rundown_article_text` as a one-line wrapper so the
three existing call sites and their tests are undisturbed:

```python
def _find_rundown_article_text(directive: Any, work_dir: Path) -> str:
    text, _path = find_rundown_article_source(directive, work_dir)
    return text
```

In `pipeline/consumer.py`, replace lines 372-381:

```python
                        rundown_articles_by_theme: dict[str, list[str]] = {}
                        writer_inputs: list[dict] = []
                        for directive in plan.directives:
                            if not directive.include_in_episode:
                                continue
                            text, src = find_rundown_article_source(
                                directive, work_dir
                            )
                            writer_inputs.append(
                                {
                                    "headline": directive.headline,
                                    "theme": directive.theme,
                                    "source_path": src,
                                    "chars": len(text),
                                }
                            )
                            if text:
                                rundown_articles_by_theme.setdefault(
                                    directive.theme, []
                                ).append(text)
                        # A directive resolving to nothing used to vanish here
                        # with no counter and no log.
                        (work_dir / "writer_inputs.json").write_text(
                            json.dumps(writer_inputs, indent=2), encoding="utf-8"
                        )
```

Update the import at `consumer.py:317` to bring in
`find_rundown_article_source`, and add `import json` at module scope if it is not
already there (the file currently imports it locally at `:402`; hoist it).

**Step 4: Run tests**

```bash
uv run pytest pipeline/ -q
```

**Step 5: Commit**

```bash
git add pipeline/__main__.py pipeline/consumer.py pipeline/test_things_happen_collector.py
git commit -m "feat(rundown): record which file each directive resolved to

Directive-to-article resolution is a fuzzy word-overlap match; any
reporter that re-ran it would be a second implementation guaranteed to
drift. The consumer writes writer_inputs.json as the match happens,
which also turns the previously silent drop of an unresolvable
directive into a recorded event."
```

### Task 2.4: The alert sender

**Files:**
- Create: `pipeline/alerts.py`
- Create: `pipeline/test_alerts.py`

`POST /alert` on the pigeon daemon is the only session-free path to the Telegram
General topic: it calls `sendMessage` with `{chat_id, text}` and no
`message_thread_id`. Pigeon's swarm channel broadcast does **not** reach Telegram.

Note the endpoint sends no `parse_mode`, so the text is rendered literally —
markdown will appear as punctuation.

**Step 1: Write the failing tests**

```python
# pipeline/test_alerts.py
from __future__ import annotations

import requests

from pipeline import alerts


class _Resp:
    def __init__(self, status_code=204):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300


def test_posts_to_alert_endpoint(monkeypatch):
    captured = {}

    def _post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Resp(204)

    monkeypatch.setattr(alerts.requests, "post", _post)
    monkeypatch.delenv("PIGEON_DAEMON_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PIGEON_DAEMON_AUTH_TOKEN_FILE", "/nonexistent")

    assert alerts.send_alert("hello", severity="info") is True
    assert captured["url"].endswith("/alert")
    assert captured["json"] == {"text": "hello", "severity": "info"}
    assert "Authorization" not in captured["headers"]


def test_includes_bearer_when_token_configured(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        alerts.requests, "post", lambda url, **kw: (captured.update(kw), _Resp())[1]
    )
    monkeypatch.setenv("PIGEON_DAEMON_AUTH_TOKEN", "sekrit")
    alerts.send_alert("hello")
    assert captured["headers"]["Authorization"] == "Bearer sekrit"


def test_returns_false_on_timeout(monkeypatch):
    def _boom(*a, **kw):
        raise requests.Timeout("slow")

    monkeypatch.setattr(alerts.requests, "post", _boom)
    assert alerts.send_alert("hello") is False


def test_returns_false_on_non_2xx(monkeypatch):
    monkeypatch.setattr(alerts.requests, "post", lambda *a, **kw: _Resp(503))
    assert alerts.send_alert("hello") is False
```

**Step 2: Run to verify failure**

```bash
uv run pytest pipeline/test_alerts.py -v
```

Expected: FAIL, no module `pipeline.alerts`.

**Step 3: Implement**

First move the token helper out of `opencode_client.py`. Cut
`_daemon_auth_headers` (lines 61-79, docstring included) into a new
`pipeline/pigeon.py`, rename it `daemon_auth_headers`, move `PIGEON_DAEMON_URL`
(`opencode_client.py:15`) with it, and have `opencode_client.py` import both.
That keeps one implementation of the auth resolution order.

Then:

```python
# pipeline/alerts.py
from __future__ import annotations

import requests

from pipeline.pigeon import PIGEON_DAEMON_URL, daemon_auth_headers


def send_alert(text: str, severity: str = "info") -> bool:
    """Post an operational message to the Telegram General topic via pigeon.

    ``POST /alert`` is the only session-free path that reaches General: it calls
    sendMessage with ``{chat_id, text}`` and no ``message_thread_id``. Pigeon's
    swarm channel broadcast deliberately skips Telegram, so it is not an option.
    Note there is no ``parse_mode``, so ``text`` renders literally.

    Never raises. A reporting failure must never be able to disturb the job that
    produced the report, so every error path returns False and prints; the
    rendered text is logged so journald retains it when pigeon is down.
    """
    if not text.strip():
        return False

    try:
        response = requests.post(
            f"{PIGEON_DAEMON_URL.rstrip('/')}/alert",
            json={"text": text, "severity": severity},
            headers=daemon_auth_headers(),
            timeout=10,
        )
    except Exception as exc:
        print(f"[alerts] send failed ({type(exc).__name__}: {exc}); text was:\n{text}")
        return False

    if not (200 <= response.status_code < 300):
        print(
            f"[alerts] send rejected (HTTP {response.status_code}); text was:\n{text}"
        )
        return False

    return True
```

**Step 4: Run tests**

```bash
uv run pytest pipeline/test_alerts.py pipeline/test_opencode_client.py -v
```

**Step 5: Commit**

```bash
git add pipeline/alerts.py pipeline/pigeon.py pipeline/opencode_client.py pipeline/test_alerts.py
git commit -m "feat(alerts): post operational messages to the Telegram General topic

POST /alert on the pigeon daemon is the only session-free path that
lands in General; the swarm channel broadcast deliberately skips
Telegram. Never raises, so a reporting failure cannot disturb the job
that produced the report, and logs the rendered text so journald keeps
it when pigeon is down.

Extracts the daemon auth-header resolution from opencode_client into
pipeline/pigeon.py so there is one implementation."
```

### Task 2.5: The funnel reporter

**Files:**
- Create: `pipeline/run_stats.py`
- Create: `pipeline/test_run_stats.py`

`collect_run_stats` is a pure function of a directory. It must degrade rather
than raise on every shape that exists on disk today — including
`/tmp/the-rundown-74a2d34d`, whose `collection_done.json` is `{}` and whose plan
has `directives: []`.

**Step 1: Write the failing tests**

Cover, at minimum:

- A fully-populated fixture work dir: every funnel number asserted exactly.
- `collection_done.json` equal to `{}` — no crash, counts fall back to what the
  artifacts show.
- Missing `plan.json` — partial stats returned, not an exception.
- Missing `tiers.json` (a pre-Task-2.2 work dir) — tiers land in an `unknown`
  bucket.
- `render_report` output contains the `IN`/`FETCH`/`EXA`/`WRITE`/`OUT` labels and
  the `paywalled:` domain histogram, and is under 4000 characters for a
  pathologically large input.

**Step 2: Run to verify failure**

```bash
uv run pytest pipeline/test_run_stats.py -v
```

**Step 3: Implement**

A pydantic `RunStats` model plus:

- `collect_run_stats(work_dir, job_id, date_str, reused_collection=False) -> RunStats`,
  reading `collection_done.json`, `tiers.json`, `plan.json`, `writer_inputs.json`,
  `articles/semafor/`, `articles/zvi/`, `script.txt`, `covered.json`. Every read
  wrapped so a missing or malformed file yields a default, never an exception.
- Domain histogram built from `tiers.json` entries whose tier is `paywalled`,
  bucketed by registrable host with `www.` stripped, top 8.
- `render_report(stats) -> str` producing the plain-text block from the design
  doc. Report the `lookback_days` from the sentinel rather than recomputing it —
  on a retry with reused collection the sentinel is the truth.
- `append_jsonl(stats, path)` appending one line to
  `/persist/my-podcasts/run-stats.jsonl`. This is the durable record: `/tmp` is
  reaped at 10 days, so the work-dir copy cannot answer "is the Exa fix working?"
  a month from now.

**Severity is always `info`.** Do not add thresholds. Measured across the last 10
successful runs, `include_in_episode` is 4-5 every time, so an obvious-looking
"warn under 5 directives" rule would fire on normal days. Thresholds get set from
`run-stats.jsonl` after two weeks, not guessed now.

**Step 4: Run tests**

```bash
uv run pytest pipeline/test_run_stats.py -v
```

**Step 5: Commit**

```bash
git add pipeline/run_stats.py pipeline/test_run_stats.py
git commit -m "feat(rundown): funnel reporter over work-dir artifacts

Reconstructs the content-acquisition funnel from the work dir plus the
two collector-emitted sentinels, renders it as plain text for the
Telegram General topic, and appends a durable line to
/persist/my-podcasts/run-stats.jsonl -- /tmp is reaped at 10 days, so
the work-dir copy cannot answer trend questions later.

Severity is always info: include_in_episode measured 4-5 on every one
of the last 10 successful runs, so a threshold guessed now would fire
on normal days."
```

### Task 2.6: Wire the reporter into the consumer

**Files:**
- Modify: `pipeline/consumer.py`
- Test: `pipeline/test_consumer_run_stats.py`

**Step 1: Write the failing test**

Test the helper, not the consumer loop:

```python
def test_report_run_stats_swallows_a_broken_work_dir(tmp_path, capsys):
    from pipeline.consumer import _report_run_stats

    # No artifacts at all, and a work dir that is actually a file.
    broken = tmp_path / "not-a-dir"
    broken.write_text("x")
    _report_run_stats(broken, job_id="j", date_str="2026-08-15")  # must not raise
    assert "run stats" in capsys.readouterr().out.lower()


def test_report_run_stats_is_idempotent(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "pipeline.consumer.send_alert", lambda text, severity="info": sent.append(text) or True
    )
    _make_minimal_work_dir(tmp_path)
    _report_run_stats(tmp_path, job_id="j", date_str="2026-08-15")
    _report_run_stats(tmp_path, job_id="j", date_str="2026-08-15")
    assert len(sent) == 1
    assert (tmp_path / "run_stats_sent").exists()
```

**Step 2: Run to verify failure**

**Step 3: Implement**

```python
def _report_run_stats(
    work_dir: Path, job_id: str, date_str: str, reused_collection: bool = False
) -> None:
    """Emit the content-acquisition funnel for a finished script stage.

    Deliberately total: this runs after script.txt, summary.txt and covered.json
    are already on disk, and swallows everything, so a reporting bug can never
    fail a job or burn retry budget.

    The run_stats_sent marker keeps a retry (which reuses collection but reruns
    the writer) from sending a second message. A /tmp marker is safe here
    because the retry budget is ~12 hours and /tmp is reaped at 10 days.
    """
    try:
        stats = collect_run_stats(
            work_dir, job_id=job_id, date_str=date_str,
            reused_collection=reused_collection,
        )
        (work_dir / "run_stats.json").write_text(
            stats.model_dump_json(indent=2), encoding="utf-8"
        )
        append_jsonl(stats, Path("/persist/my-podcasts/run-stats.jsonl"))
        marker = work_dir / "run_stats_sent"
        if not marker.exists():
            if send_alert(render_report(stats), severity="info"):
                marker.touch()
    except Exception as exc:
        print(f"[consumer] run stats reporting failed: {exc}")
```

Call it immediately after the `covered.json` write (`consumer.py:401-408`),
inside the existing `try`:

```python
                        _report_run_stats(
                            work_dir,
                            job_id=job["id"],
                            date_str=job["date_str"],
                            reused_collection=job.get("failure_count", 0) > 0,
                        )
```

The report is labeled **script stage** in `render_report` because TTS and publish
happen on a later loop iteration (`consumer.py:275`, `:409`) — the job can still
fail after this message is sent, and the message must not imply an episode
shipped.

**Step 4: Run tests**

```bash
uv run pytest pipeline/ -q
```

**Step 5: Commit**

```bash
git add pipeline/consumer.py pipeline/test_consumer_run_stats.py
git commit -m "feat(rundown): report the acquisition funnel after the script stage

Runs after the artifacts are safely on disk and swallows everything, so
a reporting bug cannot fail a job or burn retry budget. A marker file
keeps a retry -- which reuses collection but reruns the writer -- from
sending a second message. Labeled script stage because TTS and publish
happen on a later loop iteration."
```

### Task 2.7: The `run-stats` CLI

**Files:**
- Modify: `pipeline/__main__.py`

**Step 1: Implement**

Following the `sync-sources` command style at `__main__.py:1019`:

```python
@cli.command("run-stats")
@click.option("--work-dir", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--send", is_flag=True, help="Also post to the Telegram General topic.")
def run_stats_command(work_dir: Path, send: bool) -> None:
    """Render the content-acquisition funnel for an existing work dir."""
    from pipeline.alerts import send_alert
    from pipeline.run_stats import collect_run_stats, render_report

    job_id = work_dir.name.replace("the-rundown-", "")
    stats = collect_run_stats(work_dir, job_id=job_id, date_str="")
    report = render_report(stats)
    click.echo(report)
    if send:
        # Deliberately ignores run_stats_sent: a manual send is a manual send.
        click.echo("sent" if send_alert(report) else "send failed")
```

**Step 2: Verify against real data**

```bash
for d in $(ls -d /tmp/the-rundown-* | head -20); do
  uv run python -m pipeline run-stats --work-dir "$d" || echo "FAILED: $d"
done
```

Expected: every dir renders without traceback. These dirs predate the
instrumentation, so this exercises the degradation paths (`unknown` tiers,
missing sentinels) against real data and gives a rough historical baseline for
free.

**Step 3: Commit**

```bash
git add pipeline/__main__.py
git commit -m "feat(cli): add run-stats for rendering a work dir's funnel"
```

### Task 2.8: Document and ship

**Files:**
- Modify: `AGENTS.md`
- Modify: `.opencode/skills/operating-things-happen-digest/SKILL.md`

**Step 1: Update the docs**

In `AGENTS.md`, add `pipeline/run_stats.py` and `pipeline/alerts.py` to the
Rundown key-modules list, add the `run-stats` CLI to the command list, and note
`/persist/my-podcasts/run-stats.jsonl` alongside the other persistent paths.

In the Rundown skill, add a short section: where the funnel numbers come from,
what a healthy run looks like, and that a missing Telegram report means the
reporter or pigeon failed but the episode is unaffected.

**Step 2: Full verification**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected: clean, and passed count above the 405 baseline.

**Step 3: Deploy and watch a real run**

```bash
sudo systemctl restart my-podcasts-consumer
sudo systemctl status my-podcasts-consumer --no-pager
```

Then confirm the next weekday 04:30 ET run posts to Telegram, and:

```bash
tail -1 /persist/my-podcasts/run-stats.jsonl
```

**Step 4: Commit and push**

```bash
git add -A
git commit -m "docs: record the Rundown funnel reporter"
git pull --rebase
git push
git status
```

Expected: `up to date with origin`.

---

## Deferred, deliberately

Do not build these as part of this plan:

- **Exa append semantics.** Making Exa augment article text instead of standing
  in for a missing file is the full fix for defect 2, but it changes writer input
  and would pollute the before/after read on the filename repair. Separate
  commit, after Piece 2 has produced a baseline.
- **Alert thresholds.** After two weeks of `run-stats.jsonl`.
- **Open-access substitution** — the feature this investigation was for. Its
  inputs are the 93% stub rate and the `paywalled:` domain histogram.
- **The 111 mypy errors.** Tracked separately now that CI is amber.
