from __future__ import annotations

import json

from pipeline.run_stats import (
    RunStats,
    append_jsonl,
    collect_run_stats,
    render_report,
)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _populated_work_dir(tmp_path):
    work_dir = tmp_path / "the-rundown-full"
    work_dir.mkdir()

    _write_json(
        work_dir / "collection_done.json",
        {
            "job_id": "job-full",
            "started_at": "2026-08-15T04:00:00-04:00",
            "completed_at": "2026-08-15T04:04:12-04:00",
            "lookback_days": 3,
            "levine_articles": 15,
            "directives": 14,
            "fp_routed": 5,
            "enriched": 9,
            "candidates": {"levine": 21, "semafor": 19, "zvi": 7},
            "deduped": {"levine": 6, "semafor": 0, "zvi": 0},
            "exa_outcomes": {
                "slug-hit-0": "hit",
                "slug-hit-1": "hit",
                "slug-hit-2": "hit",
                "slug-empty-0": "empty",
                "slug-empty-1": "empty",
                "slug-empty-2": "empty",
                "slug-error-0": "error:ValueError",
            },
        },
    )

    tiers = {}
    for i in range(6):
        tiers[f"articles/{i:02d}-live{i}.md"] = {
            "tier": "live",
            "extracted_chars": 1000,
            "url": f"https://example.com/live{i}",
        }
    paywalled_urls = [
        "https://www.bloomberg.com/news/a",
        "https://bloomberg.com/news/b",
        "https://ft.com/content/c",
        "https://www.ft.com/content/d",
        "https://wsj.com/articles/e",
        "https://www.nytimes.com/f",
        "https://bloomberg.com/g",
        "https://economist.com/h",
    ]
    for i, url in enumerate(paywalled_urls):
        tiers[f"articles/{6 + i:02d}-pw{i}.md"] = {
            "tier": "paywalled",
            "extracted_chars": 50,
            "url": url,
        }
    tiers["articles/14-herr0.md"] = {
        "tier": "http_error",
        "extracted_chars": 0,
        "url": "https://example.com/herr0",
    }
    _write_json(work_dir / "tiers.json", tiers)

    directives = []
    for i in range(9):
        directives.append(
            {
                "headline": f"Episode story {i}",
                "source": "levine",
                "priority": i + 1,
                "theme": "Theme A",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": False,
                "fp_query": "",
                "include_in_episode": True,
            }
        )
    for i in range(5):
        directives.append(
            {
                "headline": f"FP story {i}",
                "source": "levine",
                "priority": i + 1,
                "theme": "Theme B",
                "needs_exa": False,
                "exa_query": "",
                "is_foreign_policy": True,
                "fp_query": "war",
                "include_in_episode": False,
            }
        )
    _write_json(
        work_dir / "plan.json",
        {
            "themes": ["Theme A", "Theme B", "Theme C", "Theme D"],
            "directives": directives,
            "rotation_override": None,
        },
    )

    (work_dir / "articles" / "semafor").mkdir(parents=True)
    (work_dir / "articles" / "semafor" / "cache-story.md").write_text(
        "# Cache story\n\nBody", encoding="utf-8"
    )
    (work_dir / "articles" / "zvi").mkdir(parents=True)
    (work_dir / "articles" / "zvi" / "cache-story2.md").write_text(
        "# Cache story 2\n\nBody", encoding="utf-8"
    )
    (work_dir / "enrichment" / "exa").mkdir(parents=True)
    (work_dir / "enrichment" / "exa" / "exa-story.md").write_text(
        "# Exa story\n\nBody", encoding="utf-8"
    )

    writer_inputs = [
        {
            "headline": "Episode story 0",
            "theme": "Theme A",
            "source_path": "articles/00-live0.md",
            "chars": 1000,
        },
        {
            "headline": "Episode story 1",
            "theme": "Theme A",
            "source_path": "articles/01-live1.md",
            "chars": 1000,
        },
        {
            "headline": "Episode story 2",
            "theme": "Theme A",
            "source_path": "articles/02-live2.md",
            "chars": 1000,
        },
        {
            "headline": "Episode story 3",
            "theme": "Theme A",
            "source_path": "articles/06-pw0.md",
            "chars": 50,
        },
        {
            "headline": "Episode story 4",
            "theme": "Theme A",
            "source_path": "articles/07-pw1.md",
            "chars": 50,
        },
        {
            "headline": "Episode story 5",
            "theme": "Theme A",
            "source_path": "articles/semafor/cache-story.md",
            "chars": 300,
        },
        {
            "headline": "Episode story 6",
            "theme": "Theme A",
            "source_path": "articles/zvi/cache-story2.md",
            "chars": 300,
        },
        {
            "headline": "Episode story 7",
            "theme": "Theme A",
            "source_path": "enrichment/exa/exa-story.md",
            "chars": 200,
        },
        {
            "headline": "Episode story 8",
            "theme": "Theme A",
            "source_path": None,
            "chars": 0,
        },
    ]
    _write_json(work_dir / "writer_inputs.json", writer_inputs)

    (work_dir / "script.txt").write_text(" ".join(["word"] * 25), encoding="utf-8")
    _write_json(
        work_dir / "covered.json",
        [f"Episode story {i}" for i in range(6)],
    )

    return work_dir


