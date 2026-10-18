from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline.db import PODCAST_MAX_FAILURES, PODCAST_RETRY_BACKOFF, StateStore
from pipeline.podcast_child import ChildOutcome
from pipeline.podcast_executor import run_one_podcast_episode
from pipeline.podcast_feed import PodcastItem
from pipeline.podcast_jobs import admit_item, work_dir_for
from pipeline.podcast_sources import get_podcast_source
from pipeline.podcast_transcribe import MANIFEST_NAME


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SOURCE = get_podcast_source("argument")


def _item(n: int = 1, **overrides) -> PodcastItem:
    kwargs = {
        "guid": f"audioboom-{n}",
        "title": f"Episode {n}",
        "link": f"https://audioboom.com/posts/{n}",
        "pub_date": datetime(2026, 10, 10 + n, 7, 0, tzinfo=UTC),
        "duration_s": 5000,
        "enclosure_url": "https://pscrb.fm/x.mp3?a=1&b=2",
        "description": "desc",
    }
    kwargs.update(overrides)
    return PodcastItem(**kwargs)


class Alerts:
    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = result

    def __call__(self, text: str, severity: str = "info") -> bool:
        self.calls.append((text, severity))
        return self.result


class ExplodingAlerts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, severity: str = "info") -> bool:
        self.calls.append((text, severity))
        raise RuntimeError("pigeon down")


class FakeChild:
    def __init__(self, work_root, outcome: ChildOutcome | None = None, **manifest):
        self.work_root = work_root
        self.calls: list[tuple] = []
        self.outcome = outcome or _ok()
        self.manifest = manifest

    def __call__(self, source, item, work_dir):
        self.calls.append((source.key, item.guid, work_dir))
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "audio.mp3").write_bytes(b"audio")
        (work_dir / "episode.mp3").write_bytes(b"rendered")
        (work_dir / "script.txt").write_text("script", encoding="utf-8")
        if self.outcome.ok:
            (work_dir / MANIFEST_NAME).write_text(
                json.dumps(_manifest(**self.manifest)), encoding="utf-8"
            )
        return self.outcome


def _manifest(decoded=5030.0, rss=5000, pieces=None):
    if pieces is None:
        pieces = [
            {"input_tokens": 100, "output_tokens": 50},
            {"input_tokens": 200, "output_tokens": 70},
        ]
    return {
        "decoded_duration_s": decoded,
        "rss_duration_s": rss,
        "pieces": pieces,
    }


def _ok(elapsed_s: float = 12.5) -> ChildOutcome:
    return ChildOutcome(
        ok=True,
        stage="write",
        reason="",
        code=None,
        detail="",
        result={"ok": True},
        elapsed_s=elapsed_s,
    )


def _bad(stage="transcribe", reason="deadline", code=None, detail="took too long"):
    return ChildOutcome(
        ok=False,
        stage=stage,
        reason=reason,
        code=code,
        detail=detail,
        result=None,
        elapsed_s=3.0,
    )


class Publish:
    def __init__(self, mode="published", exc: Exception | None = None):
        self.mode = mode
        self.exc = exc
        self.calls: list[dict] = []

    def __call__(self, row, work_dir, *, store, r2_client):
        self.calls.append({"row": row, "work_dir": work_dir, "r2": r2_client})
        if self.exc is not None:
            raise self.exc
        return self.mode


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def work_root(tmp_path):
    return tmp_path / "work"


def _admit(store, n=1, **overrides):
    item = _item(n, **overrides)
    admit_item(store, SOURCE, item, via="poll", now=NOW - timedelta(hours=1))
    return item


def _run(store, work_root, child, publish, alerts, now=NOW, r2="r2"):
    return run_one_podcast_episode(
        store,
        r2,
        now=now,
        run_child=child,
        publish=publish,
        alert=alerts,
        work_root=work_root,
    )


def test_nothing_due_returns_none_and_runs_no_child(store, work_root):
    child, publish, alerts = FakeChild(work_root), Publish(), Alerts()
    assert _run(store, work_root, child, publish, alerts) is None
    assert child.calls == []
    assert publish.calls == []


