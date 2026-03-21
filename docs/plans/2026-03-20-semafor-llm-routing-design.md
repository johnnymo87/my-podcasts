# Semafor LLM Routing Design

## Problem

Semafor's RSS feed assigns category tags inconsistently. About 21% of articles have no category at all, and some categories (like "CEO Signal") don't match our hardcoded mapping. The current `categorize_semafor_article()` function uses a static map of category strings to routing values (`fp`, `th`, `both`), which breaks whenever Semafor changes or omits its tags.

Examples of misrouted articles today:
- "Hungary blocks EU pipeline loan to Ukraine" -- uncategorized, falls to `both` instead of `fp`
- "K-pop megastars BTS return after four-year hiatus" -- uncategorized, falls to `both` instead of `th`
- "CEO Signal" articles -- mapped set has `"ceo"` but Semafor uses `"CEO Signal"`, so they miss the `th` bucket

## Solution

Replace the hardcoded category-to-routing map with a single Gemini Flash-Lite call at cache sync time. The model receives article titles (and RSS descriptions when available) and classifies each as `fp`, `th`, `both`, or `skip`.

## Where it runs

In `sync_semafor_cache()` in `source_cache.py`. After all articles are fetched and written to disk, one batched Gemini call classifies all newly cached articles. The routing result is written back into each cached file as a `Routing:` header line.

## Cache format

New `Routing:` line between `Category:` and `Type:`:

```
# Hungary blocks EU pipeline loan to Ukraine

URL: https://...
Published: 2026-03-20
Source: semafor
Category:
Routing: fp
Type: article

...body text...
```

`Category:` stays for debugging. `Routing:` is the authoritative routing value.

## LLM call

One batched Gemini Flash-Lite call per sync. Input is a list of `(index, title, description)` tuples -- typically under 200 characters per article, well under 1000 tokens total for ~30 articles.

Pydantic response schema:

```python
class ArticleRouting(BaseModel):
    index: int
    routing: str  # "fp", "th", "both", or "skip"

class RoutingResult(BaseModel):
    articles: list[ArticleRouting]
```

Prompt describes the two podcasts:
- `fp`: foreign policy, geopolitics, military, diplomacy, international relations
- `th`: business, technology, AI, science, culture, domestic politics, media
- `both`: articles that clearly span both (e.g., US sanctions policy, trade wars)
- `skip`: publisher self-promotion, event announcements, meta-content

Temperature 0.1, consistent with existing Gemini usage in the project.

## Fallback

- If `GEMINI_API_KEY` is missing or the call fails, all articles get `Routing: both` (same as current behavior for uncategorized articles).
- Collectors reading old cache files without a `Routing:` line fall back to the existing `categorize_semafor_article()` function. This provides backward compatibility during the 180-day cache retention window.

## Collector changes

Both `things_happen_collector.py` and `fp_collector.py` change their Semafor filtering logic:
1. Read `Routing:` header from cached file
2. If present, use it directly (skip `categorize_semafor_article()`)
3. If absent, fall back to reading `Category:` and calling `categorize_semafor_article()`
4. `Routing: skip` is treated as neither `fp` nor `th` -- article is excluded from both podcasts

## What stays

`categorize_semafor_article()`, `_SEMAFOR_FP_CATEGORIES`, and `_SEMAFOR_TH_CATEGORIES` remain as fallback for old cache files. They can be removed once all pre-change files age out.

## Testing

- Unit test: mocked Gemini response verifies `Routing:` headers are written into cache files
- Unit test: fallback path (no API key, API error) writes `Routing: both`
- Unit test: collectors read `Routing:` header and filter correctly
- Unit test: collectors fall back to `Category:` when `Routing:` is absent
- Unit test: `Routing: skip` is excluded from both collectors
