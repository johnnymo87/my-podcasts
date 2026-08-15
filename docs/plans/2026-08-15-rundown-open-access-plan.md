# Rundown Open-Access Substitution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make The Rundown's writer compose from real open-access article text instead of headline-only stubs, by firing Exa on the *measured* fetch tier rather than an LLM guess, steering away from the paywalled origin, and actually delivering the retrieved text into the writer prompt.

**Honest scope — read this before you build.** An earlier draft of this plan claimed the feature upgrades "most stories" and would cost 5-9 Exa calls per run. **Both were wrong.** Measured across 8 real plans: of ~4.75 stories selected per episode, only **1.2 on average (max 3, and 0 on some days)** are Levine stubs. The rest are Semafor/Zvi, which come from cache with real body text and need nothing. The widely-quoted "93% stub rate" is per *Levine file*, and most Levine files are never selected — it does not describe an episode. Stub stories are spread roughly evenly across priorities (2 of 8 priority-1 slots), so they are not disproportionately the lead.

So this feature upgrades **about a quarter of each episode's stories**, typically 1-3 of them. That is worth building — those stories currently reach the writer as a bare headline — but calibrate expectations, and see "What to watch" at the end: **`EXA 2 flagged` on Monday is success, not failure.**

**Architecture:** Three gaps, all required for any observable effect. (1) **Trigger** — Exa currently fires only when the editor sets `needs_exa`, measured at 2/54 directives (4%) while 93% of articles are stubs; replace with the deterministic `source_tier` signal that already exists. (2) **Retrieval policy** — Exa already finds the story (12/12 in a spike) but ranks the paywalled origin first 8/12 times; pass `exclude_domains`. (3) **Delivery** — a stub always wins the word-overlap match before Exa is consulted, so retrieved text never reaches the writer; append it at writer-input assembly. Plus instrumentation, without which the funnel cannot see any of it.

**Tech Stack:** Python 3.14, `uv`, pytest, pydantic, `exa_py`, ruff.

---

## Context you need before starting

**Read `AGENTS.md` and `pipeline/AGENTS.md` first.** Non-obvious constraints that will bite you:

- The consumer runs the **live working tree** as a long-lived systemd loop with `Restart=on-failure`. Every commit must be independently safe; an import error is a crash-loop. Merging does not deploy — a restart does.
- **Article markdown is fed VERBATIM into the writer prompt.** Never put metadata inside it. Sidecars only (`tiers.json` is the pattern).
- Tests must be **hermetic**: `tmp_path` only, no network, no real `/persist`, no real `/tmp` work dirs. `MY_PODCASTS_WORK_DIR_BASE` exists for this.
- Durable data goes to `/persist/my-podcasts/`, never `/tmp` (reaped at 10 days).
- Verify every task with `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`. Lint is blocking. mypy is non-blocking (111 known errors).
- **Never** run `git stash`/`reset`/`checkout --`/`clean` — shared worktree.

**Measurements this plan rests on** (recorded in `bd show my-podcasts-85c` / `my-podcasts-kyk`):

| Fact | Value | Source |
|---|---|---|
| Articles reaching writer as stubs | 93% | 106 real article files |
| Directives with `needs_exa=true` | 2/54 (4%) | 8 real `plan.json` |
| Exa returns ≥1 open-access result | 12/12 | spike, raw headline query |
| Exa result at ≥2500 chars | 10/12 | same spike |
| Paywalled origin ranked **first** | 8/12 | same spike |
| **Levine stubs among *selected* stories** | **1.2/run** (max 3, min 0) | 8 real plans, 38 selected |
| Selected stories per episode | ~4.75 | same |
| Directives where editor's headline echo is **not** byte-identical | 3/38 (8%) | same |

The spike used the **raw headline** as query. Production uses `directive.exa_query` (editor keywords) when present. The headline path is the one validated by measurement.

