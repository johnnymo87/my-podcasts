# Unify directive→article matching Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every directive→article join in The Rundown go through one resolver that
matches on `headline_index` exact, then *unique* slug — deleting the word-overlap fallback
and the suffix-glob tier, both of which can bind a directive to the wrong article.

**Architecture:** A new leaf module `pipeline/article_resolver.py` (stdlib only, no pipeline
imports, so it cannot create a cycle) owns the article-family `slugify`, the resolution
cascade, `URL:` header extraction, and a *shadow* diagnostic that records what a
headline-vs-headline matcher would have chosen on a miss without ever acting on it.
`__main__.find_rundown_article_source` keeps its name and 3-tuple signature and becomes a
thin wrapper. The Exa trigger and show notes resolve through the same helper, so the three
joins agree by construction instead of by coincidence.

**Tech Stack:** Python 3.11, pytest, ruff, uv.

**Scope:** Beads `3yb`, `4pw`, `mr1`. Bead `5m3` (FP routing) is deliberately **excluded**
— see "Out of scope" at the bottom. This is PR1 of two.

---

## Why (measured, not assumed)

Corpus: historical work dirs in `/tmp` (reaped at 10 days). 58 have `plan.json`; **10** have
`headline_index.json`; 54 directives in those 10. The 48 index-less dirs predate the index
feature and say nothing about the fallback either way.

1. **Cascade:** 51/54 exact hit, 3/54 exact miss. All 3 misses reached word-overlap, scored a
   *perfect* 1.0, and picked the same file slug-equality would.
2. **`exact-then-slug` covers 54/54.** Zero slug collisions across 373 distinct slugs. On this
   corpus the word-overlap tier contributes nothing exact+slug does not already get.

   **Do not overclaim this.** All 3 observed reformulations were *whitespace-only*. Slug
   matching absorbs case, punctuation, whitespace, and >50-char truncation — but **not word
   substitution** ("Pay the Most" vs "Pay Most"). On such a headline the old tier might well
   have bound the *right* stub, since a Levine stub's body literally is its headline. The
   corpus cannot bound how often that happens. What justifies deletion is not "the tier is
   useless" but the asymmetry in point 3 plus the shadow log (Task 5) turning an unbounded
   unknown into forward-growing evidence. 54 directives proved *sufficiency over 10 days*,
   not absence of the reformulation class.
3. **Separation is terrible.** Scoring the best *wrong* article per directive: a wrong article
   scores ≥1 in **50 of 54** cases, reaches ≥50% of query words in **12 of 54**, and in **1
   case ties the correct article at a perfect 4/4** — where the winner is decided by `>` and
   dict insertion order. **No threshold can separate these.** This is why bead `mr1`'s
   proposed fix (set a minimum score) is rejected: the distributions overlap at the top.

Two structural reasons the fallback is worse than "noisy":

- It scores `w in content_lower` — a **substring** test against the article's *entire body*,
  not its headline (`__main__.py:105`). Score therefore correlates with body length, so a
  long, topically omnivorous file (a Zvi AI roundup section) outscores the right short stub
  on almost any AI headline. Systematically wrong, not merely noisy.
- It only runs when exact **and** slug both miss. One major cause of that is *the correct
  article not being in the index at all* (routed away, deduped, file lost). In that regime
  `best_score > 0` **guarantees** a wrong match. The tier's dominant behavior, in exactly the
  situation it exists for, is to convert a true miss into a confident fabrication.

The article text goes **verbatim** into the writer prompt and is published unread, so a wrong
match means narrating the wrong story under a true headline. A *miss* is strictly better: it
is now observable (`miss_reason`, funnel histogram) and degrades the section, per the
append-don't-replace principle from PR #9.

**The suffix-glob tier has the same disease.** `articles_dir.glob(f"*{slug}.md")`
(`__main__.py:117`) is a suffix match: slug `ai` matches `00-openai.md` and `02-dubai.md`
(verified). It must be anchored, not merely left alone.

**Guarding the deletion.** The corpus cannot rule out an unobserved "editor substantively
reformulated the headline" class. Rather than keep a tier that fabricates, Task 5 adds a
**shadow candidate**: on a miss, compute the best token-Jaccard match against index *keys*
(headlines, never bodies), record it in telemetry, and never use it. If a month of
`run-stats.jsonl` shows misses whose shadow candidate was obviously right, we revisit with a
headline-vs-headline matcher whose threshold is set from logged data. This makes the evidence
grow forward instead of demanding a bigger corpus that cannot exist.