def test_fully_populated_fixture_every_number_exact(tmp_path):
    work_dir = _populated_work_dir(tmp_path)

    stats = collect_run_stats(work_dir, job_id="job-full", date_str="2026-08-15")

    assert stats.lookback_days == 3
    assert stats.collect_duration_seconds == 252.0

    assert stats.candidates == {"levine": 21, "semafor": 19, "zvi": 7}
    assert stats.deduped == {"levine": 6, "semafor": 0, "zvi": 0}
    assert stats.levine_articles == 15

    assert stats.fetch_tiers == {
        "live": 6,
        "paywalled": 8,
        "http_error": 1,
        "fetch_error": 0,
    }

    assert stats.directives_total == 14
    assert stats.directives_episode == 9
    assert stats.directives_fp_routed == 5
    assert stats.themes_count == 4

    assert stats.exa_flagged == 7
    assert stats.exa_outcomes == {
        "hit": 3,
        "empty": 3,
        # Always present, distinct from "empty": Exa returned results but the
        # deny-list rejected all of them (paywalled origin or bypass mirror).
        "filtered": 0,
        "no_key": 0,
        "error": 1,
    }

    assert stats.writer_selected == 9
    assert stats.writer_with_text == 8
    assert stats.writer_dropped == 1
    assert stats.writer_buckets == {
        "live": 3,
        "paywalled": 2,
        "http_error": 0,
        "fetch_error": 0,
        "cache": 2,
        "exa": 1,
        "unknown": 0,
    }

    assert stats.script_words == 25
    assert stats.covered_headlines == 6

    assert stats.paywalled_domains == [
        ("bloomberg.com", 3),
        ("ft.com", 2),
        ("economist.com", 1),
        ("nytimes.com", 1),
        ("wsj.com", 1),
    ]


def test_render_report_contains_labels_and_histogram(tmp_path):
    work_dir = _populated_work_dir(tmp_path)
    stats = collect_run_stats(work_dir, job_id="job-full", date_str="2026-08-15")

    report = render_report(stats)

    assert "IN     47 = levine 21, semafor 19, zvi 7" in report
    assert "DEDUP  -6 (levine 6)" in report
    assert "FETCH  levine 15:" in report
    assert "PLAN   14 directives = 9 episode, 5 fp-routed" in report
    assert "EXA    7 flagged ->" in report
    assert "WRITE  9 selected -> 8 with text" in report
    assert "OUT    25 words, 4 themes, 6 headlines covered" in report
    assert "paywalled: bloomberg.com 3" in report
    assert "collect 4m12s" in report
    assert "lookback 3d" in report
    assert len(report) < 4000
    # Plain text only: the delivery endpoint sends no parse_mode.
    assert "*" not in report
    assert "`" not in report


def test_collection_done_json_empty_object_does_not_crash(tmp_path):
    work_dir = tmp_path / "the-rundown-empty-sentinel"
    work_dir.mkdir()
    (work_dir / "collection_done.json").write_text("{}", encoding="utf-8")

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.lookback_days is None
    assert stats.candidates == {"levine": 0, "semafor": 0, "zvi": 0}
    assert stats.deduped == {"levine": 0, "semafor": 0, "zvi": 0}
    assert stats.levine_articles is None
    assert stats.directives_total == 0
    assert stats.exa_flagged == 0

    report = render_report(stats)
    assert "IN     0 = levine 0, semafor 0, zvi 0" in report
    assert len(report) < 4000


