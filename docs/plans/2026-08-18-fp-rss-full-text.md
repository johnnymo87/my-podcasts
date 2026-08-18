# FP Digest: fetch full text for truncated RSS articles

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Stop feeding the FP Digest writer ~360-character antiwar.com RSS teasers when the full articles are free, fetchable, and ~7x longer.

**Architecture:** Fetch at *collection* time, in `fp_collector.collect_fp_artifacts` Phase 2, **before** the editor runs — so no directive→article join is involved and nothing lands in the 180-day persistent cache. Each in-window RSS cache entry whose excerpt is short (a measured, content-only gate) gets one `trafilatura` fetch of its own URL; the fetched text replaces the excerpt in the work-dir article file **only if it is longer**. Every failure mode degrades to today's excerpt. A sidecar `rss_fetch.json` records per-article outcomes so the feature's absence is visible.

**Tech Stack:** Python 3.14, `trafilatura` (already used by the homepage path), `pytest`, `uv`.

---

## Measurements this plan rests on (taken 2026-08-18, verify before trusting)

Reproduce with the scripts under `/tmp/opencode/m*.py` (throwaway) or re-derive.

1. **The bead's "630 char" figure is the whole file, not the text.** Across the 11
   RSS-sourced directives selected in real work dirs, the *body* is **331–382 chars,
   median 361**. The ~270-char metadata header (`# title`, `URL:`, `Source:`) makes up
   the rest. The gap is therefore worse than the bead claims: ~10x, not 7x.
2. **Every antiwar RSS body is a teaser; Johnstone's are whole.** Over all 1780 cache
   files: `antiwar_news` n=942 median 355 **max 435**; `antiwar_blog` n=371 median 344
   max 454; `antiwar_original` n=320 median 348 max 451; `caitlinjohnstone` n=147
   median 4623 **min 767**. **0 of 1633** antiwar bodies exceed 1000 chars; the
   antiwar max (454) and the Johnstone min (767) do not overlap. That is the
   separation the skip gate is built on.
3. **The text is free.** Re-fetching the 8 distinct URLs behind those selected
   directives: **8/8 `tier=live`**, no paywall, no blocking, ~1.5 s each.