---

## Rules for every task

- **The consumer executes the live working tree**, and `pipeline/__main__.py` is *lazily*
  imported by the running consumer (`consumer.py:~333`). A break fails **mid-job** with a
  green `systemctl status`. So at **every** commit run:
  `uv run python -c "import pipeline.consumer"` and `uv run python -c "import pipeline.__main__"`.
  Both must exit 0. **`find_rundown_article_source` must keep its name, location, and
  3-tuple return at every commit.**
- **Those two import checks do NOT cover `things_happen_collector`**, which is itself lazily
  imported everywhere (`__main__.py:58,684`, `consumer.py:335,497`) — so both checks pass
  with that file broken, the exact green-`systemctl` shape we are guarding against. For
  Tasks 4 and 6 the *real* gate is `uv run pytest -q`, which imports it. Do not treat a
  green import check as sufficient for those tasks.
- Tests hermetic: `tmp_path` only, no network, no real `/persist`, no real `/tmp`.
- Run `uv run pytest -q` (baseline **564**), `uv run ruff check .`, `uv run ruff format --check .`.
- **Never** run `git stash`, `git reset`, `git checkout --`, `git restore`, or `git clean`.
  This worktree shares a repo with other sessions. Inspect with `git diff`/`git show` only.
- Mutation-test every behavioral change: break the implementation, confirm a test fails,
  restore. If a mutation does *not* fail a test, say so — that is a missing test, and
  reporting it honestly is more valuable than a green run.

---

### Task 1: Create the resolver module (slugify + URL extraction)

**Files:**
- Create: `pipeline/article_resolver.py`
- Create: `pipeline/test_article_resolver.py`

**Step 1: Write failing tests**

```python
import pytest
from pipeline.article_resolver import slugify, extract_url


def test_slugify_matches_article_family_behavior():
    assert slugify("US Set to  Pay Most for 30-Year Debt") == "us-set-to-pay-most-for-30-year-debt"
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("---") == ""


def test_slugify_truncates_to_50_chars():
    assert len(slugify("a" * 100)) == 50


def test_slugify_keeps_non_ascii_alphanumerics():
    # str.isalnum() is True for accented letters; the article family keeps them.
    # The R2-key family (script_processor/blog_poller) strips them. They are
    # deliberately NOT unified, so pin this difference.
    assert slugify("Beyoncé") == "beyoncé"


def test_extract_url_reads_header():
    assert extract_url("# H\n\nURL: https://x.com/a\n\nbody") == "https://x.com/a"


def test_extract_url_returns_none_when_absent():
    assert extract_url("# H\n\nbody") is None


def test_extract_url_ignores_url_beyond_the_header_block():
    body = "# H\n\n" + "\n".join(f"line {i}" for i in range(20)) + "\nURL: https://late.com/x"
    assert extract_url(body) is None
```

**Step 2: Run to verify they fail**

Run: `uv run pytest pipeline/test_article_resolver.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.article_resolver'`

**Step 3: Implement**

```python
"""Directive→article matching, shared by the collector, consumer, and show notes.

Leaf module: imports nothing from ``pipeline`` so any module can use it without
risking an import cycle. See docs/plans/2026-08-17-unify-directive-article-join.md
for the measurements behind the cascade's design.
"""

from __future__ import annotations

# Number of leading lines of an article file that may carry the ``URL:`` header.
# Bounded so a URL appearing in body prose is never mistaken for the source URL.
_URL_HEADER_LINES = 8


def slugify(text: str) -> str:
    """Create a safe filename slug from a headline.

    This is the *article-matching* slug family. ``script_processor`` and
    ``blog_poller`` have a deliberately different implementation for R2 keys
    and episode slugs: they use a regex that strips non-ASCII letters, while
    this one keeps them (``str.isalnum()`` is True for 'é'). Do not merge them.
    """
    safe = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:50]


def extract_url(text: str) -> str | None:
    """Return the ``URL:`` header value from article markdown, if present.

    All three Rundown sources write this header (verified across 335 real
    article files: 103 Levine, 166 Semafor, 66 Zvi -- 100% coverage).
    """
    for line in text.split("\n")[:_URL_HEADER_LINES]:
        if line.startswith("URL: "):
            url = line[5:].strip()
            return url or None
    return None
```