def test_row_not_yet_due_is_not_run(store, work_root):
    item = _item()
    admit_item(store, SOURCE, item, via="poll", now=NOW + timedelta(hours=1))
    child = FakeChild(work_root)
    assert _run(store, work_root, child, Publish(), Alerts()) is None
    assert child.calls == []


def test_happy_path_completes_stores_stats_deletes_audio(store, work_root):
    item = _admit(store)
    child, publish, alerts = FakeChild(work_root), Publish(), Alerts()
    result = _run(store, work_root, child, publish, alerts, r2="the-r2")

    assert result == f"argument/{item.guid}: published"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "completed"
    assert row["last_error"] is None
    stats = json.loads(row["stats_json"])
    assert stats == {
        "decoded_duration_s": 5030.0,
        "rss_duration_s": 5000,
        "decoded_minus_rss_s": 30.0,
        "allowed_s": 400.0,
        "pieces": 2,
        "input_tokens": 300,
        "output_tokens": 120,
        "child_elapsed_s": 12.5,
        "publish": "published",
    }
    work_dir = work_dir_for("argument", item.guid, work_root)
    assert not (work_dir / "audio.mp3").exists()
    assert not (work_dir / "episode.mp3").exists()
    assert (work_dir / "script.txt").exists()
    assert child.calls == [("argument", item.guid, work_dir)]
    assert publish.calls[0]["work_dir"] == work_dir
    assert publish.calls[0]["r2"] == "the-r2"
    assert alerts.calls == []


def test_reconciled_mode_is_reported_and_stored(store, work_root):
    item = _admit(store)
    result = _run(
        store, work_root, FakeChild(work_root), Publish("reconciled"), Alerts()
    )
    assert result == f"argument/{item.guid}: reconciled"
    stats = json.loads(store.get_podcast_episode("argument", item.guid)["stats_json"])
    assert stats["publish"] == "reconciled"


def test_child_deadline_goes_back_to_pending_with_backoff(store, work_root):
    item = _admit(store)
    publish, alerts = Publish(), Alerts()
    child = FakeChild(work_root, _bad(detail="no result"))
    result = _run(store, work_root, child, publish, alerts)

    assert result == f"argument/{item.guid}: failed (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "pending"
    assert row["failure_count"] == 1
    assert row["last_error"].startswith("transcribe: deadline")
    assert datetime.fromisoformat(row["next_attempt_at"]) == NOW + PODCAST_RETRY_BACKOFF
    assert publish.calls == []
    assert alerts.calls == []
    # Not due again until the backoff has passed.
    assert _run(store, work_root, child, publish, alerts) is None
    assert len(child.calls) == 1


def test_failed_audio_is_kept_for_the_retry(store, work_root):
    item = _admit(store)
    _run(store, work_root, FakeChild(work_root, _bad()), Publish(), Alerts())
    work_dir = work_dir_for("argument", item.guid, work_root)
    assert (work_dir / "audio.mp3").exists()


def test_one_undeletable_file_does_not_keep_the_other(store, work_root, monkeypatch):
    item = _admit(store)
    from pathlib import Path

    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self.name == "audio.mp3":
            raise PermissionError("nope")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    _run(store, work_root, FakeChild(work_root), Publish(), Alerts())
    work_dir = work_dir_for("argument", item.guid, work_root)
    assert (work_dir / "audio.mp3").exists()
    assert not (work_dir / "episode.mp3").exists()


def test_fourth_failure_errors_with_exactly_one_error_alert(store, work_root):
    item = _admit(store)
    alerts = Alerts()
    child = FakeChild(work_root, _bad())
    now = NOW
    for n in range(1, PODCAST_MAX_FAILURES + 1):
        result = _run(store, work_root, child, Publish(), alerts, now=now)
        kind = "failed" if n < PODCAST_MAX_FAILURES else "errored"
        assert result == f"argument/{item.guid}: {kind} ({n})"
        if n < PODCAST_MAX_FAILURES:
            assert alerts.calls == []
        now += PODCAST_RETRY_BACKOFF
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "errored"
    assert len(alerts.calls) == 1
    text, severity = alerts.calls[0]
    assert severity == "error"
    assert "Report: 2026-10-11 - The Argument - Episode 1" in text
    assert "gave up after 4 failure(s)" in text
    assert "transcribe: deadline" in text
    # Errored rows are never picked up again.
    assert _run(store, work_root, child, Publish(), alerts, now=now) is None


