from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from pipeline.db import (
    PODCAST_HEALTH_ALERT_AFTER,
    PODCAST_MAX_FAILURES,
    PODCAST_RETRY_BACKOFF,
    StateStore,
)


NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


def _admit(store, guid="g1", *, pub_date="2026-10-10T09:00:00+00:00", **overrides):
    kwargs = {
        "source": "argument",
        "guid": guid,
        "item_json": json.dumps({"guid": guid}),
        "pub_date": pub_date,
        "display_title": f"Report: 2026-10-10 - The Argument - {guid}",
        "episode_slug": f"slug-{guid}",
        "r2_key": f"episodes/yglesias/{guid}.mp3",
        "admitted_via": "poll",
        "now": NOW,
    }
    kwargs.update(overrides)
    return store.admit_podcast_episode(**kwargs)


def test_constants():
    assert PODCAST_MAX_FAILURES == 4
    assert PODCAST_RETRY_BACKOFF == timedelta(hours=6)
    assert PODCAST_HEALTH_ALERT_AFTER == 4


def test_admit_inserts_pending_row_due_now(store):
    assert _admit(store) is True
    row = store.get_podcast_episode("argument", "g1")
    assert row is not None
    assert row["status"] == "pending"
    assert row["failure_count"] == 0
    assert row["last_error"] is None
    assert row["stats_json"] is None
    assert row["admitted_via"] == "poll"
    assert row["r2_key"] == "episodes/yglesias/g1.mp3"
    assert datetime.fromisoformat(row["next_attempt_at"]) == NOW


def test_get_unknown_returns_none(store):
    assert store.get_podcast_episode("argument", "nope") is None


def test_admit_is_idempotent_and_frozen(store):
    assert _admit(store) is True
    before = store.get_podcast_episode("argument", "g1")
    assert (
        _admit(
            store,
            display_title="Changed",
            item_json='{"changed": true}',
            pub_date="2030-01-01T00:00:00+00:00",
            episode_slug="changed",
            admitted_via="enqueue",
            now=NOW + timedelta(days=1),
        )
        is False
    )
    assert store.get_podcast_episode("argument", "g1") == before


def test_admit_same_guid_different_source_is_distinct(store):
    assert _admit(store, "g1") is True
    assert _admit(store, "g1", source="other", r2_key="episodes/other/g1.mp3") is True


def test_admit_r2_key_collision_raises(store):
    assert _admit(store, "g1") is True
    with pytest.raises(sqlite3.IntegrityError):
        _admit(store, "g2", r2_key="episodes/yglesias/g1.mp3")


def test_list_orders_newest_pub_date_first_and_filters_source(store):
    _admit(store, "old", pub_date="2026-10-01T00:00:00+00:00")
    _admit(store, "new", pub_date="2026-10-15T00:00:00+00:00")
    _admit(
        store,
        "mid",
        source="other",
        pub_date="2026-10-08T00:00:00+00:00",
    )
    assert [r["guid"] for r in store.list_podcast_episodes()] == ["new", "mid", "old"]
    assert [r["guid"] for r in store.list_podcast_episodes("argument")] == [
        "new",
        "old",
    ]
    assert store.list_podcast_episodes("none") == []


def test_next_due_oldest_pub_date_first(store):
    _admit(store, "newer", pub_date="2026-10-15T00:00:00+00:00")
    _admit(store, "older", pub_date="2026-10-01T00:00:00+00:00")
    due = store.next_due_podcast_episode(NOW)
    assert due is not None
    assert due["guid"] == "older"


def test_next_due_none_when_empty(store):
    assert store.next_due_podcast_episode(NOW) is None


def test_next_due_respects_next_attempt_at(store):
    _admit(store, "g1", now=NOW + timedelta(hours=1))
    assert store.next_due_podcast_episode(NOW) is None
    assert store.next_due_podcast_episode(NOW + timedelta(minutes=59)) is None
    due = store.next_due_podcast_episode(NOW + timedelta(hours=1))
    assert due is not None
    assert due["guid"] == "g1"


