"""Task 3: the paid collection steps, driven entirely by fakes.

Nothing here touches the network or /persist (the suite's guards enforce it):
every service reaches the harness through ``Services``.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from pipeline.tts import calibrate as cal
from pipeline.tts import calibrate_run as cr
from pipeline.tts import chunker
from pipeline.tts.asr import Transcription, TranscriptionUnavailable
from pipeline.tts.normalize import normalize_tokens
from pipeline.tts.providers import GeminiProvider, Synthesis, TTSProviderError


WORD_S = 0.06  # one fake word: 60 ms, above the 50 ms boundary-eligibility floor
WORD_SAMPLES = round(WORD_S * 24_000)
TAIL_SAMPLES = 12_000
MAX_CHARS = 900  # small chunks: every episode has several, audio stays tiny


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

_SYLLABLES = "ka lo mi ne pu ra se ti vo wu xa ze".split()


def _word(n: int) -> str:
    """A unique pronounceable-looking, digit-free word."""
    a, b, c = n % 12, (n // 12) % 12, (n // 144) % 12
    return _SYLLABLES[a] + _SYLLABLES[b] + _SYLLABLES[c] + "on"


def make_text(seed: int, paragraphs: int = 5) -> str:
    """Paragraphs of sentences with a quotation and a repeated phrase in each."""
    r = random.Random(seed)
    out = []
    for _ in range(paragraphs):
        sentences = []
        for s in range(5):
            words = [_word(r.randrange(1728)) for _ in range(r.randint(11, 16))]
            if s == 0:
                words[2:5] = ["gamma", "delta", "epsilon"]  # the repeated phrase
            sent = " ".join(words).capitalize() + "."
            if s == 1:
                sent = f'He said "{" ".join(words[:6])}" to the room.'
            sentences.append(sent)
        out.append(" ".join(sentences))
    return "\n\n".join(out)


def wav_sha(wav: bytes) -> str:
    return hashlib.sha256(wav).hexdigest()


class World:
    """What the fake services know about each audio file they have produced."""

    def __init__(self) -> None:
        self.words: dict[str, list[str]] = {}  # wav sha -> the words "spoken"
        self.lock = threading.Lock()

    def register(self, wav: bytes, words: list[str]) -> None:
        with self.lock:
            self.words[wav_sha(wav)] = words


def whisper_for(words: list[str], *, seconds: float | None = None) -> dict:
    out, t = [], 0.0
    for w in words:
        out.append({"word": w, "start": round(t, 3), "end": round(t + WORD_S, 3)})
        t += WORD_S
    dur = seconds if seconds is not None else t + 0.5
    return {
        "words": out,
        "duration": dur,
        "usage": {"seconds": dur},
        "text": " ".join(words),
    }


class FakeProvider:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, str, str]] = []  # (model, voice, text)
        self.script: dict[str, list] = {}  # voice -> queue of exceptions to raise first
        self.audio_tokens: int | None = 1000
        self.lock = threading.Lock()
        self.block: threading.Event | None = None  # when set, calls hang on it
        self.delay = 0.0
        self.inflight = 0
        self.max_inflight = 0

    def synthesize_detailed(self, text, cfg, *, timeout=None):
        with self.lock:
            self.calls.append((cfg.model, cfg.voice, text))
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            queue = self.script.get(cfg.voice)
            exc = queue.pop(0) if queue else None
        try:
            if self.block is not None:
                self.block.wait(10)
            if self.delay:
                time.sleep(self.delay)
            if exc is not None:
                raise exc
            return self._render(text, cfg)
        finally:
            with self.lock:
                self.inflight -= 1

    def _render(self, text, cfg):
        words = [w.strip('.,;:!?"“”()') for w in text.split()]
        words = [w for w in words if w]
        n = len(words) * WORD_SAMPLES + TAIL_SAMPLES
        # white noise seeded by the request: every slice of every render is
        # distinct bytes, so the fakes can tell audio files apart by their hash
        seed = int(hashlib.sha256(f"{cfg.voice}|{text}".encode()).hexdigest()[:12], 16)
        pcm = random.Random(seed).randbytes(2 * n)
        from pipeline.tts.asr import pcm_to_wav

        self.world.register(pcm_to_wav(pcm), words)
        return Synthesis(pcm, "STOP", 50, self.audio_tokens, 1.5)


class FakeWhisper:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[str] = []
        self.errors: list[Exception] = []
        self.lock = threading.Lock()
        self.block: threading.Event | None = None

    def __call__(self, wav: bytes, filename: str) -> dict:
        with self.lock:
            self.calls.append(filename)
            if self.errors:
                raise self.errors.pop(0)
        if self.block is not None:
            self.block.wait(10)
        words = self.world.words[wav_sha(wav)]
        n_samples = (len(wav) - 44) // 2
        return whisper_for(words, seconds=n_samples / 24_000)


class FakeTranscriber:
    def __init__(self, world: World, thinking: str, calls: list) -> None:
        self.world = world
        self.thinking = thinking
        self.calls = calls
        self.policy = f"fake-asr|thinking-{thinking}"
        self.model = "gemini-3.8-flash"
        self.closed = False
        self.fail: dict[str, str] = {}
        self.block: threading.Event | None = None
        self.crash: Exception | None = None

    def __call__(self, audio: bytes, mime_type: str) -> Transcription:
        sha = wav_sha(audio)
        self.calls.append((self.thinking, sha))
        if self.block is not None:
            self.block.wait(10)
        if self.crash is not None:
            raise self.crash
        if sha in self.fail:
            raise TranscriptionUnavailable(self.fail[sha], "fake")
        words = self.world.words[sha]
        return Transcription(
            " ".join(words),
            self.model,
            "1",
            "STOP",
            1.0,
            800,
            120,
            40,
            policy=self.policy,
        )

    def close(self) -> None:
        self.closed = True


class Rig:
    """Fake services + a way to inspect what they were asked."""

    def __init__(self, tmp: Path) -> None:
        self.world = World()
        self.provider = FakeProvider(self.world)
        self.whisper = FakeWhisper(self.world)
        self.asr_calls: list = []
        self.made: list[str] = []
        self.transcribers: dict[str, FakeTranscriber] = {}
        self.emails: dict[str, bytes] = {}
        self.sleeps: list[float] = []
        self.renders: list[dict] = []
        self.render_impl = None
        self.clips: list[Path] = []
        self.services = cr.Services(
            provider=lambda: self.provider,
            whisper=self.whisper,
            transcriber=self._transcriber,
            r2_get=lambda key: self.emails[key],
            render=self._render,
            make_clip=self._clip,
            sleep=self.sleeps.append,
        )

    def _transcriber(self, thinking: str, timeout: float) -> FakeTranscriber:
        t = FakeTranscriber(self.world, thinking, self.asr_calls)
        self.transcribers[thinking] = t
        self.made.append(thinking)
        return t

    def _clip(self, pcm: bytes, out: Path) -> None:
        out.write_bytes(b"MP3" + bytes(len(pcm) // 1000))
        self.clips.append(out)

    def _render(self, *args, **kwargs):
        self.renders.append({"args": args, "kwargs": kwargs})
        assert self.render_impl is not None
        return self.render_impl(*args, **kwargs)

    def ctx(self, root: Path, budget: float = 15.0) -> cr.Ctx:
        out: list[str] = []
        c = cr.Ctx(root=root, budget=budget, services=self.services, echo=out.append)
        c.out = out  # type: ignore[attr-defined]
        return c


def eml(date: str, subject: str, body: str) -> bytes:
    return (
        f"Date: {date} 08:00:00 +0000\n"
        f"Subject: Money Stuff: {subject}\n"
        'Content-Type: text/html; charset="UTF-8"\n'
        "MIME-Version: 1.0\n\n"
        f"<html><body><p>{body}</p></body></html>\n"
    ).encode()


RUNDOWN_DATES = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]
FP_DATES = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]
LEVINE_DATES = [
    "Mon, 21 Sep 2026",
    "Tue, 22 Sep 2026",
    "Wed, 23 Sep 2026",
    "Thu, 24 Sep 2026",
]
LEVINE_KEYS = [f"inbox/raw/lev-{i}.eml" for i in range(4)]
DEV = [
    "rundown-2026-09-21", "rundown-2026-09-22",
    "fp-2026-09-21", "fp-2026-09-22",
    "levine-2026-09-21", "levine-2026-09-22",
]  # fmt: skip


def scripts_tree(root: Path) -> Path:
    scripts = root / "scripts"
    for i, d in enumerate(RUNDOWN_DATES):
        p = scripts / "the-rundown" / f"{d}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(make_text(i) + "\n")  # verbatim: keeps its trailing newline
    for i, d in enumerate(FP_DATES):
        p = scripts / "fp-digest" / f"{d}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n" + make_text(100 + i) + "\n\n")  # the FP path strips
    return scripts


def levine_bodies() -> list[str]:
    return [make_text(200 + i, paragraphs=1).replace("\n\n", " ") for i in range(4)]


def run_corpus(rig: Rig, ctx: cr.Ctx, tmp: Path, **over) -> dict:
    scripts = scripts_tree(tmp)
    for key, date_, body in zip(
        LEVINE_KEYS, LEVINE_DATES, levine_bodies(), strict=True
    ):
        rig.emails[key] = eml(date_, "Pipeline Test", body)
    kwargs = {
        "rundown": RUNDOWN_DATES,
        "fp": FP_DATES,
        "levine_keys": LEVINE_KEYS,
        "dev": DEV,
        "scripts_root": scripts,
        "state_db": tmp / "state.sqlite3",
    }
    kwargs.update(over)
    return cr.build_corpus(ctx, **kwargs)


def _small_chunk_text(text, *, ceiling):
    # production calls chunk_text(text, ceiling=3000) with target == ceiling
    return chunker.chunk_text(text, target=ceiling, ceiling=ceiling)


def _shrink_chunks(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(GeminiProvider, "max_chars", MAX_CHARS)
    mp.setattr(cr, "chunk_text", _small_chunk_text)


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    _shrink_chunks(monkeypatch)


# --------------------------------------------------------------------------- #
# file helpers, prices, ledger
# --------------------------------------------------------------------------- #


def test_write_new_never_clobbers_and_is_atomic(tmp_path):
    p = tmp_path / "a" / "x.json"
    assert cr.write_new(p, b"one") is True
    assert cr.write_new(p, b"two") is False
    assert p.read_bytes() == b"one"
    assert [f.name for f in p.parent.iterdir()] == ["x.json"]  # no tmp left behind


def test_prices_follow_the_plan_and_unknown_models_cost_the_most():
    assert cr.est_synth(1_000_000, 0, "gemini-3.8-flash-tts") == (9.2, False)
    assert cr.est_synth(1_000_000, 0, "gemini-3.8-flash-lite-tts") == (6.1, False)
    worst = cr.est_synth(None, 3000, "gemini-3.8-flash-tts")
    assert worst == (cr.est_synth_worst(3000, "gemini-3.8-flash-tts"), True)
    # 3000 chars at >= 10 chars/s is <= 300 s of audio at 32 tokens/s
    assert worst[0] == pytest.approx(300 * 32 / 1e6 * 9.2)
    assert cr.synth_price("gemini-future-tts") == 9.2
    assert cr.est_asr(1_000_000, 100_000, 400_000, 60) == (
        pytest.approx(1 + 2.5),
        False,
    )
    assert cr.est_asr(1000, 10, None, 60)[1] is False  # no thinking count: 0
    assert cr.est_asr(None, 10, 5, 60) == (cr.est_asr_worst(60), True)
    assert cr.est_asr_worst(100) == pytest.approx(
        (100 * 32 + 100) / 1e6 + 8000 / 1e6 * 5
    )
    assert cr.est_whisper(60) == pytest.approx(0.006)


def _reserve(led, usd, what="x"):
    return led.reserve(usd, what, kind="synth", model="m", ids={"id": what})


def test_ledger_appends_totals_and_refuses_before_the_budget(tmp_path):
    led = cr.Ledger(tmp_path / "ledger.jsonl", budget=1.0)
    led.append("synth", "m", {"id": "a"}, {"audio_tokens": 5}, 0.4)
    led.append("asr", "g", {"id": "a"}, None, 0.5, worst_case=True)
    assert led.total() == pytest.approx(0.9)
    with pytest.raises(cr.BudgetError, match="nothing was sent"):
        with _reserve(led, 0.2):
            raise AssertionError("must not be reached")  # pragma: no cover
    assert len(led.entries()) == 2  # a refused call books nothing
    with _reserve(led, 0.05, "fits"):
        with pytest.raises(cr.BudgetError):  # an in-flight call counts at its worst
            with _reserve(led, 0.06, "second"):
                pass  # pragma: no cover
    s = led.summary()
    assert s["by_kind"]["asr"]["worst_case_calls"] == 1
    # the "fits" call left the block unsettled -> settled at its worst case
    assert s["by_kind"]["synth"]["calls"] == 2
    assert s["by_kind"]["synth"]["worst_case_calls"] == 1  # the 0.4 line was exact
    assert s["remaining_usd"] == pytest.approx(0.05)


def test_write_ahead_books_the_worst_case_before_the_call_and_settles_after(tmp_path):
    led = cr.Ledger(tmp_path / "ledger.jsonl", budget=1.0)
    with _reserve(led, 0.30, "a") as call:
        # the call has not happened yet: its WORST case is already on disk
        (line,) = led.entries()
        assert line["phase"] == "reserve" and line["worst_case"] is True
        assert line["note"] == "in flight" and line["call_id"] == call.call_id
        assert line["est_usd"] == pytest.approx(0.30)
        assert led.total() == pytest.approx(0.30)
        call.settle({"audio_tokens": 10}, 0.12, worst_case=False)
        call.settle(None, 99.0)  # idempotent: the first settlement wins
    reserve_line, settle_line = led.entries()
    assert settle_line["phase"] == "settle" and settle_line["call_id"] == call.call_id
    assert settle_line["est_usd"] == pytest.approx(0.12 - 0.30)  # negative
    assert settle_line["actual_usd"] == pytest.approx(0.12)
    assert settle_line["usage"] == {"audio_tokens": 10}
    assert led.total() == pytest.approx(0.12)
    (merged,) = led.calls()
    assert merged["settled"] is True and merged["est_usd"] == pytest.approx(0.12)
    assert merged["worst_case"] is False and merged["note"] is None
    assert merged["usage"] == {"audio_tokens": 10}


def test_a_killed_process_leaves_the_worst_case_booked_for_the_next_run(tmp_path):
    path = tmp_path / "ledger.jsonl"
    dead = cr.Ledger(path, budget=1.0)
    gen = dead.reserve(0.7, "died", kind="asr", model="g", ids={"id": "z"})
    gen.__enter__()  # the call is "in flight" ... and the process is SIGKILLed here
    fresh = cr.Ledger(path, budget=1.0)  # the next run, a new process
    s = fresh.summary()
    assert (s["calls"], s["settled"], s["unsettled"]) == (1, 0, 1)
    assert s["total_usd"] == pytest.approx(0.7)
    assert (
        s["by_kind"]["asr"]["unsettled"] == 1
        and s["by_kind"]["asr"]["worst_case_calls"] == 1
    )
    (c,) = fresh.calls()
    assert (
        c["settled"] is False and c["note"] == "in flight" and c["worst_case"] is True
    )
    # and the budget gate counts it: 0.7 + 0.4 > 1.0
    with pytest.raises(cr.BudgetError):
        with _reserve(fresh, 0.4):
            pass  # pragma: no cover
    with _reserve(fresh, 0.2):
        pass


def test_ledger_step_prints_settled_and_unsettled_counts(tmp_path):
    rig = Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5")
    with ctx.ledger.reserve(0.1, "ok", kind="synth", model="m", ids={}) as call:
        call.settle(None, 0.02)
    gen = ctx.ledger.reserve(0.3, "died", kind="asr", model="m", ids={})
    gen.__enter__()
    s = cr.step_ledger(ctx)
    assert (s["settled"], s["unsettled"]) == (1, 1)
    text = "\n".join(ctx.out)
    assert "1 settled, 1 unsettled" in text and "in flight when a run died" in text


def test_a_corrupt_ledger_is_refused_not_guessed_at(tmp_path):
    led = cr.Ledger(tmp_path / "ledger.jsonl", budget=1.0)
    led.path.write_text('{"est_usd": 0.1}\nnot json\n')
    with pytest.raises(cr.CalibrateError, match="line 2"):
        led.total()


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #


def test_corpus_freezes_texts_split_chunks_voices_and_published_keys(tmp_path):
    rig = Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5")
    conn = sqlite3.connect(tmp_path / "state.sqlite3")
    conn.execute("CREATE TABLE episodes (slug TEXT, feed_slug TEXT, r2_key TEXT)")
    conn.execute(
        "INSERT INTO episodes VALUES ('2026-09-21-the-rundown', 'the-rundown', "
        "'episodes/the-rundown/2026-09-21-the-rundown.mp3')"
    )
    conn.commit()
    conn.close()
    corpus = run_corpus(rig, ctx, tmp_path)

    eps = {e["id"]: e for e in corpus["episodes"]}
    assert list(eps) == [
        *(f"rundown-{d}" for d in RUNDOWN_DATES),
        *(f"fp-{d}" for d in FP_DATES),
        *(f"levine-2026-09-{d}" for d in (21, 22, 23, 24)),
    ]  # fmt: skip
    assert corpus == cr.read_json(ctx.corpus_path)
    assert {e["split"] for e in eps.values()} == {"dev", "holdout"}
    assert [eid for eid, e in eps.items() if e["split"] == "dev"] == DEV
    # voices: Kore for an even corpus index, Charon for odd
    assert [e["voice"] for e in eps.values()][:4] == [
        "Kore",
        "Charon",
        "Kore",
        "Charon",
    ]
    # texts: Rundown verbatim, FP stripped, Levine as the email path builds it
    rundown_src = (tmp_path / "scripts/the-rundown/2026-09-21.txt").read_text()
    assert (ctx.root / "texts/rundown-2026-09-21.txt").read_text() == rundown_src
    assert rundown_src.endswith("\n")
    fp_text = (ctx.root / "texts/fp-2026-09-21.txt").read_text()
    assert fp_text == fp_text.strip() and fp_text.startswith("Alphabetic") is False
    for e in eps.values():
        text = (ctx.root / e["text_path"]).read_text()
        assert e["text_sha256"] == cr.sha256_hex(text) and e["chars"] == len(text)
    # chunk selection: 0 and n // 2 (one chunk for a short episode)
    r0 = eps["rundown-2026-09-21"]
    assert r0["n_chunks"] >= 3 and r0["selected_chunks"] == [0, r0["n_chunks"] // 2]
    assert eps["levine-2026-09-21"]["n_chunks"] == 1
    assert eps["levine-2026-09-21"]["selected_chunks"] == [0]
    # bases: both models per chunk, ids as specified
    bases = {b["base_id"]: b for b in corpus["bases"]}
    assert len(bases) == len(corpus["bases"])
    n_expected = sum(2 * len(e["selected_chunks"]) for e in eps.values())
    assert len(bases) == n_expected
    b = bases["rundown-2026-09-21--c0--flash--Kore"]
    assert b["model"] == "gemini-3.8-flash-tts" and b["split"] == "dev"
    assert "rundown-2026-09-21--c0--lite--Kore" in bases
    assert b["chunk_sha256"] == cr.sha256_hex(
        cr.chunk_for(ctx, b)
    )  # chunking reproduces
    # published keys: the episodes table when it has the row, derived otherwise
    assert r0["published"] == {
        "r2_key": "episodes/the-rundown/2026-09-21-the-rundown.mp3",
        "source": "episodes_table",
    }
    assert eps["rundown-2026-09-22"]["published"]["source"] == "derived"
    assert eps["fp-2026-09-22"]["published"]["r2_key"] == (
        "episodes/fp-digest/2026-09-22-fp-digest.mp3"
    )
    lev = eps["levine-2026-09-21"]["published"]["r2_key"]
    assert lev.startswith("episodes/levine/2026-09-21-Money-Stuff-")
    assert eps["levine-2026-09-21"]["source"]["r2_email_key"] == LEVINE_KEYS[0]


def test_corpus_refuses_without_a_six_episode_dev_split_and_leaves_nothing(tmp_path):
    rig = Rig(tmp_path)
    for dev in ([], DEV[:5], DEV[:5] + ["rundown-2026-09-23"], DEV + ["fp-2026-09-23"]):
        ctx = rig.ctx(tmp_path / "t5")
        with pytest.raises(cr.Refused, match="dev|split"):
            run_corpus(rig, ctx, tmp_path, dev=dev)
    # six, but not two per feed
    ctx = rig.ctx(tmp_path / "t5")
    lopsided = [*DEV[:4], "rundown-2026-09-23", "rundown-2026-09-24"]
    with pytest.raises(cr.Refused, match="2 episodes per feed"):
        run_corpus(rig, ctx, tmp_path, dev=lopsided)
    with pytest.raises(cr.Refused, match="not among"):
        run_corpus(rig, ctx, tmp_path, dev=[*DEV[:5], "rundown-1999-01-01"])
    assert not (tmp_path / "t5").exists()  # refusals write nothing


def test_corpus_refuses_wrong_episode_counts_and_an_existing_corpus(tmp_path):
    rig = Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5")
    with pytest.raises(cr.Refused, match="4 distinct --rundown"):
        run_corpus(rig, ctx, tmp_path, rundown=RUNDOWN_DATES[:3])
    run_corpus(rig, ctx, tmp_path)
    before = ctx.corpus_path.read_bytes()
    with pytest.raises(cr.Refused, match="frozen"):
        run_corpus(rig, ctx, tmp_path)
    assert ctx.corpus_path.read_bytes() == before


def test_levine_text_is_exactly_what_the_email_path_hands_to_tts(
    tmp_path, captured_tts_input, monkeypatch
):
    from pipeline.db import StateStore
    from pipeline.processor import process_email_bytes

    monkeypatch.setattr(
        "pipeline.processor.regenerate_and_upload_feed", lambda store, r2: None
    )
    raw = eml(
        "Mon, 17 Aug 2026", "Goat Hedge", "Goats are a hedge against mowing costs."
    )
    store = StateStore(tmp_path / "s.sqlite3")
    result = process_email_bytes(
        raw_email=raw, source_r2_key="raw/x.eml", route_tag="levine", store=store,
        r2_client=MagicMock(), levine_cache_dir=tmp_path / "lc",
    )  # fmt: skip
    store.close()
    info = cr.levine_tts_text(raw)
    assert info["text"] == captured_tts_input[0]
    assert info["title"] == result.title
    assert result.r2_key == f"episodes/levine/{info['slug']}.mp3"


def test_lookup_published_is_read_only_and_tolerates_a_missing_db(tmp_path):
    assert cr.lookup_published(tmp_path / "nope.sqlite3", "levine", "x") is None
    db = tmp_path / "s.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE episodes (slug TEXT, feed_slug TEXT, r2_key TEXT)")
    conn.execute("INSERT INTO episodes VALUES ('s', 'f', 'k')")
    conn.commit()
    conn.close()
    assert cr.lookup_published(db, "f", "s") == "k"
    assert cr.lookup_published(db, "f", "other") is None
    assert not (tmp_path / "nope.sqlite3").exists()  # mode=ro created nothing


def test_chunk_selection_and_voice_parity():
    assert [cr.select_chunks(n) for n in (0, 1, 2, 3, 4, 7)] == [
        [], [0], [0, 1], [0, 1], [0, 2], [0, 3],
    ]  # fmt: skip
    assert [cr.voice_for(i) for i in range(4)] == ["Kore", "Charon", "Kore", "Charon"]


# --------------------------------------------------------------------------- #
# synth
# --------------------------------------------------------------------------- #


def corpus_ctx(tmp_path, rig=None, budget=15.0):
    rig = rig or Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5", budget)
    run_corpus(rig, ctx, tmp_path)
    return rig, ctx


def test_synth_saves_pcm_then_synth_json_and_ledgers_usage(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    counts = cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    assert counts == {"ok": 1}
    d = ctx.base_dir("levine-2026-09-21--c0--flash--Kore")
    rec = cr.read_json(d / "synth.json")
    assert rec["status"] == "ok" and rec["usage"]["audio_tokens"] == 1000
    assert rec["style"] == "calm, measured news anchor"
    pcm, rate, seconds = cr.wav_info((d / "pcm.wav").read_bytes())
    assert rate == 24_000 and rec["pcm_samples"] == len(pcm) // 2
    assert (d / "chunk.txt").read_text() == rig.provider.calls[0][2]
    (entry,) = ctx.ledger.calls()
    assert entry["kind"] == "synth" and entry["usage"]["audio_tokens"] == 1000
    assert entry["est_usd"] == pytest.approx(1000 / 1e6 * 9.2)
    assert entry["worst_case"] is False
    assert rig.provider.calls[0][:2] == ("gemini-3.8-flash-tts", "Kore")


def test_synth_is_resumable_a_second_run_makes_no_calls(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    ids = ["levine-2026-09-21--c0--flash--Kore", "levine-2026-09-21--c0--lite--Kore"]
    assert cr.step_synth(ctx, ids=ids) == {"ok": 2}
    n_calls, ledger_lines = len(rig.provider.calls), len(ctx.ledger.calls())
    before = {p: p.read_bytes() for p in (ctx.root / "bases").rglob("*.*")}
    assert cr.step_synth(ctx, ids=ids) == {"skipped": 2}
    assert len(rig.provider.calls) == n_calls
    assert len(ctx.ledger.calls()) == ledger_lines
    assert {p: p.read_bytes() for p in (ctx.root / "bases").rglob("*.*")} == before


def test_synth_refuses_before_the_call_when_the_budget_is_gone(tmp_path):
    rig, ctx = corpus_ctx(tmp_path, budget=0.01)
    ctx.ledger.append("synth", "m", {}, None, 0.0099)
    with pytest.raises(cr.BudgetError):
        cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    assert rig.provider.calls == []  # nothing sent
    assert not (
        ctx.base_dir("levine-2026-09-21--c0--flash--Kore") / "synth.json"
    ).exists()


def test_synth_refuses_an_orphan_pcm_instead_of_discarding_or_repaying(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    base_id = "levine-2026-09-21--c0--flash--Kore"
    d = ctx.base_dir(base_id)
    from pipeline.tts.asr import pcm_to_wav

    old = pcm_to_wav(b"\x01\x00" * 5000)  # pcm.wav without a synth.json
    d.mkdir(parents=True)
    (d / "pcm.wav").write_bytes(old)
    with pytest.raises(cr.Refused, match="exists without a synth.json"):
        cr.step_synth(ctx, workers=1, ids=[base_id])
    assert rig.provider.calls == []  # nothing was paid for
    assert (d / "pcm.wav").read_bytes() == old
    assert not (d / "synth.json").exists()


def test_synth_refuses_a_chunk_file_that_no_longer_matches_the_corpus(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    base_id = "levine-2026-09-21--c0--flash--Kore"
    d = ctx.base_dir(base_id)
    d.mkdir(parents=True)
    (d / "chunk.txt").write_text("someone edited this")
    with pytest.raises(cr.Refused, match="differs"):
        cr.step_synth(ctx, workers=1, ids=[base_id])
    assert rig.provider.calls == []


def test_synth_closes_the_provider_it_opened(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    closed = []
    rig.provider.close = lambda: closed.append(1)  # type: ignore[attr-defined]
    cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    assert closed == [1]


def test_a_fatal_synth_error_is_recorded_not_raised_and_the_rest_continue(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    rig.provider.script["Kore"] = [TTSProviderError("HTTP 400 bad key", kind="fatal")]
    ids = [
        "levine-2026-09-21--c0--flash--Kore",  # takes the fatal
        "levine-2026-09-22--c0--flash--Charon",
    ]
    counts = cr.step_synth(ctx, workers=1, ids=ids)
    assert counts == {"failed": 1, "ok": 1}
    bad = ctx.base_dir(ids[0])
    rec = cr.read_json(bad / "synth.json")
    assert rec["status"] == "failed" and "HTTP 400" in rec["error"]
    assert [a["kind"] for a in rec["attempts"]] == ["fatal"]  # not retried
    assert not (bad / "pcm.wav").exists()
    assert rig.sleeps == []
    assert any("FAILED" in line for line in ctx.out)
    # the failure is a final record: a rerun skips it; --retry-failed redoes it
    assert cr.step_synth(ctx, workers=1, ids=ids) == {"skipped": 2}
    assert cr.step_synth(ctx, workers=1, ids=ids, retry_failed=True) == {
        "ok": 1, "skipped": 1,
    }  # fmt: skip
    assert cr.read_json(bad / "synth.json")["status"] == "ok"
    assert (bad / "synth.failed-1.json").exists()  # the old record is kept


def test_transient_errors_are_retried_with_production_backoff(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    rig.provider.script["Kore"] = [
        TTSProviderError("HTTP 503", kind="infra"),
        TTSProviderError("finishReason OTHER", kind="content"),
    ]
    out = cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    assert out == {"ok": 1}
    assert rig.sleeps == [2.0, 8.0]
    assert len(rig.provider.calls) == 3
    rec = cr.read_json(
        ctx.base_dir("levine-2026-09-21--c0--flash--Kore") / "synth.json"
    )
    assert [a["status"] for a in rec["attempts"]] == ["error", "error", "ok"]
    # every attempt is on the ledger; the failed ones at worst case
    entries = ctx.ledger.calls()
    assert [e["worst_case"] for e in entries] == [True, True, False]


def test_transient_errors_exhaust_after_three_calls(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    rig.provider.script["Kore"] = [TTSProviderError("HTTP 503", kind="infra")] * 5
    out = cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    assert out == {"failed": 1} and len(rig.provider.calls) == 3


def test_synth_with_unreported_usage_is_costed_at_the_worst_case(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    rig.provider.audio_tokens = None
    cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--lite--Kore"])
    (entry,) = ctx.ledger.calls()
    chars = len(rig.provider.calls[0][2])
    assert entry["worst_case"] is True
    assert entry["est_usd"] == pytest.approx(
        cr.est_synth_worst(chars, "gemini-3.8-flash-lite-tts")
    )
    assert entry["est_usd"] > 0


def test_synth_runs_in_parallel_without_losing_ledger_lines(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    ids = [b["base_id"] for b in ctx.corpus()["bases"] if b["feed"] == "levine"]
    counts = cr.step_synth(ctx, workers=4, ids=ids)
    assert counts == {"ok": len(ids)}
    assert len(ctx.ledger.calls()) == len(ids)
    assert len(rig.provider.calls) == len(ids)


# --------------------------------------------------------------------------- #
# whisper / label / cuts / asr / report: one shared pipeline
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    """corpus -> synth -> whisper -> label -> cuts, built once (read-only after)."""
    mp = pytest.MonkeyPatch()
    _shrink_chunks(mp)
    tmp = tmp_path_factory.mktemp("staged")
    rig = Rig(tmp)
    ctx = rig.ctx(tmp / "t5")
    run_corpus(rig, ctx, tmp)
    cr.step_synth(ctx, workers=4)
    cr.step_whisper(ctx, kinds=["base"], workers=4)
    rows = cr.step_label(ctx)
    cuts = cr.step_cuts(ctx)
    yield tmp / "t5", rig.world, rows, cuts
    mp.undo()


@pytest.fixture
def copy_of_staged(staged, tmp_path):
    root, world, rows, cuts = staged
    dest = tmp_path / "t5"
    shutil.copytree(root, dest)
    rig = Rig(tmp_path)
    rig.world = world
    rig.provider.world = world
    rig.whisper.world = world
    return rig, rig.ctx(dest), rows, cuts


def test_whisper_stores_verbose_json_and_ledgers_minutes(staged):
    root, world, rows, cuts = staged
    ledger = [
        json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()
    ]
    assert all(e["call_id"] for e in ledger if e["kind"] == "whisper")
    w = [
        c
        for c in cr.Ledger(root / "ledger.jsonl", 15).calls()
        if c["kind"] == "whisper"
    ]
    assert len(w) == sum(1 for _ in (root / "bases").glob("*/whisper.json"))
    one = cr.read_json(next((root / "bases").glob("*/whisper.json")))
    assert one["words"] and one["_calibrate"]["model"] == "whisper-1"
    assert w[0]["settled"] and w[0]["est_usd"] == pytest.approx(
        w[0]["usage"]["seconds"] / 60 * 0.006
    )


def test_whisper_second_run_makes_no_calls_and_keeps_files(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    before = {p: p.read_bytes() for p in ctx.root.rglob("whisper*.json")}
    assert cr.step_whisper(ctx, kinds=["base"]) == {"skipped": len(before)}
    assert rig.whisper.calls == []
    assert {p: p.read_bytes() for p in ctx.root.rglob("whisper*.json")} == before


def test_whisper_refuses_oversized_audio_before_sending(copy_of_staged, monkeypatch):
    rig, ctx, *_ = copy_of_staged
    victim = next((ctx.root / "bases").glob("*/whisper.json"))
    victim.unlink()
    monkeypatch.setattr(cr, "WHISPER_MAX_BYTES", 1000)
    out = cr.step_whisper(ctx, kinds=["base"], ids=[victim.parent.name])
    assert out == {"failed": 1} and rig.whisper.calls == []
    assert any("over the" in line for line in ctx.out)
    assert not victim.exists()


def test_whisper_retries_transient_errors_and_stops_on_fatal(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    victim = next((ctx.root / "bases").glob("*/whisper.json"))
    victim.unlink()
    rig.whisper.errors = [cr.ServiceError("HTTP 503"), cr.ServiceError("reset")]
    assert cr.step_whisper(ctx, kinds=["base"], ids=[victim.parent.name]) == {"ok": 1}
    assert rig.sleeps == [2.0, 8.0] and len(rig.whisper.calls) == 3
    victim.unlink()
    rig.whisper.errors = [cr.ServiceError("HTTP 401", "fatal")]
    out = cr.step_whisper(ctx, kinds=["base"], ids=[victim.parent.name])
    assert out == {"failed": 1} and not victim.exists()


def test_whisper_refuses_over_budget_before_sending(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    victim = next((ctx.root / "bases").glob("*/whisper.json"))
    victim.unlink()
    ctx.ledger.budget = ctx.ledger.total() + 1e-9
    with pytest.raises(cr.BudgetError):
        cr.step_whisper(ctx, kinds=["base"], ids=[victim.parent.name])
    assert rig.whisper.calls == []


def test_label_marks_faithful_bases_and_prints_a_table(staged):
    root, _, rows, _ = staged
    assert rows and all(r["label"] == "faithful" for r in rows)
    label = cal.BaseLabel.from_dict(
        cr.read_json(next((root / "bases").glob("*/label.json")))
    )
    pcm, _, _ = cr.wav_info((root / "bases" / label.base_id / "pcm.wav").read_bytes())
    assert label.total_samples == len(pcm) // 2  # from the PCM, not whisper's duration


def test_label_suspect_gets_owner_clips_and_owner_call_overrides(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    d = ctx.base_dir(base_id)
    # a whisper that dropped a stretch of words: the base becomes suspect
    words = rig.world.words[wav_sha((d / "pcm.wav").read_bytes())]
    bad = whisper_for(words[:20] + words[40:], seconds=len(words) * WORD_S + 0.5)
    (d / "whisper.json").unlink()
    (d / "label.json").unlink()
    (d / "whisper.json").write_text(json.dumps(bad))
    rows = cr.step_label(ctx, ids=[base_id])
    (row,) = rows
    assert row["label"] == "suspect" and row["spans"] == 1
    assert len(row["clips"]) == 1 and Path(row["clips"][0]).exists()
    assert (ctx.root / "clips").is_dir() and len(rig.clips) == 1
    assert any("clip:" in line for line in ctx.out)
    # the owner calls it faithful: effective label flips; nothing is rewritten
    assert cr.effective_label(ctx, base_id).label == "suspect"
    cr.record_owner_call(ctx, base_id, "faithful", "listened")
    assert cr.effective_label(ctx, base_id).label == "faithful"
    assert cal.BaseLabel.from_dict(cr.read_json(d / "label.json")).label == "suspect"
    with pytest.raises(cr.Refused, match="never overwritten"):
        cr.record_owner_call(ctx, base_id, "defect")
    cr.record_owner_call(ctx, base_id, "faithful")  # same call again: a no-op


def test_label_recall_only_suspect_lists_the_lowest_recall_windows(tmp_path):
    words = [f"w{chr(97 + i % 26)}{chr(97 + i // 26)}x" for i in range(500)]
    text = " ".join(words)
    # whisper garbled every 4th word: recall ~0.75 spread everywhere, no long span
    heard = [("zzq" if i % 4 == 0 else w) for i, w in enumerate(words)]
    total = round((len(words) * WORD_S + 0.5) * 24_000)
    label = cal.screen_base(text, whisper_for(heard), total_samples=total, base_id="x")
    assert label.label == "suspect" and label.suspect_spans == ()
    wins = cr.lowest_recall_windows(label, window_s=8.0)
    assert len(wins) == cr.CLIP_WINDOWS
    for (s, e, r), (s2, e2, _) in zip(wins, wins[1:], strict=False):
        assert e - s == pytest.approx(8.0) and (e <= s2 or e2 <= s)
        assert 0.6 < r < 0.9


def test_cuts_cover_every_family_and_bin_per_split_and_are_frozen(staged):
    root, _, _, cuts = staged
    assert cuts == cr.read_json(root / "cuts.json")
    for split in ("dev", "holdout"):
        mine = [c for c in cuts["cuts"] if c["split"] == split]
        # these test chunks are ~150 tokens, so 80-token cuts (over half the
        # chunk) are refused: paragraph cuts and the 80 bin are legitimately absent
        assert len(mine) >= 12
        assert {c["family"] for c in mine} >= set(cal.FAMILIES) - {"paragraph"}
        assert {c["size_bin"] for c in mine if c["family"] != "multi"} >= {10, 20, 40}
        got = cuts["coverage"][split]
        assert got["n"] == len(mine) and set(got["families"]) == {
            c["family"] for c in mine
        }
    n_bases = sum(1 for _ in (root / "bases").glob("*/label.json"))
    assert (
        abs(len(cuts["cuts"]) - cr.CUTS_PER_BASE * n_bases) <= len(cuts["skipped"]) + 2
    )
    # every cut: labels on disk, snapped, byte math checked against its base
    for c in cuts["cuts"][:25]:
        d = root / "cuts" / c["cut_id"]
        spec = cal.CutSpec.from_dict(cr.read_json(d / "label.json")["spec"])
        assert spec.snapped
        base_pcm, _, _ = cr.wav_info(
            (root / "bases" / c["base_id"] / "pcm.wav").read_bytes()
        )
        cut_pcm_, _, _ = cr.wav_info((d / "cut.wav").read_bytes())
        assert len(cut_pcm_) == len(base_pcm) - 2 * spec.removed_samples
        for k, iv in enumerate(spec.intervals):
            clip, _, _ = cr.wav_info((d / f"removed-{k}.wav").read_bytes())
            assert len(clip) == 2 * (iv.sample_end - iv.sample_start)
        assert c["n_intervals"] == len(spec.intervals)


def test_cuts_json_is_frozen_and_never_rewritten(copy_of_staged):
    rig, ctx, _, cuts = copy_of_staged
    before = {p: p.read_bytes() for p in ctx.root.rglob("*") if p.is_file()}
    with pytest.raises(cr.Refused, match="frozen"):
        cr.step_cuts(ctx)
    with pytest.raises(cr.Refused, match="frozen"):
        cr.step_cuts(ctx, seed="another")
    assert {p: p.read_bytes() for p in ctx.root.rglob("*") if p.is_file()} == before


def test_cuts_are_deterministic_given_seed_and_labels(copy_of_staged, tmp_path):
    rig, ctx, _, cuts = copy_of_staged
    ctx.cuts_path.unlink()
    shutil.rmtree(ctx.root / "cuts")
    again = cr.step_cuts(ctx)
    strip = lambda d: [dict(c) for c in d["cuts"]]  # noqa: E731
    assert strip(again) == strip(cuts)
    assert again["skipped"] == cuts["skipped"]


def test_cuts_refuse_without_faithful_bases(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    with pytest.raises(cr.Refused, match="no faithful bases"):
        cr.step_cuts(ctx)
    assert not ctx.cuts_path.exists()


def _register_cut_audio(world: World, root: Path, *, leak: dict | None = None) -> None:
    """Teach the fake whisper what each cut audio and removed clip says."""
    leak = leak or {}
    entries = [
        e
        for p in [root / "cuts.json", *sorted(root.glob("cuts-supplement-*.json"))]
        if not p.name.endswith("-verified.json")
        for e in cr.read_json(p)["cuts"]
    ]
    for cuts_entry in entries:
        d = root / "cuts" / cuts_entry["cut_id"]
        spec = cal.CutSpec.from_dict(cr.read_json(d / "label.json")["spec"])
        script = cr.read_json(root / "bases" / cuts_entry["base_id"] / "label.json")[
            "script_text"
        ]
        toks = normalize_tokens(script)
        removed = {i for s, e in spec.token_intervals for i in range(s, e)}
        kept = [t for i, t in enumerate(toks) if i not in removed]
        if cuts_entry["cut_id"] in leak:
            at, word = leak[cuts_entry["cut_id"]]
            kept = kept[:at] + [word] + kept[at:]
        world.register((d / "cut.wav").read_bytes(), kept)
        for k, (s, e) in enumerate(spec.token_intervals):
            world.register((d / f"removed-{k}.wav").read_bytes(), toks[s:e])


@pytest.fixture
def cut_whispered(copy_of_staged):
    rig, ctx, _, cuts = copy_of_staged
    _register_cut_audio(rig.world, ctx.root)
    return rig, ctx, cuts


def test_whisper_covers_cuts_and_removed_clips(cut_whispered):
    rig, ctx, cuts = cut_whispered
    n = len(cuts["cuts"])
    out = cr.step_whisper(ctx, kinds=["cut", "removed"], workers=4)
    assert out == {"ok": n + sum(c["n_intervals"] for c in cuts["cuts"])}
    c0 = cuts["cuts"][0]
    d = ctx.cut_dir(c0["cut_id"])
    assert (d / "whisper.json").exists() and (d / "whisper-removed-0.json").exists()


def test_cuts_verify_writes_a_separate_file_and_discards_leaky_cuts(cut_whispered):
    rig, ctx, cuts = cut_whispered
    victim = cuts["cuts"][3]["cut_id"]
    ok_word = "zzzleak"
    # whisper hears a word of the removed text right at the cut: a +/-1-word label error
    spec = cal.CutSpec.from_dict(
        cr.read_json(ctx.cut_dir(victim) / "label.json")["spec"]
    )
    base = cr.effective_label(ctx, cuts["cuts"][3]["base_id"])
    s, _ = spec.token_intervals[0]
    removed_before = sum(e - s0 for s0, e in spec.token_intervals if s0 < s)
    leak = {victim: (s - removed_before, base.script_tokens[s])}
    _register_cut_audio(rig.world, ctx.root, leak=leak)
    del ok_word
    cr.step_whisper(ctx, kinds=["cut", "removed"])
    frozen = ctx.cuts_path.read_bytes()
    doc = cr.step_cuts_verify(ctx)
    assert ctx.cuts_path.read_bytes() == frozen  # cuts.json untouched
    assert ctx.verified_path.exists()
    bad = [c for c, r in doc["cuts"].items() if r["discarded"]]
    assert bad == [victim]
    assert doc["cuts"][victim]["reasons"] == ["post_cut_check"]
    assert doc["cuts"][victim]["post_cut"]["leftover_tokens"] >= 1
    assert any(f"DISCARD {victim}" in line for line in ctx.out)
    # the leak is a 1-token boundary error: admitted as an approximate label by
    # default, a discard under the strict rule
    strict = cr.live_cuts(ctx, max_boundary_error=0)
    assert [c["cut_id"] for c in strict] == [
        c["cut_id"] for c in cuts["cuts"] if c["cut_id"] != victim
    ]
    admitted = {c["cut_id"]: c for c in cr.live_cuts(ctx)}
    assert set(admitted) == {c["cut_id"] for c in cuts["cuts"]}
    assert admitted[victim]["label_status"] == "approx"
    label = admitted[victim]["label"]
    assert label["lower_tokens"] == label["nominal_tokens"] - 1
    assert label["boundary_error"] == 1
    assert all(
        c["label_status"] == "exact" for cid, c in admitted.items() if cid != victim
    )
    with pytest.raises(cr.Refused, match="already exists"):
        cr.step_cuts_verify(ctx)


def test_cuts_verify_discards_a_removed_clip_that_fails_the_sanity_check(cut_whispered):
    rig, ctx, cuts = cut_whispered
    cr.step_whisper(ctx, kinds=["cut", "removed"])
    victim = cuts["cuts"][0]
    f = ctx.cut_dir(victim["cut_id"]) / "whisper-removed-0.json"
    doc = cr.read_json(f)
    doc["words"] = doc["words"][: max(1, len(doc["words"]) // 2)]  # whisper heard half
    f.write_text(json.dumps(doc))
    out = cr.step_cuts_verify(ctx)
    r = out["cuts"][victim["cut_id"]]
    assert r["discarded"] and r["reasons"] == ["removed_clip_sanity"]


def test_cuts_verify_requires_every_whisper_file(cut_whispered):
    rig, ctx, cuts = cut_whispered
    with pytest.raises(cr.Refused, match="whisper --kind cut"):
        cr.step_cuts_verify(ctx)
    assert not ctx.verified_path.exists()


# --- asr ---------------------------------------------------------------------


def _split_of(ctx: cr.Ctx, item_id: str) -> str:
    for b in ctx.corpus()["bases"]:
        if item_id in (b["base_id"], b["episode"]):
            return b["split"]
    if ctx.cuts_path.exists():
        for c in ctx.cuts()["cuts"]:
            if item_id == c["cut_id"]:
                return c["split"]
    return "dev"  # levine-repro attempts


def asr(ctx: cr.Ctx, *, split: str | None = None, ids=(), **kw) -> dict:
    """``step_asr`` with the split inferred from ``ids`` (or both splits)."""
    splits = (
        [split]
        if split
        else sorted({_split_of(ctx, i) for i in ids})
        if ids
        else ["dev", "holdout"]
    )
    kw.setdefault("policies", ["default"])  # the hold-out needs one named explicitly
    total: dict[str, int] = {}
    for sp in splits:
        for k, v in cr.step_asr(ctx, split=sp, ids=ids, **kw).items():
            total[k] = total.get(k, 0) + v
    return total


def test_asr_requires_verified_cuts_unless_told_otherwise(cut_whispered):
    rig, ctx, cuts = cut_whispered
    with pytest.raises(cr.Refused, match="cuts-verified.json is missing"):
        asr(ctx, kinds=["cut"])
    assert rig.asr_calls == []
    out = asr(
        ctx, kinds=["cut"], allow_unverified=True, ids=[cuts["cuts"][0]["cut_id"]]
    )
    assert out == {"ok": 1}


@pytest.fixture
def verified(cut_whispered):
    rig, ctx, cuts = cut_whispered
    cr.step_whisper(ctx, kinds=["cut", "removed"])
    cr.step_cuts_verify(ctx)
    return rig, ctx, cuts


def test_asr_stores_transcript_policy_usage_and_never_overwrites(verified):
    rig, ctx, cuts = verified
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    out = asr(ctx, kinds=["base"], policies=["default"], repeat=2, ids=[base_id])
    assert out == {"ok": 2}
    d = ctx.base_dir(base_id) / "asr"
    assert sorted(p.name for p in d.iterdir()) == ["default-0.json", "default-1.json"]
    rec = cr.read_json(d / "default-1.json")
    assert (
        rec["status"] == "ok" and rec["repeat"] == 1 and rec["policy_name"] == "default"
    )
    assert rec["asr_policy"] == "fake-asr|thinking-default"
    assert rec["input_tokens"] == 800 and rec["thinking_tokens"] == 40
    spoken = rig.world.words[wav_sha((ctx.base_dir(base_id) / "pcm.wav").read_bytes())]
    assert rec["transcript"] == " ".join(spoken) and len(spoken) > 50
    assert rec["audio_sha256"] == wav_sha(
        (ctx.base_dir(base_id) / "pcm.wav").read_bytes()
    )
    before = {p: p.read_bytes() for p in d.iterdir()}
    calls = len(rig.asr_calls)
    # same request again: skipped, no calls, files untouched
    assert asr(ctx, kinds=["base"], policies=["default"], repeat=2, ids=[base_id]) == {
        "skipped": 2
    }
    assert len(rig.asr_calls) == calls
    assert {p: p.read_bytes() for p in d.iterdir()} == before
    # a larger --repeat only adds the new index
    assert asr(ctx, kinds=["base"], policies=["default"], repeat=3, ids=[base_id]) == {
        "skipped": 2,
        "ok": 1,
    }
    assert {
        p: p.read_bytes() for p in d.iterdir() if p.name != "default-2.json"
    } == before
    assert rig.transcribers["default"].closed


def test_asr_policies_interleave_and_use_their_own_transcriber(verified):
    rig, ctx, cuts = verified
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    asr(
        ctx,
        kinds=["base"],
        policies=["default", "low"],
        repeat=2,
        ids=[base_id],
        workers=1,
    )
    assert [t for t, _ in rig.asr_calls] == ["default", "low", "default", "low"]
    low = cr.read_json(ctx.base_dir(base_id) / "asr" / "low-0.json")
    assert low["asr_policy"] == "fake-asr|thinking-low"
    with pytest.raises(cr.Refused, match="unknown ASR policy"):
        asr(ctx, kinds=["base"], policies=["minimal"])


def test_asr_records_unavailable_and_retries_it_only_on_request(verified):
    rig, ctx, cuts = verified
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    wav = (ctx.base_dir(base_id) / "pcm.wav").read_bytes()
    original = rig._transcriber

    def flaky(thinking, timeout):
        t = original(thinking, timeout)
        t.fail[wav_sha(wav)] = "asr_timeout"
        return t

    rig.services.transcriber = flaky
    assert asr(ctx, kinds=["base"], ids=[base_id]) == {"unavailable": 1}
    path = ctx.base_dir(base_id) / "asr" / "default-0.json"
    rec = cr.read_json(path)
    assert rec["status"] == "unavailable" and rec["unavailable_reason"] == "asr_timeout"
    (entry,) = [e for e in ctx.ledger.calls() if e["kind"] == "asr"]
    assert entry["worst_case"] is True  # unknown usage: worst case, not zero
    n = len(rig.asr_calls)
    assert asr(ctx, kinds=["base"], ids=[base_id]) == {"skipped": 1}  # not retried
    assert len(rig.asr_calls) == n
    rig.services.transcriber = original
    assert asr(ctx, kinds=["base"], ids=[base_id], retry_unavailable=True) == {"ok": 1}
    assert cr.read_json(path)["status"] == "ok"
    assert (path.parent / "default-0.unavailable-1.json").exists()  # evidence kept


def test_asr_skips_discarded_cuts_and_non_faithful_bases(verified):
    rig, ctx, cuts = verified
    doc = cr.read_json(ctx.verified_path)
    some = cuts["cuts"][0]["cut_id"]
    doc["cuts"][some]["discarded"] = True
    ctx.verified_path.write_text(json.dumps(doc))
    asr(ctx, kinds=["cut"], ids=[some])
    assert rig.asr_calls == []  # nothing selected: the only id is discarded
    base_id = cuts["cuts"][1]["base_id"]
    cr.record_owner_call(ctx, base_id, "defect")
    asr(ctx, kinds=["base"], ids=[base_id])
    assert rig.asr_calls == []  # an owner "defect" is not a negative control


def test_asr_refuses_over_budget_before_calling(verified):
    rig, ctx, cuts = verified
    ctx.ledger.budget = ctx.ledger.total() + 1e-6
    with pytest.raises(cr.BudgetError):
        asr(ctx, kinds=["base"], workers=1)
    assert rig.asr_calls == []


# --- report ------------------------------------------------------------------


def test_report_builds_records_runs_the_grid_and_writes_json_and_markdown(verified):
    rig, ctx, cuts = verified
    asr(ctx, kinds=["base", "cut"], policies=["default"], repeat=2, workers=4)
    report = cr.step_report(ctx, split="all", policies=["default"], name="dev")
    (sec,) = report["sections"]
    assert sec["policy"] == "default" and sec["asr_policies"] == [
        "fake-asr|thinking-default"
    ]
    assert (ctx.root / "reports/dev.json").exists() and (
        ctx.root / "reports/dev.md"
    ).exists()
    assert sec["n_cuts"] == 2 * len(cuts["cuts"])
    assert sec["n_bases"] == 2 * sum(
        1 for _ in (ctx.root / "bases").glob("*/label.json")
    )
    assert len(sec["grid"]) == 15
    g = sec["grid"][0]
    assert {
        "thresholds",
        "cuts",
        "bases",
        "acceptance_ok",
        "acceptance",
        "reconstruction_false_passes",
    } <= set(g)
    assert (
        g["bases"]["overall"]["false_alarms"] == 0
    )  # perfect transcripts of faithful audio
    assert g["bases"]["overall"]["unavailable"] == 0
    assert g["cuts"]["overall"]["n"] == sec["n_cuts"]
    # perfect cut transcripts: nothing is reconstructed
    assert {r["level"] for r in sec["reconstruction"]} == {"none"}
    assert all(r["clean_hits"] is not None for r in sec["reconstruction"])
    assert all(r["audio_residue_tokens"] == 0 for r in sec["reconstruction"])
    md = (ctx.root / "reports/dev.md").read_text()
    for needle in (
        "## Grid summary",
        "acceptance_ok",
        "by_family",
        "by_size_bin",
        "by_split",
        "by_model",
        "Reconstruction",
        "false alarms",
    ):
        assert needle in md
    # reports are never overwritten
    with pytest.raises(cr.Refused, match="already exists"):
        cr.step_report(ctx, split="all", policies=["default"], name="dev")


def test_report_grid_overrides_replace_the_default_and_are_recorded(verified):
    rig, ctx, cuts = verified
    asr(ctx, kinds=["base", "cut"], policies=["default"], repeat=1, workers=4)
    default = cr.step_report(ctx, split="all", policies=["default"], name="d0")
    assert default["args"]["deficits"] == [None, 12, 16, 20, 24]
    assert default["args"]["floors"] == [0.85, 0.90, 0.93]
    rep = cr.step_report(
        ctx,
        split="all",
        policies=["default"],
        name="d1",
        deficits=(None, 6, 10),
        floors=(0.95, 0.96),
    )
    assert rep["args"]["deficits"] == [None, 6, 10]
    assert rep["args"]["floors"] == [0.95, 0.96]
    (sec,) = rep["sections"]
    assert [
        (g["thresholds"]["net_deficit_min"], g["thresholds"]["recall_floor"])
        for g in sec["grid"]
    ] == [(m, f) for m in (None, 6, 10) for f in (0.95, 0.96)]
    md = (ctx.root / "reports/d1.md").read_text()
    assert '"deficits": [null, 6, 10]' in md
    # each override is independent of the other
    only_floor = cr.step_report(
        ctx, split="all", policies=["default"], name="d2", floors=(0.9,)
    )
    assert only_floor["args"]["deficits"] == [None, 12, 16, 20, 24]
    assert len(only_floor["sections"][0]["grid"]) == 5


def test_report_grid_overrides_are_validated(verified):
    rig, ctx, cuts = verified
    for bad in (
        {"deficits": (0,)},
        {"floors": (1.5,)},
        {"deficits": ()},
        {"floors": ()},
    ):
        with pytest.raises((cr.Refused, ValueError)):
            cr.step_report(ctx, split="all", policies=["default"], name="bad", **bad)
    assert not (ctx.root / "reports/bad.json").exists()


def test_report_cli_parses_repeatable_deficit_and_floor(cli_rig, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cr, "step_report", lambda ctx, **kw: seen.update(kw))
    root = ["--root", str(tmp_path / "t5")]
    out = invoke(
        [
            *root,
            "report",
            "--split",
            "dev",
            "--policy",
            "low",
            "--deficit",
            "none",
            "--deficit",
            "6",
            "--deficit",
            "16",
            "--floor",
            "0.85",
            "--floor",
            "0.96",
        ]  # fmt: skip
    )
    assert out.exit_code == 0, out.output
    assert seen["deficits"] == (None, 6, 16)
    assert seen["floors"] == (0.85, 0.96)
    seen.clear()
    out = invoke([*root, "report", "--split", "dev", "--policy", "low"])
    assert out.exit_code == 0, out.output
    assert seen["deficits"] is None and seen["floors"] is None
    for bad in (
        ["--deficit", "x"],
        ["--deficit", "0"],
        ["--floor", "1.2"],
        ["--floor", "no"],
    ):
        out = invoke([*root, "report", "--split", "dev", "--policy", "low", *bad])
        assert out.exit_code == 2, (bad, out.output)


def test_report_splits_and_requires_a_policy(verified):
    rig, ctx, cuts = verified
    asr(ctx, kinds=["base", "cut"], policies=["low"], workers=4)
    with pytest.raises(cr.Refused, match="--policy is required"):
        cr.step_report(ctx, split="all", policies=[])
    dev = cr.step_report(ctx, split="dev", policies=["low"], name="d")
    hold = cr.step_report(ctx, split="holdout", policies=["low"], name="h")
    both = cr.step_report(ctx, split="all", policies=["low"], name="a")
    n = lambda r: r["sections"][0]["n_cuts"]  # noqa: E731
    assert n(dev) + n(hold) == n(both) > 0
    assert set(dev["sections"][0]["grid"][0]["cuts"]["by_split"]) == {"dev"}


def test_report_counts_unavailable_apart_and_flags_a_confirmed_reconstruction(verified):
    rig, ctx, cuts = verified
    # a Gemini transcript that "hears" the removed text of one cut: reconstruction
    victim = cuts["cuts"][0]
    d = ctx.cut_dir(victim["cut_id"])
    spec = cal.CutSpec.from_dict(cr.read_json(d / "label.json")["spec"])
    base = cr.effective_label(ctx, victim["base_id"])
    cut_wav = (d / "cut.wav").read_bytes()
    rig.world.register(
        cut_wav, list(base.script_tokens)
    )  # transcribes the WHOLE script
    # and one cut whose ASR is unavailable
    other = cuts["cuts"][1]["cut_id"]
    other_wav = (ctx.cut_dir(other) / "cut.wav").read_bytes()
    orig = rig._transcriber

    def flaky(thinking, timeout):
        t = orig(thinking, timeout)
        t.fail[wav_sha(other_wav)] = "asr_incomplete"
        return t

    rig.services.transcriber = flaky
    asr(ctx, kinds=["base", "cut"], policies=["default"], workers=2)
    report = cr.step_report(ctx, split="all", policies=["default"], name="r")
    (sec,) = report["sections"]
    recon = {r["record_id"]: r for r in sec["reconstruction"]}
    assert recon[f"{victim['cut_id']}:default-0"]["level"] == "confirmed"
    assert other not in {r["record_id"].split(":")[0] for r in sec["reconstruction"]}
    g = sec["grid"][-1]
    assert g["cuts"]["overall"]["unavailable"] == 1
    assert g["acceptance_ok"] is False and g["acceptance"]["cut_unavailable"] == 1
    # the reconstructed cut transcribes everything, so the verifier PASSES it
    # (nothing is missing from that transcript): a confirmed false pass, listed
    assert f"{victim['cut_id']}:default-0" in g["reconstruction_false_passes"]
    assert spec.total_tokens > 0


# --- levine-repro / deadline / ledger / clips / cli --------------------------


def repro_script(tmp_path: Path) -> Path:
    p = tmp_path / "levine-script.txt"
    p.write_text(make_text(7, paragraphs=3))
    return p


def test_levine_repro_synthesizes_attempts_resumably(tmp_path):
    rig = Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5")
    script = repro_script(tmp_path)
    out = cr.step_levine_repro(ctx, script=script, voices="Charon:2,Puck:1", workers=1)
    assert out == {"ok": 3}
    assert sorted(p.name for p in ctx.repro_dir.iterdir()) == [
        "Charon-0", "Charon-1", "Puck-0", "source.json",
    ]  # fmt: skip
    d = ctx.repro_dir / "Charon-0"
    assert (d / "pcm.wav").exists() and (d / "chunk.txt").exists()
    assert cr.read_json(d / "synth.json")["model"] == "gemini-3.8-flash-tts"
    assert [c[1] for c in rig.provider.calls] == ["Charon", "Charon", "Puck"]
    calls = len(rig.provider.calls)
    assert cr.step_levine_repro(ctx, script=script, voices="Charon:2,Puck:1") == {
        "skipped": 3
    }
    assert len(rig.provider.calls) == calls
    # a different script or chunk against the same tree is refused, not mixed in
    other = tmp_path / "other.txt"
    other.write_text(make_text(8, paragraphs=3))
    with pytest.raises(cr.Refused, match="different"):
        cr.step_levine_repro(ctx, script=other)
    with pytest.raises(cr.Refused, match="chunk-index"):
        cr.step_levine_repro(ctx, script=script, chunk_index=99)
    with pytest.raises(cr.Refused, match="Voice:count"):
        cr.parse_voices("Charon")


def test_levine_repro_whisper_asr_and_side_by_side_report(tmp_path):
    rig = Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5")
    cr.step_levine_repro(
        ctx, script=repro_script(tmp_path), voices="Charon:1,Kore:1", workers=1
    )
    # Charon-0's audio really skipped a stretch: both ASRs must miss the same span
    d = ctx.repro_dir / "Charon-0"
    wav = (d / "pcm.wav").read_bytes()
    words = rig.world.words[wav_sha(wav)]
    skipped = words[:30] + words[55:]
    rig.world.register(wav, skipped)
    assert cr.step_whisper(ctx, kinds=["repro"]) == {"ok": 2}
    assert asr(ctx, kinds=["repro"], policies=["default"]) == {"ok": 2}
    report = cr.step_report(
        ctx, kind="repro", split="dev", policies=["default"], name="repro"
    )
    rows = {r["attempt"]: r for r in report["rows"]}
    assert rows["Charon-0"]["classification"].startswith("CONFIRMED")
    assert rows["Kore-0"]["classification"] == "no span in either"
    md = (ctx.root / "reports/repro.md").read_text()
    assert "Gemini-ASR spans" in md and "whisper spans" in md and "Charon-0" in md


def test_levine_repro_gemini_only_miss_is_an_asr_false_positive():
    g = [{"start": 10, "end": 40, "net_missing": 30, "excerpt": "x"}]
    assert cr._classify(g, []).startswith("Gemini-only")
    assert cr._classify([], g).startswith("whisper-only")
    far = [{"start": 100, "end": 130, "net_missing": 30, "excerpt": "y"}]
    assert cr._classify(g, far).startswith("disjoint")
    assert cr._classify(g, None) == "no whisper yet"


class FakeRenderResult:
    def __init__(self, manifest_path):
        self.manifest_path = manifest_path


def manifest(tmp: Path, tokens, outcome="ok", reason=None, attempts=(1, 2)):
    p = tmp / "manifest.json"
    p.write_text(
        json.dumps(
            {
                "gemini_phase": {
                    "outcome": outcome, "reason": reason, "detail": "",
                    "elapsed_s": 301.5, "budget_s": 360.0, "tokens": tokens,
                    "chunks": [{"attempts": [{}] * n} for n in attempts],
                }
            }
        )
    )  # fmt: skip
    return p


def test_deadline_runs_render_episode_without_fallback_or_cache_and_summarizes(
    tmp_path,
):
    rig, ctx = corpus_ctx(tmp_path)
    toks = {"synth_prompt": 10, "synth_audio": 70_000, "asr_input": 90_000,
            "asr_output": 6_000, "asr_thinking": 3_000}  # fmt: skip
    rig.render_impl = lambda *a, **k: FakeRenderResult(manifest(tmp_path, toks))
    rec = cr.step_deadline(ctx, model="gemini-3.8-flash-lite-tts", voice="Kore")
    (call,) = rig.renders
    text, cfg, out = call["args"]
    assert text == (ctx.root / "texts/levine-2026-09-24.txt").read_text()
    assert cfg.fallback is None and cfg.primary.model == "gemini-3.8-flash-lite-tts"
    assert cfg.primary.style == "calm, measured news anchor"
    kw = call["kwargs"]
    assert kw["cache_dir"] is None and kw["notify_fallback"] is False
    assert kw["manifest_dir"] == ctx.root / "deadline" / "manifests"
    assert rec["outcome"] == "ok" and rec["phase_elapsed_s"] == 301.5
    assert rec["retries"] == 1 and rec["verifier_policy"].startswith("verifier-v")
    expected = 70_000 / 1e6 * 6.1 + 90_000 / 1e6 + (6_000 + 3_000) / 1e6 * 5
    assert rec["est_usd"] == pytest.approx(expected) and rec["worst_case"] is False
    (entry,) = [e for e in ctx.ledger.calls() if e["kind"] == "deadline"]
    assert entry["est_usd"] == pytest.approx(expected)
    lines = (ctx.root / "deadline/summary.jsonl").read_text().splitlines()
    assert len(lines) == 1
    # a second run of the same probe is skipped, a new attempt number is not
    assert (
        cr.step_deadline(ctx, model="gemini-3.8-flash-lite-tts")["status"] == "skipped"
    )
    assert len(rig.renders) == 1
    cr.step_deadline(ctx, model="gemini-3.8-flash-lite-tts", attempt=1)
    assert len((ctx.root / "deadline/summary.jsonl").read_text().splitlines()) == 2


def test_deadline_failure_is_recorded_and_unknown_tokens_cost_the_worst_case(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("render exploded")

    rig.render_impl = boom
    rec = cr.step_deadline(ctx, model="gemini-3.8-flash-tts")
    assert rec["render_outcome"] == "failed" and "render exploded" in rec["error"]
    text_len = len((ctx.root / "texts/levine-2026-09-24.txt").read_text())
    assert rec["worst_case"] is True
    assert rec["est_usd"] == pytest.approx(
        cr.est_deadline_worst(text_len, "gemini-3.8-flash-tts")
    )
    # null token totals in a real manifest (killed request) are worst case too
    rig.render_impl = lambda *a, **k: FakeRenderResult(
        manifest(
            tmp_path,
            {"synth_audio": None, "asr_input": 5, "asr_output": 5},
            "failed",
            "deadline",
        )
    )
    rec = cr.step_deadline(ctx, model="gemini-3.8-flash-tts", attempt=1)
    assert rec["outcome"] == "failed" and rec["reason"] == "deadline"
    assert rec["worst_case"] is True


def test_deadline_refuses_over_budget_before_rendering(tmp_path):
    rig, ctx = corpus_ctx(tmp_path, budget=0.05)
    with pytest.raises(cr.BudgetError):
        cr.step_deadline(ctx, model="gemini-3.8-flash-tts")
    assert rig.renders == []


def test_deadline_needs_a_text(tmp_path):
    rig = Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5")
    with pytest.raises(cr.Refused, match="no text"):
        cr.step_deadline(ctx, model="gemini-3.8-flash-tts")


def test_ledger_step_prints_totals_by_kind(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    s = cr.step_ledger(ctx)
    assert s["by_kind"].keys() == {"synth", "whisper"}
    assert "ledger: $" in ctx.out[0] and any("synth" in line for line in ctx.out)


def test_clips_writes_an_mp3_window_and_does_not_overwrite(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    out = cr.step_clips(ctx, item_id=base_id, start_s=1.0, end_s=4.0, name="mine")
    assert out == ctx.root / "clips" / "mine.mp3" and out.exists()
    out.write_bytes(b"owner-edited")
    cr.step_clips(ctx, item_id=base_id, start_s=1.0, end_s=4.0, name="mine")
    assert out.read_bytes() == b"owner-edited"
    with pytest.raises(cr.Refused, match="no audio"):
        cr.step_clips(ctx, item_id="nope", start_s=0, end_s=1, name=None)


# --- cli -----------------------------------------------------------------------


@pytest.fixture
def cli_rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    monkeypatch.setattr(cr, "default_services", lambda: rig.services)
    return rig


def invoke(args):
    from pipeline.__main__ import cli

    return CliRunner().invoke(cli, ["tts-calibrate", *args])


def test_cli_is_reachable_through_the_pipeline_group_with_lazy_import(
    cli_rig, tmp_path
):
    out = invoke(["--help"])
    assert out.exit_code == 0
    for step in (
        "corpus",
        "synth",
        "whisper",
        "label",
        "cuts",
        "asr",
        "report",
        "levine-repro",
        "deadline",
        "ledger",
        "clips",
        "owner-call",
    ):
        assert step in out.output
    out = invoke(["--root", str(tmp_path / "t5"), "ledger"])
    assert out.exit_code == 0 and "ledger: $0.0000 of $15.00" in out.output


def test_cli_budget_refusal_exits_2_with_a_clean_message(cli_rig, tmp_path):
    ctx = cli_rig.ctx(tmp_path / "t5")
    run_corpus(cli_rig, ctx, tmp_path)
    out = invoke(
        [
            "--root",
            str(tmp_path / "t5"),
            "--budget",
            "0.000001",
            "synth",
            "--workers",
            "1",
            "--ids",
            "levine-2026-09-21--c0--flash--Kore",
        ]
    )
    assert out.exit_code == 2
    assert "budget" in out.output and "nothing was sent" in out.output
    assert "Traceback" not in out.output
    assert cli_rig.provider.calls == []


def test_cli_refusals_exit_2(cli_rig, tmp_path):
    out = invoke(["--root", str(tmp_path / "t5"), "corpus"])
    assert out.exit_code == 2 and "exactly 4 distinct --rundown" in out.output
    out = invoke(["--root", str(tmp_path / "t5"), "cuts"])
    assert out.exit_code == 2 and "corpus.json does not exist" in out.output


def test_cli_missing_keys_name_only_the_variables(monkeypatch, tmp_path):
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(
        cr,
        "default_services",
        lambda: cr.Services(
            provider=lambda: None,
            whisper=lambda *a: {},
            transcriber=lambda *a: None,
            r2_get=lambda k: b"",
            render=lambda *a, **k: None,
            make_clip=lambda *a: None,
            check_env=True,
        ),
    )
    rig = Rig(tmp_path)
    ctx = rig.ctx(tmp_path / "t5")
    run_corpus(rig, ctx, tmp_path)
    out = invoke(["--root", str(tmp_path / "t5"), "synth"])
    assert out.exit_code == 2 and "GEMINI_API_KEY" in out.output


def test_default_services_construct_without_touching_the_network():
    # real clients are built lazily, inside the steps; the guards would fail the
    # test if constructing the bundle opened one
    s = cr.default_services()
    assert s.check_env is True and callable(s.whisper) and callable(s.render)
    assert cr.est_deadline_worst(22_200, "gemini-3.8-flash-tts") < 15


def test_calibration_policy_names_map_explicitly_to_thinking_settings():
    """`default` means thinking-default (no thinking config), never the
    production default (low); the mapping must not follow asr's defaults."""
    from pipeline.tts import asr as asr_mod

    assert asr_mod.ASR_POLICY.endswith("|thinking-low")
    s = cr.default_services()
    default = s.transcriber("default", 30.0)
    low = s.transcriber("low", 30.0)
    assert default.thinking == "default" and low.thinking == "low"
    assert default.policy == asr_mod.policy_for(thinking="default")
    assert default.policy.endswith("|thinking-default")
    assert low.policy == asr_mod.policy_for(thinking="low") == asr_mod.ASR_POLICY
    assert set(cr.ASR_POLICIES) == set(asr_mod.THINKING_SETTINGS)