def test_claim_stage_is_terminal_on_first_failure(store, work_root):
    item = _admit(store)
    alerts = Alerts()
    child = FakeChild(
        work_root, _bad(stage="claim", reason="WorkDirOwned", detail="other episode")
    )
    result = _run(store, work_root, child, Publish(), alerts)
    assert result == f"argument/{item.guid}: errored (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "errored"
    assert row["failure_count"] == 1
    assert len(alerts.calls) == 1
    assert alerts.calls[0][1] == "error"


def test_http_400_is_terminal_on_first_failure(store, work_root):
    item = _admit(store)
    alerts = Alerts()
    child = FakeChild(
        work_root,
        _bad(stage="transcribe", reason="asr_error", code=400, detail="API key"),
    )
    _run(store, work_root, child, Publish(), alerts)
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "errored"
    assert row["failure_count"] == 1
    assert len(alerts.calls) == 1


@pytest.mark.parametrize("code", [429, 500, None])
def test_other_codes_are_not_terminal(store, work_root, code):
    item = _admit(store)
    child = FakeChild(work_root, _bad(reason="asr_error", code=code))
    _run(store, work_root, child, Publish(), Alerts())
    assert store.get_podcast_episode("argument", item.guid)["status"] == "pending"


def test_publish_raising_goes_back_to_pending(store, work_root):
    item = _admit(store)
    alerts = Alerts()
    publish = Publish(exc=RuntimeError("feed upload failed"))
    result = _run(store, work_root, FakeChild(work_root), publish, alerts)
    assert result == f"argument/{item.guid}: failed (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "pending"
    assert row["last_error"] == "publish: RuntimeError: feed upload failed"
    assert alerts.calls == []
    # The audio stays: the retry re-publishes from the same work dir.
    work_dir = work_dir_for("argument", item.guid, work_root)
    assert (work_dir / "audio.mp3").exists()


def test_publish_error_has_signed_url_query_scrubbed(store, work_root):
    item = _admit(store)
    publish = Publish(exc=RuntimeError("PUT https://r2.example/k.mp3?X-Sig=SECRET bad"))
    _run(store, work_root, FakeChild(work_root), publish, Alerts())
    err = store.get_podcast_episode("argument", item.guid)["last_error"]
    assert "SECRET" not in err
    assert err.startswith("publish: RuntimeError: PUT https://r2.example/k.mp3")


def test_child_raising_is_a_failure_not_a_crash(store, work_root):
    item = _admit(store)

    def child(source, item, work_dir):
        raise OSError("disk on fire")

    result = _run(store, work_root, child, Publish(), Alerts())
    assert result == f"argument/{item.guid}: failed (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "pending"
    assert row["last_error"].startswith("child: OSError: disk on fire")


def test_keyboard_interrupt_from_child_propagates_and_leaves_row_in_progress(
    store, work_root
):
    item = _admit(store)
    alerts = Alerts()

    def child(source, item, work_dir):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(store, work_root, child, Publish(), alerts)
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "in_progress"
    assert row["failure_count"] == 0
    assert alerts.calls == []
    # What the consumer does on its next start (counted as an interruption).
    (rec,) = store.recover_interrupted_podcast_episodes()
    assert rec["guid"] == item.guid and rec["status"] == "pending"


def test_keyboard_interrupt_from_publish_propagates(store, work_root):
    item = _admit(store)
    with pytest.raises(KeyboardInterrupt):
        _run(
            store,
            work_root,
            FakeChild(work_root),
            Publish(exc=KeyboardInterrupt()),
            Alerts(),
        )
    assert store.get_podcast_episode("argument", item.guid)["status"] == "in_progress"


