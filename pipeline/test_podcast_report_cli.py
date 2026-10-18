from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import requests
from click.testing import CliRunner

from pipeline import __main__ as main_mod
from pipeline import podcast_report
from pipeline.report_engine import ReportOutput


FIXTURE = Path(__file__).parent / "fixtures" / "argument_feed.xml"
GUID = "audioboom-01a13c3d-ae49-7326-a550-f82c9eff9be1"
OTHER_GUID = "audioboom-0000aaaa-bbbb-cccc-dddd-eeeeffff0000"


class FakeTranscriber:
    instances: list[FakeTranscriber] = []

    def __init__(self) -> None:
        self.closed = False
        FakeTranscriber.instances.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def calls(monkeypatch, tmp_path):
    record: dict = {"download": [], "transcribe": [], "write": []}
    FakeTranscriber.instances = []

    def boom(*a, **k):
        raise AssertionError("DB / R2 must not be touched by a dry run")

    for target in (
        "pipeline.__main__.StateStore",
        "pipeline.__main__.R2Client",
        "pipeline.db.StateStore",
        "pipeline.r2.R2Client",
    ):
        monkeypatch.setattr(target, boom)

    def fake_download(url, dest, **kw):
        record["download"].append((url, dest))
        dest.write_bytes(b"mp3")
        return 3

    def fake_transcribe(audio_path, work_dir, **kw):
        record["transcribe"].append(kw)
        if record.get("reuse"):
            return "A: hi\nB: hello"  # leaves transcript.json untouched
        (work_dir / "transcript.txt").write_text("A: hi\nB: hello")
        (work_dir / "transcript.json").write_text(
            json.dumps(
                {
                    "pieces": [
                        {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "from_checkpoint": True,
                        },
                        {
                            "input_tokens": 50,
                            "output_tokens": None,
                            "from_checkpoint": False,
                        },
                    ]
                }
            )
        )
        return "A: hi\nB: hello"

    def fake_write(**kw):
        record["write"].append(kw)
        return ReportOutput(script="one two three four", summary="short summary")

    monkeypatch.setattr(podcast_report, "download_enclosure", fake_download)
    monkeypatch.setattr(podcast_report, "transcribe_episode", fake_transcribe)
    monkeypatch.setattr(podcast_report, "write_report", fake_write)
    monkeypatch.setattr(podcast_report, "GeminiPieceTranscriber", FakeTranscriber)
    monkeypatch.setattr(podcast_report, "check_opencode_serve", lambda: None)
    return record


def _run(tmp_path, *extra, guid=GUID):
    return CliRunner().invoke(
        main_mod.cli,
        [
            "podcast-report",
            "dry-run",
            "--source",
            "argument",
            "--guid",
            guid,
            "--rss-file",
            str(FIXTURE),
            "--work-dir",
            str(tmp_path / "work"),
            *extra,
        ],
    )


