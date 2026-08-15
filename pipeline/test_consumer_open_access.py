from __future__ import annotations

import json

from pipeline.consumer import _assemble_writer_inputs
from pipeline.exa_client import exa_file_path
from pipeline.things_happen_collector import _slugify
from pipeline.things_happen_editor import RundownResearchPlan, RundownStoryDirective


def _plan(headline: str, theme: str = "Tech") -> RundownResearchPlan:
    return RundownResearchPlan(
        themes=[theme],
        directives=[
            RundownStoryDirective(
                headline=headline,
                source="levine",
                priority=1,
                theme=theme,
                needs_exa=False,
                exa_query="",
                is_foreign_policy=False,
                fp_query="",
                include_in_episode=True,
            )
        ],
    )


def _write_stub(work_dir, headline: str, stub_text: str) -> str:
    """Write a stub article + headline_index.json so it resolves exactly.

    Returns the work-dir-relative path recorded in the index.
    """
    articles_dir = work_dir / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)
    rel_path = "articles/00-stub.md"
    (work_dir / rel_path).write_text(stub_text, encoding="utf-8")
    (work_dir / "headline_index.json").write_text(
        json.dumps({headline: rel_path}), encoding="utf-8"
    )
    return rel_path


def _write_exa_hit(
    work_dir,
    slug: str,
    sections: str = "## [Widget News](https://w.example)\nReal open-access body.\n\n",
) -> None:
    p = exa_file_path(work_dir, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# Exa Results for: X\nResult: hit\nQuery: widgets\n\n" + sections,
        encoding="utf-8",
    )


def _write_exa_empty(work_dir, slug: str) -> None:
    p = exa_file_path(work_dir, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# Exa Results for: X\nResult: empty\nQuery: widgets\n\n", encoding="utf-8"
    )


def test_exa_sections_appended_to_stub(tmp_path):
    headline = "Widget Maker Announces New Product"
    work_dir = tmp_path / "work"
    stub_text = "Widget Maker Announces New Product"
    _write_stub(work_dir, headline, stub_text)
    slug = _slugify(headline)
    _write_exa_hit(work_dir, slug)

    articles_by_theme, writer_inputs = _assemble_writer_inputs(
        _plan(headline), work_dir
    )

    assert len(writer_inputs) == 1
    entry = writer_inputs[0]
    text = articles_by_theme["Tech"][0]
    assert stub_text in text
    assert "Related coverage from other outlets" in text
    assert "Real open-access body." in text
    assert entry["exa_appended"] is True
    assert entry["exa_chars"] > 0
    assert entry["chars"] == len(text)


def test_no_append_when_source_is_already_exa(tmp_path):
    headline = "Only Findable Via Search Widget Story"
    work_dir = tmp_path / "work"
    slug = _slugify(headline)
    # No stub, no legacy article files at all -- only an Exa hit, so the
    # resolver itself returns the Exa file as the source.
    _write_exa_hit(work_dir, slug)

    articles_by_theme, writer_inputs = _assemble_writer_inputs(
        _plan(headline), work_dir
    )

    entry = writer_inputs[0]
    assert entry["source_path"] == f"enrichment/exa/{slug}.md"
    assert entry["exa_appended"] is False
    assert entry["exa_chars"] == 0
    text = articles_by_theme["Tech"][0]
    # The Exa text is present exactly once (from the resolver), not doubled.
    assert text.count("Real open-access body.") == 1


def test_no_append_when_no_exa_file(tmp_path):
    headline = "Stub With No Open Access Alternative"
    work_dir = tmp_path / "work"
    stub_text = "Stub With No Open Access Alternative"
    _write_stub(work_dir, headline, stub_text)

    articles_by_theme, writer_inputs = _assemble_writer_inputs(
        _plan(headline), work_dir
    )

    entry = writer_inputs[0]
    assert entry["exa_appended"] is False
    assert entry["exa_chars"] == 0
    text = articles_by_theme["Tech"][0]
    assert text == stub_text
    assert entry["chars"] == len(stub_text)


def test_exa_not_hit_contributes_nothing(tmp_path):
    headline = "Stub Whose Search Came Up Empty"
    work_dir = tmp_path / "work"
    stub_text = "Stub Whose Search Came Up Empty"
    _write_stub(work_dir, headline, stub_text)
    slug = _slugify(headline)
    _write_exa_empty(work_dir, slug)

    articles_by_theme, writer_inputs = _assemble_writer_inputs(
        _plan(headline), work_dir
    )

    entry = writer_inputs[0]
    assert entry["exa_appended"] is False
    assert entry["exa_chars"] == 0
    text = articles_by_theme["Tech"][0]
    assert text == stub_text