def test_next_due_compares_across_timezones_and_fractional_seconds(store):
    # next_attempt_at is stored normalized to UTC, so an aware non-UTC `now`
    # and sub-second values still order correctly as strings.
    _admit(store, "g1", now=NOW + timedelta(milliseconds=500))
    assert store.next_due_podcast_episode(NOW) is None
    plus5 = timezone(timedelta(hours=5))
    assert store.next_due_podcast_episode(
        (NOW + timedelta(seconds=1)).astimezone(plus5)
    )


def test_naive_datetimes_are_rejected(store):
    with pytest.raises(ValueError):
        _admit(store, now=datetime(2026, 10, 18, 12, 0, 0))
    with pytest.raises(ValueError):
        store.next_due_podcast_episode(datetime(2026, 10, 18, 12, 0, 0))


def test_next_due_skips_non_pending(store):
    _admit(store, "g1")
    assert store.claim_podcast_episode("argument", "g1") is True
    assert store.next_due_podcast_episode(NOW) is None


def test_claim_only_from_pending(store):
    _admit(store)
    assert store.claim_podcast_episode("argument", "g1") is True
    assert store.get_podcast_episode("argument", "g1")["status"] == "in_progress"
    assert store.claim_podcast_episode("argument", "g1") is False
    assert store.claim_podcast_episode("argument", "missing") is False


def test_complete_records_stats_and_clears_error(store):
    _admit(store)
    store.claim_podcast_episode("argument", "g1")
    store.fail_podcast_episode("argument", "g1", "boom", now=NOW)
    store.claim_podcast_episode("argument", "g1")
    store.complete_podcast_episode("argument", "g1", stats={"decoded_minus_rss_s": 12})
    row = store.get_podcast_episode("argument", "g1")
    assert row["status"] == "completed"
    assert row["last_error"] is None
    assert json.loads(row["stats_json"]) == {"decoded_minus_rss_s": 12}


def test_complete_with_no_stats(store):
    _admit(store)
    store.complete_podcast_episode("argument", "g1", stats=None)
    row = store.get_podcast_episode("argument", "g1")
    assert row["status"] == "completed"
    assert row["stats_json"] is None


def test_fail_retries_three_times_then_errors_on_fourth(store):
    _admit(store)
    now = NOW
    for expected in (1, 2, 3):
        store.claim_podcast_episode("argument", "g1")
        update = store.fail_podcast_episode("argument", "g1", f"err{expected}", now=now)
        assert update.status == "pending"
        assert update.exhausted is False
        assert update.failure_count == expected
        assert update.process_after is not None
        assert datetime.fromisoformat(update.process_after) == now + timedelta(hours=6)
        row = store.get_podcast_episode("argument", "g1")
        assert row["status"] == "pending"
        assert row["failure_count"] == expected
        assert row["last_error"] == f"err{expected}"
        assert row["next_attempt_at"] == update.process_after
        # Not due until the backoff has elapsed.
        assert (
            store.next_due_podcast_episode(now + timedelta(hours=5, minutes=59)) is None
        )
        now = now + timedelta(hours=6)
        assert store.next_due_podcast_episode(now)["guid"] == "g1"

    store.claim_podcast_episode("argument", "g1")
    update = store.fail_podcast_episode("argument", "g1", "err4", now=now)
    assert update.status == "errored"
    assert update.exhausted is True
    assert update.process_after is None
    assert update.failure_count == 4
    row = store.get_podcast_episode("argument", "g1")
    assert row["status"] == "errored"
    assert store.next_due_podcast_episode(now + timedelta(days=30)) is None


def test_fail_terminal_errors_on_first_failure(store):
    _admit(store)
    store.claim_podcast_episode("argument", "g1")
    update = store.fail_podcast_episode(
        "argument", "g1", "bad key", now=NOW, terminal=True
    )
    assert update.status == "errored"
    assert update.exhausted is True
    assert update.process_after is None
    assert update.failure_count == 1
    assert store.get_podcast_episode("argument", "g1")["status"] == "errored"


def test_fail_truncates_error_to_500(store):
    _admit(store)
    store.fail_podcast_episode("argument", "g1", "x" * 900, now=NOW)
    assert len(store.get_podcast_episode("argument", "g1")["last_error"]) == 500


def test_fail_and_complete_unknown_raise_keyerror(store):
    with pytest.raises(KeyError):
        store.fail_podcast_episode("argument", "nope", "e", now=NOW)
    with pytest.raises(KeyError):
        store.complete_podcast_episode("argument", "nope", stats=None)