def test_happy_path(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    work = tmp_path / "work"

    item = json.loads((work / "item.json").read_text())
    assert item["source"] == "argument"
    assert item["guid"] == GUID
    assert item["pub_date"].startswith("2026-10-15T07:00:00")
    assert (work / "script.txt").read_text() == "one two three four"
    assert (work / "summary.txt").read_text() == "short summary"
    assert not list(work.glob("*.tmp"))

    ((url, dest),) = calls["download"]
    assert dest == work / "audio.mp3"
    assert "8964415" in url
    t = calls["transcribe"][0]
    assert t["hosts"] == ("Jerusalem Demsas", "Matt Yglesias")
    assert t["rss_duration_s"] == 4807
    assert t["carry_context"] is True
    assert isinstance(t["transcriber"], FakeTranscriber)
    assert t["transcriber"].closed
    assert calls["write"][0]["title"] == "Canceling (and un-canceling) historic heroes"

    out = result.output
    assert str(work) in out
    assert str(work / "transcript.txt") in out
    assert str(work / "script.txt") in out
    assert "Script words: 4" in out
    assert "Pieces: 2 (1/2 pieces from checkpoints)" in out
    assert "Tokens recorded in manifest" in out and "150/10" in out
    assert "download" in out and "transcribe" in out and "write" in out
    # a stage line precedes each stage
    stages = [
        out.index("Downloading "),
        out.index("Transcribing "),
        out.index("Writing report "),
        out.index("Transcript: "),
    ]
    assert stages == sorted(stages)


def test_no_carry_context(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = _run(tmp_path, "--no-carry-context")
    assert result.exit_code == 0, result.output
    assert calls["transcribe"][0]["carry_context"] is False


def test_unknown_guid_fails(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = _run(tmp_path, guid="nope")
    assert result.exit_code != 0
    assert "not found" in result.output
    assert "3 items" in result.output
    assert calls["download"] == []
    assert not (tmp_path / "work").exists()


def test_missing_api_key_fails_before_download(tmp_path, calls):
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert "GEMINI_API_KEY" in result.output
    assert calls["download"] == []
    assert not (tmp_path / "work").exists()


def test_unknown_source_fails(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    result = CliRunner().invoke(
        main_mod.cli,
        ["podcast-report", "dry-run", "--source", "nope", "--guid", GUID],
    )
    assert result.exit_code != 0
    assert "unknown podcast source" in result.output


def test_stage_failure_is_clean_error(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")

    def bad_write(**kw):
        raise RuntimeError("refused: too short")

    monkeypatch.setattr(podcast_report, "write_report", bad_write)
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert "refused: too short" in result.output
    assert not (tmp_path / "work" / "script.txt").exists()
    assert FakeTranscriber.instances[0].closed


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_work_dir_of_another_episode_is_refused_untouched(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    assert _run(tmp_path).exit_code == 0
    work = tmp_path / "work"
    before = _snapshot(work)
    n_downloads = len(calls["download"])

    result = _run(tmp_path, guid=OTHER_GUID)

    assert result.exit_code != 0
    assert f"work dir belongs to argument/{GUID}" in result.output
    assert _snapshot(work) == before  # no file changed, none added
    assert len(calls["download"]) == n_downloads
    assert len(calls["transcribe"]) == 1
    assert len(calls["write"]) == 1


@pytest.mark.parametrize(
    "leftover", ["audio.mp3", "transcript.txt", "pieces/0000.json"]
)
def test_unowned_work_dir_with_artifacts_is_refused(
    monkeypatch, tmp_path, calls, leftover
):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    work = tmp_path / "work"
    (work / leftover).parent.mkdir(parents=True, exist_ok=True)
    (work / leftover).write_bytes(b"someone else's")
    before = _snapshot(work)

    result = _run(tmp_path)

    assert result.exit_code != 0
    assert "no item.json" in result.output and leftover.split("/")[0] in result.output
    assert _snapshot(work) == before
    assert not (work / "item.json").exists()
    assert calls["download"] == []


def test_matching_rerun_keeps_original_item_json_fields(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    assert _run(tmp_path).exit_code == 0
    item_path = tmp_path / "work" / "item.json"
    original = json.loads(item_path.read_text())
    # The feed now hands out different tracking params for the same episode.
    stale = dict(original, enclosure_url="https://old.example/pinned.mp3")
    item_path.write_text(json.dumps(stale))

    result = _run(tmp_path)

    assert result.exit_code == 0, result.output
    after = json.loads(item_path.read_text())
    assert after["enclosure_url"] == "https://old.example/pinned.mp3"
    assert after == stale


def test_default_work_dir_is_per_source_hash_of_guid():
    d = podcast_report.default_work_dir("argument", GUID)
    assert d.parent == Path("/persist/my-podcasts/podcast-work/dry-run/argument")
    assert len(d.name) == 16


def test_whole_transcript_reuse_is_reported(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    assert _run(tmp_path).exit_code == 0
    calls["reuse"] = True
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    assert "transcript reused from an earlier run" in result.output


def test_stale_outputs_are_removed_before_the_write_stage(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    assert _run(tmp_path).exit_code == 0

    def bad_write(**kw):
        raise RuntimeError("refused")

    monkeypatch.setattr(podcast_report, "write_report", bad_write)
    calls["reuse"] = True
    assert _run(tmp_path).exit_code != 0
    assert not (tmp_path / "work" / "script.txt").exists()
    assert not (tmp_path / "work" / "summary.txt").exists()


def test_no_summary_means_no_summary_file(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setattr(
        podcast_report,
        "write_report",
        lambda **kw: ReportOutput(script="a b", summary=""),
    )
    assert _run(tmp_path).exit_code == 0
    assert not (tmp_path / "work" / "summary.txt").exists()


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError("serve down"),
        OSError("disk full"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_io_errors_are_clean_cli_errors(monkeypatch, tmp_path, calls, exc):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")

    def boom(**kw):
        raise exc

    monkeypatch.setattr(podcast_report, "write_report", boom)
    result = _run(tmp_path)
    assert result.exit_code == 1
    assert type(exc).__name__ in result.output
    assert "Traceback" not in result.output


def test_opencode_preflight_failure_fails_before_download(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")

    def down():
        raise podcast_report.OpencodeUnavailableError("opencode serve is down")

    monkeypatch.setattr(podcast_report, "check_opencode_serve", down)
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert "opencode serve is down" in result.output
    assert calls["download"] == []
    assert not (tmp_path / "work").exists()


def test_logging_configured_only_when_root_has_no_handlers(
    monkeypatch, tmp_path, calls
):
    import logging

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    seen: list[dict] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.append(kw))
    assert _run(tmp_path).exit_code == 0
    assert seen == []  # pytest's own handlers are on root

    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    assert _run(tmp_path).exit_code == 0
    assert seen and seen[0]["level"] == logging.INFO


def test_help_says_audio_is_the_pinned_input():
    result = CliRunner().invoke(main_mod.cli, ["podcast-report", "dry-run", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "audio.mp3" in text and "dynamically" in text


def test_check_opencode_serve(monkeypatch):
    seen: dict = {}

    def response(status: int) -> requests.Response:
        r = requests.Response()
        r.status_code = status
        return r

    def get_ok(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return response(200)

    monkeypatch.setattr(
        podcast_report.opencode_client, "OPENCODE_URL", "http://serve:1/"
    )
    monkeypatch.setattr(podcast_report.requests, "get", get_ok)
    podcast_report.check_opencode_serve()
    assert seen["url"] == "http://serve:1/global/health"
    assert seen["kw"]["timeout"] == 5.0

    monkeypatch.setattr(podcast_report.requests, "get", lambda url, **kw: response(503))
    with pytest.raises(podcast_report.OpencodeUnavailableError, match="serve:1"):
        podcast_report.check_opencode_serve()

    def refuse(url, **kw):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(podcast_report.requests, "get", refuse)
    with pytest.raises(podcast_report.OpencodeUnavailableError, match="refused"):
        podcast_report.check_opencode_serve()


def test_feed_http_error_is_a_clean_cli_error(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")

    def fetch(url, **kw):
        raise httpx.HTTPStatusError(
            "Server error '503'",
            request=httpx.Request("GET", url),
            response=httpx.Response(503, request=httpx.Request("GET", url)),
        )

    monkeypatch.setattr(podcast_report, "fetch_feed_xml", fetch)
    result = CliRunner().invoke(
        main_mod.cli,
        ["podcast-report", "dry-run", "--source", "argument", "--guid", GUID],
    )
    assert result.exit_code == 1
    assert "HTTPStatusError" in result.output and "503" in result.output
    assert "Traceback" not in result.output
    assert calls["download"] == []


def _direct_args():
    from pipeline.podcast_feed import parse_podcast_feed
    from pipeline.podcast_sources import get_podcast_source

    source = get_podcast_source("argument")
    item = next(
        i
        for i in parse_podcast_feed(FIXTURE.read_text(encoding="utf-8"))
        if i.guid == GUID
    )
    return source, item


def test_on_stage_receives_stages_in_order(tmp_path, calls):
    source, item = _direct_args()
    seen: list[str] = []
    podcast_report.run_episode_to_script(
        source, item, tmp_path / "work", on_stage=seen.append
    )
    assert seen == ["claim", "download", "transcribe", "write"]


def test_on_stage_names_the_stage_that_failed(monkeypatch, tmp_path, calls):
    source, item = _direct_args()
    seen: list[str] = []

    def bad_write(**kw):
        raise RuntimeError("refused")

    monkeypatch.setattr(podcast_report, "write_report", bad_write)
    with pytest.raises(RuntimeError):
        podcast_report.run_episode_to_script(
            source, item, tmp_path / "work", on_stage=seen.append
        )
    assert seen[-1] == "write"


def test_fresh_download_writes_acquisition_json(tmp_path, calls):
    source, item = _direct_args()
    work = tmp_path / "work"
    podcast_report.run_episode_to_script(source, item, work)

    acq = json.loads((work / "acquisition.json").read_text())
    assert acq["url"] == item.enclosure_url
    assert acq["bytes"] == 3
    assert acq["sha256"] == hashlib.sha256(b"mp3").hexdigest()
    from datetime import datetime

    assert datetime.fromisoformat(acq["downloaded_at"]).tzinfo is not None
    assert not list(work.glob("*.tmp"))


def test_existing_audio_does_not_rewrite_acquisition_json(tmp_path, calls):
    source, item = _direct_args()
    work = tmp_path / "work"
    podcast_report.run_episode_to_script(source, item, work)
    before = (work / "acquisition.json").read_bytes()
    stat_before = (work / "acquisition.json").stat()

    podcast_report.run_episode_to_script(source, item, work)

    assert (work / "acquisition.json").read_bytes() == before
    assert (work / "acquisition.json").stat().st_ino == stat_before.st_ino


def test_empty_audio_counts_as_a_fresh_download(tmp_path, calls):
    source, item = _direct_args()
    work = tmp_path / "work"
    podcast_report.run_episode_to_script(source, item, work)
    (work / "audio.mp3").write_bytes(b"")
    (work / "acquisition.json").write_text("stale")

    podcast_report.run_episode_to_script(source, item, work)

    assert json.loads((work / "acquisition.json").read_text())["bytes"] == 3


def test_item_json_is_written_once_and_never_churns(tmp_path, calls):
    source, item = _direct_args()
    work = tmp_path / "work"
    podcast_report.run_episode_to_script(source, item, work)
    before = (work / "item.json").read_bytes()
    assert "last_requested_at" not in json.loads(before)

    podcast_report.run_episode_to_script(source, item, work)
    assert (work / "item.json").read_bytes() == before

    from pipeline.podcast_feed import parse_podcast_feed

    other = next(
        i
        for i in parse_podcast_feed(FIXTURE.read_text(encoding="utf-8"))
        if i.guid != GUID
    )
    with pytest.raises(RuntimeError, match="work dir belongs to"):
        podcast_report.run_episode_to_script(source, other, work)
    assert (work / "item.json").read_bytes() == before


def test_dry_run_quiets_the_http_loggers(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    assert _run(tmp_path).exit_code == 0
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# --- enqueue / reset / list -------------------------------------------------

FEED_XML = FIXTURE.read_text(encoding="utf-8")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def queue_cli(monkeypatch):
    """Patch the feed fetch and the clock; forbid R2 and the pipeline itself.

    ``MY_PODCASTS_STATE_DB`` is already redirected to tmp_path by conftest, so
    the real ``StateStore`` runs against a throwaway DB.
    """
    state = {"fetch": lambda url: FEED_XML, "fetched": []}

    def fetch(url):
        state["fetched"].append(url)
        return state["fetch"](url)

    def boom(*a, **k):
        raise AssertionError("the queue commands must not run the pipeline or use R2")

    monkeypatch.setattr(podcast_report, "fetch_feed_xml", fetch)
    monkeypatch.setattr(main_mod, "_podcast_now", lambda: NOW)
    monkeypatch.setattr(main_mod, "R2Client", boom)
    monkeypatch.setattr("pipeline.r2.R2Client", boom)
    monkeypatch.setattr(podcast_report, "run_episode_to_script", boom)
    return state


def _q(*args):
    return CliRunner().invoke(main_mod.cli, ["podcast-report", *args])


def _store():
    return main_mod.StateStore(main_mod._default_state_db_path())


def test_enqueue_admits_a_pending_row(queue_cli):
    result = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 0, result.output
    assert f"enqueued argument/{GUID} as " in result.output
    assert '"Report: 2026-10-15 - ' in result.output
    store = _store()
    try:
        row = store.get_podcast_episode("argument", GUID)
    finally:
        store.close()
    assert row["status"] == "pending"
    assert row["admitted_via"] == "enqueue"


def test_enqueue_bypasses_since_cutoff(queue_cli):
    # The Jan 1 item predates the source's `since`; discovery would skip it.
    old = "Buzzsprout-12345"
    result = _q("enqueue", "--source", "argument", "--guid", old)
    assert result.exit_code == 0, result.output
    assert "enqueued" in result.output


def test_enqueue_twice_reports_already_pending(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    result = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 0, result.output
    assert "already pending" in result.output
    assert "reset" not in result.output


def test_enqueue_errored_row_points_at_reset(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    store = _store()
    try:
        store._conn.execute("UPDATE podcast_episodes SET status = 'errored'")
        store._conn.commit()
    finally:
        store.close()
    result = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 0, result.output
    assert "already errored" in result.output
    assert "podcast-report reset" in result.output


def test_enqueue_unknown_guid_exits_1(queue_cli):
    result = _q("enqueue", "--source", "argument", "--guid", "nope")
    assert result.exit_code == 1
    assert "not found" in result.output
    store = _store()
    try:
        assert store.list_podcast_episodes() == []
    finally:
        store.close()


def test_enqueue_unknown_source_exits_1(queue_cli):
    result = _q("enqueue", "--source", "nope", "--guid", GUID)
    assert result.exit_code == 1
    assert "unknown podcast source" in result.output
    assert queue_cli["fetched"] == []


def test_enqueue_future_item_exits_1(monkeypatch, queue_cli):
    monkeypatch.setattr(
        main_mod, "_podcast_now", lambda: datetime(2026, 10, 14, tzinfo=UTC)
    )
    result = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 1
    assert "future-dated" in result.output
    store = _store()
    try:
        assert store.list_podcast_episodes() == []
    finally:
        store.close()


def test_enqueue_fetch_error_exits_1(queue_cli):
    def down(url):
        raise httpx.ConnectError("no route")

    queue_cli["fetch"] = down
    result = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 1
    assert "ConnectError" in result.output


def test_enqueue_r2_key_collision_exits_1(queue_cli):
    first = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert first.exit_code == 0, first.output
    # Same title and date under a different guid -> same R2 key.
    clash = FEED_XML.replace(OTHER_GUID, "audioboom-clash").replace(
        "Thu, 08 Oct 2026 07:00:00 +0000", "Thu, 15 Oct 2026 07:00:00 +0000"
    )
    first_title = re.search(r"<title>([^<]*)</title>", FEED_XML.split("<item>")[1])
    second = clash.split("<item>")
    assert first_title is not None
    # Rewrite the second item's title to equal the first's.
    second[2] = re.sub(
        r"<title>[^<]*</title>",
        f"<title>{first_title.group(1)}</title>",
        second[2],
        count=1,
    )
    queue_cli["fetch"] = lambda url: "<item>".join(second)
    result = _q("enqueue", "--source", "argument", "--guid", "audioboom-clash")
    assert result.exit_code == 1, result.output
    assert "collides" in result.output


def test_reset_errored_row(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    store = _store()
    try:
        store._conn.execute(
            "UPDATE podcast_episodes SET status = 'errored', failure_count = 4, "
            "last_error = 'boom'"
        )
        store._conn.commit()
    finally:
        store.close()
    result = _q("reset", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 0, result.output
    assert f"reset argument/{GUID} (was errored)" in result.output
    store = _store()
    try:
        row = store.get_podcast_episode("argument", GUID)
    finally:
        store.close()
    assert (row["status"], row["failure_count"], row["last_error"]) == (
        "pending",
        0,
        None,
    )


@pytest.mark.parametrize("status", ["completed", "in_progress"])
def test_reset_refuses_completed_and_in_progress(queue_cli, status):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    store = _store()
    try:
        store._conn.execute("UPDATE podcast_episodes SET status = ?", (status,))
        store._conn.commit()
    finally:
        store.close()
    result = _q("reset", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 1
    assert status in result.output
    store = _store()
    try:
        assert store.get_podcast_episode("argument", GUID)["status"] == status
    finally:
        store.close()


def test_reset_unknown_row_exits_1(queue_cli):
    result = _q("reset", "--source", "argument", "--guid", "nope")
    assert result.exit_code == 1
    assert "no podcast episode" in result.output


def test_list_empty(queue_cli):
    result = _q("list")
    assert result.exit_code == 0
    assert "No podcast episodes" in result.output


def test_list_shows_rows_and_truncates_error(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    _q("enqueue", "--source", "argument", "--guid", OTHER_GUID)
    store = _store()
    try:
        store._conn.execute(
            "UPDATE podcast_episodes SET status = 'errored', failure_count = 4, "
            "last_error = ? WHERE guid = ?",
            ("x" * 300 + "\nsecond line", GUID),
        )
        store._conn.commit()
    finally:
        store.close()
    result = _q("list")
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("errored\t2026-10-15\tfailures=4\tReport: ")
    assert lines[0].endswith("\t" + "x" * 120)
    assert lines[1].startswith("pending\t2026-10-08\tfailures=0\t")
    assert _q("list", "--source", "argument").output == result.output
    assert "No podcast episodes" in _q("list", "--source", "other").output


def test_queue_commands_never_touch_r2_or_pipeline(queue_cli):
    # The fixture's boom() raises AssertionError if a command reaches R2 or
    # runs the pipeline; CliRunner would surface that as result.exception.
    for args, code in (
        (("enqueue", "--source", "argument", "--guid", GUID), 0),
        (("list",), 0),
        (("reset", "--source", "argument", "--guid", GUID), 0),
    ):
        result = _q(*args)
        assert result.exit_code == code, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


def test_enqueue_completed_row_reports_already_completed(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    store = _store()
    try:
        store._conn.execute("UPDATE podcast_episodes SET status = 'completed'")
        store._conn.commit()
    finally:
        store.close()
    result = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 0, result.output
    assert "already completed" in result.output
    assert "reset" not in result.output


@pytest.mark.parametrize("body", ["<html><body>Bad gateway", "", "not xml at all"])
def test_enqueue_unparseable_feed_exits_1_cleanly(queue_cli, body):
    queue_cli["fetch"] = lambda url: body
    result = _q("enqueue", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 1
    assert "ParseError" in result.output
    assert not isinstance(result.exception, ET.ParseError)
    store = _store()
    try:
        assert store.list_podcast_episodes() == []
    finally:
        store.close()


def test_reset_pending_row_makes_it_due_now(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    store = _store()
    try:
        store._conn.execute(
            "UPDATE podcast_episodes SET failure_count = 2, last_error = 'e', "
            "next_attempt_at = '2099-01-01T00:00:00+00:00'"
        )
        store._conn.commit()
    finally:
        store.close()
    result = _q("reset", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 0, result.output
    assert f"reset argument/{GUID} (was pending)" in result.output
    store = _store()
    try:
        row = store.get_podcast_episode("argument", GUID)
    finally:
        store.close()
    assert row["failure_count"] == 0
    assert datetime.fromisoformat(row["next_attempt_at"]) == NOW


def test_reset_in_progress_hints_at_restarting_the_consumer(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    store = _store()
    try:
        store._conn.execute("UPDATE podcast_episodes SET status = 'in_progress'")
        store._conn.commit()
    finally:
        store.close()
    result = _q("reset", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 1
    assert "restart the consumer" in result.output


def test_reset_completed_does_not_suggest_restarting(queue_cli):
    _q("enqueue", "--source", "argument", "--guid", GUID)
    store = _store()
    try:
        store._conn.execute("UPDATE podcast_episodes SET status = 'completed'")
        store._conn.commit()
    finally:
        store.close()
    result = _q("reset", "--source", "argument", "--guid", GUID)
    assert result.exit_code == 1
    assert "restart" not in result.output