4. **`trafilatura` is the right extractor, not `article_fetcher`.** `article_fetcher`
   returns *more* characters (median 3881 vs 2243) but the surplus is site chrome —
   "Comment Policy", "Recent Stories", "Advertise on Antiwar.com", a topic list.
   Article markdown goes **verbatim** into the writer prompt (ROADMAP, "Facts that
   must survive compaction"), so the extra 1600 chars are actively harmful.
   `fp_collector._extract_article_text` (trafilatura, `favor_precision=True`) returns
   clean prose and is already what the routed-links path uses.
5. **Volume is small.** RSS candidates per real run: `[18, 13, 4, 14, 18, 18, 3, 18,
   30, 17, 3, 4, 17]` (median 17). New cache files/day: mean 10.3.
6. **Selection is rare: ~1.2 of ~17 candidates per run.** This is the argument
   *against* the cheapest-looking option; see "Design decisions" below.

### Second defect found while measuring (in scope, Task 4)

`source_cache.sync_antiwar_rss_cache` stores the RSS `summary` **raw** when the entry
carries no `content` element:

```python
text = _strip_html(content_html) if content_html else summary   # <- summary unescaped
```

Antiwar entries have no `content`, so every cached antiwar body contains literal HTML
entities and reaches the writer prompt that way:

```
... struck what he called a &#8220;Saudi-led coalition landing ship&#8221; ... with [&#8230;]
```

`caitlinjohnstone` goes through `_strip_html` and is clean, which is why this never
showed up in the full-text feed.

---

## Design decisions, with the alternative that was rejected

**Fetch at collection time (pre-plan), not at cache-sync time.** Sync time would put
full text in the durable cache, but the cache has 180-day retention: 1633 antiwar
files x ~2.2 KB of added text is ~3.5 MB/180 days of permanent growth, for text that
is re-derivable. Collection-time output lands in `/tmp` work dirs
(`consumer._cleanup_old_work_dirs`, consumer.py:208, reaps at **180** days — not the
10 days a comment at consumer.py:76 implies, which is systemd-tmpfiles folklore), so
growth is bounded by what `/tmp` already holds and **`/persist` grows by zero**. Fetch
volume is comparable either way (~10-17/day).

**Pre-plan, not post-plan.** Fetching only the ~1.2 *selected* articles per run would
be ~14x cheaper, and was the bead's suggestion. It is rejected because knowing which
article file a directive refers to **is** the directive→article join — the exact
disagreement `my-podcasts-wfh` is filed against. On a join miss the fetch would
silently not happen and the story would ship as a teaser with nothing to show for it:
a new invisible failure mode bolted onto a known-broken join. Pre-plan fetching needs
no join at all; it iterates cache files the collector is already reading. ~17 requests
once a day at 1 s spacing is not a politeness problem.

**Gate on excerpt length, not on feed name and not on the trailing ellipsis.**
A `source == "caitlinjohnstone"` allowlist hard-codes today's feed roster and would
keep fetching antiwar if antiwar ever switched to full text. The trailing `[&#8230;]`
marker is present in 942/942 `antiwar_news` files but only 354/371 `antiwar_blog` and
318/320 `antiwar_original` — marker-based detection already misses ~4% here, and this
project has been bitten by brittle marker detection before (see the transcript
detector history in AGENTS.md). The length gate is content-only and self-retiring.

**Threshold: 600 chars.** Sits inside the measured gap (antiwar max 454, Johnstone min
767) with margin on both sides. At this threshold, on the full 1780-file corpus,
**1633/1633 antiwar files fetch and 147/147 Johnstone files do not.**

**Never regress.** Fetched text is used only when `len(fetched) > len(excerpt)`. An
empty extraction, a 404, a timeout, a redirect to a paywall stub — all leave the
excerpt in place. The excerpt is the floor.

*Accepted residual risk, stated rather than papered over:* "longer" is not "better." If
a full-text feed ever posts a genuinely short item (Johnstone's shortest measured body
is 767 chars, so this is unexercised today) and the gate fires on it, an extraction
that returns site boilerplate longer than the real post would replace it, with
`upgraded=true` in the sidecar and no alert. A `> 1.2x` margin was considered and
rejected as a second unmeasured threshold guarding against an unmeasured case;
`trafilatura(favor_precision=True)` is the actual mitigation. The sidecar is what makes
it visible if it ever happens.

**Bounded work.** Adaptive lookback maxes at 14 days (`consumer._compute_lookback`).
At 10 new files/day that is ~140 candidates -> ~6 minutes of serialized fetching in
the worst case. Capped at 40 fetches per run, newest-first, so the cap trims the
oldest candidates rather than the freshest.

**Bounded across retries too — this is not "17 requests once a day."** An early draft
of this plan claimed it was, and that claim is false. `collection_done.json` is written
at the *end* of collection (fp_collector.py:407), after `generate_fp_research_plan`. A
persistently failing editor — precedented; a Gemini endpoint outage on 2026-05-26 is in
AGENTS.md — means every retry re-runs collection from the top, and `MAX_RETRY_FAILURES`
is **51** (db.py:43) over a ~12-hour budget. That is up to ~850 fetches/day against a
small nonprofit's site, and if antiwar.com responds by rate-limiting or banning the IP,
this feature degrades silently back to excerpts forever with no funnel to notice. So
Task 3 reuses the *previous attempt's own output*: the work dir survives retries, so if
`articles/rss/{source}/{slug}.md` already holds a body longer than the cache excerpt,
that text is reused and no request is made. Failed fetches (body == excerpt) do retry,
which is the wanted behavior. This bounds a full retry storm to ~17 successful fetches
plus one retry per genuine failure.

**`my-podcasts-wfh` is NOT bundled.** It was worth considering — the bead and the
roadmap both suggest one pass over `fp_collector.py` — but this design deliberately
touches no join, so bundling would add the riskier half of a P2 bug to a PR that is
otherwise a contained collection-phase change, and would make the diff review harder
for both. Recorded as the decision, not an oversight.

---

## Task 1: `_should_fetch_full_text` gate

**Files:**
- Modify: `pipeline/fp_collector.py`
- Test: `pipeline/test_fp_collector.py`

**Step 1: Write the failing tests**

```python
def test_should_fetch_full_text_gate_matches_measured_corpus():
    """The gate separates antiwar teasers (max 454c) from Johnstone (min 767c).

    Threshold sits in the measured gap; see
    docs/plans/2026-08-18-fp-rss-full-text.md.
    """
    from pipeline.fp_collector import _should_fetch_full_text

    # Longest antiwar teaser measured across 1633 cache files.
    assert _should_fetch_full_text("x" * 454, "https://news.antiwar.com/a/") is True
    # Shortest caitlinjohnstone body measured across 147 cache files.
    assert _should_fetch_full_text("x" * 767, "https://x.substack.com/p/a") is False


def test_should_fetch_full_text_requires_a_url():
    from pipeline.fp_collector import _should_fetch_full_text

    assert _should_fetch_full_text("short", "") is False
    assert _should_fetch_full_text("short", "   ") is False


def test_should_fetch_full_text_boundary_is_exclusive():
    from pipeline.fp_collector import _should_fetch_full_text

    assert _should_fetch_full_text("x" * 599, "https://a/") is True
    assert _should_fetch_full_text("x" * 600, "https://a/") is False
```

**Step 2: Run to verify failure**

Run: `uv run pytest pipeline/test_fp_collector.py -k should_fetch -q`
Expected: FAIL, `ImportError: cannot import name '_should_fetch_full_text'`

**Step 3: Implement**

Add to `pipeline/fp_collector.py`, below `_extract_article_text`:

```python
# Bodies shorter than this are RSS teasers worth re-fetching in full.
#
# Measured 2026-08-18 over all 1780 files in the antiwar RSS cache: the three
# antiwar feeds top out at 454 chars (n=1633, median ~350) while the full-text
# caitlinjohnstone feed bottoms out at 767 (n=147, median 4623). 600 sits in
# that gap. Deliberately a content-only rule rather than a feed allowlist, so
# it retires itself if antiwar ever publishes full text.
#
# Note the gate sees the body *after* HTML entities are decoded, while the
# corpus was measured on raw bodies. Decoding only shortens (&#8217; -> '), so
# every measured body moves away from the threshold, not toward it.
_TEASER_MAX_CHARS = 600

# Worst case for the 14-day adaptive lookback ceiling is ~140 candidates at
# ~10 new cache files/day; at ~1.5s per fetch plus a 1s delay that is ~6
# minutes. Cap the work and take the newest candidates.
_MAX_RSS_FETCHES = 40

# Seconds between outbound article fetches; matches the Levine path's
# fetch_all_articles(..., delay_between=1.0).
_RSS_FETCH_DELAY = 1.0


def _should_fetch_full_text(excerpt: str, url: str) -> bool:
    """True when a cached RSS body looks like a teaser worth re-fetching."""
    if not url.strip():
        return False
    return len(excerpt) < _TEASER_MAX_CHARS
```

**Step 4: Run to verify pass**

Run: `uv run pytest pipeline/test_fp_collector.py -k should_fetch -q`
Expected: PASS (3 tests)

**Step 5: Mutation-test**

Change `_TEASER_MAX_CHARS` to `2000`; confirm
`test_should_fetch_full_text_gate_matches_measured_corpus` fails. Change `<` to
`<=`; confirm `test_should_fetch_full_text_boundary_is_exclusive` fails. Delete the
`url.strip()` guard; confirm `test_should_fetch_full_text_requires_a_url` fails.
Restore. **Report any mutation that does not bite.**

**Step 6: Commit**

```bash
git add pipeline/fp_collector.py pipeline/test_fp_collector.py
git commit -m "feat(fp): add measured teaser-length gate for RSS full-text fetch"
```

---

## Task 2: sever network in the test suite (hermeticity, do this before Task 3)

**Files:**
- Modify: `pipeline/conftest.py`
- Test: `pipeline/test_fp_collector.py`

**Why first:** Task 3 makes `collect_fp_artifacts` reach the network on any short RSS
body. **To be accurate about the state of play: every existing test that writes RSS
cache files already patches `_extract_article_text`** (test_fp_collector.py:107, 264,
323, 377, 589), so nothing is broken today and Step 4 below will find no pre-existing
offender. This task is not a repair; it is the same structural guarantee `conftest.py`
already gives Telegram (`_block_real_telegram_posts`), extended to a second silent
transport. The reason it is worth a task: `_extract_article_text` swallows every
exception and returns `""` (fp_collector.py:40), which is the *degrade-to-excerpt*
path — so a future test that grows a new fetch does not fail, it makes a real outbound
request to an invented hostname and still passes green.

**Step 1: Write the failing test**

```python
def test_collector_cannot_reach_the_network_in_tests():
    """conftest severs fp_collector's HTTP transport structurally.

    Mirrors _block_real_telegram_posts: a test that grows a new outbound fetch
    must fail loudly rather than silently hit a real host.
    """
    import pytest

    from pipeline.fp_collector import _extract_article_text

    # _extract_article_text swallows exceptions and returns "", which is the
    # degrade-to-excerpt path; assert the transport itself is blocked.
    from pipeline import fp_collector

    with pytest.raises(AssertionError, match="real HTTP"):
        fp_collector.requests.get("https://example.invalid/")

    assert _extract_article_text("https://example.invalid/") == ""
```

**Step 2: Run to verify failure**

Run: `uv run pytest pipeline/test_fp_collector.py -k cannot_reach -q`
Expected: FAIL — `requests.get` raises `ConnectionError`/`InvalidURL`, not the
expected `AssertionError`.

**Step 3: Implement**

Add to `pipeline/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _block_real_article_fetches(request):
    """No test may fetch a real article over HTTP.

    ``fp_collector`` fetches article bodies during collection. Its fetch helper
    swallows every exception and returns "" (the degrade-to-excerpt path), so an
    unpatched fetch in a test does not fail — it makes a real outbound request to
    whatever hostname the fixture invented, and the test still passes green.
    Severing the transport makes that impossible rather than merely discouraged.

    Tests that exercise fetching patch ``pipeline.fp_collector._extract_article_text``,
    which sits above this and takes precedence.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    with patch(
        "pipeline.fp_collector.requests.get",
        side_effect=AssertionError(
            "A test made a real outbound HTTP GET through pipeline's requests "
            "module. Patch the fetch helper your code path uses (e.g. "
            "pipeline.fp_collector._extract_article_text)."
        ),
    ):
        yield
```

Note the scope honestly: patching `pipeline.fp_collector.requests.get` patches the
attribute on the shared `requests` module, so this blocks `requests.get` for
`arxiv`/`substack`/`blog_poller`/`article_fetcher`/`opencode_client` too. That is
deliberate and matches the Telegram fixture's shape — but it is why the message must
not name one module as the fix.

**Step 4: Run**

Run: `uv run pytest pipeline/test_fp_collector.py -k cannot_reach -q` -> PASS
Run: `uv run pytest -q` -> **622 baseline** + 1 = 623. A pre-existing failure here would
be a surprise (see "Why first"); if one appears, report it before fixing it.

**Step 5: Commit**

```bash
git add pipeline/conftest.py pipeline/test_fp_collector.py
git commit -m "test(fp): sever real HTTP article fetches in the pipeline suite"
```

---

## Task 3: fetch full text for teaser RSS articles

**Files:**
- Modify: `pipeline/fp_collector.py:149-195` (Phase 2)
- Test: `pipeline/test_fp_collector.py`

**Step 1: Write the failing tests**

```python
def test_rss_teaser_is_replaced_with_fetched_full_text(tmp_path, monkeypatch):
    """A short RSS body is upgraded to the fetched article text."""
    rss_cache = tmp_path / "rss"
    rss_cache.mkdir()
    today = _today_et()
    _write_rss_cache_file(
        rss_cache, today, "antiwar_news", "Strikes Kill Eleven",
        "https://news.antiwar.com/strikes/", text="Teaser body [&#8230;]",
    )
    full = "Full article text. " * 100
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text", lambda url: full
    )
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: None)
    work_dir = tmp_path / "work"

    with patch(
        "pipeline.fp_collector.generate_fp_research_plan",
        return_value=_make_empty_plan(),
    ):
        collect_fp_artifacts(
            "job-1", work_dir,
            scripts_source_dir=tmp_path / "scripts",
            fp_routed_dir=tmp_path / "routed",
            homepage_cache_dir=tmp_path / "hp",
            antiwar_rss_cache_dir=rss_cache,
            semafor_cache_dir=tmp_path / "sem",
        )

    art = (work_dir / "articles" / "rss" / "antiwar_news"
           / f"{_slugify('Strikes Kill Eleven')}.md").read_text()
    assert "Full article text." in art
    assert "Teaser body" not in art


def test_failed_fetch_degrades_to_the_excerpt(tmp_path, monkeypatch):
    """A fetch failure must never drop or shorten the story."""
    # ... same setup, but:
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: "")
    # assert the teaser text is still present in the written article
    assert "Teaser body" in art


def test_shorter_fetch_result_never_replaces_the_excerpt(tmp_path, monkeypatch):
    """The excerpt is the floor: a shorter extraction is discarded."""
    # excerpt "Teaser body ..." (~40c); fetch returns "Tiny." (5c)
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: "Tiny.")
    assert "Teaser body" in art
    assert "Tiny." not in art


def test_full_text_rss_article_is_not_refetched(tmp_path, monkeypatch):
    """caitlinjohnstone-shaped bodies are already whole; leave them alone."""
    calls = []
    monkeypatch.setattr(
        "pipeline.fp_collector._extract_article_text",
        lambda url: calls.append(url) or "SHOULD NOT BE USED",
    )
    # cache file body is "x" * 800
    assert calls == []


def test_fetched_text_reaches_the_editor_snippet(tmp_path, monkeypatch):
    """The editor sees the fetched text, not the teaser.

    Derived from the editor's own input rather than from the article file, so
    this cannot pass by construction alongside the file assertion above.
    """
    captured = {}

    def _fake_plan(headlines, **kwargs):
        captured["headlines"] = headlines
        return _make_empty_plan()

    # full text begins "Distinctive opening sentence."
    # assert any("Distinctive opening sentence." in h for h in captured["headlines"])


def test_fetches_are_capped_and_take_the_newest(tmp_path, monkeypatch):
    """Bounded work under a 14-day lookback; the cap trims the oldest.

    The newer date must carry at least _MAX_RSS_FETCHES files on its own, or the
    "all fetched urls are from the newer date" assertion is not entailed by the
    cap and would fail for a legitimate implementation.
    """
    # write _MAX_RSS_FETCHES + 5 files dated today, and 5 dated yesterday;
    # lookback_days=3 so both dates are in window
    # assert len(calls) == _MAX_RSS_FETCHES
    # assert every fetched url belongs to today's batch


def test_fetch_delay_matches_the_levine_path(tmp_path, monkeypatch):
    """One 1.0s sleep between fetches, none before the first."""
    sleeps = []
    monkeypatch.setattr("pipeline.fp_collector.time.sleep", lambda s: sleeps.append(s))
    # with 3 teaser articles:
    # assert sleeps == [1.0, 1.0]


def test_cached_entities_are_decoded_on_read(tmp_path, monkeypatch):
    """Bodies already on disk are entity-encoded; decode when reading them.

    Deliberately arranged so the body is NOT upgraded (the fetch fails), because
    that is the only state in which the on-read decode is observable — a
    successful fetch would replace the body and hide it. This is the test whose
    absence made an earlier draft's mutation list dishonest.
    """
    _write_rss_cache_file(
        rss_cache, today, "antiwar_news", "Ansar Allah Announces Attacks",
        "https://news.antiwar.com/a/",
        text="he called it a &#8220;landing ship&#8221; [&#8230;]",
    )
    monkeypatch.setattr("pipeline.fp_collector._extract_article_text", lambda url: "")
    # ... run collect_fp_artifacts ...
    art = (work_dir / "articles" / "rss" / "antiwar_news"
           / f"{_slugify('Ansar Allah Announces Attacks')}.md").read_text()
    assert "&#8220;" not in art
    assert "&#8230;" not in art
    assert "\u201clanding ship\u201d" in art
    assert "\u2026" in art


def test_retry_reuses_the_prior_attempt_and_makes_no_request(tmp_path, monkeypatch):
    """A re-run in the same work dir must not re-fetch what it already has.

    Collection re-runs from the top on every retry (collection_done.json is
    written last), and MAX_RETRY_FAILURES is 51 — so without this, a failing
    editor turns ~17 requests/day into ~850 against a small nonprofit's site.
    """
    # run 1: fetch returns a long body
    # run 2 (same work_dir): patch _extract_article_text to a callable that
    #        appends to `calls` and returns "SHOULD NOT BE USED"
    # assert calls == []
    # assert the long body from run 1 is still in the article file


def test_retry_does_refetch_a_previously_failed_article(tmp_path, monkeypatch):
    """The reuse must not cache a failure. Derived from the opposite direction
    to the test above so the two cannot both pass on a stuck implementation."""
    # run 1: fetch returns "" -> article body == excerpt
    # run 2: fetch returns a long body -> assert it IS fetched and IS used
```

The implementer writes all ten in full, following the existing helpers
(`_write_rss_cache_file`, `_make_empty_plan`) and the setup shape already used at
`pipeline/test_fp_collector.py:259`.

**Step 2: Run to verify failure**

Run: `uv run pytest pipeline/test_fp_collector.py -k "rss_teaser or degrades or shorter_fetch or not_refetched or editor_snippet or capped or fetch_delay" -q`
Expected: all FAIL.

**Step 3: Implement**

Add `import time` to the imports. Restructure Phase 2 (`fp_collector.py:159-195`) so
the cache is read into a list first, the teaser candidates are fetched, and only then
are article files written:

```python
    if _rss_cache.exists():
        pending: list[dict] = []
        for cache_path in sorted(_rss_cache.glob("*.md")):
            if not _in_window(cache_path.name):
                continue
            raw = cache_path.read_text(encoding="utf-8")
            lines = raw.split("\n")

            title = " ".join(lines[0].lstrip("# ").split()) if lines else ""
            url = ""
            source = ""
            for line in lines[1:]:
                if line.startswith("URL: "):
                    url = line[5:].strip()
                elif line.startswith("Source: "):
                    source = line[8:].strip()

            if not title or not url:
                continue
            if url in homepage_urls:
                continue

            body_parts = raw.split("\n\n", 2)
            # Cached antiwar bodies are the raw RSS <summary>, so they carry
            # undecoded HTML entities (&#8217;, the trailing [&#8230;]). Decode
            # here as well as at sync time, because cache files already on disk
            # keep the old encoding for the length of the retention window.
            text = html_mod.unescape(
                body_parts[2].strip() if len(body_parts) > 2 else ""
            )
            pending.append(
                {"headline": title, "url": url, "source": source, "text": text}
            )

        # Upgrade teasers to full text, newest first, bounded.
        candidates = [
            item
            for item in reversed(pending)
            if _should_fetch_full_text(item["text"], item["url"])
        ][:_MAX_RSS_FETCHES]
        fetch_log: list[dict] = []
        fetched_count = 0
        for item in candidates:
            # Collection re-runs from the top on every retry (the sentinel is
            # written last), and the retry budget is 51 attempts — so reuse this
            # work dir's own prior output rather than re-requesting. A body that
            # is still excerpt-length means last attempt's fetch failed, and
            # that one is worth retrying.
            prior = _prior_fetched_body(
                articles_rss_dir / item["source"] / f"{_slugify(item['headline'])}.md",
                item["text"],
            )
            if prior is not None:
                item["text"] = prior
                fetch_log.append(
                    {
                        "url": item["url"],
                        "headline": item["headline"],
                        "excerpt_chars": len(item["text"]),
                        "fetched_chars": len(prior),
                        "upgraded": True,
                        "reused": True,
                    }
                )
                continue

            if fetched_count > 0:
                time.sleep(_RSS_FETCH_DELAY)
            fetched_count += 1
            fetched = _extract_article_text(item["url"])
            # The excerpt is the floor, never the ceiling: an empty extraction,
            # an HTTP error, or a paywall stub all leave it in place.
            upgraded = len(fetched) > len(item["text"])
            fetch_log.append(
                {
                    "url": item["url"],
                    "headline": item["headline"],
                    "excerpt_chars": len(item["text"]),
                    "fetched_chars": len(fetched),
                    "upgraded": upgraded,
                    "reused": False,
                }
            )
            if upgraded:
                item["text"] = fetched

        for item in pending:
            source_dir = articles_rss_dir / item["source"]
            source_dir.mkdir(parents=True, exist_ok=True)
            art_path = source_dir / f"{_slugify(item['headline'])}.md"
            art_path.write_text(
                f"# {item['headline']}\n\nURL: {item['url']}\n"
                f"Source: {item['source']}\n\n{item['text']}",
                encoding="utf-8",
            )
            rss_articles_data.append(item)
```

The sidecar is written **unconditionally**, outside the `if _rss_cache.exists():`
block — a missing cache dir is exactly the case where "did this feature do anything?"
most needs an answer, and it is the case that would otherwise write no file:

```python
    (work_dir / "rss_fetch.json").write_text(
        json.dumps(fetch_log, indent=2), encoding="utf-8"
    )
```

with `fetch_log: list[dict] = []` initialized before Phase 2.

Add `import html as html_mod` and `import time` at the top, and the reuse helper next
to `_should_fetch_full_text`:

```python
def _prior_fetched_body(art_path: Path, excerpt: str) -> str | None:
    """Return a previous attempt's fetched body for this article, if any.

    The work dir survives retries, so a body already longer than the cache
    excerpt is text a prior attempt successfully fetched. Returns None when
    there is nothing to reuse — including when the prior body is merely the
    excerpt, which means that attempt's fetch failed and should be retried.
    """
    if not art_path.exists():
        return None
    parts = art_path.read_text(encoding="utf-8").split("\n\n", 2)
    body = parts[2].strip() if len(parts) > 2 else ""
    return body if len(body) > len(excerpt) else None
```

Note `sorted()` puts cache filenames in ascending date order, so `reversed(pending)`
is newest-first **at date granularity only** — within a single date it is reverse
alphabetical by source+slug, not publication time. That is enough for the cap's job
(trim the oldest *days* under a long lookback); do not describe it as chronological.
`pending` itself keeps its original order for writing, so article files and
`rss_articles_data` are unchanged in ordering.

**Step 4: Run**

Run: `uv run pytest pipeline/test_fp_collector.py -q` -> PASS
Run: `uv run pytest -q` -> no regressions.

Note the existing assertion at `pipeline/test_fp_collector.py:256`
(`mock_extract.assert_not_called()`, comment at :255) now encodes the *old* intent —
and with this change the mock IS called for that test's two RSS files. Update it to
assert it is not called **for homepage articles**, and say so in the commit message.

**Step 5: Mutation-test**

- Flip `len(fetched) > len(item["text"])` to `bool(fetched)`
  -> `test_shorter_fetch_result_never_replaces_the_excerpt` must fail.
- Delete the `[:_MAX_RSS_FETCHES]` slice -> `test_fetches_are_capped_and_take_the_newest` must fail.
- Drop `reversed(...)` -> the "newest" half of that test must fail.
- Delete `if fetched_count > 0` -> `test_fetch_delay_matches_the_levine_path` must fail.
- Remove the Phase-2 `html_mod.unescape` -> `test_cached_entities_are_decoded_on_read`
  must fail. **Task 4's test cannot cover this** — it drives
  `source_cache.sync_antiwar_rss_cache` with a patched `fetch_feed` and never touches
  `fp_collector`. An earlier draft of this plan claimed otherwise, which would have
  shipped the on-read decode untested; that is why the test above exists.
- Make the fetch unconditional (drop `_should_fetch_full_text`)
  -> `test_full_text_rss_article_is_not_refetched` must fail.
- Make `_prior_fetched_body` always return `None`
  -> `test_retry_reuses_the_prior_attempt_and_makes_no_request` must fail.
- Make `_prior_fetched_body` return the body whenever the file exists (drop the
  length comparison) -> `test_retry_does_refetch_a_previously_failed_article` must fail.

**Report every mutation that does not bite.**

**Step 6: Import check + commit**

```bash
uv run python -c "import pipeline.consumer"
uv run python -c "import pipeline.__main__"
git add pipeline/fp_collector.py pipeline/test_fp_collector.py
git commit -m "feat(fp): fetch full text for truncated antiwar RSS articles"
```

---

## Task 4: decode HTML entities at cache-sync time

**Files:**
- Modify: `pipeline/source_cache.py:228`
- Test: `pipeline/test_source_cache.py`

**Step 1: Write the failing test**

```python
def test_rss_summary_entities_are_decoded(tmp_path):
    """Antiwar entries carry no <content>, so the summary must still be decoded.

    Measured 2026-08-18: every cached antiwar body holds literal &#8217;/&#8220;
    and a trailing [&#8230;], and those characters reach the writer prompt
    verbatim.
    """
    entry = {
        "title": "Ansar Allah Announces Attacks",
        "link": "https://news.antiwar.com/a/",
        "summary": "he called it a &#8220;landing ship&#8221; [&#8230;]",
        "published_parsed": (2026, 8, 17, 0, 0, 0, 0, 0, 0),
    }
    # patch fetch_feed to return a feed with this single entry for one source
    body = written[0].read_text(encoding="utf-8").split("\n\n", 2)[2]
    assert "&#8220;" not in body
    assert '\u201cLanding'.lower() in body.lower() or "\u201clanding ship\u201d" in body
    assert "\u2026" in body
```

**Step 2: Run to verify failure**

Run: `uv run pytest pipeline/test_source_cache.py -k entities -q` -> FAIL

**Step 3: Implement**

`pipeline/source_cache.py:228`:

```python
            # Antiwar's feeds ship no <content>, so the summary is the body —
            # and it arrives HTML-encoded. Strip and unescape it the same way,
            # or entities reach the writer prompt verbatim.
            text = _strip_html(content_html or summary)
```

**The identical line exists one screen up for Semafor** (`source_cache.py:167`,
`text = _strip_html(content_html) if content_html else description`). Semafor entries
normally *do* carry `content`, so it may never have bitten — but "fix a defect
everywhere it lives, in the same change" is a standing lesson in `docs/ROADMAP.md`,
earned by exactly this shape (delivery fixed, show notes left). Apply the same
`_strip_html(content_html or description)` there, with its own test.

`fp_collector` Phase 2c (Semafor, fp_collector.py:270-271) reads cached bodies the same
way Phase 2 does; give it the same `html_mod.unescape` on read, for the same reason
(files already on disk keep the old encoding).

**Step 4: Run**

Run: `uv run pytest pipeline/test_source_cache.py pipeline/test_fp_collector.py -q` -> PASS

**Step 5: Mutation-test**

Revert each of the three sites in turn (RSS summary, Semafor description, Phase 2c
unescape); confirm a specific test fails for each. Restore. **Report any that do not
bite.**

**Step 6: Commit**

```bash
git add pipeline/source_cache.py pipeline/test_source_cache.py
git commit -m "fix(fp): decode HTML entities in cached RSS summaries"
```

---

## Task 5: docs + beads

**Files:**
- Modify: `AGENTS.md` (FP Digest Pipeline section), `docs/ROADMAP.md`

**Step 1:** In `AGENTS.md` under "FP Digest Pipeline", after the sources list:

```markdown
**RSS full text.** antiwar.com's three feeds publish ~360-character teasers (measured
max 454 across 1633 cache files) while the free article behind the link runs ~2000-4000
characters. `fp_collector` therefore re-fetches any in-window RSS body under 600 chars
(`_should_fetch_full_text`) through `trafilatura` before the editor runs, newest-first
and capped at 40 per run with a 1.0 s delay. The threshold is a *measured* gap, not a
guess: antiwar tops out at 454 and the full-text `caitlinjohnstone` feed bottoms out at
767, so the same rule leaves Johnstone alone without naming it.

**The excerpt is the floor.** Fetched text replaces the excerpt only when it is
strictly longer, so an empty extraction, an HTTP error, or a paywall stub degrades to
exactly what shipped before. `work_dir/rss_fetch.json` records per-article
`excerpt_chars` / `fetched_chars` / `upgraded`, which is the only way to tell a
working fetch path from a dead one — FP has no funnel report.

The fetch is at **collection** time, not cache-sync time, so nothing lands in the
180-day persistent cache; and **before** the editor, so it needs no directive→article
join (`my-podcasts-wfh` is untouched by it).
```

**Step 2:** `docs/ROADMAP.md`, in the spine near item 2, record that `tgb` shipped and
that `wfh` was explicitly *not* bundled, with the reason.

**Step 3:** Verify the numbers in the prose against the measurement scripts before
committing. Do not restate the bead's "630 chars" or "7x" — both are wrong; the body
is ~361 chars and the gain is ~10x.

**Step 4: Commit**

```bash
git add AGENTS.md docs/ROADMAP.md
git commit -m "docs: record FP RSS full-text fetch and its measured threshold"
```

---

## Final gates (before the PR)

```bash
uv run pytest -q                 # 622 baseline + new tests, 0 failures
uv run ruff check .              # blocking
uv run ruff format --check .     # blocking
uv run python -c "import pipeline.consumer"
uv run python -c "import pipeline.__main__"
uv run python -c "import pipeline.fp_collector; import pipeline.source_cache"
```

**Do not merge. Do not restart `my-podcasts-consumer`.** Open the PR, get CI green,
stop.

---

## Explicitly out of scope

- `my-podcasts-wfh` (the two disagreeing FP joins) — see "Design decisions".
- Backfilling entity-encoded cache files already on disk. Tasks 3 and 4 decode them on
  read, which covers every file that can still enter a lookback window.
- The `_should_fetch_full_text` URL guard is dead code in production (Phase 2 already
  `continue`s on an empty url). Kept as a cheap precondition on a helper that could
  acquire a second caller; noted so a reviewer does not mistake it for live logic.
- The FP writer's honest "the report available to me breaks off" admissions. Those are
  correct behavior; this change removes the *cause*, and must not touch the prompt.
- Any funnel/`run-stats.jsonl` reporting for FP. `rss_fetch.json` is the minimum that
  makes this feature's absence visible; a full funnel is its own piece of work.