def test_reset_errored_returns_prior_status_and_clears_state(store):
    _admit(store)
    store.fail_podcast_episode("argument", "g1", "bad", now=NOW, terminal=True)
    later = NOW + timedelta(days=2)
    assert store.reset_podcast_episode("argument", "g1", now=later) == "errored"
    row = store.get_podcast_episode("argument", "g1")
    assert row["status"] == "pending"
    assert row["failure_count"] == 0
    assert row["last_error"] is None
    assert datetime.fromisoformat(row["next_attempt_at"]) == later
    assert store.next_due_podcast_episode(later)["guid"] == "g1"


def test_reset_pending_backed_off_makes_due_now(store):
    _admit(store)
    store.fail_podcast_episode("argument", "g1", "e", now=NOW)
    assert store.next_due_podcast_episode(NOW) is None
    assert store.reset_podcast_episode("argument", "g1", now=NOW) == "pending"
    row = store.get_podcast_episode("argument", "g1")
    assert row["failure_count"] == 0
    assert store.next_due_podcast_episode(NOW)["guid"] == "g1"


def test_reset_refuses_completed_and_in_progress(store):
    _admit(store, "done")
    store.complete_podcast_episode("argument", "done", stats=None)
    _admit(store, "busy")
    store.claim_podcast_episode("argument", "busy")
    with pytest.raises(ValueError):
        store.reset_podcast_episode("argument", "done", now=NOW)
    with pytest.raises(ValueError):
        store.reset_podcast_episode("argument", "busy", now=NOW)
    assert store.get_podcast_episode("argument", "done")["status"] == "completed"
    assert store.get_podcast_episode("argument", "busy")["status"] == "in_progress"


def test_reset_loses_a_race_with_a_claim_and_leaves_the_row_alone(store):
    """The consumer claims the row between reset's SELECT and its UPDATE."""
    _admit(store)
    store.fail_podcast_episode("argument", "g1", "e", now=NOW)
    real = store._conn

    class RacingConn:
        def __init__(self) -> None:
            self.raced = False

        def execute(self, sql, *args):
            if sql.startswith("UPDATE podcast_episodes SET status = 'pending'"):
                if not self.raced:
                    self.raced = True
                    real.execute("UPDATE podcast_episodes SET status = 'in_progress'")
            return real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(real, name)

    store._conn = RacingConn()
    try:
        with pytest.raises(ValueError, match="in_progress"):
            store.reset_podcast_episode("argument", "g1", now=NOW)
    finally:
        store._conn = real
    row = store.get_podcast_episode("argument", "g1")
    assert row["status"] == "in_progress"
    assert row["failure_count"] == 1  # untouched, not zeroed
    assert row["last_error"] == "e"


def test_reset_unknown_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.reset_podcast_episode("argument", "nope", now=NOW)


def test_recover_flips_only_in_progress(store):
    for guid in ("pend", "busy1", "busy2", "done", "err"):
        _admit(store, guid)
    store.claim_podcast_episode("argument", "busy1")
    store.claim_podcast_episode("argument", "busy2")
    store.complete_podcast_episode("argument", "done", stats=None)
    store.fail_podcast_episode("argument", "err", "x", now=NOW, terminal=True)
    recovered = store.recover_interrupted_podcast_episodes()
    assert sorted(r["guid"] for r in recovered) == ["busy1", "busy2"]
    statuses = {r["guid"]: r["status"] for r in store.list_podcast_episodes()}
    assert statuses == {
        "pend": "pending",
        "busy1": "pending",
        "busy2": "pending",
        "done": "completed",
        "err": "errored",
    }
    assert store.recover_interrupted_podcast_episodes() == []


def test_recover_returns_row_summaries(store):
    _admit(store)
    store.claim_podcast_episode("argument", "g1")
    row = store.get_podcast_episode("argument", "g1")
    assert store.recover_interrupted_podcast_episodes() == [
        {
            "source": "argument",
            "guid": "g1",
            "display_title": row["display_title"],
            "failure_count": 1,
            "status": "pending",
            "last_error": "interrupted: consumer stopped mid-run",
        }
    ]


