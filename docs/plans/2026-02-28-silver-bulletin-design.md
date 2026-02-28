# Silver Bulletin Newsletter Integration

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Nate Silver's Silver Bulletin Substack as a third podcast feed, generalizing the Yglesias-specific Substack logic into a reusable SubstackAdapter.

**Architecture:** Refactor `YglesiasAdapter` into a parameterized `SubstackAdapter(brand_name, domain)` that handles all Substack newsletters. Register instances for both Yglesias and Silver Bulletin. Add a new pipeline preset, update the email ingest worker sender allowlist, and add cover art.

**Tech Stack:** Python (pipeline), TypeScript (Cloudflare Workers), BeautifulSoup, pytest

---

### Task 1: Refactor YglesiasAdapter into SubstackAdapter

**Files:**
- Modify: `pipeline/source_adapters.py` (lines 148-282)
- Test: `pipeline/test_yglesias_cleanup.py`, `pipeline/test_source_url.py`, `pipeline/test_processor_titles.py`

The goal is to rename/refactor without changing behavior. All existing tests must continue to pass unchanged (except import names).

**Step 1: Rename internal helpers**

In `pipeline/source_adapters.py`:
- Rename `_extract_yglesias_source_url(raw_email)` to `_extract_substack_source_url(raw_email, domain)` and parameterize the domain. Replace the hardcoded `slowboring.com` pattern with one built from the `domain` parameter.
- Rename `_clean_yglesias_body` to `_clean_substack_body` (no logic changes needed -- it's already generic Substack boilerplate removal).

In `_extract_substack_source_url`, change:
```python
substack_post_pattern = re.compile(
    r"https://www\.slowboring\.com/p/[^\s<>?#]+",
    flags=re.IGNORECASE,
)
```
to:
```python
escaped_domain = re.escape(domain)
substack_post_pattern = re.compile(
    rf"https://(?:www\.)?{escaped_domain}/p/[^\s<>?#]+",
    flags=re.IGNORECASE,
)
```

Also update the redirect fallback link matching. Change:
```python
redirect_links = [
    link
    for link in links
    if link.startswith("https://substack.com/redirect/")
    or link.startswith("https://www.slowboring.com/action/")
]
```
to:
```python
redirect_links = [
    link
    for link in links
    if link.startswith("https://substack.com/redirect/")
    or f"{domain}/action/" in link
]
```

**Step 2: Rename YglesiasAdapter to SubstackAdapter with parameters**

Replace:
```python
@dataclass(frozen=True)
class YglesiasAdapter(DefaultAdapter):
    def format_title(
        self,
        *,
        date_str: str,
        subject_raw: str,
        subject_slug: str,
    ) -> str:
        subject = subject_raw.strip() or subject_slug.replace("-", " ")
        subject = re.sub(r"^Slow Boring:\s*", "", subject, flags=re.IGNORECASE)
        return f"{date_str} - Slow Boring - {subject}"

    def clean_body(self, *, raw_email: bytes, body: str) -> str:
        return _clean_yglesias_body(raw_email, body)

    def extract_source_url(
        self,
        *,
        raw_email: bytes,
        date_str: str,
        subject_raw: str,
    ) -> str | None:
        return _extract_yglesias_source_url(raw_email)
```

With:
```python
@dataclass(frozen=True)
class SubstackAdapter(DefaultAdapter):
    brand_name: str = ""
    domain: str = ""

    def format_title(
        self,
        *,
        date_str: str,
        subject_raw: str,
        subject_slug: str,
    ) -> str:
        subject = subject_raw.strip() or subject_slug.replace("-", " ")
        subject = re.sub(
            rf"^{re.escape(self.brand_name)}:\s*",
            "",
            subject,
            flags=re.IGNORECASE,
        )
        return f"{date_str} - {self.brand_name} - {subject}"

    def clean_body(self, *, raw_email: bytes, body: str) -> str:
        return _clean_substack_body(raw_email, body)

    def extract_source_url(
        self,
        *,
        raw_email: bytes,
        date_str: str,
        subject_raw: str,
    ) -> str | None:
        return _extract_substack_source_url(raw_email, self.domain)
```

**Step 3: Keep backward-compatible aliases**

Add these below the `SubstackAdapter` class for test compatibility:
```python
# Backward-compatible aliases
YglesiasAdapter = SubstackAdapter
```

**Step 4: Update ADAPTERS dict**

Replace:
```python
ADAPTERS: dict[str, SourceAdapter] = {
    "levine": LevineAdapter(),
    "yglesias": YglesiasAdapter(),
}
```

With:
```python
ADAPTERS: dict[str, SourceAdapter] = {
    "levine": LevineAdapter(),
    "yglesias": SubstackAdapter(brand_name="Slow Boring", domain="slowboring.com"),
    "silver": SubstackAdapter(brand_name="Silver Bulletin", domain="natesilver.net"),
}
```

**Step 5: Update internal test references**

In `pipeline/test_yglesias_cleanup.py`, update the call:
```python
# Old:
cleaned = adapters._clean_yglesias_body(raw, "fallback")
# New:
cleaned = adapters._clean_substack_body(raw, "fallback")
```

In `pipeline/test_source_url.py`, update the calls:
```python
# Old:
url = adapters._extract_yglesias_source_url(raw)
# New:
url = adapters._extract_substack_source_url(raw, "slowboring.com")
```

In `pipeline/test_processor_titles.py`, update the import:
```python
# Old:
from pipeline.source_adapters import LevineAdapter, YglesiasAdapter
# New:
from pipeline.source_adapters import LevineAdapter, SubstackAdapter
```

And the Yglesias test:
```python
# Old:
title = YglesiasAdapter().format_title(...)
# New:
title = SubstackAdapter(brand_name="Slow Boring", domain="slowboring.com").format_title(...)
```

**Step 6: Run all tests to verify refactor is behavior-preserving**

Run: `uv run pytest -v`
Expected: All 26 existing tests pass.

**Step 7: Commit**

```bash
git add pipeline/source_adapters.py pipeline/test_yglesias_cleanup.py pipeline/test_source_url.py pipeline/test_processor_titles.py
git commit -m "refactor: generalize YglesiasAdapter into parameterized SubstackAdapter"
```

---

### Task 2: Add Silver Bulletin tests

**Files:**
- Modify: `pipeline/test_processor_titles.py`
- Modify: `pipeline/test_source_url.py`

**Step 1: Write failing test for Silver Bulletin title formatting**

Add to `pipeline/test_processor_titles.py`:
```python
def test_silver_bulletin_title_uses_brand_format() -> None:
    adapter = SubstackAdapter(brand_name="Silver Bulletin", domain="natesilver.net")
    title = adapter.format_title(
        date_str="2026-02-28",
        subject_raw="Silver Bulletin: The Forecast Was Wrong",
        subject_slug="Silver-Bulletin-The-Forecast-Was-Wrong",
    )
    assert title == "2026-02-28 - Silver Bulletin - The Forecast Was Wrong"
```

**Step 2: Run test to verify it passes**

This should pass immediately since SubstackAdapter is already parameterized.

Run: `uv run pytest pipeline/test_processor_titles.py -v`
Expected: All 3 tests pass (levine, yglesias, silver).

**Step 3: Write test for Silver Bulletin URL extraction**

Add to `pipeline/test_source_url.py`:
```python
def test_extract_silver_source_from_list_post_header() -> None:
    raw = (
        b"Subject: The Forecast Was Wrong\n"
        b"Date: Fri, 28 Feb 2026 12:00:00 +0000\n"
        b"List-Post: <https://www.natesilver.net/p/the-forecast-was-wrong>\n"
        b"Content-Type: text/plain; charset=utf-8\n\n"
        b"Text body\n"
    )
    url = adapters._extract_substack_source_url(raw, "natesilver.net")
    assert url == "https://www.natesilver.net/p/the-forecast-was-wrong"


def test_extract_silver_source_from_body_link() -> None:
    raw = (
        b"Subject: The Forecast Was Wrong\n"
        b"Date: Fri, 28 Feb 2026 12:00:00 +0000\n"
        b"Content-Type: text/plain; charset=utf-8\n\n"
        b"View this post on the web at "
        b"https://www.natesilver.net/p/the-forecast-was-wrong?utm_source=email\n"
    )
    url = adapters._extract_substack_source_url(raw, "natesilver.net")
    assert url == "https://www.natesilver.net/p/the-forecast-was-wrong"
```

**Step 4: Run tests**

Run: `uv run pytest pipeline/test_source_url.py -v`
Expected: All 7 tests pass.

**Step 5: Commit**

```bash
git add pipeline/test_processor_titles.py pipeline/test_source_url.py
git commit -m "test: add Silver Bulletin title and URL extraction tests"
```

---

### Task 3: Add Silver Bulletin preset

**Files:**
- Modify: `pipeline/presets.py` (lines 16-33)

**Step 1: Add the preset**

Add after the Yglesias preset in the `PRESETS` tuple:
```python
NewsletterPreset(
    name="Nate Silver - Silver Bulletin",
    route_tags=("silver", "natesilver", "silverbulletin"),
    tts_model="tts-1-hd",
    tts_voice="echo",
    category="News",
    feed_slug="silver",
),
```

**Step 2: Run tests**

Run: `uv run pytest -v`
Expected: All tests pass.

**Step 3: Commit**

```bash
git add pipeline/presets.py
git commit -m "feat: add Silver Bulletin newsletter preset"
```

---

### Task 4: Add cover art

**Files:**
- Create: `assets/podcast/cover-silver.jpg`

**Step 1: Download and convert cover art**

```bash
curl -sL "https://substack-post-media.s3.amazonaws.com/public/images/e58c0d53-c964-4884-aa7d-513d7c41b386_625x625.png" -o /tmp/cover-silver.png
convert /tmp/cover-silver.png assets/podcast/cover-silver.jpg
```

If `convert` (ImageMagick) is not available, use `ffmpeg`:
```bash
ffmpeg -i /tmp/cover-silver.png -y assets/podcast/cover-silver.jpg
```

**Step 2: Upload to R2**

The cover art gets served by the podcast-serve worker from R2. Upload it:
```bash
# Use the pipeline's R2 client:
export R2_ACCOUNT_ID="$(sudo cat /run/secrets/r2_account_id)"
export R2_ACCESS_KEY_ID="$(sudo cat /run/secrets/r2_access_key_id)"
export R2_SECRET_ACCESS_KEY="$(sudo cat /run/secrets/r2_secret_access_key)"
uv run python3 -c "
from pipeline.r2 import R2Client
from pathlib import Path
r2 = R2Client()
r2.upload_file(Path('assets/podcast/cover-silver.jpg'), 'cover-silver.jpg', content_type='image/jpeg')
print('Uploaded cover-silver.jpg to R2')
"
```

**Step 3: Verify it serves**

```bash
curl -sI https://podcast.mohrbacher.dev/cover-silver.jpg | head -5
```
Expected: HTTP 200 with `Content-Type: image/jpeg`

**Step 4: Commit**

```bash
git add assets/podcast/cover-silver.jpg
git commit -m "feat: add Silver Bulletin podcast cover art"
```

---

### Task 5: Update email ingest worker

**Files:**
- Modify: `workers/email-ingest/src/index.ts` (lines 18-32)

**Step 1: Add Silver Bulletin sender to allowlist and routing**

In `DEFAULT_ALLOWED_SENDERS`, add:
```typescript
"natesilver@substack.com",
```

In `SENDER_ROUTE_TAGS`, add:
```typescript
"natesilver@substack.com": "silver",
```

In `LIST_ID_ROUTE_TAGS`, add:
```typescript
{ pattern: "natesilver", routeTag: "silver" },
{ pattern: "silver bulletin", routeTag: "silver" },
```

**Step 2: Typecheck**

Run: `npm --prefix workers/email-ingest run typecheck`
Expected: No errors.

**Step 3: Deploy**

Run: `npm --prefix workers/email-ingest run deploy`

**Step 4: Commit**

```bash
git add workers/email-ingest/src/index.ts
git commit -m "feat: add Silver Bulletin to email ingest allowlist and routing"
```

---

### Task 6: Verify end-to-end and push

**Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass.

**Step 2: Verify podcast feeds are accessible**

```bash
curl -sI https://podcast.mohrbacher.dev/feed.xml | head -3
curl -sI https://podcast.mohrbacher.dev/feeds/levine.xml | head -3
curl -sI https://podcast.mohrbacher.dev/feeds/yglesias.xml | head -3
curl -sI https://podcast.mohrbacher.dev/cover-silver.jpg | head -3
```

**Step 3: Push all commits**

```bash
git push
```

**Note:** The `feeds/silver.xml` feed will be auto-generated the first time a Silver Bulletin email is processed by the consumer. No manual feed creation needed.

**Note:** Cloudflare email routing rules may need to be updated in the dashboard to ensure `natesilver@substack.com` emails are routed to the worker. Check the existing routing configuration.