**Step 4: Verify pass**

Run: `uv run pytest pipeline/test_article_resolver.py -q` → all pass.

**Step 5: Mutation-check**

Change `[:50]` to `[:60]` → truncation test must fail. Change `_URL_HEADER_LINES` to `100` →
the "beyond the header block" test must fail. Restore both.

**Step 6: Commit**

```bash
git add pipeline/article_resolver.py pipeline/test_article_resolver.py
git commit -m "feat(resolver): add leaf module for article-family slugify and URL extraction"
```

---

### Task 2: Add the resolution cascade to the resolver

**Files:**
- Modify: `pipeline/article_resolver.py`
- Modify: `pipeline/test_article_resolver.py`

The cascade takes an already-loaded index dict and returns a relative path or a miss reason.
It does **no** file I/O, which is what makes it trivially testable and reusable by the
collector (which holds the index in memory) and `__main__` (which loads it from JSON).

**Step 1: Write failing tests**

```python
from pipeline.article_resolver import resolve_headline


def test_exact_match_wins():
    index = {"A Headline": "articles/00-a.md"}
    assert resolve_headline("A Headline", index) == ("articles/00-a.md", None)


def test_slug_match_rescues_whitespace_variation():
    # The real failure: Levine headlines come from sentence extraction and can
    # carry a double space that Gemini normalizes when echoing it back.
    index = {"US Set to  Pay Most": "articles/00-us.md"}
    assert resolve_headline("US Set to Pay Most", index) == ("articles/00-us.md", None)


def test_ambiguous_slug_is_a_miss_not_a_coin_flip():
    # Two headlines sharing a >50-char prefix collapse to one slug. Picking the
    # first would be arbitrary (dict order), so refuse and say why.
    long = "A" * 60
    index = {long + " one": "articles/00-one.md", long + " two": "articles/01-two.md"}
    assert resolve_headline(long + " three", index) == (None, "slug_ambiguous")


def test_no_match_reports_index_no_match():
    index = {"Something Else": "articles/00-x.md"}
    assert resolve_headline("Totally Unrelated", index) == (None, "index_no_match")


def test_empty_index_reports_index_no_match():
    assert resolve_headline("Anything", {}) == (None, "index_no_match")


def test_empty_slug_headline_does_not_match_anything():
    index = {"!!!": "articles/00-x.md"}
    # Both slugify to "". Matching on an empty slug would pair arbitrary
    # punctuation-only headlines with each other.
    assert resolve_headline("???", index) == (None, "index_no_match")
```

**Step 2: Verify they fail** (`ImportError: cannot import name 'resolve_headline'`).

**Step 3: Implement**

```python
def resolve_headline(
    headline: str, index: dict[str, str]
) -> tuple[str | None, str | None]:
    """Resolve a directive headline to a work-dir-relative article path.

    Cascade, in order:
      1. Exact match on the headline the collector recorded.
      2. Unique slug match (absorbs whitespace/punctuation reformulation).

    Returns ``(rel_path, None)`` on a hit and ``(None, reason)`` on a miss.

    There is deliberately **no** fuzzy tier. A word-overlap fallback used to sit
    here; measured against real data, a *wrong* article scored at least one
    query word in 50 of 54 cases and tied the correct article at a perfect score
    in one, so no threshold could separate them -- while exact+slug already
    covered 54/54. Article text is fed verbatim to the writer and published
    unread, so a miss (observable, degrades the section) is strictly preferable
    to a wrong match (invisible, fabricates confidently).
    """
    if headline in index:
        return index[headline], None

    slug = slugify(headline)
    if not slug:
        return None, "index_no_match"

    matches = {rel for key, rel in index.items() if slugify(key) == slug}
    if len(matches) == 1:
        return matches.pop(), None
    if len(matches) > 1:
        return None, "slug_ambiguous"
    return None, "index_no_match"
```

**Step 4: Verify pass.**

**Step 5: Mutation-check**

- Return the first match instead of requiring uniqueness → `test_ambiguous_slug_is_a_miss_not_a_coin_flip` must fail.
- Drop the `if not slug` guard → `test_empty_slug_headline_does_not_match_anything` must fail.

Note `matches` is a **set of paths**: two index keys that differ but point at the *same* file
are not ambiguous. Add a test for that if it isn't obvious.