def test_recover_counts_an_interruption_as_a_failure(store):
    _admit(store)
    store.fail_podcast_episode("argument", "g1", "e", now=NOW)
    row = store.get_podcast_episode("argument", "g1")
    store.claim_podcast_episode("argument", "g1")
    assert len(store.recover_interrupted_podcast_episodes()) == 1
    after = store.get_podcast_episode("argument", "g1")
    assert after["next_attempt_at"] == row["next_attempt_at"]
    assert after["failure_count"] == 2
    assert after["last_error"] == "interrupted: consumer stopped mid-run"
    assert after["status"] == "pending"


def test_recover_errors_a_row_that_exhausts_its_budget(store):
    """A job that crashes the consumer every time must not loop forever."""
    _admit(store)
    for expected in range(1, PODCAST_MAX_FAILURES + 1):
        assert store.claim_podcast_episode("argument", "g1")
        (rec,) = store.recover_interrupted_podcast_episodes()
        assert rec["failure_count"] == expected
        if expected < PODCAST_MAX_FAILURES:
            assert rec["status"] == "pending"
    assert rec["status"] == "errored"
    assert store.get_podcast_episode("argument", "g1")["status"] == "errored"
    assert store.next_due_podcast_episode(NOW + timedelta(days=30)) is None
    assert store.recover_interrupted_podcast_episodes() == []


def test_recover_makes_just_claimed_row_run_at_once(store):
    _admit(store)
    store.claim_podcast_episode("argument", "g1")
    store.recover_interrupted_podcast_episodes()
    assert store.next_due_podcast_episode(NOW)["guid"] == "g1"


def test_health_alerts_exactly_once_at_threshold(store):
    results = [
        store.record_podcast_discovery(
            "argument", ok=False, error=f"e{i}", now=NOW + timedelta(hours=i)
        )
        for i in range(1, 7)
    ]
    assert results == [
        (1, False),
        (2, False),
        (3, False),
        (4, True),
        (5, False),
        (6, False),
    ]


def test_health_success_resets_and_rearms(store):
    for _ in range(4):
        store.record_podcast_discovery("argument", ok=False, error="e", now=NOW)
    assert store.record_podcast_discovery(
        "argument", ok=True, error=None, now=NOW + timedelta(hours=1)
    ) == (0, False)
    results = [
        store.record_podcast_discovery(
            "argument", ok=False, error="e", now=NOW + timedelta(hours=2 + i)
        )
        for i in range(4)
    ]
    assert results == [(1, False), (2, False), (3, False), (4, True)]


def test_health_records_error_and_last_success(store):
    store.record_podcast_discovery("argument", ok=True, error=None, now=NOW)
    store.record_podcast_discovery("argument", ok=False, error="boom", now=NOW)
    row = store.get_podcast_health("argument")
    assert row["consecutive_failures"] == 1
    assert row["last_error"] == "boom"
    assert datetime.fromisoformat(row["last_success_at"]) == NOW


def test_health_is_per_source(store):
    for _ in range(3):
        store.record_podcast_discovery("a", ok=False, error="e", now=NOW)
    assert store.record_podcast_discovery("b", ok=False, error="e", now=NOW) == (
        1,
        False,
    )
    assert store.record_podcast_discovery("a", ok=False, error="e", now=NOW) == (
        4,
        True,
    )