**The 8% echo mismatch is the single most dangerous fact in this table.** All three cases are a **double space** in the source headline (`US Set to  Pay Most...`, `...Writing to  Theater Chains`, `...Money at  $500 Million Valuation`) which Gemini normalizes to a single space when it echoes the headline back. Levine headlines come from sentence extraction (`things_happen_extractor.py:48-51`) and are used raw as both the article headline and the `headline_index` key; Semafor headlines *are* whitespace-normalized (`things_happen_collector.py:144`) but Levine's are not. The resolver's word-overlap fallback papers over this everywhere today — which is exactly why a new **exact** match would fail silently while every unit test passes. Match on slug, never on raw headline equality.

---

## Task 1: Plumb `exclude_domains` through the Exa client

**Why:** 8/12 spike searches ranked the paywalled origin first, returning a 425–1000 char preview. `exa_py`'s `search` supports `exclude_domains`; `search_related_status` only plumbs `include_domains`.

**Files:**
- Modify: `pipeline/exa_client.py`
- Test: `pipeline/test_exa_client.py`

**Step 1: Write the failing test**

```python
def test_search_related_status_passes_exclude_domains(monkeypatch):
    captured = {}

    class _FakeExa:
        def __init__(self, key): pass
        def search(self, query, **kwargs):
            captured.update(kwargs)
            class _R: results = []
            return _R()

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr("pipeline.exa_client.Exa", _FakeExa)

    from pipeline.exa_client import search_related_status

    search_related_status("headline", exclude_domains=["bloomberg.com"])
    assert captured["exclude_domains"] == ["bloomberg.com"]
```

**Step 2: Run it, expect FAIL**

`uv run pytest pipeline/test_exa_client.py -k exclude_domains -v` → `TypeError: unexpected keyword argument 'exclude_domains'`.

**Step 3: Implement**

In `search_related_status`, add the keyword-only parameter and forward it:

```python
def search_related_status(
    headline: str,
    *,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    num_results: int = 3,
) -> tuple[list[ExaResult], str]:
```

and in the `exa.search(...)` call add `exclude_domains=exclude_domains,`.

Do **not** add it to `search_related` — nothing needs it there, and the FP path depends on that wrapper's current shape (YAGNI).

**Step 4: Run, expect PASS.** Then full suite: `uv run pytest -q`.

**Step 5: Commit**

```bash
git add pipeline/exa_client.py pipeline/test_exa_client.py
git commit -m "feat(exa): plumb exclude_domains through search_related_status"
```

---

## Task 1b: Bound the Exa call with a timeout (do this before Task 4)

**Why:** `exa_py` issues `requests.get`/`requests.post` with **no `timeout=`** (verified in the installed package, `exa_py/api.py:1417-1439`). `search_related_status` catches exceptions, but a hung TCP connection is not an exception — it blocks forever. The consumer is a *single* loop serving The Rundown, FP Digest, TTS and blog polling, so one wedged call stalls the whole pipeline, and `Restart=on-failure` never fires because the process is alive. Task 4 multiplies this exposure roughly 6x, so it must be bounded first.

**Files:** Modify `pipeline/exa_client.py`; test in `pipeline/test_exa_client.py`.

**Implementation:** run the call in a worker thread and bound it, mapping expiry onto the existing status vocabulary:

```python
_EXA_TIMEOUT_SECONDS = 30


def _search_with_timeout(exa, headline, **kwargs):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: exa.search(headline, **kwargs)).result(
            timeout=_EXA_TIMEOUT_SECONDS
        )
```

Wrap the existing call, and let `TimeoutError` fall into the existing `except Exception` so the status becomes `error:TimeoutError`. Note the worker thread is left running on timeout — that is acceptable here (it is one idle socket, and the process is a long-lived loop), but do not "fix" it by joining, which would reintroduce the hang.

**Test:** monkeypatch `Exa` with a fake whose `search` sleeps past the timeout (patch `_EXA_TIMEOUT_SECONDS` to something small like 0.1 so the suite stays fast), and assert the status is `error:TimeoutError` and no exception escapes.