def test_corrupt_item_json_is_a_setup_failure(store, work_root):
    item = _admit(store)
    store._conn.execute(
        "UPDATE podcast_episodes SET item_json = '{not json' WHERE guid = ?",
        (item.guid,),
    )
    store._conn.commit()
    child = FakeChild(work_root)
    result = _run(store, work_root, child, Publish(), Alerts())
    assert result == f"argument/{item.guid}: failed (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "pending"
    assert row["last_error"].startswith("setup: JSONDecodeError")
    assert child.calls == []


def test_unknown_source_is_a_setup_failure(store, work_root):
    store.admit_podcast_episode(
        source="nosuch",
        guid="g",
        item_json="{}",
        pub_date="2026-10-10T09:00:00+00:00",
        display_title="t",
        episode_slug="s",
        r2_key="episodes/x/s.mp3",
        admitted_via="poll",
        now=NOW - timedelta(hours=1),
    )
    child = FakeChild(work_root)
    result = _run(store, work_root, child, Publish(), Alerts())
    assert result == "nosuch/g: failed (1)"
    row = store.get_podcast_episode("nosuch", "g")
    assert row["last_error"].startswith("setup: KeyError")
    assert child.calls == []


def test_exhaustion_alert_text_has_no_signed_url(store, work_root):
    _admit(store)
    alerts = Alerts()
    child = FakeChild(
        work_root,
        _bad(stage="claim", detail="GET https://cdn.example/a.mp3?sig=SECRET failed"),
    )
    _run(store, work_root, child, Publish(), alerts)
    assert len(alerts.calls) == 1
    assert "SECRET" not in alerts.calls[0][0]
    assert "https://cdn.example/a.mp3" in alerts.calls[0][0]


def test_headroom_over_75_percent_sends_one_info_alert(store, work_root):
    item = _admit(store)
    alerts = Alerts()
    # allowed = max(120, 0.08 * 5000) = 400; 80% of that = 320 > 300.
    child = FakeChild(work_root, decoded=5320.0)
    _run(store, work_root, child, Publish(), alerts)
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "completed"
    stats = json.loads(row["stats_json"])
    assert stats["decoded_minus_rss_s"] == 320.0
    assert stats["allowed_s"] == 400.0
    assert len(alerts.calls) == 1
    text, severity = alerts.calls[0]
    assert severity == "info"
    assert "Episode 1" in text
    assert "320" in text and "400" in text


def test_headroom_in_the_short_direction_also_warns(store, work_root):
    # The refusal in podcast_transcribe is on abs(); so is the early warning.
    item = _admit(store)
    alerts = Alerts()
    _run(store, work_root, FakeChild(work_root, decoded=4680.0), Publish(), alerts)
    assert len(alerts.calls) == 1 and alerts.calls[0][1] == "info"
    stats = json.loads(store.get_podcast_episode("argument", item.guid)["stats_json"])
    assert stats["decoded_minus_rss_s"] == -320.0


def test_headroom_at_exactly_75_percent_does_not_alert(store, work_root):
    _admit(store)
    alerts = Alerts()
    _run(store, work_root, FakeChild(work_root, decoded=5300.0), Publish(), alerts)
    assert alerts.calls == []


def test_short_episode_uses_the_two_minute_floor(store, work_root):
    item = _admit(store)
    # rss 600 s: 8% = 48 < floor 120. 100 s over = 83% of 120.
    child = FakeChild(work_root, decoded=700.0, rss=600)
    alerts = Alerts()
    _run(store, work_root, child, Publish(), alerts)
    stats = json.loads(store.get_podcast_episode("argument", item.guid)["stats_json"])
    assert stats["allowed_s"] == 120.0
    assert len(alerts.calls) == 1


def test_info_alert_failing_to_send_does_not_fail_the_job(store, work_root):
    item = _admit(store)
    exploding = ExplodingAlerts()
    _run(store, work_root, FakeChild(work_root, decoded=5320.0), Publish(), exploding)
    assert store.get_podcast_episode("argument", item.guid)["status"] == "completed"
    assert [sev for _, sev in exploding.calls] == ["info"]