def test_render_report_cosmetics_empty_date_fetch_and_dedup(tmp_path):
    """Finding 7: three visible-in-every-manual-report cosmetics.

    - An empty date_str (the CLI's `run-stats` command always passes one)
      must not leave a doubled space in the header.
    - An empty FETCH breakdown must not leave a dangling colon.
    - A zero DEDUP total must not render as "-0".
    """
    work_dir = tmp_path / "the-rundown-cosmetics"
    work_dir.mkdir()
    # No collection_done.json/tiers.json/writer_inputs.json at all -- every
    # dict-valued field defaults to its zero shape.

    stats = collect_run_stats(work_dir, job_id="job-cosmetic", date_str="")
    report = render_report(stats)

    assert "The Rundown (job job-cosmetic) - script stage" in report
    assert "The Rundown  (job" not in report
    assert "FETCH  levine 0\n" in report
    assert "FETCH  levine 0:" not in report
    assert "DEDUP  0" in report
    assert "DEDUP  -0" not in report


def test_missing_plan_json_yields_partial_stats(tmp_path):
    work_dir = tmp_path / "the-rundown-no-plan"
    work_dir.mkdir()
    _write_json(
        work_dir / "collection_done.json",
        {
            "started_at": "2026-08-15T04:00:00-04:00",
            "completed_at": "2026-08-15T04:01:00-04:00",
            "lookback_days": 2,
            "levine_articles": 3,
            "candidates": {"levine": 3, "semafor": 0, "zvi": 0},
            "deduped": {"levine": 0, "semafor": 0, "zvi": 0},
        },
    )
    tiers = {
        f"articles/{i:02d}-live{i}.md": {
            "tier": "live",
            "extracted_chars": 100,
            "url": f"https://example.com/{i}",
        }
        for i in range(3)
    }
    _write_json(work_dir / "tiers.json", tiers)
    # No plan.json written at all.

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.directives_total == 0
    assert stats.directives_episode == 0
    assert stats.directives_fp_routed == 0
    assert stats.themes_count == 0
    assert stats.fetch_tiers["live"] == 3

    report = render_report(stats)
    assert "PLAN   0 directives = 0 episode, 0 fp-routed" in report


def test_missing_tiers_json_lands_in_unknown_bucket(tmp_path):
    work_dir = tmp_path / "the-rundown-pre-instrumentation"
    work_dir.mkdir()
    _write_json(
        work_dir / "collection_done.json",
        {"levine_articles": 5},
    )
    # No tiers.json -- a pre-Task-2.2 work dir.
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Some story",
                "theme": "Theme A",
                "source_path": "articles/00-some-story.md",
                "chars": 400,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    # Fetch breakdown cannot be classified without tiers.json, but the
    # sentinel's levine_articles count is still real activity -- it must
    # land as "unknown", not silently vanish to zero.
    assert stats.fetch_tiers.get("unknown") == 5
    assert stats.fetch_tiers["live"] == 0

    # The writer input can't be joined against a tier either.
    assert stats.writer_buckets["unknown"] == 1
    assert stats.writer_with_text == 1

    report = render_report(stats)
    assert "FETCH  levine 5: unknown 5" in report


def test_work_dir_does_not_exist(tmp_path):
    missing = tmp_path / "does-not-exist"

    stats = collect_run_stats(missing, job_id="j", date_str="2026-08-15")

    assert stats.directives_total == 0
    assert stats.candidates == {}
    report = render_report(stats)
    assert "IN     0 =" in report
    assert len(report) < 4000


def test_work_dir_is_a_file(tmp_path):
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x", encoding="utf-8")

    stats = collect_run_stats(not_a_dir, job_id="j", date_str="2026-08-15")

    assert stats.directives_total == 0
    report = render_report(stats)
    assert len(report) < 4000


