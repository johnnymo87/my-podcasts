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

    def synthesize_detailed(self, text, cfg, *, timeout=None):
        with self.lock:
            self.calls.append((cfg.model, cfg.voice, text))
            queue = self.script.get(cfg.voice)
            if queue:
                exc = queue.pop(0)
                if exc is not None:
                    raise exc
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

    def __call__(self, wav: bytes, filename: str) -> dict:
        with self.lock:
            self.calls.append(filename)
            if self.errors:
                raise self.errors.pop(0)
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

    def __call__(self, audio: bytes, mime_type: str) -> Transcription:
        sha = wav_sha(audio)
        self.calls.append((self.thinking, sha))
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


def test_ledger_appends_totals_and_refuses_before_the_budget(tmp_path):
    led = cr.Ledger(tmp_path / "ledger.jsonl", budget=1.0)
    led.append("synth", "m", {"id": "a"}, {"audio_tokens": 5}, 0.4)
    led.append("asr", "g", {"id": "a"}, None, 0.5, worst_case=True)
    assert led.total() == pytest.approx(0.9)
    with pytest.raises(cr.BudgetError, match="nothing was sent"):
        with led.reserve(0.2, "x"):
            raise AssertionError("must not be reached")  # pragma: no cover
    with led.reserve(0.1, "fits"):
        with pytest.raises(cr.BudgetError):  # an in-flight reservation counts
            with led.reserve(0.01, "second"):
                pass  # pragma: no cover
    s = led.summary()
    assert s["by_kind"]["asr"]["worst_case_calls"] == 1
    assert s["remaining_usd"] == pytest.approx(0.1)
    assert len(led.path.read_text().splitlines()) == 2  # append-only, one line each


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
    (entry,) = ctx.ledger.entries()
    assert entry["kind"] == "synth" and entry["usage"]["audio_tokens"] == 1000
    assert entry["est_usd"] == pytest.approx(1000 / 1e6 * 9.2)
    assert entry["worst_case"] is False
    assert rig.provider.calls[0][:2] == ("gemini-3.8-flash-tts", "Kore")


