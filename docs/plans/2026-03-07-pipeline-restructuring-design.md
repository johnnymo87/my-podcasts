# Pipeline Restructuring: FP Digest / Things Happen Specialization + Semaphore

**Goal**: Eliminate foreign policy overlap between FP Digest and Things Happen by routing FP content to FP Digest exclusively, and add Semaphore as a new source split across both podcasts by category.

## Current State

Both podcasts independently cover foreign policy:
- **FP Digest** collects from antiwar.com (homepage + 3 RSS feeds) and Caitlin Johnstone, runs daily at 6 PM weekdays.
- **Things Happen** receives Levine's links (~2 PM, Mon/Wed/Fri), and for links flagged `is_foreign_policy`, does independent RSS enrichment from the same antiwar.com sources. The agent then covers these FP stories in its script alongside finance/business stories.

This creates redundancy: the same Iran war story gets covered in both podcasts with potentially different analysis, wasting API costs and listener attention.

## Design

### 1. Route Levine's FP Links to FP Digest

The Things Happen editor already classifies each link with `is_foreign_policy: bool`. Instead of enriching FP links within Things Happen, write them to a shared staging directory for FP Digest to pick up.

**Things Happen collector changes** (`things_happen_collector.py`):
- After the editor AI returns directives, partition them: `is_foreign_policy=True` links get written to `/persist/my-podcasts/fp-routed-links/{date}.json` as a JSON array of `{headline, url, snippet}` objects.
- Skip all enrichment (Exa, xAI, RSS) for FP-routed links. They will not appear in the Things Happen agent's working directory.
- The remaining non-FP links proceed through enrichment as before.

**FP Digest collector changes** (`fp_collector.py`):
- At collection time, check `/persist/my-podcasts/fp-routed-links/` for a same-day file (and previous day, to handle timing gaps).
- Fetch full article text for each routed link via `trafilatura`, write to `{work_dir}/articles/routed/`.
- These articles join the existing antiwar + CJ pool for editor AI triage.

**Things Happen agent prompt changes** (`things_happen_agent.py`):
- Remove the instruction about foreign policy stories. The agent's working directory will no longer contain FP articles or FP enrichment data.
- Add a brief note: "Foreign policy stories from the newsletter have been routed to the Foreign Policy Digest podcast. Focus on finance, business, technology, law, and other non-FP topics."

**Cleanup of FP-specific enrichment in Things Happen collector:**
- Remove the FP RSS search block (lines 112-129 of `things_happen_collector.py`) since FP links are no longer processed here.
- The `is_foreign_policy` and `fp_query` fields on `ResearchDirective` remain — they're used for classification, just not for enrichment anymore.

### 2. Add Semaphore as a Source

Semaphore (`semafor.com`) publishes a single RSS feed at `https://www.semafor.com/rss.xml` containing full article content in `<content:encoded>` with `<category>` tags per article. ~20-30 items, updated continuously.

**Category routing:**

| Category | Routes to |
|----------|-----------|
| Africa | FP Digest |
| Gulf | FP Digest |
| Security | FP Digest |
| Politics | Both (each editor AI decides per-article) |
| Business | Things Happen |
| Technology | Things Happen |
| Media | Things Happen |
| CEO | Things Happen |
| Energy | Things Happen |

**Implementation in `rss_sources.py`:**
- Add a single `SEMAFOR` RSS source pointing at `https://www.semafor.com/rss.xml`.
- Add a utility function `categorize_semafor_article(category: str) -> str` that returns `"fp"`, `"th"`, or `"both"` based on the category tag.

**FP Digest collector changes:**
- Fetch Semafor RSS, filter to articles categorized as `"fp"` or `"both"`.
- Write to `{work_dir}/articles/semafor/` alongside existing sources.
- Since `<content:encoded>` includes full text, no separate `trafilatura` fetch needed.

**Things Happen collector changes:**
- Fetch Semafor RSS, filter to articles categorized as `"th"` or `"both"`.
- Write to `{work_dir}/articles/semafor/` as additional context for the agent.
- These articles appear in the agent's working directory but are not processed through the editor AI — they're supplementary context, similar to RSS enrichment results.

### 3. Timing

```
~2 PM (Mon/Wed/Fri): Levine email arrives
  → Things Happen editor triages links
  → FP links written to /persist/my-podcasts/fp-routed-links/{date}.json
  → Things Happen collector enriches non-FP links only
  → Things Happen agent generates script (non-FP content + Semafor business/tech)

6 PM (Mon-Fri): FP Digest timer fires
  → Collector gathers: antiwar.com + CJ + Semafor FP categories + routed Levine links
  → Editor AI triages combined pool into themes
  → Writer AI generates script
  → TTS + publish
```

On non-Levine days (Tue/Thu), FP Digest runs without routed links. The routed links directory simply has no file for that date.

### 4. Staging Directory Lifecycle

- `/persist/my-podcasts/fp-routed-links/{YYYY-MM-DD}.json` files accumulate.
- The consumer's existing cleanup logic (which already handles script retention) gets extended to prune routed-link files older than 7 days.
- FP Digest collector reads same-day and previous-day files to handle edge cases (e.g., Levine email arrives late Friday, FP Digest runs Monday).

### 5. What Does NOT Change

- FP Digest's existing sources (antiwar.com homepage scraper, 3 antiwar RSS feeds, Caitlin Johnstone RSS) remain exactly as they are.
- Things Happen's core flow (email-triggered, async agent with Telegram approval, opencode-serve session) stays the same.
- TTS, publishing, feed generation, and preset configuration are untouched.
- The `is_foreign_policy` and `fp_query` fields on Things Happen's `ResearchDirective` remain for classification purposes.
- Things Happen's Exa and xAI enrichment for non-FP links stays the same.
- Things Happen's AI RSS enrichment (`is_ai` / `ai_query`) stays the same.

### 6. Files Changed

| File | Change |
|------|--------|
| `pipeline/rss_sources.py` | Add Semafor RSS source + category routing function |
| `pipeline/things_happen_collector.py` | Route FP links to staging dir, skip FP enrichment, add Semafor TH articles |
| `pipeline/things_happen_agent.py` | Update prompt to note FP stories are handled elsewhere |
| `pipeline/fp_collector.py` | Pick up routed Levine links + Semafor FP articles |
| `pipeline/consumer.py` | Add cleanup for `/persist/my-podcasts/fp-routed-links/` |