def test_render_report_bounded_for_pathologically_large_input(tmp_path):
    work_dir = tmp_path / "the-rundown-huge"
    work_dir.mkdir()

    _write_json(
        work_dir / "collection_done.json",
        {
            "started_at": "2026-08-15T04:00:00-04:00",
            "completed_at": "2026-08-15T05:00:00-04:00",
            "lookback_days": 14,
            "levine_articles": 5000,
            "candidates": {"levine": 5000, "semafor": 5000, "zvi": 5000},
            "deduped": {"levine": 100, "semafor": 100, "zvi": 100},
            "exa_outcomes": {f"slug-{i}": "hit" for i in range(5000)},
        },
    )

    tiers = {}
    for i in range(5000):
        tiers[f"articles/{i:05d}.md"] = {
            "tier": "paywalled",
            "extracted_chars": 50,
            "url": f"https://publisher-{i}.example.com/story",
        }
    _write_json(work_dir / "tiers.json", tiers)

    directives = [
        {
            "headline": f"Story {i}" * 20,  # deliberately long headline text
            "source": "levine",
            "priority": 1,
            "theme": "Theme",
            "needs_exa": False,
            "exa_query": "",
            "is_foreign_policy": False,
            "fp_query": "",
            "include_in_episode": True,
        }
        for i in range(5000)
    ]
    _write_json(
        work_dir / "plan.json",
        {"themes": [f"Theme {i}" for i in range(200)], "directives": directives},
    )

    writer_inputs = [
        {
            "headline": f"Story {i}",
            "theme": "Theme",
            "source_path": f"articles/{i:05d}.md",
            "chars": 50,
        }
        for i in range(5000)
    ]
    _write_json(work_dir / "writer_inputs.json", writer_inputs)

    (work_dir / "script.txt").write_text(" ".join(["word"] * 50000), encoding="utf-8")
    _write_json(work_dir / "covered.json", [f"Story {i}" for i in range(5000)])

    stats = collect_run_stats(work_dir, job_id="huge-job", date_str="2026-08-15")
    report = render_report(stats)

    assert len(report) < 4000
    # The histogram must actually be capped at 8 rows, not just short by luck.
    assert len(stats.paywalled_domains) == 8