def test_exhaustion_alert_failing_to_send_still_errors_the_row(store, work_root):
    item = _admit(store)
    exploding = ExplodingAlerts()
    child = FakeChild(work_root, _bad(stage="claim"))
    _run(store, work_root, child, Publish(), exploding)
    assert store.get_podcast_episode("argument", item.guid)["status"] == "errored"
    assert [sev for _, sev in exploding.calls] == ["error"]


@pytest.mark.parametrize("kind", ["missing", "garbage", "wrong_shape"])
def test_unreadable_manifest_gives_none_stats_and_still_completes(
    store, work_root, kind
):
    item = _admit(store)
    alerts = Alerts()

    def child(source, item, work_dir):
        work_dir.mkdir(parents=True, exist_ok=True)
        if kind == "garbage":
            (work_dir / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        elif kind == "wrong_shape":
            (work_dir / MANIFEST_NAME).write_text("[1, 2]", encoding="utf-8")
        return _ok(7.0)

    result = _run(store, work_root, child, Publish(), alerts)
    assert result == f"argument/{item.guid}: published"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "completed"
    stats = json.loads(row["stats_json"])
    assert stats["decoded_duration_s"] is None
    assert stats["decoded_minus_rss_s"] is None
    assert stats["allowed_s"] is None
    assert stats["pieces"] is None
    assert stats["child_elapsed_s"] == 7.0
    assert stats["publish"] == "published"
    assert alerts.calls == []


def test_manifest_without_rss_duration_has_no_headroom(store, work_root):
    item = _admit(store)
    alerts = Alerts()
    _run(store, work_root, FakeChild(work_root, rss=None), Publish(), alerts)
    stats = json.loads(store.get_podcast_episode("argument", item.guid)["stats_json"])
    assert stats["decoded_duration_s"] == 5030.0
    assert stats["rss_duration_s"] is None
    assert stats["decoded_minus_rss_s"] is None
    assert stats["allowed_s"] is None
    assert alerts.calls == []


def test_unknown_token_counts_make_the_total_none(store, work_root):
    item = _admit(store)
    pieces = [
        {"input_tokens": 100, "output_tokens": 50},
        {"input_tokens": None, "output_tokens": 70},
    ]
    _run(store, work_root, FakeChild(work_root, pieces=pieces), Publish(), Alerts())
    stats = json.loads(store.get_podcast_episode("argument", item.guid)["stats_json"])
    assert stats["pieces"] == 2
    assert stats["input_tokens"] is None
    assert stats["output_tokens"] == 120


def test_audio_delete_failure_does_not_fail_the_job(store, work_root, monkeypatch):
    item = _admit(store)
    from pathlib import Path

    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self.name in ("audio.mp3", "episode.mp3"):
            raise PermissionError("nope")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = _run(store, work_root, FakeChild(work_root), Publish(), Alerts())
    assert result == f"argument/{item.guid}: published"
    assert store.get_podcast_episode("argument", item.guid)["status"] == "completed"


def test_complete_raising_after_publish_is_recorded_as_a_failure(
    store, work_root, monkeypatch
):
    item = _admit(store)

    def boom(*a, **kw):
        raise RuntimeError("db is locked")

    monkeypatch.setattr(store, "complete_podcast_episode", boom)
    result = _run(store, work_root, FakeChild(work_root), Publish(), Alerts())
    assert result == f"argument/{item.guid}: failed (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "pending"
    assert row["last_error"].startswith("complete: RuntimeError: db is locked")
    # The audio survives so the reconciling retry has everything it needs.
    work_dir = work_dir_for("argument", item.guid, work_root)
    assert (work_dir / "audio.mp3").exists()


def test_failure_recording_raising_is_contained_and_alerted(
    store, work_root, monkeypatch
):
    item = _admit(store)

    def boom(*a, **kw):
        raise RuntimeError("db is locked https://x/y.mp3?sig=SECRET")

    monkeypatch.setattr(store, "fail_podcast_episode", boom)
    alerts = Alerts()
    result = _run(store, work_root, FakeChild(work_root, _bad()), Publish(), alerts)
    assert result == f"argument/{item.guid}: failure not recorded"
    # Left in_progress: the next consumer start recovers it.
    assert store.get_podcast_episode("argument", item.guid)["status"] == "in_progress"
    assert len(alerts.calls) == 1
    text, severity = alerts.calls[0]
    assert severity == "error"
    assert text.startswith(
        f"could not record failure for argument/{item.guid}; "
        "row stuck in_progress until consumer restart: "
    )
    assert "transcribe: deadline" in text
    assert "SECRET" not in text


def test_failure_recording_and_alert_both_raising_is_still_contained(
    store, work_root, monkeypatch
):
    item = _admit(store)

    def boom(*a, **kw):
        raise RuntimeError("db is locked")

    monkeypatch.setattr(store, "fail_podcast_episode", boom)
    exploding = ExplodingAlerts()
    result = _run(store, work_root, FakeChild(work_root, _bad()), Publish(), exploding)
    assert result == f"argument/{item.guid}: failure not recorded"
    assert len(exploding.calls) == 1


def test_stats_collection_raising_does_not_fail_the_job(store, work_root, monkeypatch):
    item = _admit(store)

    def boom(*a, **kw):
        raise RuntimeError("stats exploded")

    monkeypatch.setattr("pipeline.podcast_executor.collect_stats", boom)
    alerts = Alerts()
    result = _run(store, work_root, FakeChild(work_root), Publish(), alerts)
    assert result == f"argument/{item.guid}: published"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "completed"
    stats = json.loads(row["stats_json"])
    assert stats["decoded_duration_s"] is None
    assert stats["pieces"] is None
    assert stats["child_elapsed_s"] == 12.5
    assert stats["publish"] == "published"
    assert alerts.calls == []


def test_failures_use_a_fresh_clock_unless_now_is_injected(store, work_root):
    # Admitted long ago, due at once; backoff must be measured from the real
    # clock at failure time, not from a stale value taken before a 30 min child.
    item = _item(pub_date=datetime(2019, 1, 1, tzinfo=UTC))
    admit_item(store, SOURCE, item, via="poll", now=datetime(2020, 1, 1, tzinfo=UTC))
    before = datetime.now(UTC)
    run_one_podcast_episode(
        store,
        "r2",
        run_child=FakeChild(work_root, _bad()),
        publish=Publish(),
        alert=Alerts(),
        work_root=work_root,
    )
    after = datetime.now(UTC)
    row = store.get_podcast_episode("argument", item.guid)
    due = datetime.fromisoformat(row["next_attempt_at"])
    assert before + PODCAST_RETRY_BACKOFF <= due <= after + PODCAST_RETRY_BACKOFF


def test_clock_is_re_read_after_the_child_runs(store, work_root, monkeypatch):
    item = _item(pub_date=datetime(2019, 1, 1, tzinfo=UTC))
    admit_item(store, SOURCE, item, via="poll", now=datetime(2020, 1, 1, tzinfo=UTC))
    import pipeline.podcast_executor as ex

    times = iter(
        [
            datetime(2026, 10, 18, 12, 0, tzinfo=UTC),  # selecting the row
            datetime(2026, 10, 18, 12, 25, tzinfo=UTC),  # recording the failure
        ]
    )

    class _Clock:
        @staticmethod
        def now(tz=None):
            return next(times)

    monkeypatch.setattr(ex, "datetime", _Clock)
    run_one_podcast_episode(
        store,
        "r2",
        run_child=FakeChild(work_root, _bad()),
        publish=Publish(),
        alert=Alerts(),
        work_root=work_root,
    )
    row = store.get_podcast_episode("argument", item.guid)
    assert datetime.fromisoformat(row["next_attempt_at"]) == datetime(
        2026, 10, 18, 18, 25, tzinfo=UTC
    )


def test_two_due_rows_one_call_runs_only_the_oldest(store, work_root):
    newer = _admit(store, 3)
    older = _admit(store, 1)
    child = FakeChild(work_root)
    result = _run(store, work_root, child, Publish(), Alerts())
    assert result == f"argument/{older.guid}: published"
    assert [c[1] for c in child.calls] == [older.guid]
    assert store.get_podcast_episode("argument", newer.guid)["status"] == "pending"
    # A second call takes the other one.
    assert _run(store, work_root, child, Publish(), Alerts()) == (
        f"argument/{newer.guid}: published"
    )


def test_lost_claim_returns_none(store, work_root, monkeypatch):
    item = _admit(store)
    monkeypatch.setattr(store, "claim_podcast_episode", lambda s, g: False)
    child = FakeChild(work_root)
    assert _run(store, work_root, child, Publish(), Alerts()) is None
    assert child.calls == []
    assert store.get_podcast_episode("argument", item.guid)["status"] == "pending"


def test_now_defaults_to_the_wall_clock(store, work_root):
    # Admitted long ago, so it is due whatever the wall clock says.
    item = _item(pub_date=datetime(2019, 1, 1, tzinfo=UTC))
    admit_item(store, SOURCE, item, via="poll", now=datetime(2020, 1, 1, tzinfo=UTC))
    result = run_one_podcast_episode(
        store,
        "r2",
        run_child=FakeChild(work_root),
        publish=Publish(),
        alert=Alerts(),
        work_root=work_root,
    )
    assert result == f"argument/{item.guid}: published"


def test_a_hanging_alert_cannot_stall_the_executor(store, work_root, monkeypatch):
    import threading
    import time

    from pipeline import alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "BOUNDED_ALERT_TIMEOUT_S", 0.2)
    release = threading.Event()

    def hung(text, severity="info"):
        release.wait(30)
        return True

    item = _admit(store)
    child = FakeChild(work_root, _bad(stage="claim", reason="WorkDirOwned"))
    try:
        t0 = time.monotonic()
        result = _run(store, work_root, child, Publish(), hung)
        assert time.monotonic() - t0 < 5.0
    finally:
        release.set()
    # The give-up alert timed out; the row transition happened regardless.
    assert result == f"argument/{item.guid}: errored (1)"
    assert store.get_podcast_episode("argument", item.guid)["status"] == "errored"