**Step 6: Commit**

```bash
git commit -m "feat(resolver): resolve a headline by exact match then unique slug"
```

---

### Task 3: Route `find_rundown_article_source` through the resolver

**Files:**
- Modify: `pipeline/__main__.py:27-150`
- Modify: `pipeline/test_things_happen_collector.py`

This is the behavioral heart: word-overlap is deleted, the suffix-glob is anchored, and
`index_no_overlap` becomes `index_no_match`.

**Step 1: Write the failing tests**

```python
def test_word_overlap_no_longer_binds_a_partially_matching_article(tmp_path):
    """A wrong article sharing common words must NOT be returned (bead mr1).

    Measured on real data: a wrong article shared >=1 query word in 50 of 54
    cases, so the old `best_score > 0` accept made this the common case, not an
    edge case.
    """
    articles = tmp_path / "articles"
    articles.mkdir(parents=True)
    (articles / "00-trade.md").write_text(
        "# China trade deal talks\n\nURL: https://x.com/a\n\n"
        "Beijing and Washington discussed tariffs on Tuesday."
    )
    index = {"China trade deal talks": "articles/00-trade.md"}
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    from pipeline.__main__ import find_rundown_article_source

    class D:
        headline = "China launches lunar probe mission"  # shares only "china"
        source = "levine"

    text, path, reason = find_rundown_article_source(D(), tmp_path)
    assert text == ""
    assert path is None
    assert reason == "index_no_match"


def test_legacy_glob_is_anchored_not_suffix_matched(tmp_path):
    """Slug 'ai' must not match '00-openai.md' (verified: fnmatch says it does).

    No index here, so the legacy filesystem tier is what runs.
    """
    articles = tmp_path / "articles"
    articles.mkdir(parents=True)
    (articles / "00-openai.md").write_text("# OpenAI ships a model\n\nURL: https://x.com/o\n\nbody")

    from pipeline.__main__ import find_rundown_article_source

    class D:
        headline = "AI"
        source = "levine"

    text, path, reason = find_rundown_article_source(D(), tmp_path)
    assert path is None
    assert reason == "no_index"
```

Also **update** `test_find_rundown_article_source_no_false_match` and the three tests
asserting `"index_no_overlap"` (`test_things_happen_collector.py:610,886,1224`) to expect
`"index_no_match"`, and delete/convert
`test_find_rundown_article_source_reports_path_on_word_overlap_match:675` — it pins the tier
being removed. Converting it to a *slug* match preserves its intent (a non-exact index hit
still reports its path).

**Step 2: Verify they fail.**

**Step 3: Implement.** Replace the index block (`__main__.py:65-111`) with a call to
`resolve_headline`, keeping the `no_index` / `index_unreadable` distinction that the funnel
depends on:

```python
    index_path = work_dir / "headline_index.json"
    if index_path.exists():
        try:
            index = _json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(index, dict):
                raise ValueError("headline_index.json is not an object")
        except Exception:
            index, miss_reason = {}, "index_unreadable"
        else:
            rel_path, miss_reason = resolve_headline(headline, index)
            if rel_path is not None:
                fpath = work_dir / rel_path
                if fpath.exists():
                    return fpath.read_text(encoding="utf-8"), rel_path, None
                # Index points at a file that is gone: fall through to the
                # filesystem tiers rather than reporting a hit we cannot read.
                miss_reason = "index_no_match"
            elif miss_reason == "slug_ambiguous":
                # Refusing to guess is the whole point of the uniqueness check,
                # and the filesystem tiers below would immediately undo it:
                # two Levine files whose headlines share a 50-char slug BOTH
                # match the anchored glob, and it returns the first. Stop here.
                return "", None, miss_reason
```

**This `elif` is load-bearing.** Without it Task 2's refuse-ambiguity property is defeated one
tier later by an arbitrary `sorted()[0]` pick, and no test in this plan would catch it.
Write the test for it:

```python
def test_ambiguous_slug_does_not_fall_through_to_the_filesystem(tmp_path):
    long = "A" * 60
    articles = tmp_path / "articles"
    articles.mkdir(parents=True)
    slug = ("a" * 50)
    (articles / f"00-{slug}.md").write_text("# one\n\nURL: https://x/1\n\nbody one")
    (articles / f"01-{slug}.md").write_text("# two\n\nURL: https://x/2\n\nbody two")
    index = {long + " one": f"articles/00-{slug}.md", long + " two": f"articles/01-{slug}.md"}
    (tmp_path / "headline_index.json").write_text(json.dumps(index))

    from pipeline.__main__ import find_rundown_article_source

    class D:
        headline = long + " three"
        source = "levine"

    text, path, reason = find_rundown_article_source(D(), tmp_path)
    assert (text, path, reason) == ("", None, "slug_ambiguous")
```

and anchor the flat-article glob (`__main__.py:117`) so it cannot suffix-match:

```python
    # Flat Levine articles are written as "{NN}-{slug}.md"
    # (things_happen_collector.py:143). A bare glob(f"*{slug}.md") is a SUFFIX
    # match -- slug "ai" matches "00-openai.md" -- so match the real shape.
    if articles_dir.exists():
        for match in sorted(articles_dir.glob(f"*-{slug}.md")):
            if match.parent != articles_dir:
                continue
            if re.fullmatch(rf"\d+-{re.escape(slug)}\.md", match.name):
                return (
                    match.read_text(encoding="utf-8"),
                    str(match.relative_to(work_dir)),
                    None,
                )
```

Also **anchor the Zvi glob**, which is worse than the flat one being fixed:
`zvi_dir.glob(f"*{slug}*.md")` (`__main__.py:137`) is a substring match on *both* sides. Zvi
files are named `{date}-{post_slug}-{section_slug}.md` (`zvi_cache.py:97-105`), so substring
matching is the only way it ever hits — which means it cannot simply be anchored to an exact
name. Require **uniqueness** instead: collect all matches, return the file only if there is
exactly one, otherwise treat it as a miss. Same principle as the slug tier: refuse rather
than pick arbitrarily.

Sweep the stale comment at `consumer.py:368-371`, which still names the old reason
vocabulary. Do it here, in the commit that changes the vocabulary, not in Task 10.

Update the docstring: the cascade is now exact → unique slug → legacy filesystem → Exa, and
the reason vocabulary is `no_index` / `index_unreadable` / `index_no_match` / `slug_ambiguous`.

**Bonus property worth a comment:** anchoring also fixes an empty-slug catastrophe. Today a
punctuation-only headline slugifies to `""`, so `glob(f"*{slug}.md")` becomes `glob("*.md")`
and matches *any* flat article. The anchored form cannot.

**Step 4: Verify.** Full suite. Then **both import checks**.

**Step 5: Mutation-check** — restore `best_score > 0` logic → the new false-match test must
fail. Un-anchor the glob → the `openai` test must fail.

**Step 6: Commit**

```bash
git commit -m "fix(rundown): resolve directives by exact-then-unique-slug, not word overlap

Deletes the word-overlap tier (bead mr1) and anchors the legacy glob."
```

---

### Task 3b: Frame the direct-Exa tier's text as third-party coverage

**Files:**
- Modify: `pipeline/__main__.py:144-148`
- Modify: `pipeline/test_things_happen_collector.py`

**Why this is in scope even though Task 3 didn't create it.** Deleting word-overlap makes
this tier *more reachable*. Today the fallback returns something on an index-present dir in
the overwhelming majority of cases (a wrong article scores ≥1 in 50/54), so the Exa tier
rarely runs. After Task 3, a `needs_exa` directive whose headline was lexically reformulated
misses every earlier tier and lands here — where `exa_result_sections` returns header-stripped
search results as the directive's **primary** text, with no true-headline stub anchoring it
and none of the `_OPEN_ACCESS_HEADING` framing that `consumer.py:346-358` applies on the
append path. That is the append-don't-replace principle (PR #9) violated by an existing tier
that this PR widens. Leaving it unframed while claiming this PR reduces fabrication risk
would be dishonest.

**Fix:** prefix the returned text with an explicit heading stating there is no original
article text and the following is third-party coverage retrieved by search, reusing the
existing `_OPEN_ACCESS_HEADING` wording rather than inventing a second vocabulary. The
writer prompt already instructs the model to name the outlet when drawing on such coverage.

**Test:** a work dir with no index and only an Exa enrichment file resolves to text that
*begins with* the framing heading, and the funnel still buckets it as `exa`.

**Mutation-check:** remove the prefix → the test must fail.

```bash
git commit -m "fix(rundown): frame direct-Exa resolutions as third-party coverage"
```

---