def test_mixed_naive_aware_sentinel_timestamps_never_raise(tmp_path):
    """Finding 2: a sentinel with one naive and one timezone-aware timestamp
    parses fine individually via datetime.fromisoformat, but subtracting
    them raises TypeError ("can't subtract offset-naive and offset-aware
    datetimes"). collect_run_stats's docstring and module header both claim
    it never raises -- this pins that a mixed pair degrades to a None
    duration instead of propagating.
    """
    work_dir = tmp_path / "the-rundown-mixed-tz"
    work_dir.mkdir()
    _write_json(
        work_dir / "collection_done.json",
        {
            "started_at": "2026-08-15T04:00:00",  # naive
            "completed_at": "2026-08-15T04:04:12-04:00",  # aware
        },
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")  # no raise

    assert stats.collect_duration_seconds is None
    # The raw strings are still surfaced -- only the derived duration is lost.
    assert stats.collect_started_at == "2026-08-15T04:00:00"
    assert stats.collect_completed_at == "2026-08-15T04:04:12-04:00"

    report = render_report(stats)  # must also not raise
    assert "script stage" in report


def test_append_jsonl_writes_one_line_and_creates_parent_dir(tmp_path):
    work_dir = tmp_path / "the-rundown-append"
    work_dir.mkdir()
    stats = collect_run_stats(work_dir, job_id="j1", date_str="2026-08-15")

    target = tmp_path / "nested" / "run-stats.jsonl"
    append_jsonl(stats, target)

    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["job_id"] == "j1"
    assert parsed["date_str"] == "2026-08-15"


def test_writer_input_with_exa_appended_is_counted(tmp_path):
    """A paywalled stub that received Exa text must be distinguishable."""
    work_dir = tmp_path / "the-rundown-exa-appended"
    work_dir.mkdir()
    _write_json(
        work_dir / "tiers.json",
        {
            "articles/00-pw0.md": {
                "tier": "paywalled",
                "extracted_chars": 50,
                "url": "https://bloomberg.com/news/a",
            }
        },
    )
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Stubbed story",
                "theme": "Theme A",
                "source_path": "articles/00-pw0.md",
                "chars": 550,
                "exa_appended": True,
                "exa_chars": 500,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_exa_appended == 1
    assert stats.writer_exa_chars == 500
    assert stats.writer_buckets["paywalled+exa"] == 1
    assert stats.writer_buckets["paywalled"] == 0

    report = render_report(stats)
    assert ", 1 +open-access" in report


def test_exa_appended_absent_on_historical_dirs(tmp_path):
    """Work dirs written before this feature have no exa_appended key."""
    work_dir = tmp_path / "the-rundown-historical"
    work_dir.mkdir()
    _write_json(
        work_dir / "tiers.json",
        {
            "articles/00-live0.md": {
                "tier": "live",
                "extracted_chars": 1000,
                "url": "https://example.com/live0",
            }
        },
    )
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Live story",
                "theme": "Theme A",
                "source_path": "articles/00-live0.md",
                "chars": 1000,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_exa_appended == 0
    assert stats.writer_exa_chars == 0
    assert stats.writer_buckets["live"] == 1
    assert "live+exa" not in stats.writer_buckets

    report = render_report(stats)
    assert "+open-access" not in report


def test_render_report_byte_identical_when_no_exa_appended(tmp_path):
    """Historical work dirs must render exactly as before this feature."""
    work_dir = _populated_work_dir(tmp_path)
    stats = collect_run_stats(work_dir, job_id="job-full", date_str="2026-08-15")

    report = render_report(stats)

    assert (
        "WRITE  9 selected -> 8 with text (3 live, 2 paywalled, 2 cache, 1 exa), "
        "1 dropped" in report
    )
    assert "+open-access" not in report


def test_append_jsonl_appends_across_calls(tmp_path):
    work_dir = tmp_path / "the-rundown-append2"
    work_dir.mkdir()
    stats1 = collect_run_stats(work_dir, job_id="j1", date_str="2026-08-15")
    stats2 = collect_run_stats(work_dir, job_id="j2", date_str="2026-08-16")

    target = tmp_path / "run-stats.jsonl"
    append_jsonl(stats1, target)
    append_jsonl(stats2, target)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["job_id"] == "j1"
    assert json.loads(lines[1])["job_id"] == "j2"


def test_dropped_before_prompt_counts_text_that_never_reached_the_model(tmp_path):
    """reached_prompt False with chars > 0 is a regression canary: text was
    resolved (has a source_path and length) but never rendered into a
    section. Should be zero on every healthy run."""
    work_dir = tmp_path / "the-rundown-regression"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Orphaned story",
                "theme": "Ghost Theme",
                "source_path": "articles/00-live0.md",
                "chars": 500,
                "reached_prompt": False,
                "miss_reason": None,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_dropped_before_prompt == 1

    report = render_report(stats)
    assert "DROPPED-AFTER-RESOLVE" in report


def test_missing_reached_prompt_key_is_not_counted_as_dropped(tmp_path):
    """Historical writer_inputs.json predates the field; must not false-alarm."""
    work_dir = tmp_path / "the-rundown-legacy"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Legacy story",
                "theme": "Theme A",
                "source_path": "articles/00-live0.md",
                "chars": 500,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_dropped_before_prompt == 0

    report = render_report(stats)
    assert "DROPPED-AFTER-RESOLVE" not in report


def test_render_report_shows_miss_reason_histogram(tmp_path):
    work_dir = tmp_path / "the-rundown-misses"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Miss one",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "no_index",
            },
            {
                "headline": "Miss two",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "index_no_overlap",
            },
            {
                "headline": "Miss three",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "index_no_overlap",
            },
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_reasons == {"no_index": 1, "index_no_overlap": 2}

    report = render_report(stats)
    assert "no_index 1" in report
    assert "index_no_overlap 2" in report


def test_missing_miss_reason_key_produces_no_histogram_entry(tmp_path):
    """Historical writer_inputs.json predates miss_reason; must not invent data."""
    work_dir = tmp_path / "the-rundown-legacy-miss"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Legacy miss",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_reasons == {}
    assert stats.writer_dropped == 1

    report = render_report(stats)
    assert "misses" not in report


def test_legacy_index_no_overlap_reason_renders_without_shadow(tmp_path):
    """Retired fuzzy-tier value + no `shadow` key at all (pre-shadow data).

    Both `/persist/my-podcasts/run-stats.jsonl` history and old
    `writer_inputs.json` files on disk carry `index_no_overlap`, and predate
    the `shadow` field entirely. Neither should crash or be miscounted.
    """
    work_dir = tmp_path / "the-rundown-legacy-overlap"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Old miss",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "index_no_overlap",
                # no "shadow" key at all
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_reasons == {"index_no_overlap": 1}
    assert stats.writer_miss_shadow_hits == 0

    report = render_report(stats)
    assert "index_no_overlap 1" in report
    assert "w/ shadow" not in report


