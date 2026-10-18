# The Argument Audio Reports — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Publish a weekly AI "Report:" briefing of *The Argument* podcast to the
`yglesias` feed, built from the free public audio.

**Architecture:** Design doc `docs/plans/2026-10-18-argument-audio-report-design.md`
(read it first). Phase 1 builds the pure pipeline (RSS parse -> download -> segment
-> Gemini ASR with speaker labels -> report writer) behind a `--dry-run` CLI and
runs the **diarization pilot gate**. Phase 2 (state table, killable child, publish
identity, poller, consumer wiring) is outlined here and gets expanded into full
bite-sized tasks **after** the pilot, because the pilot may change the
transcription shape (context carry-over, piece size, attribution hedging).

**Tech stack:** Python 3 / uv / pytest, `google-genai`, ffmpeg, click,
opencode-serve via `pipeline/report_engine.py`.

**Ground rules (from repo AGENTS.md — violating these has caused real incidents):**
- Tests never touch the network, `/persist`, or real API keys. Autouse guards in
  `pipeline/conftest.py` enforce it. Patch `pipeline.tts.asr._make_genai_client`
  (the guard patches that exact name) or inject fakes.
- Persist model output atomically (`report_engine.persist_raw_output`).
- The consumer is the only publisher. Phase 1 publishes nothing.
- Do not edit `pipeline/tts/asr.py:ASR_PROMPT` or its generation config — they are
  calibrated for the TTS verifier.
- Run tests with `uv run pytest <path> -q`. Full suite: `uv run pytest -q`.
- Commit bare `git commit` (no identity flags), one commit per task.

Work in `/home/dev/projects/my-podcasts/.worktrees/argument-audio-report`.

---

## Handoff state (keep current)