**Commit:** `fix(exa): bound search calls with a timeout so a hang cannot wedge the consumer`

---

## Task 2: Return only the result sections, not the file headers

**Why:** `exa_text_if_hit` returns the **whole file**, including the `# Exa Results for: ...`, `Result: hit` and `Query: ...` lines written at `things_happen_collector.py:328-332`. Those go verbatim into the writer prompt today, violating the no-metadata-in-article-text rule. Both the resolver fallback and the new append path need a clean version.

**Do not change `exa_text_if_hit`.** `show_notes.py` and the FP path share it, including the permanent headerless-file branch. Add a sibling.

**Files:**
- Modify: `pipeline/exa_client.py`
- Test: `pipeline/test_exa_client.py`

**Step 1: Write the failing tests**

```python
def test_exa_result_sections_strips_headers(tmp_path):
    from pipeline.exa_client import exa_file_path, exa_result_sections
    p = exa_file_path(tmp_path, "slug")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# Exa Results for: Some Headline\n"
        "Result: hit\n"
        "Query: some keywords\n\n"
        "## [Title A](https://a.example/x)\nBody A\n\n",
        encoding="utf-8",
    )
    out = exa_result_sections(tmp_path, "slug")
    assert out.startswith("## [Title A]")
    assert "Result:" not in out
    assert "Query:" not in out
    assert "Exa Results for" not in out
    assert "Body A" in out


def test_exa_result_sections_empty_when_not_hit(tmp_path):
    from pipeline.exa_client import exa_file_path, exa_result_sections
    p = exa_file_path(tmp_path, "slug")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Exa Results for: H\nResult: empty\nQuery: q\n\n", encoding="utf-8")
    assert exa_result_sections(tmp_path, "slug") == ""


def test_exa_result_sections_missing_file(tmp_path):
    from pipeline.exa_client import exa_result_sections
    assert exa_result_sections(tmp_path, "nope") == ""


def test_exa_result_sections_limit(tmp_path):
    from pipeline.exa_client import exa_file_path, exa_result_sections
    p = exa_file_path(tmp_path, "slug")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# Exa Results for: H\nResult: hit\nQuery: q\n\n"
        "## [A](https://a.example)\nAAA\n\n"
        "## [B](https://b.example)\nBBB\n\n"
        "## [C](https://c.example)\nCCC\n\n",
        encoding="utf-8",
    )
    out = exa_result_sections(tmp_path, "slug", limit=2)
    assert "AAA" in out and "BBB" in out
    assert "CCC" not in out
```

**Step 2: Run, expect FAIL** (`ImportError`).

**Step 3: Implement**