def test_slug_ambiguous_renders_in_miss_histogram(tmp_path):
    """New miss reason: two indexed headlines share the directive's slug."""
    work_dir = tmp_path / "the-rundown-ambiguous"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Ambiguous story",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "slug_ambiguous",
                "shadow": None,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_reasons == {"slug_ambiguous": 1}

    report = render_report(stats)
    assert "slug_ambiguous 1" in report


def test_shadow_hit_count_rendered_alongside_miss_histogram(tmp_path):
    """Misses that had a shadow candidate are surfaced as a count."""
    work_dir = tmp_path / "the-rundown-shadow-hits"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Miss with shadow",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "index_no_match",
                "shadow": {"path": "articles/00-live0.md", "score": 0.5},
            },
            {
                "headline": "Miss without shadow",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "index_no_match",
                "shadow": None,
            },
            {
                "headline": "Another miss reason",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "no_index",
                "shadow": None,
            },
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_reasons == {"index_no_match": 2, "no_index": 1}
    assert stats.writer_miss_shadow_hits == 1

    report = render_report(stats)
    assert "1 w/ shadow" in report


def test_zero_shadow_hits_renders_no_shadow_fragment(tmp_path):
    """No noise when nothing had a shadow candidate -- matches today's format."""
    work_dir = tmp_path / "the-rundown-no-shadow-hits"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Plain miss",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "no_index",
                "shadow": None,
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_shadow_hits == 0

    report = render_report(stats)
    assert "[misses: no_index 1]" in report
    assert "w/ shadow" not in report


def test_shadow_counter_only_counts_missed_entries(tmp_path):
    """A `shadow` value on a *hit* entry (miss_reason None) must not count.

    consumer.py never actually produces this shape (shadow is only computed
    when miss_reason is not None), but the counter must not trust the
    presence of `shadow` alone -- it has to gate on an actual miss.
    """
    work_dir = tmp_path / "the-rundown-shadow-on-hit"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Resolved story",
                "theme": "Theme A",
                "source_path": "articles/00-live0.md",
                "chars": 500,
                "reached_prompt": True,
                "miss_reason": None,
                "shadow": {"path": "articles/00-live0.md", "score": 0.9},
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_reasons == {}
    assert stats.writer_miss_shadow_hits == 0

    report = render_report(stats)
    assert "w/ shadow" not in report


def test_missing_shadow_key_on_a_miss_is_not_counted_as_a_hit(tmp_path):
    """A missing `shadow` key on a miss entry must read as 'no candidate',
    never as a shadow hit -- same discipline as the missing-miss_reason and
    missing-reached_prompt precedents in this file."""
    work_dir = tmp_path / "the-rundown-shadow-key-missing"
    work_dir.mkdir()
    _write_json(
        work_dir / "writer_inputs.json",
        [
            {
                "headline": "Miss no shadow key",
                "theme": "Theme A",
                "source_path": None,
                "chars": 0,
                "reached_prompt": False,
                "miss_reason": "no_index",
                # no "shadow" key at all
            }
        ],
    )

    stats = collect_run_stats(work_dir, job_id="j", date_str="2026-08-15")

    assert stats.writer_miss_shadow_hits == 0


def _rundown_stats():
    return RunStats(job_id="j", date_str="2026-08-19")


def test_run_stats_feed_defaults_to_the_rundown():
    """Historical jsonl rows carry no `feed` key and must still parse."""
    stats = RunStats(job_id="j", date_str="2026-08-19")
    assert stats.feed == "the-rundown"
    revived = RunStats.model_validate_json('{"job_id":"j","date_str":"2026-08-19"}')
    assert revived.feed == "the-rundown"


def test_render_report_is_unchanged_for_the_rundown():
    """Commit 1 must be a strict no-op for the feed already in production."""
    stats = _rundown_stats()
    report = render_report(stats)
    assert report.startswith("The Rundown 2026-08-19 (job j) - script stage")
    for stage in ("IN ", "DEDUP ", "FETCH ", "PLAN ", "EXA ", "WRITE ", "OUT "):
        assert stage in report


def test_fp_report_header_names_the_feed():
    stats = RunStats(job_id="j", date_str="2026-08-19", feed="fp-digest")
    report = render_report(stats)
    assert report.startswith("FP Digest 2026-08-19 (job j) - script stage")