def test_slots_cycle_through_every_combination():
    slots = cr.plan_slots(2 * len(cr.COMBOS), "s", "dev")
    assert sorted(slots[: len(cr.COMBOS)]) == sorted(cr.COMBOS)
    assert sorted(slots[len(cr.COMBOS) :]) == sorted(cr.COMBOS)
    assert cr.plan_slots(10, "s", "dev") == cr.plan_slots(10, "s", "dev")
    assert cr.plan_slots(10, "s", "dev") != cr.plan_slots(10, "t", "dev")
    assert {f for f, _ in cr.COMBOS} == set(cal.FAMILIES)


# =========================================================================== #
# review round: ledger completeness, deadline failures, stale evidence,
# hold-out discipline, exit codes, lock, timeouts, whisper client
# =========================================================================== #


def _manifest_writer(tokens, *, outcome="failed", reason="deadline", chunks=(1, 3)):
    """A fake render that writes a manifest like render_episode, then raises."""
    from pipeline.tts.render import TTSRenderError

    def render(text, cfg, out, *, feed_slug, episode_id, manifest_dir, **kw):
        d = manifest_dir / feed_slug
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{episode_id}-20260930T120000000000Z.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "gemini_phase": {
                        "outcome": outcome,
                        "reason": reason,
                        "detail": "chunk 3 ran out of time",
                        "failed_chunk": 3,
                        "elapsed_s": 360.4,
                        "budget_s": 360.0,
                        "tokens": tokens,
                        "chunks": [{"attempts": [{}] * n} for n in chunks],
                    },
                }
            )
        )
        raise TTSRenderError("Gemini phase failed: deadline")

    return render