```python
def exa_result_sections(work_dir: Path, slug: str, *, limit: int = 2) -> str:
    """The `## [title](url)` sections of a slug's Exa file, headers stripped.

    `exa_text_if_hit` returns the raw file, which carries the `Result:` and
    `Query:` bookkeeping headers. Those must never reach the writer prompt,
    which consumes article text verbatim. This is the writer-facing view.

    Returns "" when the file is absent, when the search was not a hit, or
    when the file carries no result sections. `limit` caps how many results
    are returned; syndicated copies of the same wire story are common, so
    the tail is usually redundant.
    """
    text = exa_text_if_hit(work_dir, slug)
    if not text:
        return ""
    # Anchor on the "## [title](url)" shape the collector writes, NOT on a
    # bare "## ": Exa result bodies are scraped article text and can contain
    # their own markdown headings, which would split a section mid-body and
    # silently drop the next result.
    parts = re.split(r"\n(?=## \[)", text)
    sections = [p.strip() for p in parts if p.lstrip().startswith("## [")]
    if not sections:
        return ""
    return "\n\n".join(sections[:limit])
```

Add `import re` at module top if absent. **Add a test** with an Exa body that itself contains a `## Subheading` line, asserting both results survive and the subheading stays inside its own section — that is the failure this regex prevents.

**Step 4: Run, expect PASS.** Full suite.

**Step 5: Commit**

```bash
git add pipeline/exa_client.py pipeline/test_exa_client.py
git commit -m "feat(exa): add header-stripped exa_result_sections for writer input"
```

---

## Task 3: Use the header-stripped view in the resolver fallback

**Why:** `find_rundown_article_source`'s 7th return site currently returns `exa_text_if_hit(...)` — headers and all — straight into the writer prompt. Fix the leak on the existing path before building on it.

**Files:**
- Modify: `pipeline/__main__.py` (the Exa fallback in `find_rundown_article_source`)
- Test: `pipeline/test_things_happen_collector.py` (append). **Verified 2026-08-15:** there is no `test_main_article_source.py`; despite the name, the 24 existing tests for `find_rundown_article_source` live in `test_things_happen_collector.py`. Do not create a new file for these.

**Step 1: Write the failing test**

```python
def test_exa_fallback_has_no_bookkeeping_headers(tmp_path):
    from pipeline.__main__ import find_rundown_article_source
    from pipeline.exa_client import exa_file_path
    from pipeline.things_happen_collector import _slugify

    class D:
        headline = "Some Very Distinctive Headline About Widgets"
        source = ""

    slug = _slugify(D.headline)
    p = exa_file_path(tmp_path, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# Exa Results for: X\nResult: hit\nQuery: widgets\n\n"
        "## [Widget News](https://w.example)\nReal body text.\n\n",
        encoding="utf-8",
    )
    text, src = find_rundown_article_source(D(), tmp_path)
    assert "Real body text." in text
    assert "Result:" not in text
    assert "Query:" not in text
    assert src == f"enrichment/exa/{slug}.md"
```

**Step 2: Run, expect FAIL** (`Result:` present).

**Step 3: Implement** — in the Exa fallback block, swap the call:

```python
    # Exa enrichment
    exa_text = exa_result_sections(work_dir, slug)
    if exa_text:
        exa_path = exa_file_path(work_dir, slug)
        return exa_text, str(exa_path.relative_to(work_dir))
```

and update the local import to `from pipeline.exa_client import exa_file_path, exa_result_sections`. **Leave `exa_text_if_hit` imported only if still used**; if it is now unused in this function, drop it from the import or ruff will flag it.

**Step 4: Run, expect PASS.** Full suite + `uv run ruff check .`.

**Step 5: Commit**

```bash
git add pipeline/__main__.py pipeline/test_things_happen_collector.py
git commit -m "fix(rundown): stop leaking Exa bookkeeping headers into writer prompt"
```

---

## Task 4: Trigger Exa on the measured tier, not the editor's guess

**Why:** the dominant bottleneck. `needs_exa` is set on 2/54 directives (4%) because `things_happen_editor.py:19-20` asks the model to judge "paywalled or inaccessible" from **headlines and snippets only** — it never sees the fetch outcome. The deterministic answer already exists as `Article.source_tier`.

**Design decisions, do not deviate without measuring:**
- **Match the article to the directive by SLUG, never by raw headline equality.** Verified: exact match loses 3 of 38 selected directives to a double-space mismatch. `_slugify` (`things_happen_collector.py:21-27`) collapses whitespace, punctuation and case, and slug equality is already the join used by `show_notes._find_article_file` and the resolver's legacy path — so this is the codebase's existing invariant, not a new one.
- Fire when the matched article's `source_tier != "live"` **OR** the editor set `needs_exa` (union — the editor flag is still real signal, just under-fired).
- **When no article matches (Semafor/Zvi), still fire if `directive.needs_exa` is set.** The current code fires for *any* non-FP directive with the flag; dropping that would be a silent regression of behavior this plan claims to preserve. Without a matched article there is no origin domain to exclude, so pass only the bypass deny-list.
- Only for directives with `include_in_episode` — the FP path already does this and the others are never written.
- Query: `directive.exa_query` when non-empty, else `directive.headline`. The headline path is the one the 12/12 spike validated.
- Exclude the origin domain plus a bypass deny-list. `archive.ph` is a paywall-circumvention mirror; feeding it into published work is not acceptable.
- Directives with **no matching Levine article** (Semafor/Zvi, which come from cache with real text) do not fire.

**Files:**
- Modify: `pipeline/things_happen_collector.py` (Phase 3, ~line 312-335)
- Test: `pipeline/test_things_happen_collector.py`

**Step 1: Write the failing tests**

```python
def test_exa_fires_for_stubbed_article_without_editor_flag(monkeypatch, tmp_path):
    """The editor flags 4% of directives; the tier signal must drive this."""
    # Build a plan with needs_exa=False and an article whose tier is "paywalled".
    # Assert search_related_status was called once, with the headline as query.
    ...

def test_exa_skips_live_article(monkeypatch, tmp_path):
    """A successfully fetched article does not need substitution."""
    ...

def test_exa_excludes_origin_domain_and_bypass_mirrors(monkeypatch, tmp_path):
    """8/12 spike searches ranked the paywalled origin first."""
    # Assert exclude_domains contains the article's host and "archive.ph".
    ...

def test_exa_prefers_editor_query_when_present(monkeypatch, tmp_path):
    ...
```

Follow the existing fixtures in that file for building a plan + articles; do not invent a new harness.

**Step 2: Run, expect FAIL.**

**Step 3: Implement**

Add near the top of the module:

```python
# Paywall-circumvention mirrors and front-ends. Never cite these: the show
# republishes what it reads, and laundering a paywalled article through an
# archive mirror is materially different from citing an outlet that published
# openly. archive.today rotates TLDs, so all known ones are listed.
BYPASS_DOMAINS = (
    "archive.ph",
    "archive.is",
    "archive.today",
    "archive.md",
    "archive.li",
    "archive.vn",
    "archive.fo",
    "12ft.io",
    "1ft.io",
    "freedium.cfd",
    "removepaywall.com",
)


def _host_banned(url: str, origin: str) -> bool:
    """True if a result URL is a bypass mirror or the paywalled origin itself.

    Suffix matching, so a subdomain (news.archive.ph) cannot slip through.
    """
    host = (urlparse(url).hostname or "").removeprefix("www.").lower()
    if not host:
        return True
    if origin and (host == origin or host.endswith("." + origin)):
        return True
    return any(host == b or host.endswith("." + b) for b in BYPASS_DOMAINS)
```

Add unit tests for `_host_banned` covering: exact bypass host, subdomain of a bypass host, the origin domain, a subdomain of the origin, an empty/garbage URL, and a legitimate host that must pass.

Replace the Phase 3 gate:

```python
    exa_outcomes: dict[str, str] = {}
    for directive in non_fp_directives:
        if not directive.include_in_episode:
            continue

        # Slug, not raw equality: Levine headlines come from sentence
        # extraction and can carry a double space that Gemini normalizes away
        # when it echoes the headline. Measured: exact match loses 3 of 38
        # selected directives, silently, with every unit test still passing.
        d_slug = _slugify(directive.headline)
        art = next((a for a in articles if _slugify(a.headline) == d_slug), None)

        # No matching Levine article means a Semafor/Zvi story, read from
        # cache with real body text. It needs no substitution UNLESS the
        # editor explicitly asked for one -- dropping that case would silently
        # regress today's behavior.
        if art is None and not directive.needs_exa:
            continue

        # The editor judges "paywalled" from headlines alone and sets the flag
        # on ~4% of directives while ~93% of articles arrive as stubs. The
        # fetch tier is the measured answer; the flag stays in the union
        # because it is real signal, just badly under-fired.
        if art is not None and art.source_tier == "live" and not directive.needs_exa:
            continue

        query = directive.exa_query or directive.headline
        origin = (
            (urlparse(art.url).hostname or "").removeprefix("www.")
            if art is not None
            else ""
        )
        exclude = [d for d in (origin, *BYPASS_DOMAINS) if d]

        slug = d_slug
        exa_results, status = search_related_status(query, exclude_domains=exclude)
        # Defence in depth: exclude_domains is a request parameter honored by a
        # third-party API. Ethics policy must not depend on Exa's compliance,
        # so drop banned hosts from the results locally too.
        exa_results = [r for r in exa_results if not _host_banned(r.url, origin)]
        if not exa_results and status == "hit":
            status = "empty"
        exa_outcomes[slug] = status

        out = (
            f"# Exa Results for: {directive.headline}\n"
            f"Result: {status}\n"
            f"Query: {query}\n\n"
        )
        for exa_r in exa_results:
            out += f"## [{exa_r.title}]({exa_r.url})\n{exa_r.text}\n\n"
        exa_file_path(work_dir, slug).write_text(out, encoding="utf-8")
```

Add `from urllib.parse import urlparse` at module top if absent.

**Note the cost change:** Exa calls per run go from ~0.25 to roughly the number of stubbed selected stories — **measured at 1.2/run, max 3**, plus any Semafor/Zvi directive the editor flags (rare). This is a small change in absolute terms; do not expect a large number in the funnel.

**Step 4: Run, expect PASS.** Full suite.

**Step 5: Commit**

```bash
git add pipeline/things_happen_collector.py pipeline/test_things_happen_collector.py
git commit -m "feat(rundown): trigger Exa on measured fetch tier instead of editor guess"
```

---

## Task 5: Deliver the text — append to the stub at writer-input assembly

**Why:** the resolver returns the stub (a single shared 4+ char word wins the word-overlap match), so Exa text never reaches the writer when any article file exists.

**Append, do not replace.** The failure asymmetry decides it: with a fully-automated writer and no human review, replacing a 60-char stub with a wrong-story article makes the writer confidently narrate a false story under a true headline. Appending leaves the true headline anchoring the section, so a mismatch degrades rather than fabricates. The spike also returned `claude.com` and `mlp.com` — the subjects' own promo pages — as "best open" for two stories, so top-open-result substitution would sometimes replace journalism with PR.

Do this in `consumer.py`, **not** in `find_rundown_article_source`. The resolver stays a pure resolver; the assembly loop already has `work_dir`, the directive, the text and the source path.

**Step 0 (do this first): extract the assembly into a testable function.** The append target currently sits inline inside the giant `consume_forever` loop, where the only way to test it is to drive a whole loop iteration with heavy mocking. Extract it *unchanged* first, as its own commit, then add the append in a second commit:

```python
def _assemble_writer_inputs(
    plan: RundownResearchPlan, work_dir: Path
) -> tuple[dict[str, list[str]], list[dict]]:
    """Resolve each selected directive to article text. Pure function of disk state."""
```

Move the existing loop body into it verbatim, have the consumer call it, and confirm the suite still passes with no behavior change. Commit as `refactor(consumer): extract _assemble_writer_inputs`. This keeps the behavior change in Task 5 reviewable and makes both new tests trivial.

**Files:**
- Modify: `pipeline/consumer.py` (~lines 450-472)
- Test: `pipeline/test_consumer_open_access.py` (create — verified 2026-08-15 that no such file exists)

**Step 1: Write the failing test**

```python
def test_exa_sections_appended_to_stub(tmp_path):
    """A stub wins resolution; the Exa text must still reach the writer."""
    # Arrange a work dir with a stub article + an Exa hit file for the slug,
    # run the assembly path, and assert the writer text contains BOTH the
    # stub headline and the Exa body, with the provenance heading between.
    ...

def test_exa_not_appended_when_source_is_already_exa(tmp_path):
    """No double-append when the resolver already returned the Exa file."""
    ...
```

**Step 2: Run, expect FAIL.**

**Step 3: Implement**

Add the heading constant near the top of `consumer.py`:

```python
# Framing matters: this text is retrieved by keyword search and is not
# guaranteed to cover the same story. Telling the writer that is the cheap
# mitigation for a wrong-story match in a pipeline with no human review.
_OPEN_ACCESS_HEADING = (
    "## Related coverage from other outlets\n"
    "(Retrieved by search. Use only the parts that clearly describe the "
    "story in the headline above; ignore anything that does not match.)"
)
```

In the assembly loop:

```python
                        for directive in plan.directives:
                            if not directive.include_in_episode:
                                continue
                            text, src = find_rundown_article_source(directive, work_dir)
                            exa_extra = ""
                            if src is not None and not src.startswith("enrichment/exa/"):
                                exa_extra = exa_result_sections(
                                    work_dir, _slugify(directive.headline)
                                )
                                if exa_extra:
                                    text = f"{text}\n\n{_OPEN_ACCESS_HEADING}\n\n{exa_extra}"
                            writer_inputs.append(
                                {
                                    "headline": directive.headline,
                                    "theme": directive.theme,
                                    "source_path": src,
                                    "chars": len(text),
                                    "exa_appended": bool(exa_extra),
                                    "exa_chars": len(exa_extra),
                                }
                            )
```

with imports `from pipeline.exa_client import exa_result_sections` and `from pipeline.things_happen_collector import _slugify` at module top.

**Step 4: Run, expect PASS.** Full suite.

**Step 5: Commit**

```bash
git add pipeline/consumer.py pipeline/test_consumer_open_access.py
git commit -m "feat(rundown): append open-access coverage to stubbed articles"
```

---

## Task 6: Make the funnel able to see any of this

**Why (this is not optional):** `run_stats.collect_run_stats` buckets each writer input by its single `source_path`. An augmented stub keeps `source_path` = the stub file, so it stays in the `paywalled` bucket and the Exa contribution is **invisible**. Without this task the whole measurement plan reports "nothing changed except chars".

**Files:**
- Modify: `pipeline/run_stats.py`
- Test: `pipeline/test_run_stats.py`

**Step 1: Write the failing tests**

```python
def test_writer_input_with_exa_appended_is_counted(tmp_path):
    """A paywalled stub that received Exa text must be distinguishable."""
    # writer_inputs.json entry with exa_appended=True, source_path a stub in
    # tiers.json with tier "paywalled".
    # Assert stats.writer_exa_appended == 1 and the bucket is "paywalled+exa".
    ...

def test_exa_appended_absent_on_historical_dirs(tmp_path):
    """Work dirs written before this feature have no exa_appended key."""
    # Assert writer_exa_appended == 0 and buckets unchanged.
    ...
```

**Step 2: Run, expect FAIL.**

**Step 3: Implement**

Add to `RunStats`:

```python
    # Writer inputs that had open-access coverage appended, and the total
    # characters of that appended text.
    writer_exa_appended: int = 0
    writer_exa_chars: int = 0
```

In the writer-input loop, after the bucket is chosen, suffix it and count:

```python
        appended = item.get("exa_appended") is True
        if appended:
            stats.writer_exa_appended += 1
            exa_chars = item.get("exa_chars")
            if isinstance(exa_chars, int) and not isinstance(exa_chars, bool):
                stats.writer_exa_chars += exa_chars
```

and where the bucket key is written, use `f"{bucket}+exa"` when `appended`. Keep the plain keys for the non-appended case so historical dirs render identically.

**Trap:** `write_buckets` is pre-seeded with `dict.fromkeys(_WRITE_TIERS, 0)` (`run_stats.py:293`), so `write_buckets[f"{tier}+exa"] += 1` raises `KeyError` — and `collect_run_stats` documents that it never raises, so this would break the module's core contract on the very first augmented run. Use `write_buckets[key] = write_buckets.get(key, 0) + 1` for every bucket write.

In `render_report`, extend the `WRITE` line with `, N +open-access` when `writer_exa_appended` is nonzero, and leave it off entirely when zero — historical dirs must render exactly as before. Keep the report under 4000 chars.

**Step 4: Run, expect PASS.** Then re-render real dirs and confirm no crash and no visual change on historical ones:

```bash
for d in /tmp/the-rundown-*; do uv run python -m pipeline run-stats --work-dir "$d" >/dev/null || echo "FAILED: $d"; done
```

**Step 5: Commit**

```bash
git add pipeline/run_stats.py pipeline/test_run_stats.py
git commit -m "feat(run-stats): report open-access text appended to writer inputs"
```

---

## Task 7: Credit the outlets actually used

**Why:** the script is now partly composed from Reuters/AP/CNN reporting rather than the Bloomberg link. A news digest should name the outlet it is drawing from, and the show notes should link what was actually consulted.

**Files:**
- Modify: `pipeline/rundown_writer.py` (prompt), `pipeline/show_notes.py`
- Test: `pipeline/test_show_notes.py`

**DESCOPED to the writer prompt only.** Add one instruction: attribute the outlet when facts come from a named source ("Reuters reports…"). Commit as `feat(rundown): attribute open-access outlets in the script`.

**Why the show-notes half is cut:** `show_notes.extract_show_notes_articles` returns exactly **one** `url` per directive (`show_notes.py:184-195`), and `_find_article_file` returns the stub first (`show_notes.py:48-50`), so the Exa branch (`show_notes.py:81`) is unreachable whenever a stub exists — the same shadowing bug this plan fixes in the writer path. Surfacing Exa URLs therefore requires changing the show-notes dict shape to carry multiple URLs per story, plus whatever renders episode descriptions downstream. That is a real feature with its own design, not a step in this one. **File it as a follow-up bead** rather than improvising it here.

---

## Task 8: Documentation

Update `AGENTS.md` (The Rundown Pipeline section: the tier-driven Exa trigger, `exclude_domains` policy, the append behavior) and `.opencode/skills/operating-things-happen-digest/SKILL.md` (how to read the new `+exa` buckets and what a healthy vs unhealthy open-access rate looks like — and state plainly that thresholds are still unset, per `my-podcasts-3qs`).

Commit as `docs: describe open-access substitution path`.

---

## Verification before opening the PR

```bash
uv run pytest -q                 # expect > 469 passing
uv run ruff check .
uv run ruff format --check .
for d in /tmp/the-rundown-*; do uv run python -m pipeline run-stats --work-dir "$d" >/dev/null || echo "FAILED: $d"; done
ls /tmp | grep -c 'the-rundown-\|fp-digest-\|things-happen-'   # before and after pytest: delta must be 0
```

Then: PR, adversarial review of the diff, merge, **restart the consumer** (check `pending_the_rundown` / `pending_fp_digest` in `/persist/my-podcasts/state.sqlite3` for in-flight jobs first), and verify with `readlink /proc/<pid>/cwd`.

## What to watch on the first production run (Monday 04:30 ET)

The funnel report answers all three gaps separately:

- **Trigger** — `EXA n flagged` should go from ~0-1 to **1-3 on a typical day, and legitimately 0 on a light one**. Measured stub counts across 8 real runs were `[0, 1, 0, 3, 1, 2, 1, 2]`. **`EXA 2 flagged` is success.** Do not diagnose a broken trigger from a small number; check it against that day's `FETCH` stub count instead, which is the only fair denominator.
- **Retrieval** — the hit rate within those flagged.
- **Delivery** — `WRITE` should show `paywalled+exa` entries; if it still shows bare `paywalled`, delivery is broken regardless of what the EXA line says.

Because the daily numbers are this small, **a single run cannot confirm or refute this feature.** Give it a week before drawing conclusions, and prefer the JSONL history over any one report.

If the hit rate collapses in production, the likely cause is that `directive.exa_query` (editor keywords) performs worse than the raw headline the spike validated. That is a one-line change and a measurable follow-up, not a redesign.