- Bead: `my-podcasts-bw3`. Branch `argument-audio-report`, worktree
  `/home/dev/projects/my-podcasts/.worktrees/argument-audio-report` (based on
  `origin/main` 96afaeb; the main checkout's local `main` was behind origin).
- Status 2026-10-18: design + plan committed; adversarial review of the design
  done (approve-with-changes, all 8 findings folded into the design doc). No
  feature code yet. Next: Phase 1 via subagent-driven development.
- Owner process (verbatim intent): full discipline — optional oracle-astra
  consult, SDD, adversarial-reviewer-astra on the finished diff, PR if
  applicable, keep bead + this plan updated.
- **Pilot gate:** after Task 7, hand the owner both dry-run scripts + attribution
  findings and STOP; Phase 2 only after sign-off. Suggested PR shape: Phase 1 is
  inert (dry-run only) — open its PR after the pilot, or fold into the Phase 2 PR
  if the pilot changes transcription; decide with the owner.
- Episode GUIDs (live feed 2026-10-18):
  - 2026-10-15 `audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1` Canceling (and un-canceling) historic heroes (80 min) — pilot, hosts only
  - 2026-10-08 `audioboom-01a1185a-d441-72a3-9173-6bc283662916` Trump, Xi, and the race for superintelligence (85 min) — pilot, guest Jordan Schneider
  - 2026-10-01 `audioboom-01a0f39d-75cf-79af-9db5-6192b276aebd` What the hell is going on in Europe? (61 min)
  - 2026-09-10 `audioboom-01a0882b-df68-7722-984d-ba79baf4bb6e` How 9/11 changed American politics forever (97 min; longest — size the child deadline from it)
  - Backfill set after ship = Sep 10, Oct 1, Oct 8, Oct 15 (owner chose all four). Sep 17 / Sep 24 already have Slow Boring Reports — do not backfill.
- Correction to the review: `segment.decode_to_pcm_with_log` already keeps ffmpeg
  warnings; only `decode_to_pcm` drops them. Use the `_with_log` variant.
- `gws` is authenticated as jonathan.mohrbacher@gmail.com (re-authed this
  session) if mail needs re-checking.

---

## Phase 1 — pure pipeline + pilot

### Task 1: Podcast source definition and RSS parsing

**Files:**
- Create: `pipeline/podcast_sources.py`
- Create: `pipeline/podcast_feed.py`
- Create: `pipeline/fixtures/argument_feed.xml`
- Test: `pipeline/test_podcast_feed.py`

**Step 1: Create the fixture.** A trimmed copy of the real audioboom feed shape
(three items: one with a guest, one plain, one future-dated; one item with a
`Buzzsprout-` style GUID). Write it by hand from this template (the real item
shape, verified 2026-10-18):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
<title>The Argument</title>
<item>
  <title>Canceling (and un-canceling) historic heroes</title>
  <link>https://audioboom.com/posts/8964415</link>
  <enclosure url="https://pscrb.fm/rss/p/dts.podtrac.com/redirect.mp3/audioboom.com/posts/8964415.mp3?modified=1792013674&amp;sid=5173020&amp;source=rss" length="0" type="audio/mpeg" />
  <itunes:duration>4807</itunes:duration>
  <description><![CDATA[<div><p>Jerusalem Demsas and Matt Yglesias debate which historical figures deserve to be canceled.</p><p>(0:00) Who deserves to be canceled?<br>(1:30) Why Matt wants more statues</p></div>]]></description>
  <pubDate>Thu, 15 Oct 2026 07:00:00 +0000</pubDate>
  <guid isPermaLink="false">audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1</guid>
</item>
<item>
  <title>Trump, Xi, and the race for superintelligence</title>
  <link>https://audioboom.com/posts/8960000</link>
  <enclosure url="https://audioboom.com/posts/8960000.mp3?source=rss" length="0" type="audio/mpeg" />
  <itunes:duration>5152</itunes:duration>
  <description><![CDATA[<p>ChinaTalk's Jordan Schneider joins Jerusalem Demsas and Matt Yglesias.</p>]]></description>
  <pubDate>Thu, 08 Oct 2026 07:00:00 +0000</pubDate>
  <guid isPermaLink="false">audioboom-0000aaaa-bbbb-cccc-dddd-eeeeffff0000</guid>
</item>
<item>
  <title>An older episode</title>
  <link>https://audioboom.com/posts/1</link>
  <enclosure url="https://audioboom.com/posts/1.mp3" length="12345" type="audio/mpeg" />
  <itunes:duration>01:01:05</itunes:duration>
  <description>Plain text description.</description>
  <pubDate>Thu, 01 Jan 2026 07:00:00 +0000</pubDate>
  <guid isPermaLink="false">Buzzsprout-12345</guid>
</item>
</channel>
</rss>
```

**Step 2: Write failing tests** in `pipeline/test_podcast_feed.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.podcast_feed import PodcastItem, parse_podcast_feed, eligible_items
from pipeline.podcast_sources import PODCAST_SOURCES, get_podcast_source

FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"


def _items() -> list[PodcastItem]:
    return parse_podcast_feed(FIXTURE.read_text(encoding="utf-8"))


def test_parses_identity_and_metadata() -> None:
    first = _items()[0]
    assert first.guid == "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"
    assert first.title == "Canceling (and un-canceling) historic heroes"
    assert first.link == "https://audioboom.com/posts/8964415"
    assert first.pub_date == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    assert first.duration_s == 4807
    assert first.enclosure_url.startswith("https://pscrb.fm/")
    assert "&amp;" not in first.enclosure_url


def test_description_is_plain_text() -> None:
    desc = _items()[0].description
    assert "<" not in desc
    assert "Jerusalem Demsas and Matt Yglesias" in desc


def test_hms_duration() -> None:
    assert _items()[2].duration_s == 3665


def test_item_without_enclosure_or_guid_is_skipped() -> None:
    xml = FIXTURE.read_text(encoding="utf-8").replace(
        '<guid isPermaLink="false">Buzzsprout-12345</guid>', ""
    )
    assert [i.guid for i in parse_podcast_feed(xml)][-1] != "Buzzsprout-12345"
    assert len(parse_podcast_feed(xml)) == 2


def test_eligible_items_cutoff_future_and_oldest_first() -> None:
    now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    since = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    got = eligible_items(_items(), since=since, now=now)
    assert [i.guid[:14] for i in got] == ["audioboom-0000", "audioboom-01a1"]
    # A future-dated item is never eligible.
    assert eligible_items(_items(), since=since, now=datetime(2026, 10, 10, tzinfo=UTC))[-1].pub_date.day == 8


def test_source_registry() -> None:
    src = get_podcast_source("argument")
    assert src.feed_slug == "yglesias"
    assert src.rss_url == "https://audioboom.com/channels/5173020.rss"
    assert src.since == datetime(2026, 10, 16, tzinfo=UTC)
    assert [s.key for s in PODCAST_SOURCES] == ["argument"]
    with pytest.raises(KeyError):
        get_podcast_source("nope")
```

**Step 3:** `uv run pytest pipeline/test_podcast_feed.py -q` -> FAIL (ImportError).

**Step 4: Implement.**

`pipeline/podcast_sources.py`:

```python
"""Podcast-audio sources whose episodes become "Report:" briefings.

The transcript for these shows is not available to us (The Argument's is
paid-only, see docs/plans/2026-10-18-argument-audio-report-design.md), so the
pipeline transcribes the free public audio itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class PodcastSource:
    key: str
    name: str
    rss_url: str
    feed_slug: str
    title_label: str
    hosts: tuple[str, ...]
    # Automatic discovery ignores items published before this instant. Explicit
    # enqueue (backfill) bypasses it.
    since: datetime


PODCAST_SOURCES: tuple[PodcastSource, ...] = (
    PodcastSource(
        key="argument",
        name="The Argument",
        rss_url="https://audioboom.com/channels/5173020.rss",
        feed_slug="yglesias",
        title_label="The Argument",
        hosts=("Jerusalem Demsas", "Matt Yglesias"),
        since=datetime(2026, 10, 16, tzinfo=UTC),
    ),
)


def get_podcast_source(key: str) -> PodcastSource:
    for source in PODCAST_SOURCES:
        if source.key == key:
            return source
    raise KeyError(key)
```

`pipeline/podcast_feed.py`:

```python
"""Parse a podcast RSS feed into items keyed by GUID (never enclosure URL:
audioboom's carries mutable tracking query strings)."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@dataclass(frozen=True)
class PodcastItem:
    guid: str
    title: str
    link: str
    pub_date: datetime
    duration_s: int | None
    enclosure_url: str
    description: str


def _duration(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if all(p.isdigit() for p in parts) and 1 < len(parts) <= 3:
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total
    return None


def _plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def parse_podcast_feed(rss_xml: str) -> list[PodcastItem]:
    root = ET.fromstring(rss_xml)
    items: list[PodcastItem] = []
    for it in root.iter("item"):
        guid = (it.findtext("guid") or "").strip()
        enclosure = it.find("enclosure")
        url = enclosure.get("url", "").strip() if enclosure is not None else ""
        raw_date = it.findtext("pubDate")
        if not guid or not url or not raw_date:
            continue
        try:
            pub = parsedate_to_datetime(raw_date).astimezone(UTC)
        except (TypeError, ValueError):
            continue
        items.append(
            PodcastItem(
                guid=guid,
                title=(it.findtext("title") or "").strip(),
                link=(it.findtext("link") or "").strip(),
                pub_date=pub,
                duration_s=_duration(it.findtext(f"{_ITUNES}duration")),
                enclosure_url=url,
                description=_plain(it.findtext("description") or ""),
            )
        )
    return items


def eligible_items(
    items: list[PodcastItem], *, since: datetime, now: datetime
) -> list[PodcastItem]:
    """Items with since <= pub_date <= now, oldest first."""
    return sorted(
        (i for i in items if since <= i.pub_date <= now), key=lambda i: i.pub_date
    )
```

**Step 5:** `uv run pytest pipeline/test_podcast_feed.py -q` -> PASS. Fix the test,
not the code, only if a test assertion is itself wrong (e.g. ordering expectation).

**Step 6: Commit** `git add pipeline/podcast_sources.py pipeline/podcast_feed.py pipeline/fixtures/argument_feed.xml pipeline/test_podcast_feed.py && git commit -m "Podcast-audio source registry and RSS parsing (argument)"`

---

### Task 2: Enclosure download with completeness check

**Files:**
- Create: `pipeline/podcast_download.py`
- Test: `pipeline/test_podcast_download.py`

Behavior: stream to `<dest>.tmp`, follow redirects (audioboom's enclosure goes
through several tracking hops), refuse above `max_bytes` (default 300 MB), refuse
when `Content-Length` is present and the byte count differs, then `os.replace`. An
existing `dest` is reused (retry does not re-download). Use `httpx` (already a
dependency, see `pipeline/tts/asr.py`). The function takes an injectable
`client: httpx.Client` so tests use `httpx.MockTransport` — no network.

**Step 1: Failing tests:**

```python
import httpx
import pytest

from pipeline.podcast_download import DownloadError, download_enclosure


def _client(body: bytes, *, length: str | None = None, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-length": length} if length is not None else {}
        return httpx.Response(status, content=body, headers=headers)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_downloads_atomically(tmp_path) -> None:
    dest = tmp_path / "audio.mp3"
    n = download_enclosure("https://x/a.mp3", dest, client=_client(b"abc"))
    assert n == 3 and dest.read_bytes() == b"abc"
    assert not (tmp_path / "audio.mp3.tmp").exists()


def test_reuses_existing_file(tmp_path) -> None:
    dest = tmp_path / "audio.mp3"
    dest.write_bytes(b"old")
    def boom(request):  # must not be called
        raise AssertionError("re-downloaded")
    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert download_enclosure("https://x/a.mp3", dest, client=client) == 3


def test_short_body_vs_content_length_refused(tmp_path) -> None:
    # MockTransport recomputes content-length from content, so simulate a
    # truncated stream by declaring a larger length via a raw stream.
    def handler(request):
        return httpx.Response(200, headers={"content-length": "10"},
                              stream=httpx.ByteStream(b"abc"))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DownloadError, match="incomplete"):
        download_enclosure("https://x/a.mp3", tmp_path / "a.mp3", client=client)
    assert not (tmp_path / "a.mp3").exists()


def test_size_cap(tmp_path) -> None:
    with pytest.raises(DownloadError, match="exceeds"):
        download_enclosure("https://x/a.mp3", tmp_path / "a.mp3",
                           client=_client(b"x" * 11), max_bytes=10)


def test_http_error(tmp_path) -> None:
    with pytest.raises(DownloadError, match="404"):
        download_enclosure("https://x/a.mp3", tmp_path / "a.mp3",
                           client=_client(b"", status=404))
```

If `httpx` raises its own error on a short stream before our check, catch
`httpx.HTTPError` and wrap it as `DownloadError("incomplete ...")` — the test
asserts the wrapped message.

**Step 2:** run -> FAIL. **Step 3: Implement:**

```python
"""Download a podcast enclosure to disk, refusing partial or oversized files."""

from __future__ import annotations

import os
from pathlib import Path

import httpx

MAX_BYTES = 300 * 1024 * 1024
_TIMEOUT = httpx.Timeout(30.0, read=60.0)


class DownloadError(RuntimeError):
    pass


def download_enclosure(
    url: str,
    dest: Path,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = MAX_BYTES,
) -> int:
    if dest.exists() and dest.stat().st_size > 0:
        return dest.stat().st_size
    tmp = dest.with_name(dest.name + ".tmp")
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT,
                                    headers={"User-Agent": "my-podcasts/1.0"})
    try:
        with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise DownloadError(f"HTTP {resp.status_code} for {url}")
            declared = resp.headers.get("content-length")
            written = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise DownloadError(f"download exceeds {max_bytes} bytes")
                    fh.write(chunk)
        if declared is not None and declared.isdigit() and int(declared) != written:
            raise DownloadError(f"incomplete download: {written} of {declared} bytes")
        if written == 0:
            raise DownloadError("incomplete download: empty body")
        os.replace(tmp, dest)
        return written
    except httpx.HTTPError as exc:
        raise DownloadError(f"incomplete download: {exc!r}") from exc
    finally:
        tmp.unlink(missing_ok=True)
        if own:
            client.close()
```

The wall-clock bound is not here (httpx timeouts are per-read); Phase 2 runs
this inside the killable child.

**Step 4:** run -> PASS. **Step 5: Commit** "Podcast enclosure download with completeness and size checks".

---

### Task 3: Transcription — segmentation, encoding, per-piece ASR, manifest

**Files:**
- Create: `pipeline/podcast_transcribe.py`
- Test: `pipeline/test_podcast_transcribe.py`

Design points (each pinned by a test):
- `decode_to_pcm_with_log` (existing, keeps ffmpeg warnings) -> PCM. Decoded
  duration must be within tolerance of the RSS duration: `abs(decoded - rss) <=
  max(120, 0.08 * rss)` seconds (dynamic ad insertion changes length a little;
  truncation changes it a lot). `rss=None` skips the check but records it.
- `split_pcm(pcm, target_s=600, search_s=20)` (existing) -> contiguous ranges.
  Assert ranges are contiguous and cover `[0, len(pcm))`.
- Each piece is encoded to mono 64 kbps mp3 by ffmpeg from PCM stdin
  (`-f s16le -ar 24000 -ac 1 -i pipe:0 -b:a 64k -f mp3 pipe:1`, timeout 120 s).
  Assert encoded size <= `MAX_PIECE_BYTES = 15 * 1024 * 1024` (Gemini inline
  request limit is 20 MB incl. base64 overhead; 10 min at 64k is ~4.8 MB).
- Transcriber is an injected callable `(audio: bytes, prompt: str) -> PieceResult`
  so tests never touch Gemini. The real one, `GeminiPieceTranscriber`, builds its
  client via `pipeline.tts.asr._make_genai_client` (the name the conftest guard
  patches), model `gemini-3.8-flash`, temperature 0, thinking LOW,
  `max_output_tokens=16384`, its own prompt. It refuses no-candidate and
  non-STOP responses (raise `TranscriptionError`).
- Prompt (`PIECE_PROMPT_VERSION = "1"`): transcribe verbatim; one turn per line as
  `Name: text`; use the listed host names and guests named in the description
  **only when the voice is identified by an introduction, by being addressed by
  name, or by the speaker map from earlier pieces**; otherwise `Unknown speaker
  A/B`; never infer identity from opinions; wrap sponsor reads as
  `[SPONSOR] ... [/SPONSOR]`. Context carry-over (`carry_context=True`, the pilot
  default): the prompt for piece i>0 includes the established speaker list and the
  last 20 lines of piece i-1's transcript, labelled as context not to repeat.
  Pieces therefore run **sequentially** when `carry_context` is on.
- Per piece, refuse if `words < MIN_WORDS_PER_MIN * piece_minutes` with
  `MIN_WORDS_PER_MIN = 80` (gross-shortfall heuristic only).
- Any piece failure raises `TranscriptionError`; no partial transcript is returned
  or written.
- Writes, atomically, `work_dir/transcript.txt` and `work_dir/transcript.json`
  (manifest: audio sha256, rss/decoded duration, ffmpeg warnings, prompt version,
  model, carry_context, and per piece: index, start_s, end_s, mp3 sha256, bytes,
  finish_reason, input/output tokens, words, text). If `transcript.txt` and a
  manifest whose `audio_sha256` matches the current audio both exist, return the
  stored transcript with **zero** transcriber calls.

**Step 1: Failing tests.** Use real ffmpeg on synthetic audio (ffmpeg is on the host
and already used by `pipeline/tts` tests; check `rg -n "ffmpeg" pipeline/tts/test_segment*.py`
and follow their skip marker if one exists). Generate a 25-minute tone-with-gaps
mp3 once per module with ffmpeg `-f lavfi -i "sine=frequency=440:duration=1500"` —
or, to keep tests fast, monkeypatch `decode_to_pcm_with_log` to return synthetic
PCM (silence + noise blocks) and monkeypatch the encoder to return
`b"mp3:" + sha(pcm)`. Prefer the monkeypatched route for logic tests and keep one
real-ffmpeg test for `encode_piece`.

```python
import json
import pytest

from pipeline import podcast_transcribe as pt


def _pcm(seconds: float) -> bytes:
    # 24 kHz s16 mono; alternating loud/quiet 1 s blocks so split_pcm finds cuts
    loud = (b"\x00\x40" * 24000)
    quiet = (b"\x00\x00" * 24000)
    out = b""
    for i in range(int(seconds)):
        out += loud if i % 7 else quiet
    return out


class FakeTranscriber:
    def __init__(self, words_per_piece: int = 2000, fail_at: int | None = None,
                 finish: str = "STOP"):
        self.calls: list[str] = []
        self.words = words_per_piece
        self.fail_at = fail_at
        self.finish = finish

    def __call__(self, audio: bytes, prompt: str) -> pt.PieceResult:
        i = len(self.calls)
        self.calls.append(prompt)
        if i == self.fail_at:
            raise pt.TranscriptionError("asr_error: boom")
        text = "\n".join(f"Jerusalem Demsas: piece{i} " + "word " * 19
                         for _ in range(self.words // 20))
        return pt.PieceResult(text=text, finish_reason=self.finish,
                              input_tokens=1, output_tokens=2)


@pytest.fixture
def audio(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fake-mp3")
    monkeypatch.setattr(pt, "decode_to_pcm_with_log", lambda p: (_pcm(1500), ""))
    monkeypatch.setattr(pt, "encode_piece", lambda pcm: b"mp3" + pcm[:16])
    return path


def test_pieces_cover_audio_in_order(audio, tmp_path) -> None:
    fake = FakeTranscriber()
    text = pt.transcribe_episode(audio, tmp_path, rss_duration_s=1500,
                                 description="d", hosts=("Jerusalem Demsas", "Matt Yglesias"),
                                 transcriber=fake)
    manifest = json.loads((tmp_path / "transcript.json").read_text())
    pieces = manifest["pieces"]
    assert len(pieces) == len(fake.calls) >= 2
    assert pieces[0]["start_s"] == 0
    for a, b in zip(pieces, pieces[1:]):
        assert a["end_s"] == b["start_s"]
    assert pieces[-1]["end_s"] == pytest.approx(1500, abs=0.01)
    assert text.index("piece0") < text.index("piece1")


def test_one_failed_piece_fails_all(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError):
        pt.transcribe_episode(audio, tmp_path, rss_duration_s=1500, description="",
                              hosts=(), transcriber=FakeTranscriber(fail_at=1))
    assert not (tmp_path / "transcript.txt").exists()


def test_word_rate_floor(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError, match="words"):
        pt.transcribe_episode(audio, tmp_path, rss_duration_s=1500, description="",
                              hosts=(), transcriber=FakeTranscriber(words_per_piece=100))


def test_truncated_audio_refused_by_duration(audio, tmp_path) -> None:
    with pytest.raises(pt.TranscriptionError, match="duration"):
        pt.transcribe_episode(audio, tmp_path, rss_duration_s=4800, description="",
                              hosts=(), transcriber=FakeTranscriber())


def test_reuse_skips_asr(audio, tmp_path) -> None:
    pt.transcribe_episode(audio, tmp_path, rss_duration_s=1500, description="",
                          hosts=(), transcriber=FakeTranscriber())
    again = FakeTranscriber()
    pt.transcribe_episode(audio, tmp_path, rss_duration_s=1500, description="",
                          hosts=(), transcriber=again)
    assert again.calls == []


def test_context_carried_into_later_prompts(audio, tmp_path) -> None:
    fake = FakeTranscriber()
    pt.transcribe_episode(audio, tmp_path, rss_duration_s=1500, description="",
                          hosts=("Jerusalem Demsas",), transcriber=fake)
    assert "piece0" not in fake.calls[0]
    assert "piece0" in fake.calls[1]


def test_piece_size_ceiling(audio, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pt, "encode_piece", lambda pcm: b"x" * (pt.MAX_PIECE_BYTES + 1))
    with pytest.raises(pt.TranscriptionError, match="bytes"):
        pt.transcribe_episode(audio, tmp_path, rss_duration_s=1500, description="",
                              hosts=(), transcriber=FakeTranscriber())


def test_gemini_transcriber_refuses_non_stop(monkeypatch) -> None:
    class Cand:  # minimal fake response
        finish_reason = type("F", (), {"name": "MAX_TOKENS"})()
    class Resp:
        candidates = [Cand()]
        text = "Jerusalem Demsas: hi"
        usage_metadata = None
        prompt_feedback = None
    class Models:
        def generate_content(self, **kw):
            return Resp()
    class Client:
        models = Models()
        def close(self): pass
    monkeypatch.setattr("pipeline.tts.asr._make_genai_client", lambda timeout_s: Client())
    with pytest.raises(pt.TranscriptionError, match="MAX_TOKENS"):
        pt.GeminiPieceTranscriber()(b"mp3", "prompt")
```

Add one real-ffmpeg test of `encode_piece` (2 s of silence -> bytes starting with an
mp3 frame sync or `ID3`), skipped if `shutil.which("ffmpeg")` is None.

**Step 2:** run -> FAIL.

**Step 3: Implement** `pipeline/podcast_transcribe.py`. Key signatures:

```python
PIECE_PROMPT_VERSION = "1"
ASR_MODEL = "gemini-3.8-flash"
MAX_PIECE_BYTES = 15 * 1024 * 1024
MIN_WORDS_PER_MIN = 80
PIECE_TARGET_S = 600.0
PIECE_SEARCH_S = 20.0
CONTEXT_LINES = 20

class TranscriptionError(RuntimeError): ...

@dataclass(frozen=True)
class PieceResult:
    text: str
    finish_reason: str
    input_tokens: int | None
    output_tokens: int | None

def encode_piece(pcm: bytes) -> bytes: ...          # ffmpeg s16le -> mp3 64k
def build_piece_prompt(*, index: int, count: int, hosts: tuple[str, ...],
                       description: str, context: str | None) -> str: ...
class GeminiPieceTranscriber:                       # (audio, prompt) -> PieceResult
    def __init__(self, *, model: str = ASR_MODEL, timeout_s: float = 300.0): ...
def transcribe_episode(audio_path: Path, work_dir: Path, *, rss_duration_s: int | None,
                       description: str, hosts: tuple[str, ...],
                       transcriber: Callable[[bytes, str], PieceResult],
                       carry_context: bool = True) -> str: ...
```

`GeminiPieceTranscriber.__call__`: `client.models.generate_content(model=...,
contents=[types.Part.from_bytes(data=audio, mime_type="audio/mpeg"), prompt],
config=types.GenerateContentConfig(temperature=0, max_output_tokens=16384,
thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)))`;
wrap any exception as `TranscriptionError(f"asr_error: {exc!r}")`; no candidates ->
`TranscriptionError("asr_empty ...")` (include block reason); finish name != STOP ->
`TranscriptionError(f"asr_incomplete: finish_reason={name}")`. Import
`_make_genai_client` as a module attribute lookup at call time
(`from pipeline.tts import asr as _asr; _asr._make_genai_client(timeout_s)`) so
the conftest patch applies.

Prompt text (keep as one constant; bump `PIECE_PROMPT_VERSION` on any edit):

```
Transcribe this podcast audio verbatim. This is piece {n} of {count} of one episode.

Output one speaker turn per line, formatted as "Speaker Name: words spoken".
Hosts: {hosts}. Episode description (may name guests): {description}

Attribute a turn to a named person ONLY when the voice has been identified by a
self-introduction, by being addressed by name, or by the speaker list carried
over from earlier pieces below. Otherwise label it "Unknown speaker A",
"Unknown speaker B", and keep those labels consistent within this piece. Never
guess who is speaking from the opinions expressed.

Wrap advertisements and sponsor reads in [SPONSOR] ... [/SPONSOR] on their own
lines. No timestamps, headings, or commentary.
{context_block}
```

`context_block` (only for i>0 with carry_context): "Context from the end of the
previous piece (do not repeat it; use it to keep speaker names consistent):\n" +
last 20 lines + "\nSpeakers identified so far: " + comma list of `Name:` prefixes
seen (excluding `Unknown speaker *`).

Atomic writes: write `.tmp` then `os.replace` for both files (copy the pattern of
`report_engine.persist_raw_output`).

**Step 4:** run -> PASS; also `uv run pytest pipeline/tts -q` (nothing in `tts/`
changed; this guards accidental edits). **Step 5: Commit** "Podcast transcription: quiet-point pieces, compressed upload, speaker-labelled Gemini ASR, manifest".

---

### Task 4: Audio-report prompt and instruction builder

**Files:**
- Modify: `pipeline/transcript_report.py` (add constant + `build_audio_report_instruction`; leave `TRANSCRIPT_FEEDS`, `build_report_prompt`, `generate_report`, `maybe_rewrite_transcript` byte-identical in behavior)
- Test: `pipeline/test_transcript_report.py` (exists? check with `ls`; else create `pipeline/test_transcript_report_audio.py`)

**Step 1: Failing tests:**

```python
from pipeline import transcript_report as tr


def test_email_yglesias_prompt_unchanged() -> None:
    p = tr.build_report_prompt(body="B", subject="S", feed_slug="yglesias")
    assert "It ran in" in p and "Slow Boring" in p


def test_audio_instruction_is_its_own_prompt() -> None:
    ins = tr.build_audio_report_instruction(
        transcript="Jerusalem Demsas: hi", title="T", description="D")
    assert "Slow Boring" not in ins
    assert "[SPONSOR]" in ins
    assert "Unknown speaker" in ins
    assert "<script>" in ins and "<summary>" in ins
    assert ins.rstrip().endswith("Jerusalem Demsas: hi")


def test_audio_prompt_not_registered_for_email() -> None:
    assert tr._ARGUMENT_AUDIO_TEMPLATE not in tr.TRANSCRIPT_FEEDS.values()
```

**Step 2:** FAIL. **Step 3: Implement.** Add after `_SILVER_TEMPLATE`:

```python
# The Argument, built from our own ASR of the free public audio (the official
# transcript is paid-only). Separate constant from _YGLESIAS_TEMPLATE on purpose:
# that one describes a Slow Boring email post; this one must tell the writer the
# transcript is machine-made, speaker names may be "Unknown speaker", and
# sponsor reads are marked.
AUDIO_REPORT_PROMPT_VERSION = "1"
_ARGUMENT_AUDIO_TEMPLATE = """\
You are writing a spoken briefing about an episode of The Argument, a
debate-and-conversation podcast with Jerusalem Demsas and Matt Yglesias. Your
listener does NOT want to hear the transcript read aloud — they want a clear,
structured report on what was said.

Episode title: {title}
Episode description: {description}

The transcript below is an automatic transcription of the audio. Speaker
labels are names where the transcriber could identify the voice, and
"Unknown speaker A/B" where it could not. When a claim comes from an unknown
speaker, do not assign it to a named participant — describe it without a name
("one participant argued...", "the other pushed back..."). Skip everything
between [SPONSOR] and [/SPONSOR]; never mention advertisements.

Produce a 5–10 minute spoken briefing (roughly 800–1500 words) covering:

- Who participated (hosts and any guests, with affiliations if stated).
- What topics and questions were debated, in the order that best
  illuminates the conversation.
- The key claims, arguments, and evidence each participant offered,
  with attribution where the transcript supports it.
- Where they disagreed and where they found common ground.
- Concrete details, numbers, and examples that gave the conversation weight.

Write for the ear: plain spoken English, no markdown, no bullet points, no
headers. Do not editorialize beyond what the participants said, and do not
invent facts. Transcription errors are possible: if a word or name looks
garbled, leave it out rather than guess.

TRANSCRIPT:

{transcript}
"""


def build_audio_report_instruction(*, transcript: str, title: str, description: str) -> str:
    prompt = _ARGUMENT_AUDIO_TEMPLATE.format(
        title=title, description=description, transcript=transcript
    )
    return (
        "Read the following transcript and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n" + prompt
    )
```

Note `str.format` on a transcript containing `{`/`}` would raise — the transcript is
a format *argument*, not part of the template, so it is safe; add a test with
`transcript="a {b} c"` to pin that.

**Step 4:** PASS + `uv run pytest pipeline -q -k transcript` . **Step 5: Commit** "Separate report prompt for The Argument audio transcripts".

---

### Task 5: Podcast writer (fetch -> persist -> parse)

**Files:**
- Create: `pipeline/podcast_writer.py`
- Test: `pipeline/test_podcast_writer.py`

Copy the shape of `pipeline/rundown_writer.py:207-252`. Raw output path:
`work_dir / f"raw_writer_output-{key}.txt"` with
`key = sha256(transcript + AUDIO_REPORT_PROMPT_VERSION)[:16]`, so a new transcript
or prompt never reuses an old reply. `parse_report(full, label="argument",
min_chars=2000, require_tags=True)`; on `RuntimeError` unlink the raw file and
re-raise.

**Step 1: Failing tests** (monkeypatch `pipeline.podcast_writer.fetch_report_text`):

```python
from pipeline import podcast_writer as pw

GOOD = "<summary>S</summary><script>" + "word " * 600 + "</script>"


def test_generates_and_persists(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(pw, "fetch_report_text", lambda ins, label: calls.append(1) or GOOD)
    out = pw.write_report(transcript="T", title="t", description="d", work_dir=tmp_path)
    assert out.script.startswith("word")
    assert len(list(tmp_path.glob("raw_writer_output-*.txt"))) == 1
    pw.write_report(transcript="T", title="t", description="d", work_dir=tmp_path)
    assert calls == [1]  # reused


def test_new_transcript_does_not_reuse(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(pw, "fetch_report_text", lambda ins, label: calls.append(1) or GOOD)
    pw.write_report(transcript="T1", title="t", description="d", work_dir=tmp_path)
    pw.write_report(transcript="T2", title="t", description="d", work_dir=tmp_path)
    assert calls == [1, 1]


def test_refusal_unlinks_raw(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pw, "fetch_report_text", lambda ins, label: "no tags here " * 400)
    import pytest
    with pytest.raises(RuntimeError):
        pw.write_report(transcript="T", title="t", description="d", work_dir=tmp_path)
    assert list(tmp_path.glob("raw_writer_output-*.txt")) == []
```

**Step 2:** FAIL. **Step 3:** implement (≈30 lines, import `fetch_report_text`,
`persist_raw_output`, `parse_report`, `ReportOutput` from `pipeline.report_engine`).
**Step 4:** PASS. **Step 5: Commit** "Podcast report writer with transcript-keyed raw-output reuse".

---

### Task 6: `podcast-report --dry-run` CLI

**Files:**
- Modify: `pipeline/__main__.py` (new click group `podcast-report` with a `dry-run` command; follow the existing `@cli.command` style near `jobs`)
- Test: `pipeline/test_podcast_report_cli.py`

`uv run python -m pipeline podcast-report dry-run --source argument --guid G
[--rss-file PATH] [--work-dir DIR] [--no-carry-context]`:
1. Fetch RSS (or read `--rss-file`), find the item by GUID (error if absent).
2. Work dir default `/persist/my-podcasts/podcast-work/dry-run/<sha256(guid)[:16]>/`.
   Write `item.json` (the frozen metadata).
3. `download_enclosure` -> `transcribe_episode(... transcriber=GeminiPieceTranscriber())`
   -> `write_report`.
4. Write `script.txt`, echo the paths, word count, piece count, token totals.
   **Touches no DB, no R2, no feed.**

**Test** with click's `CliRunner`, `--rss-file` pointing at the fixture, and
monkeypatched `download_enclosure`, `transcribe_episode`, `write_report`,
`GeminiPieceTranscriber`; assert output files in a tmp work dir and that
`pipeline.db.StateStore` and `pipeline.r2.R2Client` (check actual import names in
`__main__.py`) were never constructed (monkeypatch them to raise). Unknown GUID ->
non-zero exit with a clear message.

**Commit** "podcast-report dry-run CLI (no publish)".

Then run the full suite: `uv run pytest -q` -> all pass. Fix anything red before
the pilot.

---

### Task 7: Diarization pilot (GATE — owner decision required)

Not code. On the host, from the worktree:

```bash
export GEMINI_API_KEY="$(sudo cat /run/secrets/gemini_api_key)"
# opencode-serve must be up for the writer (same as the daily writers)
uv run python -m pipeline podcast-report dry-run --source argument \
  --guid audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1          # Oct 15, hosts only
uv run python -m pipeline podcast-report dry-run --source argument \
  --guid <Oct 8 GUID from the live feed>                          # guest: Jordan Schneider
```

Get the Oct 8 GUID with:
`curl -sL https://audioboom.com/channels/5173020.rss | grep -A12 "race for superintelligence" | grep guid`.

Measure and record in `docs/plans/2026-10-18-argument-audio-report-pilot.md`:
wall time per stage, peak RSS (`/usr/bin/time -v`), piece count, tokens, decoded vs
RSS duration, ffmpeg warnings. Spot-check by ear (clip with
`ffmpeg -ss <start> -t 60 -i audio.mp3 clip.mp3`): the intro, one middle piece,
the last piece, two piece boundaries — compare speaker labels in `transcript.txt`
to who is actually talking. Count mislabels per spot-check.

Hand the owner both `script.txt` files plus the attribution findings. Outcomes:
- names correct across pieces -> proceed to Phase 2 as designed;
- drift -> tune the context carry-over / prompt, bump `PIECE_PROMPT_VERSION`, re-pilot;
- unfixable -> rely on the report prompt's unknown-speaker hedging, confirm with owner.

**Do not start Phase 2 until the owner signs off on the pilot.**

---

## Phase 2 — outline (expand into full tasks after the pilot)

Each item below becomes a TDD task of the Phase 1 shape. Order matters.

8. **Killable child for download+transcribe.** Run `download_enclosure` +
   `transcribe_episode` in a `spawn`ed child with a wall-clock deadline (size it
   from the pilot's measured time on the 97-min Sep 10 episode, ×2), modelled on
   `pipeline/tts/gemini_phase.py` (`_make_process`, `_Reaper`, scratch input file,
   parent validates outputs). Test with a real spawn whose transcriber blocks
   forever (see `pipeline/tts/test_gemini_phase_spawn.py`).
9. **State table.** `podcast_episodes` in `pipeline/db.py` schema; `StateStore`
   methods: `admit_podcast_episode(item, source, slug, r2_key)` (INSERT OR IGNORE,
   frozen metadata JSON), `next_pending_podcast_episode(now)`,
   `claim/complete/fail_podcast_episode` (backoff 6 h, 4th failure -> errored),
   `reset_podcast_episode(guid)`, `recover_interrupted_podcast_episodes()`
   (in_progress -> pending, called at consumer startup).
10. **Publish identity + reconciliation.** Extract `episode_identity(title,
    feed_slug, date_str) -> (slug, r2_key)` from `publish_script`
    (`script_processor.py:215-217`) and use it in both places. Add a
    `StateStore.get_episode_by_slug(feed_slug, slug)`. Podcast publish: if the row
    exists, skip TTS/insert and call `regenerate_and_upload_feed`, then complete.
    Tests for failure after upload, after insert, during feed upload.
    `date_str` = item pub date (UTC) so backfills key by their real date.
11. **Poller (discovery).** `pipeline/podcast_poller.py:discover(store, now)`:
    fetch RSS per source, admit `eligible_items(since=source.since, now=now)`.
    Track consecutive discovery failures/empty feeds in a small
    `podcast_source_health` table; alert after 4 consecutive. Own try/except.
12. **Executor.** `run_one_podcast_episode(store, r2_client)`: next pending row ->
    claim -> child (task 8) -> `write_report` -> publish (task 10) -> complete;
    failure -> `fail_podcast_episode` and, on transition to errored, one
    `send_alert` attempt (log its boolean result).
13. **Consumer wiring.** `pipeline/consumer.py`: startup recovery; discovery on its
    own 6 h timer (separate from `_last_blog_poll`); at most one
    `run_one_podcast_episode` per loop iteration.
14. **CLI.** `podcast-report enqueue --source S --guid G` (bypasses cutoff; refuses a
    completed row), `podcast-report reset --guid G`, `podcast-report list`.
15. **Docs.** AGENTS.md section "Podcast Audio Reports (The Argument)" + update the
    "Registered feeds" note under Transcript Report Path (the yglesias email path is
    now dormant for The Argument; Slow Boring stopped cross-posting 2026-09-24).
16. **Ship.** PR via `shepherding-pull-requests`; after merge: pull on host,
    `sudo systemctl restart my-podcasts-consumer`, enqueue Sep 10 / Oct 1 / Oct 8 /
    Oct 15, watch them publish one per iteration, confirm Oct 22 unattended.
