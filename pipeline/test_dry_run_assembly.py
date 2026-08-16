from __future__ import annotations

import json
from unittest.mock import MagicMock

from pipeline.exa_client import exa_file_path
from pipeline.things_happen_collector import _slugify


def test_dry_run_uses_the_same_assembler_as_the_consumer(tmp_path, monkeypatch):
    """Dry-run must get the Exa append, not a private loop that predates it.

    The old hand-rolled loop in _the_rundown_dry_run called
    _find_rundown_article_text directly, which has no Exa-append step (that
    logic lives only in consumer._assemble_writer_inputs). A hand-published
    --dry-run episode was therefore generated from a materially different
    prompt than production. This pins the fix: the dry-run path must go
    through the same assembler.
    """
    import pipeline.__main__ as main_module

    headline = "Widget Maker Announces New Product"
    theme = "Tech"
    slug = _slugify(headline)

    def fake_collect_all_artifacts(run_id, work_dir, **kwargs):
        # Stand in for the real collector: write a plan + a stub article
        # (that resolves via headline_index.json) + an Exa hit for the same
        # slug, exactly like the real disk layout _assemble_writer_inputs
        # expects.
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "plan.json").write_text(
            json.dumps(
                {
                    "themes": [theme],
                    "directives": [
                        {
                            "headline": headline,
                            "source": "levine",
                            "priority": 1,
                            "theme": theme,
                            "needs_exa": False,
                            "exa_query": "",
                            "is_foreign_policy": False,
                            "fp_query": "",
                            "include_in_episode": True,
                        }
                    ],
                    "rotation_override": None,
                }
            ),
            encoding="utf-8",
        )
        articles_dir = work_dir / "articles"
        articles_dir.mkdir(parents=True, exist_ok=True)
        rel_path = "articles/00-stub.md"
        (work_dir / rel_path).write_text(headline, encoding="utf-8")
        (work_dir / "headline_index.json").write_text(
            json.dumps({headline: rel_path}), encoding="utf-8"
        )
        exa_path = exa_file_path(work_dir, slug)
        exa_path.parent.mkdir(parents=True, exist_ok=True)
        exa_path.write_text(
            "# Exa Results for: X\nResult: hit\nQuery: widgets\n\n"
            "## [Widget News](https://w.example)\nReal open-access body.\n\n",
            encoding="utf-8",
        )

    # Redirect the hardcoded "/tmp/the-rundown-<uuid>" work dir under
    # tmp_path so the test stays hermetic. _the_rundown_dry_run builds this
    # path with a bare `Path(f"/tmp/the-rundown-{run_id}")`, so we intercept
    # the module's Path name rather than an env var (there's no seam for one
    # here -- that's a separate, pre-existing gap, not part of this task).
    real_path_cls = main_module.Path

    def fake_path(arg):
        if isinstance(arg, str) and arg.startswith("/tmp/the-rundown-"):
            return tmp_path / arg.removeprefix("/tmp/")
        return real_path_cls(arg)

    monkeypatch.setattr(main_module, "Path", fake_path)
    monkeypatch.setattr(
        "pipeline.things_happen_collector.collect_all_artifacts",
        fake_collect_all_artifacts,
    )
    writer_mock = MagicMock(
        return_value=MagicMock(script="script", summary="summary", covered_headlines=[])
    )
    monkeypatch.setattr("pipeline.rundown_writer.generate_rundown_script", writer_mock)

    main_module._the_rundown_dry_run("2026-03-10")

    assert writer_mock.called
    articles_by_theme = writer_mock.call_args.kwargs["articles_by_theme"]
    assembled_text = "\n".join(articles_by_theme[theme])
    assert "Related coverage from other outlets" in assembled_text
    assert "Real open-access body." in assembled_text