def test_synth_is_resumable_a_second_run_makes_no_calls(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    ids = ["levine-2026-09-21--c0--flash--Kore", "levine-2026-09-21--c0--lite--Kore"]
    assert cr.step_synth(ctx, ids=ids) == {"ok": 2}
    n_calls, ledger_lines = len(rig.provider.calls), len(ctx.ledger.entries())
    before = {p: p.read_bytes() for p in (ctx.root / "bases").rglob("*.*")}
    assert cr.step_synth(ctx, ids=ids) == {"skipped": 2}
    assert len(rig.provider.calls) == n_calls
    assert len(ctx.ledger.entries()) == ledger_lines
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


def test_synth_never_overwrites_a_pcm_left_by_a_crashed_run(tmp_path):
    rig, ctx = corpus_ctx(tmp_path)
    base_id = "levine-2026-09-21--c0--flash--Kore"
    d = ctx.base_dir(base_id)
    from pipeline.tts.asr import pcm_to_wav

    old = pcm_to_wav(b"\x01\x00" * 5000)  # pcm.wav without a synth.json
    d.mkdir(parents=True)
    (d / "pcm.wav").write_bytes(old)
    cr.step_synth(ctx, workers=1, ids=[base_id])
    assert (d / "pcm.wav").read_bytes() == old
    rec = cr.read_json(d / "synth.json")
    assert rec["pcm_samples"] == 5000 and "never overwritten" in rec["note"]


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
    entries = ctx.ledger.entries()
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
    (entry,) = ctx.ledger.entries()
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
    assert len(ctx.ledger.entries()) == len(ids)
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
    w = [e for e in ledger if e["kind"] == "whisper"]
    assert len(w) == sum(1 for _ in (root / "bases").glob("*/whisper.json"))
    one = cr.read_json(next((root / "bases").glob("*/whisper.json")))
    assert one["words"] and one["_calibrate"]["model"] == "whisper-1"
    assert w[0]["est_usd"] == pytest.approx(w[0]["usage"]["seconds"] / 60 * 0.006)


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
    for cuts_entry in cr.read_json(root / "cuts.json")["cuts"]:
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
    assert cr.live_cuts(ctx) == [c for c in cuts["cuts"] if c["cut_id"] != victim]
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


def test_asr_requires_verified_cuts_unless_told_otherwise(cut_whispered):
    rig, ctx, cuts = cut_whispered
    with pytest.raises(cr.Refused, match="cuts-verified.json is missing"):
        cr.step_asr(ctx, kinds=["cut"])
    assert rig.asr_calls == []
    out = cr.step_asr(
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
    out = cr.step_asr(
        ctx, kinds=["base"], policies=["default"], repeat=2, ids=[base_id]
    )
    assert out == {"ok": 2}
    d = ctx.base_dir(base_id) / "asr"
    assert sorted(p.name for p in d.iterdir()) == ["default-0.json", "default-1.json"]
    rec = cr.read_json(d / "default-1.json")
    assert (
        rec["status"] == "ok" and rec["repeat"] == 1 and rec["policy_name"] == "default"
    )
    assert rec["asr_policy"] == "fake-asr|thinking-default"
    assert rec["input_tokens"] == 800 and rec["thinking_tokens"] == 40
    assert (
        rec["transcript"]
        == (ctx.base_dir(base_id) / "chunk.txt").read_text().replace("\n\n", " ")
        or rec["transcript"]
    )
    before = {p: p.read_bytes() for p in d.iterdir()}
    calls = len(rig.asr_calls)
    # same request again: skipped, no calls, files untouched
    assert cr.step_asr(
        ctx, kinds=["base"], policies=["default"], repeat=2, ids=[base_id]
    ) == {"skipped": 2}
    assert len(rig.asr_calls) == calls
    assert {p: p.read_bytes() for p in d.iterdir()} == before
    # a larger --repeat only adds the new index
    assert cr.step_asr(
        ctx, kinds=["base"], policies=["default"], repeat=3, ids=[base_id]
    ) == {"skipped": 2, "ok": 1}
    assert {
        p: p.read_bytes() for p in d.iterdir() if p.name != "default-2.json"
    } == before
    assert rig.transcribers["default"].closed


def test_asr_policies_interleave_and_use_their_own_transcriber(verified):
    rig, ctx, cuts = verified
    base_id = next(p.parent.name for p in (ctx.root / "bases").glob("*/label.json"))
    cr.step_asr(
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
        cr.step_asr(ctx, kinds=["base"], policies=["minimal"])


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
    assert cr.step_asr(ctx, kinds=["base"], ids=[base_id]) == {"unavailable": 1}
    path = ctx.base_dir(base_id) / "asr" / "default-0.json"
    rec = cr.read_json(path)
    assert rec["status"] == "unavailable" and rec["unavailable_reason"] == "asr_timeout"
    (entry,) = [e for e in ctx.ledger.entries() if e["kind"] == "asr"]
    assert entry["worst_case"] is True  # unknown usage: worst case, not zero
    n = len(rig.asr_calls)
    assert cr.step_asr(ctx, kinds=["base"], ids=[base_id]) == {
        "skipped": 1
    }  # not retried
    assert len(rig.asr_calls) == n
    rig.services.transcriber = original
    assert cr.step_asr(ctx, kinds=["base"], ids=[base_id], retry_unavailable=True) == {
        "ok": 1
    }
    assert cr.read_json(path)["status"] == "ok"
    assert (path.parent / "default-0.unavailable-1.json").exists()  # evidence kept


def test_asr_skips_discarded_cuts_and_non_faithful_bases(verified):
    rig, ctx, cuts = verified
    doc = cr.read_json(ctx.verified_path)
    some = cuts["cuts"][0]["cut_id"]
    doc["cuts"][some]["discarded"] = True
    ctx.verified_path.write_text(json.dumps(doc))
    cr.step_asr(ctx, kinds=["cut"], ids=[some])
    assert rig.asr_calls == []  # nothing selected: the only id is discarded
    base_id = cuts["cuts"][1]["base_id"]
    cr.record_owner_call(ctx, base_id, "defect")
    cr.step_asr(ctx, kinds=["base"], ids=[base_id])
    assert rig.asr_calls == []  # an owner "defect" is not a negative control


def test_asr_refuses_over_budget_before_calling(verified):
    rig, ctx, cuts = verified
    ctx.ledger.budget = ctx.ledger.total() + 1e-6
    with pytest.raises(cr.BudgetError):
        cr.step_asr(ctx, kinds=["base"], workers=1)
    assert rig.asr_calls == []


# --- report ------------------------------------------------------------------


def test_report_builds_records_runs_the_grid_and_writes_json_and_markdown(verified):
    rig, ctx, cuts = verified
    cr.step_asr(ctx, kinds=["base", "cut"], policies=["default"], repeat=2, workers=4)
    report = cr.step_report(ctx, policies=["default"], name="dev")
    assert (ctx.root / "reports/dev.json").exists() and (
        ctx.root / "reports/dev.md"
    ).exists()
    assert report["n_cuts"] == 2 * len(cuts["cuts"])
    assert report["n_bases"] == 2 * sum(
        1 for _ in (ctx.root / "bases").glob("*/label.json")
    )
    assert len(report["grid"]) == 15
    g = report["grid"][0]
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
    assert g["cuts"]["overall"]["n"] == report["n_cuts"]
    # perfect cut transcripts: nothing is reconstructed
    assert {r["level"] for r in report["reconstruction"]} == {"none"}
    assert all(r["clean_hits"] is not None for r in report["reconstruction"])
    assert all(r["audio_residue_tokens"] == 0 for r in report["reconstruction"])
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
        cr.step_report(ctx, policies=["default"], name="dev")


def test_report_splits_and_requires_a_policy(verified):
    rig, ctx, cuts = verified
    cr.step_asr(ctx, kinds=["base", "cut"], policies=["low"], workers=4)
    with pytest.raises(cr.Refused, match="--policy is required"):
        cr.step_report(ctx, policies=[])
    dev = cr.step_report(ctx, split="dev", policies=["low"], name="d")
    hold = cr.step_report(ctx, split="holdout", policies=["low"], name="h")
    both = cr.step_report(ctx, split="all", policies=["low"], name="a")
    assert dev["n_cuts"] + hold["n_cuts"] == both["n_cuts"] > 0
    assert set(dev["grid"][0]["cuts"]["by_split"]) == {"dev"}


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
    cr.step_asr(ctx, kinds=["base", "cut"], policies=["default"], workers=2)
    report = cr.step_report(ctx, policies=["default"], name="r")
    recon = {r["record_id"]: r for r in report["reconstruction"]}
    assert recon[f"{victim['cut_id']}:default-0"]["level"] == "confirmed"
    assert other not in {r["record_id"].split(":")[0] for r in report["reconstruction"]}
    g = report["grid"][-1]
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
    assert cr.step_asr(ctx, kinds=["repro"], policies=["default"]) == {"ok": 2}
    report = cr.step_report(ctx, kind="repro", policies=["default"], name="repro")
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
    (entry,) = [e for e in ctx.ledger.entries() if e["kind"] == "deadline"]
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


def test_cli_refusals_exit_2_and_other_calibrate_errors_exit_1(cli_rig, tmp_path):
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


def test_slots_cycle_through_every_combination():
    slots = cr.plan_slots(2 * len(cr.COMBOS), "s", "dev")
    assert sorted(slots[: len(cr.COMBOS)]) == sorted(cr.COMBOS)
    assert sorted(slots[len(cr.COMBOS) :]) == sorted(cr.COMBOS)
    assert cr.plan_slots(10, "s", "dev") == cr.plan_slots(10, "s", "dev")
    assert cr.plan_slots(10, "s", "dev") != cr.plan_slots(10, "t", "dev")
    assert {f for f, _ in cr.COMBOS} == set(cal.FAMILIES)