# --- reconcile before the child -------------------------------------------------


def _commit_episode(store, item):
    """The state an earlier attempt left: audio uploaded, episode row inserted."""
    from pipeline.db import Episode

    row = store.get_podcast_episode("argument", item.guid)
    store.insert_episode(
        Episode(
            id="e1",
            title=row["display_title"],
            slug=row["episode_slug"],
            pub_date="Sun, 18 Oct 2026 12:00:00 +0000",
            r2_key=row["r2_key"],
            feed_slug="yglesias",
            category="News",
            source_tag=None,
            preset_name="Script",
            source_url=item.link,
            size_bytes=10,
            duration_seconds=60,
            summary="s",
            show_notes_html=None,
        )
    )


class Reconcile:
    """The real ``reconcile_committed`` with the feed upload replaced."""

    def __init__(self, monkeypatch, exc: Exception | None = None):
        self.calls: list[tuple] = []
        self.exc = exc
        monkeypatch.setattr(
            "pipeline.podcast_publish.regenerate_and_upload_feed", self._regen
        )

    def _regen(self, store, r2):
        self.calls.append((store, r2))
        if self.exc is not None:
            raise self.exc


def _no_child(*a, **k):
    raise AssertionError("the child must not run for a committed episode")


def _run_no_child(store, work_root, publish, alerts, **kw):
    return run_one_podcast_episode(
        store,
        "the-r2",
        now=NOW,
        run_child=_no_child,
        publish=publish,
        alert=alerts,
        work_root=work_root,
        **kw,
    )


