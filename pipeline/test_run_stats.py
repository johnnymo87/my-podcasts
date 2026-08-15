from __future__ import annotations

import json

from pipeline.run_stats import append_jsonl, collect_run_stats, render_report


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
        "unknown": 1,
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
    assert "FETCH  levine 5: unknown 5" in report or "unknown 5" in report


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