def test_fp_report_omits_stages_with_no_data_source():
    """Not zeros: FP writes no candidates/tiers/exa/writer_inputs at all.

    Rendering `IN 0 = levine 0, semafor 0, zvi 0` on an FP dir would assert
    FP has Levine and Zvi sources, which it does not. See my-podcasts-8m8.
    """
    stats = RunStats(job_id="j", date_str="2026-08-19", feed="fp-digest")
    report = render_report(stats)
    for absent in ("IN ", "ROUTE ", "DEDUP ", "FETCH ", "EXA ", "WRITE ", "paywalled"):
        assert absent not in report
    assert "levine" not in report
    assert "zvi" not in report


def test_fp_plan_line_drops_the_degenerate_routing_split():
    """directives_fp_routed is 0 on all 13 real FP work dirs -- FP *is* the fp feed."""
    stats = RunStats(
        job_id="j",
        date_str="2026-08-19",
        feed="fp-digest",
        directives_total=6,
        directives_episode=6,
        directives_fp_routed=0,
    )
    line = [x for x in render_report(stats).splitlines() if x.startswith("PLAN")][0]
    assert line == "PLAN   6 directives"


def test_fp_report_keeps_out_line_that_catches_the_real_incidents():
    """The 2026-08-18 placeholder published as `1 words`; 5d2519dc refused at 76."""
    stats = RunStats(
        job_id="j",
        date_str="2026-08-19",
        feed="fp-digest",
        directives_total=6,
        script_words=1,
        themes_count=5,
        covered_headlines=4,
    )
    report = render_report(stats)
    assert "OUT    1 words, 5 themes, 4 headlines covered" in report
    assert "PLAN   6 directives" in report


def test_unknown_feed_falls_back_to_the_full_rundown_shaped_report():
    """A typo'd feed must not silently render an empty report."""
    stats = RunStats(job_id="j", date_str="2026-08-19", feed="wat")
    report = render_report(stats)
    assert "PLAN " in report and "OUT " in report


def test_collect_run_stats_forwards_feed_onto_run_stats(tmp_path):
    """collect_run_stats must thread its `feed` argument onto the RunStats it
    builds, not just accept and drop it -- the FP call site depends on this."""
    work_dir = tmp_path / "fp-digest-abc123"
    work_dir.mkdir()

    stats = collect_run_stats(
        work_dir, job_id="abc123", date_str="2026-08-19", feed="fp-digest"
    )

    assert stats.feed == "fp-digest"


def test_collect_run_stats_feed_defaults_to_the_rundown(tmp_path):
    work_dir = tmp_path / "the-rundown-abc123"
    work_dir.mkdir()

    stats = collect_run_stats(work_dir, job_id="abc123", date_str="2026-08-19")

    assert stats.feed == "the-rundown"


def test_render_report_golden_full_string_for_the_rundown(tmp_path):
    """Byte-for-byte regression: The Rundown's report is in daily production
    use, and hand-wrapping every render block in `if "<stage>" in stages:`
    is exactly where an indent or spacing slip would sneak in unnoticed by
    the substring assertions above."""
    work_dir = _populated_work_dir(tmp_path)
    stats = collect_run_stats(work_dir, job_id="job-full", date_str="2026-08-15")

    report = render_report(stats)

    assert report == (
        "The Rundown 2026-08-15 (job job-full) - script stage - "
        "collect 4m12s, lookback 3d\n"
        "\n"
        "IN     47 = levine 21, semafor 19, zvi 7\n"
        "DEDUP  -6 (levine 6)\n"
        "FETCH  levine 15: live 6, paywalled 8, http_error 1\n"
        "PLAN   14 directives = 9 episode, 5 fp-routed\n"
        "EXA    7 flagged -> 3 hit, 3 empty, 1 error\n"
        "WRITE  9 selected -> 8 with text (3 live, 2 paywalled, 2 cache, "
        "1 exa), 1 dropped\n"
        "OUT    25 words, 4 themes, 6 headlines covered\n"
        "\n"
        "paywalled: bloomberg.com 3, ft.com 2, economist.com 1, "
        "nytimes.com 1, wsj.com 1"
    )

    assert stats.writer_miss_shadow_hits == 0