def test_committed_episode_is_reconciled_without_running_the_child(
    store, work_root, monkeypatch
):
    item = _admit(store)
    _commit_episode(store, item)
    recon, publish, alerts = Reconcile(monkeypatch), Publish(), Alerts()
    work_dir = work_dir_for("argument", item.guid, work_root)
    work_dir.mkdir(parents=True)
    (work_dir / "audio.mp3").write_bytes(b"a")
    (work_dir / "episode.mp3").write_bytes(b"b")

    result = _run_no_child(store, work_root, publish, alerts)

    assert result == f"argument/{item.guid}: reconciled"
    assert recon.calls == [(store, "the-r2")]
    assert publish.calls == []  # no publish either: feeds only
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "completed"
    stats = json.loads(row["stats_json"])
    assert stats["publish"] == "reconciled"
    assert stats["child_elapsed_s"] == 0.0
    assert stats["decoded_minus_rss_s"] is None  # no manifest: best effort
    assert not (work_dir / "audio.mp3").exists()
    assert not (work_dir / "episode.mp3").exists()
    assert alerts.calls == []


def test_reconcile_uses_the_manifest_stats_when_a_prior_run_left_one(
    store, work_root, monkeypatch
):
    item = _admit(store)
    _commit_episode(store, item)
    Reconcile(monkeypatch)
    work_dir = work_dir_for("argument", item.guid, work_root)
    work_dir.mkdir(parents=True)
    (work_dir / MANIFEST_NAME).write_text(json.dumps(_manifest()), encoding="utf-8")

    _run_no_child(store, work_root, Publish(), Alerts())

    stats = json.loads(store.get_podcast_episode("argument", item.guid)["stats_json"])
    assert stats["decoded_minus_rss_s"] == 30.0
    assert stats["publish"] == "reconciled"