### Task 4: Unify the article-family `_slugify` copies (bead 4pw)

**Files:**
- Modify: `pipeline/things_happen_collector.py:57`, `pipeline/fp_collector.py:21`,
  `pipeline/show_notes.py:13`, `pipeline/zvi_cache.py:20`, `pipeline/source_cache.py:25`
- Modify: `pipeline/test_article_resolver.py`

Bead `4pw` says two copies; there are **five** functionally identical ones in this family
(measured). Replace each body with a re-export so existing `from .x import _slugify` callers
keep working:

```python
from pipeline.article_resolver import slugify as _slugify
```

**Step 1: Write the failing test** — pin that the R2-key family is intentionally different,
so a future reader does not "helpfully" merge all seven:

```python
def test_r2_key_slugify_family_is_deliberately_different():
    from pipeline.article_resolver import slugify
    from pipeline.script_processor import _slugify as r2_slugify

    # Article family keeps non-ASCII alphanumerics; the R2-key family strips them.
    assert slugify("Beyoncé Tour") == "beyoncé-tour"
    assert r2_slugify("Beyoncé Tour") == "beyonc-tour"
```

**Step 2-4:** verify it passes as-is (it documents current behavior), then make the five
replacements and confirm the full suite still passes — behavior must be **identical**, since
the five bodies were already identical.

**Step 5: Commit**

```bash
git commit -m "refactor: single article-family slugify, shared via article_resolver (4pw)"
```

---

### Task 5: Record a shadow candidate on every miss

**Files:**
- Modify: `pipeline/article_resolver.py`, `pipeline/__main__.py`
- Modify: `pipeline/test_article_resolver.py`

This is the guard on Task 3's deletion: gather evidence for the reformulation class the
corpus cannot rule out, **without ever acting on it**.

**Step 1: Write failing tests**

```python
from pipeline.article_resolver import shadow_candidate


def test_shadow_candidate_scores_headlines_not_bodies():
    index = {"China trade deal talks": "articles/00-trade.md"}
    cand = shadow_candidate("China trade talks resume", index)
    assert cand is not None
    assert cand["path"] == "articles/00-trade.md"
    assert cand["score"] > 0.5  # 3 of 4-ish tokens shared


def test_shadow_candidate_is_none_when_nothing_overlaps():
    assert shadow_candidate("Lunar probe launch", {"Bank earnings rise": "a.md"}) is None


def test_shadow_candidate_never_affects_resolution():
    # A high-scoring shadow must not turn a miss into a hit.
    index = {"China trade deal talks": "articles/00-trade.md"}
    assert resolve_headline("China trade talks resume", index) == (None, "index_no_match")
```

**Step 2-4:** implement Jaccard over token sets of *index keys*, returning
`{"path": ..., "score": ...}` or `None`.

**Do NOT widen `find_rundown_article_source`'s return.** It stays a 3-tuple. Widening it
would touch all 55 call sites and break the lazily-imported signature contract mid-deploy —
the precise failure this plan's rules exist to prevent. Instead call `shadow_candidate`
**separately**, at the miss site in `consumer._assemble_writer_inputs`, and record the result
on that directive's `writer_inputs.json` entry alongside `miss_reason`.

That caller needs the index, which `find_rundown_article_source` loads internally and does
not expose. **Do not add a second ad-hoc index read** — that is exactly the drifted-duplicate
shape that caused `my-podcasts-78b`. Add one small helper in `article_resolver.py`, e.g.
`load_index(work_dir) -> dict[str, str]`, returning `{}` for missing/unreadable/wrong-shape,
and have **both** `find_rundown_article_source` and the shadow call site use it. Its
unreadable/non-dict handling must match Task 3's exactly, so the `index_unreadable` reason
and the shadow path cannot disagree about what the index is.

**Step 5: Commit**

```bash
git commit -m "feat(resolver): log what a headline matcher would have picked on a miss"
```

---

### Task 6: Route the Exa trigger through the resolver (bead 3yb)

**Files:**
- Modify: `pipeline/things_happen_collector.py:355-370`
- Modify: `pipeline/test_things_happen_collector.py`

The trigger currently matches `_slugify(a.headline) == d_slug` against the in-memory
`articles` list (Levine-only). Switch it to `resolve_headline(directive.headline,
headline_index)` — the same call delivery makes — and derive the tier from `tiers[rel]`.