def test_old_schema_db_gains_both_tables(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE episodes (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "slug TEXT NOT NULL, pub_date TEXT NOT NULL, r2_key TEXT NOT NULL, "
        "size_bytes INTEGER NOT NULL, duration_seconds INTEGER)"
    )
    conn.execute(
        "INSERT INTO episodes VALUES "
        "('e1', 't', 's', 'Mon, 01 Jan 2026 00:00:00 +0000', 'k', 1, NULL)"
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)
    try:
        tables = {
            r["name"]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"podcast_episodes", "podcast_source_health"} <= tables
        assert _admit(store) is True
        kept = store._conn.execute("SELECT id FROM episodes").fetchall()
        assert [r["id"] for r in kept] == ["e1"]
    finally:
        store.close()


def test_reopening_store_keeps_rows(tmp_path):
    path = tmp_path / "test.db"
    first = StateStore(path)
    _admit(first)
    first.close()
    second = StateStore(path)
    try:
        assert second.get_podcast_episode("argument", "g1") is not None
    finally:
        second.close()


def test_admit_integrity_error_rolls_back_and_releases_write_lock(tmp_path):
    path = tmp_path / "lock.db"
    store = StateStore(path)
    try:
        assert _admit(store, "g1") is True
        with pytest.raises(sqlite3.IntegrityError):
            _admit(store, "g2", r2_key="episodes/yglesias/g1.mp3")
        assert not store._conn.in_transaction
        other = sqlite3.connect(path, timeout=0.2)
        try:
            other.execute("CREATE TABLE probe (x)")
            other.commit()
        finally:
            other.close()
    finally:
        store.close()


def test_admit_normalizes_pub_date_for_ordering_across_offsets(store):
    # 09:00-05:00 is 14:00Z, which is later than 10:00Z even though the
    # naive string "09:00..." sorts before "10:00...".
    _admit(store, "late", pub_date="2026-10-10T09:00:00-05:00")
    _admit(store, "early", pub_date="2026-10-10T10:00:00Z")
    _admit(store, "frac", pub_date="2026-10-10T12:00:00.5+00:00")
    assert [r["guid"] for r in store.list_podcast_episodes()] == [
        "late",
        "frac",
        "early",
    ]
    row = store.get_podcast_episode("argument", "late")
    assert row["pub_date"] == "2026-10-10T14:00:00.000000+00:00"
    assert store.next_due_podcast_episode(NOW)["guid"] == "early"


@pytest.mark.parametrize("bad", ["2026-10-10T09:00:00", "not a date", ""])
def test_admit_rejects_naive_or_garbage_pub_date(store, bad):
    with pytest.raises(ValueError):
        _admit(store, pub_date=bad)
    assert store.get_podcast_episode("argument", "g1") is None


def test_fail_refuses_completed_row(store):
    _admit(store)
    store.complete_podcast_episode("argument", "g1", stats={"a": 1})
    with pytest.raises(ValueError):
        store.fail_podcast_episode("argument", "g1", "late failure", now=NOW)
    with pytest.raises(ValueError):
        store.fail_podcast_episode(
            "argument", "g1", "late failure", now=NOW, terminal=True
        )
    row = store.get_podcast_episode("argument", "g1")
    assert row["status"] == "completed"
    assert row["failure_count"] == 0
    assert row["last_error"] is None


def test_get_podcast_health(store):
    assert store.get_podcast_health("argument") is None
    store.record_podcast_discovery("argument", ok=False, error=None, now=NOW)
    health = store.get_podcast_health("argument")
    assert health["consecutive_failures"] == 1
    assert health["last_error"] is None
    assert health["last_success_at"] is None
    assert health["alerted"] == 0


def test_health_alerted_flag_set_at_threshold_and_cleared_on_success(store):
    for _ in range(3):
        store.record_podcast_discovery("argument", ok=False, error="e", now=NOW)
    assert store.get_podcast_health("argument")["alerted"] == 0
    store.record_podcast_discovery("argument", ok=False, error="e", now=NOW)
    assert store.get_podcast_health("argument")["alerted"] == 1
    store.record_podcast_discovery("argument", ok=False, error="e", now=NOW)
    assert store.get_podcast_health("argument")["alerted"] == 1
    store.record_podcast_discovery("argument", ok=True, error=None, now=NOW)
    assert store.get_podcast_health("argument")["alerted"] == 0


def test_get_episode_by_r2_key(store):
    from pipeline.db import Episode

    assert store.get_episode_by_r2_key("episodes/yglesias/x.mp3") is None
    ep = Episode(
        id="e1",
        title="T",
        slug="x",
        pub_date="Thu, 13 Mar 2026 12:00:00 +0000",
        r2_key="episodes/yglesias/x.mp3",
        feed_slug="yglesias",
        category="News",
        source_tag=None,
        preset_name="Script",
        source_url="https://example.com/p",
        size_bytes=10,
        duration_seconds=5,
        summary="s",
        show_notes_html="<p>s</p>",
    )
    store.insert_episode(ep)
    assert store.get_episode_by_r2_key("episodes/yglesias/x.mp3") == ep
    assert store.get_episode_by_r2_key("episodes/yglesias/y.mp3") is None