def test_feed_upload_failure_during_reconcile_is_a_normal_failure(
    store, work_root, monkeypatch
):
    item = _admit(store)
    _commit_episode(store, item)
    Reconcile(monkeypatch, exc=OSError("r2 down"))
    alerts = Alerts()

    result = _run_no_child(store, work_root, Publish(), alerts)

    assert result == f"argument/{item.guid}: failed (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["status"] == "pending"  # not terminal
    assert row["failure_count"] == 1
    assert row["last_error"].startswith("publish: OSError: r2 down")
    assert datetime.fromisoformat(row["next_attempt_at"]) == NOW + PODCAST_RETRY_BACKOFF
    assert alerts.calls == []  # one failure is not give-up


def test_reconcile_failure_exhausts_the_budget_like_any_other(
    store, work_root, monkeypatch
):
    item = _admit(store)
    _commit_episode(store, item)
    Reconcile(monkeypatch, exc=OSError("r2 down"))
    alerts = Alerts()
    now = NOW
    for _ in range(PODCAST_MAX_FAILURES):
        run_one_podcast_episode(
            store,
            "r2",
            now=now,
            run_child=_no_child,
            publish=Publish(),
            alert=alerts,
            work_root=work_root,
        )
        now += PODCAST_RETRY_BACKOFF
    assert store.get_podcast_episode("argument", item.guid)["status"] == "errored"
    assert len(alerts.calls) == 1


def test_drift_is_refused_before_the_child_and_before_reconcile(
    store, work_root, monkeypatch
):
    item = _admit(store)
    _commit_episode(store, item)
    recon = Reconcile(monkeypatch)
    store._conn.execute("UPDATE podcast_episodes SET episode_slug = 'drifted'")
    store._conn.commit()

    result = _run_no_child(store, work_root, Publish(), Alerts())

    assert result == f"argument/{item.guid}: failed (1)"
    row = store.get_podcast_episode("argument", item.guid)
    assert row["last_error"].startswith("identity: RuntimeError: identity drift")
    assert recon.calls == []


def test_uncommitted_episode_still_runs_child_then_publish(
    store, work_root, monkeypatch
):
    item = _admit(store)
    recon = Reconcile(monkeypatch)
    child, publish = FakeChild(work_root), Publish()
    result = run_one_podcast_episode(
        store,
        "r2",
        now=NOW,
        run_child=child,
        publish=publish,
        alert=Alerts(),
        work_root=work_root,
    )
    assert result == f"argument/{item.guid}: published"
    assert len(child.calls) == 1 and len(publish.calls) == 1
    assert recon.calls == []