**Critical semantics to preserve** (write this down in a comment; it is the easiest place to
silently regress): `tiers` is populated for **Levine articles only** (`:151`). Today
`art is None` means "Semafor/Zvi story, read from cache with real body text, needs no
substitution unless the editor flagged `needs_exa`" (`:368`). After the change, the
equivalent is **`rel not in tiers`**. The union with `needs_exa` must be preserved exactly.

**The mapping is not perfectly equivalent. Three divergences — decide each deliberately:**

1. **Resolver miss (`rel is None`).** `None not in tiers` is True, so a miss is treated as
   "cache story, skip unless `needs_exa`". The old code, on an ambiguous headline, would
   have picked the first Levine match and fired per its tier. New behavior silently drops
   enrichment for that directive. That is acceptable (a refused match should not trigger a
   search built from a headline we could not resolve) but it **must be an explicit branch
   with its own test**, not an accident of `None not in tiers`.
2. **The index collapses duplicate headlines across sources.** `headline_index[headline]` is
   last-write-wins, and Semafor (`:218`) / Zvi (`:260`) are written *after* Levine (`:162`),
   so a same-headline collision resolves to the cache path and therefore never fires. The old
   trigger matched the Levine `articles` list and fired per tier. Arguably an improvement
   (the cache copy has real body text), but it contradicts "the measured fetch tier drives
   this," so note it in the comment.
3. **`origin` / `exclude_domains` derivation.** The old code builds the origin from
   `art.url` (`:379-383`). The new code **must** use `tiers[rel]["url"]` (`:154`), *not* the
   `URL:` header of the resolved file. If an implementer reaches for the file header instead,
   Semafor `needs_exa` searches start excluding `semafor.com` — a behavior change nobody
   decided. Pin this with a test.

**Step 1:** Write tests for: a Semafor directive with `needs_exa=False` does **not** fire; the
same with `needs_exa=True` **does**; a Levine stub whose headline differs only by whitespace
still fires (the case slug matching rescues); a resolver miss does not fire; and the excluded
origin comes from `tiers[rel]["url"]`.

**Steps 2-5:** implement, verify, then mutation-check **both directions** — force
`rel not in tiers` always False (the Semafor no-fire test must fail) *and* always True (a
Levine paywalled stub must stop firing, failing that test). One direction alone would leave
half the branch untested. Commit.

```bash
git commit -m "fix(rundown): fire Exa from the same join delivery uses (3yb)"
```

---

### Task 7: Route show notes through the resolver

**Files:**
- Modify: `pipeline/show_notes.py:35-60`
- Modify: `pipeline/test_show_notes.py`

`_find_article_file` is a third copy of this join (flat suffix-glob included), and it decides
which URL each episode's show notes link to. If it is left alone, trigger and delivery agree
while show notes disagree — the same class of bug, one layer down. Replace its body with
`resolve_headline` against the work dir's `headline_index.json`, keeping its existing
`Path | None` signature and its empty-slug guard, then falling back to the filesystem search.

**Two constraints, both easy to get wrong:**

1. **The filesystem fallback is permanent, not legacy.** `show_notes` is shared with the FP
   pipeline (`fp_processor.py:14-16`), and **`fp_collector` writes no `headline_index.json`
   at all** (verified: zero occurrences). So for every FP work dir the filesystem path is the
   *only* path. Do not label it "older work dirs" and do not trim its tiers as dead code —
   that would break FP show notes outright. `exa_client.py:136` already carries a comment
   calling this branch permanent; keep it true.
2. **Fall back on a *miss*, not merely on index-absence.** `__main__` falls through to the
   filesystem when the index is present but does not match. If show notes only fall back when
   the index is *absent*, the two disagree on exactly the reformulated headlines this task
   exists to reconcile. Pin identical cascade semantics with a test that a reformulated
   headline resolves to the same file in both `find_rundown_article_source` and
   `_find_article_file`.

Commit: `refactor(show-notes): resolve article files through the shared resolver`

---

### Task 8: Teach the funnel the new miss vocabulary

**Files:**
- Modify: `pipeline/run_stats.py`, `pipeline/test_run_stats.py`

The histogram at `run_stats.py:~123` is generic, so no code change may be needed — verify.
What *is* needed:

- A test that a historical `writer_inputs.json` containing the retired `index_no_overlap`
  still renders (backward compatibility — do not lose old JSONL comparability). The histogram
  at `run_stats.py:357-359` is already fully generic, so this likely needs **no** code change;
  confirm that by test rather than by reading.
- A test that `slug_ambiguous` renders.
- Surface the shadow-candidate count, e.g. `[misses: index_no_match 2 (1 w/ shadow)]`, so the
  escalation trigger is visible without grepping work dirs.

Commit: `feat(run-stats): report the new miss reasons and shadow-candidate hits`

---

### Task 9: Replay verification against real work dirs (not a shipped test)

**Not a committed test** — the data lives in `/tmp` and is reaped. This is a manual gate
before opening the PR.

Write a throwaway script that, for all 10 work dirs with `headline_index.json`, resolves
every directive with **both** the old code (a `git worktree add --detach` of `main` in a
`$(mktemp -d)`) and the new code, then diffs the chosen paths.

**Expected:** identical resolutions for all 54 directives — the 3 that previously resolved by
word-overlap now resolve by slug to *the same file* (measured). Any difference is a genuine
regression and must be explained before the PR opens.

**Also diff show-notes URL resolution pre/post**, not just delivery paths. Task 7 changes
`_find_article_file`, and a replay that only compares `find_rundown_article_source` would not
notice a show-notes regression at all. Run it over FP work dirs too (`/tmp/fp-digest-*`,
107 with `plan.json`) — those exercise the index-less path that PR1 must leave untouched.

Remove the throwaway worktree with `git worktree remove --force` when done. **Do not** run
any destructive git command in the shared checkout.

---

### Task 10: Documentation

**Files:** `AGENTS.md`, `.opencode/skills/operating-things-happen-digest/SKILL.md`

Document: the single resolver and its cascade; that the word-overlap tier was removed and
why (with the 50-of-54 and 4/4-tie numbers, so nobody "restores" it); the two slugify
families and why they stay separate; the new `slug_ambiguous` / `index_no_match` reasons.

Four things that are easy to omit and expensive to rediscover:

- **The shadow log's escalation criterion, stated carefully.** The dominant miss cause is
  *the correct article being absent from the index* — and in that regime the shadow will, by
  construction, score some plausible but **wrong** headline. So "shadow scored 0.7" is **not**
  evidence that fuzzy matching should return; read that way it would recreate the original
  bug with logged ammunition. Escalation requires checking candidates against ground truth
  and distinguishing a reformulation-shaped miss from an absent-article miss.
- **`run-stats.jsonl` spans the rename.** Series carry `index_no_overlap` before this change
  and `index_no_match` after; anyone trending miss reasons must union them.
- **`show_notes._headlines_match` (`show_notes.py:92-120`) is a remaining word-overlap join**,
  used to filter show notes by coverage. Different join, much lower stakes (it never selects
  article text for the prompt), deliberately left alone — say so, so a later reader doesn't
  think it was missed.
- **`extract_url` ships unused in PR1**, staged for the FP-routing fix in PR2. Say so
  explicitly, otherwise a reviewer flags it as dead code or an implementer "helpfully" wires
  it into `show_notes._extract_url_from_article:21-32`, which has deliberately different
  behavior (whole-file scan, tolerant of a missing space).

Commit: `docs: describe the unified directive-article resolver`

---

## Out of scope (file/keep beads)

- **`5m3` — FP routing.** Excluded on purpose; it ships as **PR2**. Measurement overturned
  the bead's premise: all 12 routed links on disk have an empty `url` (**100%**, not the ~8%
  the bead predicts) because **16/16** FP-flagged directives are `source: semafor` while the
  join searches the Levine-only `articles` list. The bead's proposed slug one-liner would not
  have fixed it. The real fix resolves through `headline_index` (which spans all sources) and
  parses the `URL:` header. It is separated because it changes **another podcast's input**,
  and it carries two hazards this PR should not drown out: (a) `fp_collector.py:232,342` label
  every routed link `levine-routed` / `[routed/levine]`, which is already wrong for 16/16 and
  becomes materially misleading once real content flows; (b) filling in URLs enables a
  URL-dedup that empty URLs silently disabled, so a `Routing: both` Semafor article could
  arrive in FP twice in one run — needs a within-run dedup test.
- **FP has the identical two-join disease** (`consumer._find_article_text:275` +
  `fp_collector.py:392`). File a bead; do not scope-creep.
</content>