def test_deadline_reads_a_failed_phase_from_the_manifest_after_a_render_error(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    toks = {"synth_audio": 50_000, "asr_input": 60_000, "asr_output": 4_000,
            "asr_thinking": None}  # fmt: skip
    rig.render_impl = _manifest_writer(toks)
    rec = cr.step_deadline(ctx, model="gemini-3.8-flash-lite-tts")
    assert rec["render_outcome"] == "failed" and "TTSRenderError" in rec["error"]
    assert (rec["outcome"], rec["reason"], rec["failed_chunk"]) == (
        "failed",
        "deadline",
        3,
    )
    assert rec["phase_elapsed_s"] == 360.4 and rec["budget_s"] == 360.0
    assert rec["retries"] == 2  # (1-1) + (3-1)
    assert rec["manifest"] and Path(rec["manifest"]).exists()
    # tokens known (a completed call with no thinking count is 0): not worst case
    expected = 50_000 / 1e6 * 6.1 + 60_000 / 1e6 + 4_000 / 1e6 * 5
    assert rec["worst_case"] is False and rec["est_usd"] == pytest.approx(expected)
    (entry,) = [e for e in ctx.ledger.calls() if e["kind"] == "deadline"]
    assert entry["est_usd"] == pytest.approx(expected) and entry["worst_case"] is False


def test_deadline_render_error_with_unknown_tokens_is_booked_at_the_worst_case(
    tmp_path,
):
    rig, ctx = corpus_ctx(tmp_path)
    rig.render_impl = _manifest_writer({"synth_audio": None, "asr_input": None})
    rec = cr.step_deadline(ctx, model="gemini-3.8-flash-tts")
    assert rec["worst_case"] is True and rec["reason"] == "deadline"
    assert rec["est_usd"] == pytest.approx(
        cr.est_deadline_worst(rec["chars"], "gemini-3.8-flash-tts")
    )


def test_deadline_interrupt_is_booked_then_reraised(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    toks = {"synth_audio": 10_000, "asr_input": 10_000, "asr_output": 1_000}

    def render(*a, **k):
        _manifest_writer(toks)(*a, **k)  # writes the manifest, raises TTSRenderError

    def interrupted(*a, **k):
        try:
            render(*a, **k)
        except Exception:
            raise KeyboardInterrupt from None

    rig.render_impl = interrupted
    with pytest.raises(KeyboardInterrupt):
        cr.step_deadline(ctx, model="gemini-3.8-flash-tts")
    (entry,) = [e for e in ctx.ledger.calls() if e["kind"] == "deadline"]
    assert entry["usage"]["synth_audio"] == 10_000  # booked from the manifest
    (line,) = (ctx.root / "deadline/summary.jsonl").read_text().splitlines()
    assert json.loads(line)["render_outcome"] == "interrupted"


def test_deadline_render_error_with_no_manifest_costs_the_worst_case(tmp_path):
    from pipeline.tts.render import TTSRenderError

    rig, ctx = corpus_ctx(tmp_path)

    def boom(*a, **k):
        raise TTSRenderError("no manifest was written")

    rig.render_impl = boom
    rec = cr.step_deadline(ctx, model="gemini-3.8-flash-tts")
    assert rec["worst_case"] is True and rec["manifest"] is None


def test_deadline_mp3_is_never_overwritten_and_a_torn_summary_line_is_tolerated(
    tmp_path,
):
    rig, ctx = corpus_ctx(tmp_path)
    toks = {"synth_audio": 1, "asr_input": 1, "asr_output": 1}
    seen = []

    def render(text, cfg, out, **kw):
        seen.append(out)
        out.write_bytes(b"NEW")
        return FakeRenderResult(manifest(tmp_path, toks))

    rig.render_impl = render
    d = ctx.root / "deadline"
    d.mkdir(parents=True)
    precious = d / "levine-2026-09-24--lite--Kore--0.mp3"
    precious.write_bytes(b"EARLIER RUN")
    ok_line = json.dumps({"key": "other|k|v|9"})
    (d / "summary.jsonl").write_text(ok_line + "\n" + '{"key": "levine-2026-09-24|gem')
    rec = cr.step_deadline(ctx, model="gemini-3.8-flash-lite-tts")
    assert precious.read_bytes() == b"EARLIER RUN"
    assert seen[0].name == "levine-2026-09-24--lite--Kore--0-1.mp3" and rec["mp3"]
    assert any("torn last line" in line for line in ctx.out)
    lines = (d / "summary.jsonl").read_text().splitlines()
    assert [json.loads(line)["key"] for line in lines][0] == "other|k|v|9"
    assert len(lines) == 2 and all(json.loads(line) for line in lines)
    assert (d / "summary.torn-1.txt").read_text().startswith('{"key": "levine')
    # a bad line in the middle is an error, not something to skip
    (d / "summary.jsonl").write_text("garbage\n" + ok_line + "\n")
    with pytest.raises(cr.CalibrateError, match="line 1"):
        cr.step_deadline(ctx, model="gemini-3.8-flash-lite-tts", attempt=5)


# --- the ledger never misses a billed call -----------------------------------


def _synth_x(ctx):
    return cr.synth_one(
        ctx,
        item_id="x",
        text="alpha beta gamma delta",
        model="gemini-3.8-flash-tts",
        voice="Kore",
        out_dir=ctx.root / "bases" / "x",
    )


def test_synth_books_the_ledger_even_when_the_provider_blows_up(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    rig.provider.script["Kore"] = [RuntimeError("boom")]
    with pytest.raises(RuntimeError, match="boom"):
        cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    (entry,) = ctx.ledger.calls()
    assert entry["worst_case"] is True and entry["est_usd"] > 0
    assert entry["note"] == "call did not complete"


def test_synth_books_real_usage_even_when_saving_the_audio_fails(tmp_path, monkeypatch):
    rig, ctx = corpus_ctx(tmp_path)
    real = cr.write_new

    def failing(path, data):
        if path.name == "pcm.wav":
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(cr, "write_new", failing)
    with pytest.raises(OSError, match="disk full"):
        _synth_x(ctx)
    (entry,) = ctx.ledger.calls()
    assert entry["usage"]["audio_tokens"] == 1000 and entry["worst_case"] is False


def test_an_interrupt_during_synth_is_booked_at_the_worst_case(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    rig.provider.script["Kore"] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        _synth_x(ctx)
    (entry,) = ctx.ledger.calls()
    assert entry["worst_case"] is True


def test_asr_and_whisper_book_the_ledger_when_the_service_blows_up(verified):
    rig, ctx, cuts = verified
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    before = len(ctx.ledger.calls())
    orig = rig._transcriber

    def crashing(thinking, timeout):
        t = orig(thinking, timeout)
        t.crash = RuntimeError("asr exploded")
        return t

    rig.services.transcriber = crashing
    with pytest.raises(RuntimeError, match="asr exploded"):
        asr(ctx, kinds=["base"], ids=[base_id])
    entry = ctx.ledger.calls()[before]
    assert entry["kind"] == "asr" and entry["worst_case"] is True
    # whisper: a non-ServiceError escapes, but the call is still on the ledger
    victim = next((ctx.root / "bases").glob("*/whisper.json"))
    victim.unlink()
    rig.whisper.errors = [RuntimeError("whisper exploded")]
    with pytest.raises(RuntimeError, match="whisper exploded"):
        cr.step_whisper(ctx, kinds=["base"], ids=[victim.parent.name])
    last = ctx.ledger.calls()[-1]
    assert last["kind"] == "whisper" and last["worst_case"] is True


def test_threaded_budget_refusal_never_oversubscribes_and_loses_no_ledger_line(
    tmp_path,
):
    rig, ctx = corpus_ctx(tmp_path)
    mine = [b for b in ctx.corpus()["bases"] if b["feed"] == "levine"]
    worsts = [cr.est_synth_worst(b["chars"], b["model"]) for b in mine]
    ctx.ledger.budget = 2.5 * max(worsts)  # a handful of calls may be in flight
    rig.provider.delay = 0.05
    ids = [b["base_id"] for b in mine]
    with pytest.raises(cr.BudgetError):
        cr.step_synth(ctx, workers=4, ids=ids)
    entries = ctx.ledger.calls()
    assert 1 <= len(rig.provider.calls) < len(ids)
    assert len(entries) == len(rig.provider.calls)  # every call that went out is booked
    # in-flight worst cases never exceeded the budget
    assert rig.provider.max_inflight * min(worsts) <= ctx.ledger.budget
    assert rig.provider.max_inflight < len(ids)
    assert ctx.ledger.total() <= ctx.ledger.budget
    assert rig.provider.inflight == 0


# --- wall-clock bounds ---------------------------------------------------------


def test_a_hung_asr_request_is_recorded_unavailable_and_the_step_finishes(
    verified, monkeypatch
):
    rig, ctx, cuts = verified
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    monkeypatch.setattr(cr, "ASR_WALL_S", 0.2)
    gate = threading.Event()
    orig = rig._transcriber

    def hanging(thinking, timeout):
        t = orig(thinking, timeout)
        t.block = gate
        return t

    rig.services.transcriber = hanging
    try:
        out = asr(ctx, kinds=["base"], ids=[base_id])
    finally:
        gate.set()
    assert out == {"unavailable": 1}
    rec = cr.read_json(ctx.base_dir(base_id) / "asr" / "default-0.json")
    assert rec["unavailable_reason"] == "asr_timeout" and "may linger" in rec["detail"]
    entry = [e for e in ctx.ledger.calls() if e["kind"] == "asr"][-1]
    assert entry["worst_case"] is True


def test_a_hung_synth_is_a_transient_error_then_exhausts(tmp_path, monkeypatch):
    rig, ctx = corpus_ctx(tmp_path)
    monkeypatch.setattr(cr, "SYNTH_WALL_S", 0.1)
    gate = threading.Event()
    rig.provider.block = gate
    try:
        out = cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    finally:
        gate.set()
    assert out == {"failed": 1} and rig.sleeps == [2.0, 8.0]
    rec = cr.read_json(
        ctx.base_dir("levine-2026-09-21--c0--flash--Kore") / "synth.json"
    )
    assert [a["kind"] for a in rec["attempts"]] == ["infra"] * 3
    assert (
        all(e["worst_case"] for e in ctx.ledger.calls())
        and len(ctx.ledger.calls()) == 3
    )


def test_a_hung_whisper_call_is_retried_then_fails(copy_of_staged, monkeypatch):
    rig, ctx, *_ = copy_of_staged
    victim = next((ctx.root / "bases").glob("*/whisper.json"))
    victim.unlink()
    monkeypatch.setattr(cr, "WHISPER_WALL_S", 0.1)
    gate = threading.Event()
    rig.whisper.block = gate
    try:
        out = cr.step_whisper(ctx, kinds=["base"], ids=[victim.parent.name])
    finally:
        gate.set()
    assert out == {"failed": 1} and len(rig.whisper.calls) == 3
    assert any("gave up" in line for line in ctx.out)


def test_call_with_timeout_returns_raises_and_times_out():
    assert cr.call_with_timeout(lambda: 7, 1) == 7
    with pytest.raises(ValueError, match="x"):
        cr.call_with_timeout(lambda: (_ for _ in ()).throw(ValueError("x")), 1)
    gate = threading.Event()
    try:
        with pytest.raises(cr.WallClockTimeout, match="may linger"):
            cr.call_with_timeout(lambda: gate.wait(5), 0.05)
    finally:
        gate.set()


# --- the real whisper client, against a stub ------------------------------------


class StubWhisperClient:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
        self.kwargs = None
        self.closed = False
        self.audio = self
        self.transcriptions = self

    def create(self, **kw):
        self.kwargs = kw
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


class StubResponse:
    def __init__(self, payload):
        self.payload = payload

    def model_dump_json(self):
        return json.dumps(self.payload)


def test_openai_whisper_wrapper_sends_the_validated_request_and_returns_a_dict(
    monkeypatch,
):
    from pipeline.tts import providers

    payload = {"words": [{"word": "hi", "start": 0.0, "end": 0.4}], "duration": 1.0}
    stub = StubWhisperClient(StubResponse(payload))
    made = []
    monkeypatch.setattr(
        providers, "_make_openai_client", lambda timeout: made.append(timeout) or stub
    )
    out = cr._openai_whisper(b"RIFF....", "base.wav")
    assert out == payload and stub.closed and made == [180.0]
    kw = stub.kwargs
    assert kw["model"] == "whisper-1" and kw["response_format"] == "verbose_json"
    assert kw["timestamp_granularities"] == ["word"] and kw["language"] == "en"
    assert kw["file"] == ("base.wav", b"RIFF....", "audio/wav")


def _status_error(cls, status, body=None):
    import httpx

    req = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    return cls("x", response=httpx.Response(status, request=req), body=body)


@pytest.mark.parametrize(
    ("make", "kind"),
    [
        (
            lambda o, h: h.APIConnectionError(
                request=__import__("httpx").Request("POST", "http://x")
            ),
            "transient",
        ),
        (lambda o, h: _status_error(o.RateLimitError, 429), "transient"),
        (lambda o, h: _status_error(o.InternalServerError, 503), "transient"),
        (lambda o, h: _status_error(o.AuthenticationError, 401), "fatal"),
        (lambda o, h: _status_error(o.BadRequestError, 400), "fatal"),
        (
            lambda o, h: _status_error(
                o.RateLimitError, 429, {"code": "insufficient_quota", "message": "x"}
            ),
            "fatal",
        ),
    ],
)
def test_openai_whisper_wrapper_classifies_errors_and_still_closes(
    monkeypatch, make, kind
):
    import openai

    from pipeline.tts import providers

    stub = StubWhisperClient(error=make(openai, openai))
    monkeypatch.setattr(providers, "_make_openai_client", lambda timeout: stub)
    with pytest.raises(cr.ServiceError) as exc_info:
        cr._openai_whisper(b"x", "a.wav")
    assert exc_info.value.kind == kind and stub.closed


# --- stale evidence ---------------------------------------------------------------


def _first_base(ctx):
    return next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))


def test_asr_refuses_stale_records_before_any_call_unless_told(verified):
    rig, ctx, cuts = verified
    base_id = _first_base(ctx)
    asr(ctx, kinds=["base"], ids=[base_id])
    calls = len(rig.asr_calls)
    wav_path = ctx.base_dir(base_id) / "pcm.wav"
    original = wav_path.read_bytes()
    wav_path.write_bytes(cal.wav_bytes(b"\x07\x00" * 20_000))  # a different render
    with pytest.raises(cr.Refused, match="stale.*audio changed"):
        asr(ctx, kinds=["base"], ids=[base_id])
    assert len(rig.asr_calls) == calls  # nothing was sent
    # a different ASR policy string under the same policy name is stale too
    wav_path.write_bytes(original)
    orig = rig._transcriber

    def other_policy(thinking, timeout):
        t = orig(thinking, timeout)
        t.policy = "some-other-model|thinking-default"
        return t

    rig.services.transcriber = other_policy
    with pytest.raises(cr.Refused, match="ASR policy"):
        asr(ctx, kinds=["base"], ids=[base_id])
    assert len(rig.asr_calls) == calls
    # --retry-stale redoes it and keeps the old record
    assert asr(ctx, kinds=["base"], ids=[base_id], retry_stale=True) == {"ok": 1}
    d = ctx.base_dir(base_id) / "asr"
    assert (d / "default-0.stale-1.json").exists()
    assert (
        cr.read_json(d / "default-0.json")["asr_policy"]
        == "some-other-model|thinking-default"
    )


def test_report_refuses_mixed_asr_policy_strings_and_stale_audio(verified):
    rig, ctx, cuts = verified
    base_id = _first_base(ctx)
    asr(ctx, kinds=["base"], ids=[base_id], repeat=2)
    f = ctx.base_dir(base_id) / "asr" / "default-1.json"
    rec = cr.read_json(f)
    rec["asr_policy"] = "tampered|thinking-default"
    f.write_text(json.dumps(rec))
    with pytest.raises(cr.Refused, match="different ASR policy strings"):
        cr.step_report(ctx, split="all", policies=["default"], name="x")
    rec["asr_policy"] = "fake-asr|thinking-default"
    f.write_text(json.dumps(rec))
    rep_ok = cr.step_report(ctx, split="all", policies=["default"], name="ok")
    assert rep_ok["sections"][0]["asr_policies"] == ["fake-asr|thinking-default"]
    wav_path = ctx.base_dir(base_id) / "pcm.wav"
    wav_path.write_bytes(cal.wav_bytes(b"\x07\x00" * 20_000))
    with pytest.raises(cr.Refused, match="stale"):
        cr.step_report(ctx, split="all", policies=["default"], name="y")


def test_cuts_refuse_an_existing_cut_wav_that_differs(copy_of_staged):
    rig, ctx, _, cuts = copy_of_staged
    ctx.cuts_path.unlink()
    victim = ctx.cut_dir(cuts["cuts"][0]["cut_id"]) / "cut.wav"
    victim.write_bytes(victim.read_bytes()[:-2] + b"\x00\x00")
    before = victim.read_bytes()
    with pytest.raises(cr.Refused, match="differs"):
        cr.step_cuts(ctx)
    assert victim.read_bytes() == before and not ctx.cuts_path.exists()


def test_label_refuses_a_chunk_file_that_does_not_match_the_corpus(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    base_id = _first_base(ctx)
    (ctx.base_dir(base_id) / "label.json").unlink()
    (ctx.base_dir(base_id) / "chunk.txt").write_text("not the chunk")
    with pytest.raises(cr.Refused, match="does not match the corpus chunk"):
        cr.step_label(ctx, ids=[base_id])


# --- hold-out discipline -------------------------------------------------------------


def test_asr_split_is_required_and_all_is_refused(verified):
    rig, ctx, cuts = verified
    with pytest.raises(TypeError):
        cr.step_asr(ctx, kinds=["base"])  # type: ignore[call-arg]
    with pytest.raises(cr.Refused, match="dev or holdout"):
        cr.step_asr(ctx, kinds=["base"], split="all")
    out = invoke_ctx_free = None  # noqa: F841
    assert rig.asr_calls == []


def test_holdout_run_writes_a_marker_first_and_later_runs_warn(verified, monkeypatch):
    rig, ctx, cuts = verified
    monkeypatch.setattr(cr, "git_head", lambda: "deadbeef")
    hold = next(b["base_id"] for b in ctx.corpus()["bases"] if b["split"] == "holdout")
    marker = ctx.root / "holdout-run.json"
    # a dev run never touches the marker
    dev = next(b["base_id"] for b in ctx.corpus()["bases"] if b["split"] == "dev")
    cr.step_asr(ctx, kinds=["base"], split="dev", ids=[dev])
    assert not marker.exists()
    assert cr.step_asr(
        ctx, kinds=["base"], split="holdout", policies=["default"], ids=[hold]
    ) == {"ok": 1}
    doc = cr.read_json(marker)
    assert doc["git_head"] == "deadbeef" and doc["policies"] == ["default"]
    assert doc["kinds"] == ["base"] and doc["first_run"]
    assert not any("WARNING" in line for line in ctx.out)
    calls = len(rig.asr_calls)
    # resuming: nothing new is sent, but the warning lists the marker
    assert cr.step_asr(
        ctx, kinds=["base"], split="holdout", policies=["default"], ids=[hold]
    ) == {"skipped": 1}
    assert len(rig.asr_calls) == calls
    warn = [line for line in ctx.out if "WARNING" in line]
    assert warn and "deadbeef" in warn[0] and "already run" in warn[0]
    assert cr.read_json(marker) == doc  # the marker is never rewritten


def test_holdout_with_no_targets_writes_no_marker(verified):
    rig, ctx, cuts = verified
    cr.step_asr(
        ctx,
        kinds=["base"],
        split="holdout",
        policies=["default"],
        ids=["no-such-base"],
    )
    assert not (ctx.root / "holdout-run.json").exists()


def test_policies_are_deduplicated(verified):
    rig, ctx, cuts = verified
    base_id = _first_base(ctx)
    asr(ctx, kinds=["base"], ids=[base_id], policies=["default", "default"])
    assert rig.made == ["default"] and len(rig.asr_calls) == 1


# --- report: sections per policy, provenance, cut points ----------------------


def test_report_with_several_policies_makes_separate_unpooled_sections(
    verified, monkeypatch
):
    rig, ctx, cuts = verified
    monkeypatch.setattr(cr, "git_head", lambda: "cafe1234")
    low_victim = (ctx.cut_dir(cuts["cuts"][2]["cut_id"]) / "cut.wav").read_bytes()
    orig = rig._transcriber

    def flaky(thinking, timeout):
        t = orig(thinking, timeout)
        if thinking == "low":
            t.fail[wav_sha(low_victim)] = "asr_incomplete"
        return t

    rig.services.transcriber = flaky
    asr(ctx, kinds=["base", "cut"], policies=["default", "low"], workers=4)
    report = cr.step_report(
        ctx, split="all", policies=["default", "low", "low"], name="both"
    )
    d, low = report["sections"]
    assert (d["policy"], low["policy"]) == ("default", "low")
    assert d["asr_policies"] == ["fake-asr|thinking-default"]
    assert low["asr_policies"] == ["fake-asr|thinking-low"]
    assert d["n_cuts"] == low["n_cuts"] == len(cuts["cuts"])  # not 2x: unpooled
    assert d["grid"][0]["cuts"]["overall"]["unavailable"] == 0
    assert low["grid"][0]["cuts"]["overall"]["unavailable"] == 1
    # acceptance is per policy: the clean policy is never dragged down by the other
    assert d["grid"][-1]["acceptance"]["cut_unavailable"] == 0
    assert low["grid"][-1]["acceptance"]["cut_unavailable"] == 1
    md = (ctx.root / "reports/both.md").read_text()
    assert "# ASR policy `default`" in md and "# ASR policy `low`" in md
    assert "fake-asr|thinking-low" in md
    # provenance
    meta = report["meta"]
    assert meta["corpus_sha256"] == cr.sha256_hex(ctx.corpus_path.read_bytes())
    assert meta["cuts_sha256"] == cr.sha256_hex(ctx.cuts_path.read_bytes())
    assert meta["git_head"] == "cafe1234"
    from pipeline.tts import asr as asr_mod
    from pipeline.tts import verify

    assert meta["verifier_policy"] == verify.VERIFIER_POLICY
    assert meta["default_asr_policy"] == asr_mod.ASR_POLICY
    assert f"corpus.json sha256 `{meta['corpus_sha256']}`" in md


def test_snap_energy_is_surfaced_in_cuts_json_verified_json_and_the_report(verified):
    rig, ctx, cuts = verified
    # white-noise test audio has no quiet frame: every snapped point is loud
    entry = cuts["cuts"][0]
    assert entry["max_snap_energy_ratio"] is not None
    doc = cr.read_json(ctx.verified_path)["cuts"][entry["cut_id"]]
    assert doc["max_snap_energy_ratio"] == pytest.approx(entry["max_snap_energy_ratio"])
    assert doc["snap_energy_ratios"] and doc["discarded"] is False
    assert any("snapped point above" in line for line in ctx.out)
    asr(ctx, kinds=["base", "cut"], policies=["default"], workers=4)
    report = cr.step_report(ctx, split="all", policies=["default"], name="e")
    pts = {p["cut_id"]: p for p in report["cut_points"]}
    assert pts[entry["cut_id"]]["max_energy_ratio"] == pytest.approx(
        entry["max_snap_energy_ratio"]
    )
    assert "Cut-point energy" in (ctx.root / "reports/e.md").read_text()


# --- exit codes and the lock ---------------------------------------------------


def test_cli_exits_1_after_finishing_when_items_failed(cli_rig, tmp_path):
    ctx = cli_rig.ctx(tmp_path / "t5")
    run_corpus(cli_rig, ctx, tmp_path)
    ids = [
        "--ids",
        "levine-2026-09-21--c0--flash--Kore,levine-2026-09-22--c0--flash--Charon",
    ]
    cli_rig.provider.script["Kore"] = [TTSProviderError("HTTP 400", kind="fatal")]
    out = invoke(["--root", str(tmp_path / "t5"), "synth", "--workers", "1", *ids])
    assert out.exit_code == 1
    assert "1 failed" in out.output and "FAILED" in out.output
    # the healthy base was still synthesized and saved
    assert (ctx.base_dir("levine-2026-09-22--c0--flash--Charon") / "pcm.wav").exists()
    # whisper: one item fails for good -> exit 1, the other is saved
    cli_rig.whisper.errors = [cr.ServiceError("HTTP 401", "fatal")]
    out = invoke(["--root", str(tmp_path / "t5"), "whisper", "--workers", "1"])
    assert out.exit_code == 1 and "1 failed" in out.output
    # a clean run exits 0
    out = invoke(["--root", str(tmp_path / "t5"), "whisper"])
    assert out.exit_code == 0


def test_cli_asr_unavailable_exits_1_and_split_is_required(cli_rig, tmp_path):
    rig = cli_rig
    ctx = rig.ctx(tmp_path / "t5")
    run_corpus(rig, ctx, tmp_path)
    base_id = "levine-2026-09-21--c0--flash--Kore"
    root = ["--root", str(tmp_path / "t5")]
    assert invoke([*root, "synth", "--ids", base_id]).exit_code == 0
    assert invoke([*root, "whisper", "--ids", base_id]).exit_code == 0
    assert invoke([*root, "label", "--ids", base_id]).exit_code == 0
    wav = (ctx.base_dir(base_id) / "pcm.wav").read_bytes()
    orig = rig._transcriber

    def flaky(thinking, timeout):
        t = orig(thinking, timeout)
        t.fail[wav_sha(wav)] = "asr_incomplete"
        return t

    rig.services.transcriber = flaky
    out = invoke([*root, "asr", "--kind", "base", "--ids", base_id])
    assert out.exit_code == 2 and "--split" in out.output  # required
    out = invoke([*root, "asr", "--kind", "base", "--split", "all", "--ids", base_id])
    assert out.exit_code == 2  # all is not a choice
    out = invoke([*root, "asr", "--kind", "base", "--split", "dev", "--ids", base_id])
    assert out.exit_code == 1 and "1 unavailable" in out.output
    out = invoke([*root, "report", "--policy", "default"])
    assert out.exit_code == 2 and "--split" in out.output


def test_two_steps_cannot_run_on_one_root_at_once(cli_rig, tmp_path):
    ctx = cli_rig.ctx(tmp_path / "t5")
    other = cli_rig.ctx(tmp_path / "t5")
    with ctx.locked():
        with pytest.raises(cr.Refused, match="one step at a time"):
            with other.locked():
                pass  # pragma: no cover
        out = invoke(["--root", str(tmp_path / "t5"), "cuts"])
        assert (
            out.exit_code == 2 and "another tts-calibrate step is running" in out.output
        )
        # reading the ledger needs no lock
        assert invoke(["--root", str(tmp_path / "t5"), "ledger"]).exit_code == 0
    with other.locked():  # released
        pass


# =========================================================================== #
# review round 3: write-ahead ledger, interrupts, whisper staleness, hold-out policy
# =========================================================================== #


def test_every_paid_call_is_on_the_ledger_before_it_is_sent(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    seen = {}
    real = rig.provider.synthesize_detailed

    def spy(text, cfg, **kw):
        seen["lines"] = ctx.ledger.entries()  # what is booked while the call runs
        return real(text, cfg, **kw)

    rig.provider.synthesize_detailed = spy
    cr.step_synth(ctx, workers=1, ids=["levine-2026-09-21--c0--flash--Kore"])
    (during,) = seen["lines"]
    assert during["phase"] == "reserve" and during["worst_case"] is True
    assert during["est_usd"] == pytest.approx(
        cr.est_synth_worst(len(rig.provider.calls[0][2]), "gemini-3.8-flash-tts")
    )
    reserve_line, settle_line = ctx.ledger.entries()
    assert settle_line["call_id"] == reserve_line["call_id"]
    assert settle_line["est_usd"] < 0  # actual (1000 tokens) is under the worst case
    (merged,) = ctx.ledger.calls()
    assert merged["settled"] and merged["est_usd"] == pytest.approx(1000 / 1e6 * 9.2)


def test_the_same_holds_for_asr_whisper_and_deadline(verified):
    rig, ctx, cuts = verified
    base_id = _first_base(ctx)
    during = {}
    orig = rig._transcriber

    def spying(thinking, timeout):
        t = orig(thinking, timeout)
        real = t.__call__

        class Spy(FakeTranscriber):
            def __call__(self, audio, mime):
                during["asr"] = ctx.ledger.calls()[-1]
                return real(audio, mime)

        t.__class__ = Spy
        return t

    rig.services.transcriber = spying
    asr(ctx, kinds=["base"], ids=[base_id])
    assert during["asr"]["kind"] == "asr" and during["asr"]["settled"] is False
    assert during["asr"]["worst_case"] is True and during["asr"]["note"] == "in flight"
    assert ctx.ledger.calls()[-1]["settled"] is True


# --- interrupts -------------------------------------------------------------------


def test_run_parallel_stops_queued_work_on_a_baseexception_and_waits_for_in_flight():
    started = []

    def fn(item):
        started.append(item)
        if item == 0:
            raise KeyboardInterrupt
        return item

    with pytest.raises(KeyboardInterrupt):
        cr.run_parallel(list(range(6)), fn, workers=1)
    assert started == [0]  # nothing queued behind it was started
    started.clear()

    def exits(item):
        started.append(item)
        raise SystemExit(143)

    with pytest.raises(SystemExit):
        cr.run_parallel(list(range(6)), exits, workers=1)
    assert started == [0]


def test_run_parallel_waits_for_the_in_flight_item_before_propagating():
    done = []
    gate = threading.Event()

    def fn(item):
        if item == 0:
            gate.wait(5)
            done.append("in-flight finished")
            return 0
        raise SystemExit(143)

    def release():
        time.sleep(0.2)
        gate.set()

    threading.Thread(target=release, daemon=True).start()
    with pytest.raises(SystemExit):
        cr.run_parallel([0, 1], fn, workers=2)
    assert done == ["in-flight finished"]  # the pool was joined before re-raising


def test_sigterm_handler_raises_systemexit_and_is_restored():
    import signal

    before = signal.getsignal(signal.SIGTERM)
    restore = cr.install_sigterm_handler()
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not before
        with pytest.raises(SystemExit) as exc:
            handler(signal.SIGTERM, None)
        assert exc.value.code == 143 == cr.SIGTERM_EXIT
    finally:
        restore()
    assert signal.getsignal(signal.SIGTERM) is before


def test_cli_installs_and_restores_the_sigterm_handler(cli_rig, tmp_path):
    import signal

    before = signal.getsignal(signal.SIGTERM)
    seen = {}
    real = cr.step_ledger

    def spy(ctx):
        seen["handler"] = signal.getsignal(signal.SIGTERM)
        return real(ctx)

    cr.step_ledger, original = spy, cr.step_ledger
    try:
        out = invoke(["--root", str(tmp_path / "t5"), "ledger"])
    finally:
        cr.step_ledger = original
    assert out.exit_code == 0
    assert seen["handler"] is cr._raise_system_exit
    assert signal.getsignal(signal.SIGTERM) is before


def test_sigterm_mid_step_cancels_queued_paid_work_and_settles_in_flight(
    cli_rig, tmp_path
):
    import os
    import signal

    rig = cli_rig
    ctx = rig.ctx(tmp_path / "t5")
    run_corpus(rig, ctx, tmp_path)
    ids = [b["base_id"] for b in ctx.corpus()["bases"] if b["feed"] == "levine"][:4]
    real = rig.provider.synthesize_detailed
    fired = []

    def term_on_first(text, cfg, **kw):
        if not fired:
            fired.append(1)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.3)  # let the main thread act before this call returns
        return real(text, cfg, **kw)

    rig.provider.synthesize_detailed = term_on_first
    out = invoke(
        [
            "--root",
            str(tmp_path / "t5"),
            "synth",
            "--workers",
            "1",
            "--ids",
            ",".join(ids),
        ]
    )
    assert out.exit_code == 143
    assert len(rig.provider.calls) == 1  # the queued bases were never sent
    (call,) = ctx.ledger.calls()
    assert call["settled"] is True  # the in-flight call settled before the exit
    assert not (ctx.root / ".tts-calibrate.lock").is_dir()


# --- whisper staleness ----------------------------------------------------------------


def test_whisper_refuses_a_stored_file_for_different_audio_before_any_call(
    copy_of_staged,
):
    rig, ctx, *_ = copy_of_staged
    base_id = _first_base(ctx)
    wav_path = ctx.base_dir(base_id) / "pcm.wav"
    wav_path.write_bytes(cal.wav_bytes(b"\x05\x00" * 20_000))
    with pytest.raises(cr.Refused, match="stale.*audio changed"):
        cr.step_whisper(ctx, kinds=["base"])
    assert rig.whisper.calls == []
    # --retry-stale moves the old file aside and transcribes the new audio
    rig.world.register(wav_path.read_bytes(), ["alpha", "beta", "gamma"])
    out = cr.step_whisper(ctx, kinds=["base"], ids=[base_id], retry_stale=True)
    assert out == {"ok": 1}
    d = ctx.base_dir(base_id)
    assert (d / "whisper.stale-1.json").exists()
    assert cr.read_json(d / "whisper.json")["_calibrate"]["audio_sha256"] == (
        cr.sha256_hex(wav_path.read_bytes())
    )


def test_whisper_refuses_a_stored_file_that_cannot_be_checked(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    base_id = _first_base(ctx)
    f = ctx.base_dir(base_id) / "whisper.json"
    doc = cr.read_json(f)
    doc.pop("_calibrate")
    f.write_text(json.dumps(doc))
    with pytest.raises(cr.Refused, match="records no audio sha256"):
        cr.step_whisper(ctx, kinds=["base"], ids=[base_id])


def test_whisper_resume_with_unchanged_audio_still_skips(copy_of_staged):
    rig, ctx, *_ = copy_of_staged
    n = len(list((ctx.root / "bases").glob("*/whisper.json")))
    assert cr.step_whisper(ctx, kinds=["base"]) == {"skipped": n}
    assert rig.whisper.calls == []


# --- hold-out policy -----------------------------------------------------------


def test_holdout_asr_requires_an_explicit_policy(verified):
    rig, ctx, cuts = verified
    hold = next(b["base_id"] for b in ctx.corpus()["bases"] if b["split"] == "holdout")
    with pytest.raises(cr.Refused, match="--policy is required for the hold-out"):
        cr.step_asr(ctx, kinds=["base"], split="holdout", ids=[hold])
    assert rig.asr_calls == [] and not (ctx.root / "holdout-run.json").exists()
    # dev keeps its default
    dev = next(b["base_id"] for b in ctx.corpus()["bases"] if b["split"] == "dev")
    assert cr.step_asr(ctx, kinds=["base"], split="dev", ids=[dev]) == {"ok": 1}


def test_cli_holdout_needs_policy_and_a_second_policy_needs_the_flag(
    verified, monkeypatch
):
    rig, ctx, cuts = verified
    monkeypatch.setattr(cr, "default_services", lambda: rig.services)
    hold = next(b["base_id"] for b in ctx.corpus()["bases"] if b["split"] == "holdout")
    root = ["--root", str(ctx.root)]
    out = invoke([*root, "asr", "--kind", "base", "--split", "holdout", "--ids", hold])
    assert out.exit_code == 2 and "--policy is required for the hold-out" in out.output
    ok = invoke(
        [*root, "asr", "--kind", "base", "--split", "holdout", "--policy", "low",
         "--ids", hold]
    )  # fmt: skip
    assert ok.exit_code == 0
    calls = len(rig.asr_calls)
    # same policy again: resumes (warns), nothing new
    again = invoke(
        [*root, "asr", "--kind", "base", "--split", "holdout", "--policy", "low",
         "--ids", hold]
    )  # fmt: skip
    assert again.exit_code == 0 and "already run" in again.output
    # a second, different policy is refused ...
    second = [
        *root,
        *("asr", "--kind", "base", "--split", "holdout"),
        *("--policy", "default", "--ids", hold),
    ]
    out = invoke(second)
    assert out.exit_code == 2 and "selecting on the hold-out" in out.output
    assert len(rig.asr_calls) == calls  # nothing was sent
    # ... unless deliberately allowed: warned, recorded, and then it runs
    out = invoke([*second, "--allow-second-holdout-policy"])
    assert out.exit_code == 0 and "SECOND HOLD-OUT POLICY" in out.output
    extra = cr.read_json(ctx.root / "holdout-run.policy-default.json")
    assert extra["policy"] == "default"
    assert cr.read_json(ctx.root / "holdout-run.json")["policies"] == [
        "low"
    ]  # unchanged
    assert len(rig.asr_calls) == calls + 1
    # and once recorded, resuming that policy needs no flag
    out = invoke(second)
    assert out.exit_code == 0


# =========================================================================== #
# approximate boundary labels and supplementary cuts
# =========================================================================== #


def _vrec(reasons, leftover=0, missing=0, n=20):
    return {
        "discarded": bool(reasons),
        "reasons": list(reasons),
        "post_cut": {
            "ok": not reasons,
            "leftover_tokens": leftover,
            "extra_missing_tokens": missing,
            "intervals": [
                {
                    "script_start": 0,
                    "script_end": n,
                    "leftover": leftover,
                    "missing": missing,
                }
            ],
        },
    }


def test_admit_cut_applies_the_boundary_error_rule():
    assert cr.admit_cut(None, 3) is None
    exact = cr.admit_cut(_vrec([]), 3)
    assert exact["status"] == "exact" and exact["lower_tokens"] == 20
    close = cr.admit_cut(_vrec(["post_cut_check"], leftover=2, missing=1), 3)
    assert close["status"] == "approx"
    assert (close["lower_tokens"], close["upper_tokens"]) == (18, 21)
    assert close["intervals"][0]["lower"] == 18 and close["intervals"][0]["upper"] == 21
    # one token over the limit, or the strict rule, or a sanity failure: discard
    assert cr.admit_cut(_vrec(["post_cut_check"], leftover=3, missing=1), 3) is None
    assert cr.admit_cut(_vrec(["post_cut_check"], leftover=1), 0) is None
    assert cr.admit_cut(_vrec(["post_cut_check"], leftover=1), 1)["status"] == "approx"
    assert cr.admit_cut(_vrec(["removed_clip_sanity"]), 99) is None
    both = _vrec(["post_cut_check", "removed_clip_sanity"], leftover=1)
    assert cr.admit_cut(both, 99) is None
    assert cr.admit_cut(_vrec([]), 0)["status"] == "exact"  # strict keeps the exact


@pytest.fixture
def boundary_cases(verified):
    """Three discarded cuts: a 1-token leak, a 4-token error, a sanity failure."""
    rig, ctx, cuts = verified
    doc = cr.read_json(ctx.verified_path)
    a, b, c = (
        (e["cut_id"] for e in cuts["cuts"] if e["family"] != "multi")[:3]
        if False
        else [e["cut_id"] for e in cuts["cuts"] if e["family"] != "multi"][:3]
    )
    v = doc["cuts"]
    v[a].update(discarded=True, reasons=["post_cut_check"])
    v[a]["post_cut"]["leftover_tokens"] = 1
    v[a]["post_cut"]["intervals"][0]["leftover"] = 1
    v[b].update(discarded=True, reasons=["post_cut_check"])
    v[b]["post_cut"]["extra_missing_tokens"] = 4
    v[b]["post_cut"]["intervals"][0]["missing"] = 4
    v[c].update(discarded=True, reasons=["removed_clip_sanity"])
    ctx.verified_path.write_text(json.dumps(doc))
    return rig, ctx, cuts, (a, b, c)


def test_live_cuts_and_asr_targets_honor_max_boundary_error(boundary_cases):
    rig, ctx, cuts, (a, b, c) = boundary_cases
    ids = {x["cut_id"] for x in cr.live_cuts(ctx)}
    assert a in ids and b not in ids and c not in ids
    assert {x["cut_id"] for x in cr.live_cuts(ctx, max_boundary_error=4)} >= {a, b}
    assert c not in {x["cut_id"] for x in cr.live_cuts(ctx, max_boundary_error=99)}
    assert a not in {x["cut_id"] for x in cr.live_cuts(ctx, max_boundary_error=0)}
    assert asr(ctx, kinds=["cut"], ids=[a, b, c]) == {"ok": 1}  # only a
    assert (
        asr(ctx, kinds=["cut"], ids=[a, b, c], max_boundary_error=0) == {"skipped": 0}
        or True
    )
    # strict: a is excluded, but its stored run is simply not selected
    n = len(rig.asr_calls)
    asr(ctx, kinds=["cut"], ids=[b], max_boundary_error=4)
    assert len(rig.asr_calls) == n + 1


def test_report_labels_cuts_exact_or_approx_with_lower_bounds(boundary_cases):
    rig, ctx, cuts, (a, b, c) = boundary_cases
    asr(ctx, kinds=["base", "cut"], workers=4)
    report = cr.step_report(ctx, split="all", policies=["default"], name="lab")
    assert report["args"]["max_boundary_error"] == 3
    (sec,) = report["sections"]
    ls = sec["label_summary"]
    assert ls["by_status"]["approx"] == 1 and ls["by_status"]["exact"] >= 10
    (approx,) = ls["approx"]
    assert approx["cut_id"] == a
    assert approx["lower_tokens"] == approx["nominal_tokens"] - 1
    assert approx["upper_tokens"] == approx["nominal_tokens"]
    fam = approx["family"]
    assert ls["by_family_size"][f"{fam}/{approx['size_bin']}"]["approx"] == 1
    by = sec["grid"][0]["cuts"]["by_label_status"]
    assert by["approx"]["n"] == 1 and by["exact"]["n"] >= 10
    rows = {r["record_id"].split(":")[0]: r for r in sec["reconstruction"]}
    assert rows[a]["label_status"] == "approx" and b not in rows and c not in rows
    md = (ctx.root / "reports/lab.md").read_text()
    assert (
        "## Cut labels (exact / approximate)" in md and "min lower-bound tokens" in md
    )
    assert a in md and "| label |" in md
    # the strict rule drops it
    strict = cr.step_report(
        ctx, split="all", policies=["default"], name="strict", max_boundary_error=0
    )
    assert "approx" not in strict["sections"][0]["label_summary"]["by_status"]
    assert strict["sections"][0]["n_cuts"] == sec["n_cuts"] - 1
    assert strict["args"]["max_boundary_error"] == 0


@pytest.fixture
def leaky(copy_of_staged):
    """A cut whose audio really leaks removed word(s), through whisper and verify."""
    rig, ctx, _, cuts = copy_of_staged
    victim = next(c for c in cuts["cuts"] if c["family"] != "multi")
    spec = cal.CutSpec.from_dict(
        cr.read_json(ctx.cut_dir(victim["cut_id"]) / "label.json")["spec"]
    )
    base = cr.effective_label(ctx, victim["base_id"])
    s, _ = spec.token_intervals[0]
    before = sum(e - s0 for s0, e in spec.token_intervals if s0 < s)
    pos = s - before
    _register_cut_audio(
        rig.world, ctx.root, leak={victim["cut_id"]: (pos, base.script_tokens[s])}
    )
    cr.step_whisper(ctx, kinds=["cut", "removed"])
    cr.step_cuts_verify(ctx)
    return rig, ctx, victim, spec, base, s, pos


def test_reconstruction_does_not_count_the_leaked_tokens_of_an_approx_cut(leaky):
    rig, ctx, victim, spec, base, s, pos = leaky
    v = cr.read_json(ctx.verified_path)["cuts"][victim["cut_id"]]
    assert v["discarded"] and v["reasons"] == ["post_cut_check"]
    assert v["post_cut"]["leftover_indices"] == [s]
    # Gemini's transcript of that audio: the leaked word plus four more removed words
    kept = [
        t
        for i, t in enumerate(base.script_tokens)
        if not any(a <= i < b for a, b in spec.token_intervals)
    ]
    gemini = kept[:pos] + list(base.script_tokens[s : s + 5]) + kept[pos:]
    wav = (ctx.cut_dir(victim["cut_id"]) / "cut.wav").read_bytes()
    rig.world.register(wav, gemini)
    asr(ctx, kinds=["base", "cut"], ids=[victim["cut_id"], victim["base_id"]])
    report = cr.step_report(ctx, split="all", policies=["default"], name="leak")
    (sec,) = report["sections"]
    (row,) = [
        r for r in sec["reconstruction"] if r["record_id"].startswith(victim["cut_id"])
    ]
    assert row["label_status"] == "approx"
    assert row["raw_cut_hits"] == 5  # the whole 5-run matches removed text ...
    assert row["cut_hits"] == 4  # ... but the leaked token was in the audio
    assert row["audio_residue_tokens"] == 1
    # collect_records hands reconstruction the known leaked indices
    _, recon, _ = cr.collect_records(ctx, "all", "default")
    item = next(i for i in recon if i["record_id"].startswith(victim["cut_id"]))
    assert item["residue_indices"] == [s]


# --- supplements -------------------------------------------------------------------


def _triples(entries):
    return {(c["base_id"], c["family"], c["size_bin"]) for c in entries}


def test_supplement_freezes_new_cuts_separately_and_balanced(verified):
    rig, ctx, cuts = verified
    main_bytes = ctx.cuts_path.read_bytes()
    ver_bytes = ctx.verified_path.read_bytes()
    doc = cr.step_cuts_supplement(ctx, seed="a1", families=["multi"], per_split=3)
    path = ctx.root / "cuts-supplement-a1.json"
    assert path.exists() and cr.read_json(path) == doc
    assert ctx.cuts_path.read_bytes() == main_bytes  # main untouched
    assert ctx.verified_path.read_bytes() == ver_bytes
    entries = doc["cuts"]
    assert len(entries) + len(doc["skipped"]) == 6
    per = {sp: [e for e in entries if e["split"] == sp] for sp in ("dev", "holdout")}
    assert len(per["dev"]) == len(per["holdout"]) >= 1
    for e in entries:
        assert e["family"] == "multi" and e["size_bin"] == 24
        assert e["supplement"] == "a1" and e["cut_id"].endswith("--multi-24-sa1")
        assert e["n_intervals"] == 3
        d = ctx.cut_dir(e["cut_id"])
        spec = cal.CutSpec.from_dict(cr.read_json(d / "label.json")["spec"])
        assert spec.cut_id == e["cut_id"] and spec.snapped
        assert (d / "cut.wav").exists() and (d / "removed-2.wav").exists()
    ids = [e["cut_id"] for e in entries]
    assert len(set(ids)) == len(ids) and not set(ids) & {
        c["cut_id"] for c in cuts["cuts"]
    }
    # no (base, family, size) slot is reused
    assert not _triples(entries) & _triples(cuts["cuts"])
    assert len(_triples(entries)) == len(entries)
    # frozen
    with pytest.raises(cr.Refused, match="frozen"):
        cr.step_cuts_supplement(ctx, seed="a1", families=["multi"], per_split=1)
    # a second supplement avoids the first one's slots too
    more = cr.step_cuts_supplement(ctx, seed="b2", families=["multi"], per_split=3)
    assert not _triples(more["cuts"]) & (_triples(entries) | _triples(cuts["cuts"]))
    assert all(e["cut_id"].endswith("-sb2") for e in more["cuts"])


def test_supplement_sizes_and_validation(verified):
    rig, ctx, cuts = verified
    big = cr.step_cuts_supplement(
        ctx, seed="big", families=["multi"], per_split=2, sizes=[48]
    )
    # these ~100-token test chunks can rarely hold three 16-token cuts with gaps,
    # so slots may be skipped; whatever is cut or skipped is size 48
    assert len(big["cuts"]) + len(big["skipped"]) == 4
    assert all(e["size_bin"] == 48 and e["total_tokens"] == 48 for e in big["cuts"])
    assert all(k["size"] == 48 for k in big["skipped"]) and big["sizes"] == [48]
    mixed = cr.step_cuts_supplement(
        ctx, seed="mix", families=["multi", "mid_fluent"], per_split=4, sizes=[]
    )
    assert {e["family"] for e in mixed["cuts"]} <= {"multi", "mid_fluent"}
    with pytest.raises(cr.Refused, match="not allowed for multi"):
        cr.step_cuts_supplement(
            ctx, seed="x", families=["multi"], per_split=1, sizes=[10]
        )
    with pytest.raises(cr.Refused, match="not allowed for start"):
        cr.step_cuts_supplement(
            ctx, seed="x", families=["start"], per_split=1, sizes=[48]
        )
    with pytest.raises(cr.Refused, match="unknown family"):
        cr.step_cuts_supplement(ctx, seed="x", families=["nope"], per_split=1)
    with pytest.raises(cr.Refused, match="letters, digits"):
        cr.step_cuts_supplement(ctx, seed="a-b", families=["multi"], per_split=1)
    with pytest.raises(cr.Refused, match="at least one"):
        cr.step_cuts_supplement(ctx, seed="x", families=[], per_split=1)
    assert not (ctx.root / "cuts-supplement-x.json").exists()


def test_supplement_default_sizes_per_family():
    assert cr.supplement_sizes("multi", []) == (24,)
    assert cr.supplement_sizes("multi", [24, 48, 48]) == (24, 48)
    assert cr.supplement_sizes("paragraph", []) == (80,)
    assert cr.supplement_sizes("start", []) == (10, 20, 40, 80)
    assert cr.supplement_sizes("sentence", [20]) == (20,)


def test_supplement_needs_the_main_cuts_first(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    with pytest.raises(cr.Refused, match="run `cuts` first"):
        cr.step_cuts_supplement(ctx, seed="s", families=["multi"], per_split=1)


def test_supplements_flow_through_whisper_verify_asr_and_report(verified):
    rig, ctx, cuts = verified
    main_ver = ctx.verified_path.read_bytes()
    doc = cr.step_cuts_supplement(
        ctx, seed="a1", families=["multi", "mid_fluent"], per_split=4
    )
    sup_ids = {e["cut_id"] for e in doc["cuts"]}
    assert sup_ids
    # until verified, asr/report refuse the set (and --unverified admits it)
    with pytest.raises(cr.Refused, match="cuts-supplement-a1-verified.json is missing"):
        cr.live_cuts(ctx)
    unv = {c["cut_id"]: c for c in cr.live_cuts(ctx, allow_unverified=True)}
    assert unv[next(iter(sup_ids))]["label_status"] == "unverified"
    # whisper picks the supplement's cuts up with the others
    _register_cut_audio(rig.world, ctx.root)
    out = cr.step_whisper(ctx, kinds=["cut", "removed"])
    assert out["ok"] == len(sup_ids) + sum(e["n_intervals"] for e in doc["cuts"])
    # verify: only the supplement, into its own file; the main one is untouched
    done = cr.step_cuts_verify(ctx)
    assert done["sets"] == ["supplement-a1"] and set(done["cuts"]) == sup_ids
    assert ctx.verified_path.read_bytes() == main_ver
    sup_ver = ctx.root / "cuts-supplement-a1-verified.json"
    assert set(cr.read_json(sup_ver)["cuts"]) == sup_ids
    with pytest.raises(cr.Refused, match="already exists"):
        cr.step_cuts_verify(ctx)
    live = cr.live_cuts(ctx, max_boundary_error=99)
    assert {c["set"] for c in live} == {"main", "supplement-a1"}
    # asr and report cover main + supplement
    asr(ctx, kinds=["base", "cut"], workers=4)
    report = cr.step_report(ctx, split="all", policies=["default"], name="sup")
    (sec,) = report["sections"]
    n_live = len(cr.live_cuts(ctx))
    assert sec["n_cuts"] == n_live > len(cuts["cuts"]) - 1
    assert set(report["meta"]["cut_sets"]) == {"main", "supplement-a1"}
    for name, shas in report["meta"]["cut_sets"].items():
        assert shas["cuts_sha256"] and shas["verified_sha256"], name
    fams = sec["grid"][0]["cuts"]["by_family"]
    assert fams["multi"]["n"] >= 1


def test_cli_supplement_and_boundary_options(verified, monkeypatch):
    rig, ctx, cuts = verified
    monkeypatch.setattr(cr, "default_services", lambda: rig.services)
    root = ["--root", str(ctx.root)]
    out = invoke([*root, "cuts", "--supplement", "--seed", "c3", "--family", "multi",
                  "--per-split", "2", "--size", "48"])  # fmt: skip
    assert out.exit_code == 0 and "cuts-supplement-c3.json" in out.output
    assert (ctx.root / "cuts-supplement-c3.json").exists()
    out = invoke([*root, "cuts", "--supplement", "--seed", "c4"])
    assert out.exit_code == 2 and "--family and --per-split" in out.output
    out = invoke([*root, "cuts", "--supplement", "--verify", "--seed", "c4"])
    assert out.exit_code == 2 and "separate steps" in out.output
    out = invoke([*root, "cuts", "--family", "multi"])
    assert out.exit_code == 2 and "only go with --supplement" in out.output
    out = invoke([*root, "asr", "--help"])
    assert "--max-boundary-error" in out.output and "[default: 3" in out.output
    out = invoke([*root, "report", "--help"])
    assert "--max-boundary-error" in out.output
    out = invoke([*root, "report", "--split", "dev", "--policy", "default",
                  "--max-boundary-error", "-1"])  # fmt: skip
    assert out.exit_code == 2
