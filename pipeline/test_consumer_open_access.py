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


def _plan_multi(
    themes: list[str], directives: list[tuple[str, str]]
) -> RundownResearchPlan:
    """Build a plan with several (headline, theme) directives."""
    return RundownResearchPlan(
        themes=themes,
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
            for headline, theme in directives
        ],
    )


def _write_stub(work_dir, headline: str, stub_text: str) -> str:
    """Write a stub article + headline_index.json so it resolves exactly.

    Returns the work-dir-relative path recorded in the index.
    """
    articles_dir = work_dir / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"articles/{_slugify(headline)}.md"
    (work_dir / rel_path).write_text(stub_text, encoding="utf-8")
    index_path = work_dir / "headline_index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    index[headline] = rel_path
    index_path.write_text(json.dumps(index), encoding="utf-8")
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


def _section_texts(sections, theme: str) -> list[str]:
    """Look up a theme's article texts in the ordered sections list."""
    for section_theme, texts in sections:
        if section_theme == theme:
            return texts
    raise AssertionError(f"no section named {theme!r} in {sections!r}")


def test_exa_sections_appended_to_stub(tmp_path):
    headline = "Widget Maker Announces New Product"
    work_dir = tmp_path / "work"
    stub_text = "Widget Maker Announces New Product"
    _write_stub(work_dir, headline, stub_text)
    slug = _slugify(headline)
    _write_exa_hit(work_dir, slug)

    sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    assert len(writer_inputs) == 1
    entry = writer_inputs[0]
    text = _section_texts(sections, "Tech")[0]
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

    sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    entry = writer_inputs[0]
    assert entry["source_path"] == f"enrichment/exa/{slug}.md"
    assert entry["exa_appended"] is False
    assert entry["exa_chars"] == 0
    text = _section_texts(sections, "Tech")[0]
    # The Exa text is present exactly once (from the resolver), not doubled.
    assert text.count("Real open-access body.") == 1


def test_no_append_when_no_exa_file(tmp_path):
    headline = "Stub With No Open Access Alternative"
    work_dir = tmp_path / "work"
    stub_text = "Stub With No Open Access Alternative"
    _write_stub(work_dir, headline, stub_text)

    sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    entry = writer_inputs[0]
    assert entry["exa_appended"] is False
    assert entry["exa_chars"] == 0
    text = _section_texts(sections, "Tech")[0]
    assert text == stub_text
    assert entry["chars"] == len(stub_text)


def test_exa_not_hit_contributes_nothing(tmp_path):
    headline = "Stub Whose Search Came Up Empty"
    work_dir = tmp_path / "work"
    stub_text = "Stub Whose Search Came Up Empty"
    _write_stub(work_dir, headline, stub_text)
    slug = _slugify(headline)
    _write_exa_empty(work_dir, slug)

    sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    entry = writer_inputs[0]
    assert entry["exa_appended"] is False
    assert entry["exa_chars"] == 0
    text = _section_texts(sections, "Tech")[0]
    assert text == stub_text


def test_writer_inputs_marks_resolved_directive_as_reached_prompt(tmp_path):
    headline = "Widget Maker Announces New Product"
    work_dir = tmp_path / "work"
    _write_stub(work_dir, headline, "Widget Maker Announces New Product")

    _sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    assert writer_inputs[0]["reached_prompt"] is True


def test_writer_inputs_marks_unresolved_directive_as_not_reached_prompt(tmp_path):
    """source_path is None -> reached_prompt is False."""
    headline = "Nothing Resolves For This Headline"
    work_dir = tmp_path / "work"

    _sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    entry = writer_inputs[0]
    assert entry["source_path"] is None
    assert entry["reached_prompt"] is False


def test_orphan_directive_is_reached_prompt(tmp_path):
    """Regression guard for a3x: the orphan genuinely reaches the model now."""
    work_dir = tmp_path / "work"
    headline_orphan = "Orphan Story About Gizmos"
    _write_stub(work_dir, headline_orphan, "Orphan article text")
    plan = _plan_multi(
        themes=["Alpha"],
        directives=[(headline_orphan, "Invented Name")],
    )

    _sections, writer_inputs = _assemble_writer_inputs(plan, work_dir)

    assert writer_inputs[0]["reached_prompt"] is True


def test_writer_inputs_records_miss_reason_for_unresolved_directive(tmp_path):
    headline = "Nothing Resolves For This Headline"
    work_dir = tmp_path / "work"

    _sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    entry = writer_inputs[0]
    assert entry["source_path"] is None
    assert entry["miss_reason"] == "no_index"


def test_writer_inputs_records_no_miss_reason_on_hit(tmp_path):
    headline = "Widget Maker Announces New Product"
    work_dir = tmp_path / "work"
    _write_stub(work_dir, headline, "Widget Maker Announces New Product")

    _sections, writer_inputs = _assemble_writer_inputs(_plan(headline), work_dir)

    assert writer_inputs[0]["miss_reason"] is None


def test_assembly_keeps_plan_theme_order(tmp_path):
    """Sections follow plan.themes order, not directive arrival order."""
    work_dir = tmp_path / "work"
    headline_a = "Alpha Story About Widgets"
    headline_b = "Beta Story About Gadgets"
    _write_stub(work_dir, headline_a, "Alpha article text")
    _write_stub(work_dir, headline_b, "Beta article text")
    # Directives arrive Beta-first; plan.themes lists Alpha first.
    plan = _plan_multi(
        themes=["Alpha", "Beta"],
        directives=[(headline_b, "Beta"), (headline_a, "Alpha")],
    )

    sections, _writer_inputs = _assemble_writer_inputs(plan, work_dir)

    assert [theme for theme, _texts in sections] == ["Alpha", "Beta"]


def test_assembly_omits_themes_with_no_articles(tmp_path):
    """A plan theme with no resolving directives is not a section."""
    work_dir = tmp_path / "work"
    headline_a = "Alpha Story About Widgets"
    _write_stub(work_dir, headline_a, "Alpha article text")
    plan = _plan_multi(
        themes=["Alpha", "Empty"],
        directives=[(headline_a, "Alpha")],
    )

    sections, _writer_inputs = _assemble_writer_inputs(plan, work_dir)

    assert [theme for theme, _texts in sections] == ["Alpha"]


def test_assembly_appends_orphan_theme_as_its_own_section(tmp_path):
    """a3x: a directive theme absent from plan.themes must still reach the model."""
    work_dir = tmp_path / "work"
    headline_a = "Alpha Story About Widgets"
    headline_orphan = "Orphan Story About Gizmos"
    _write_stub(work_dir, headline_a, "Alpha article text")
    _write_stub(work_dir, headline_orphan, "Orphan article text")
    plan = _plan_multi(
        themes=["Alpha"],
        directives=[(headline_a, "Alpha"), (headline_orphan, "Invented Name")],
    )

    sections, _writer_inputs = _assemble_writer_inputs(plan, work_dir)

    assert [theme for theme, _texts in sections] == ["Alpha", "Invented Name"]
    assert "Orphan article text" in _section_texts(sections, "Invented Name")[0]


def test_assembly_does_not_reassign_orphan_to_a_similar_plan_theme(tmp_path):
    """Guards the design decision: no fuzzy matching, ever."""
    work_dir = tmp_path / "work"
    headline_orphan = "Regulators Weigh In On AI Safety Rules"
    _write_stub(work_dir, headline_orphan, "AI safety article text")
    plan = _plan_multi(
        themes=["AI & Machine Learning"],
        directives=[(headline_orphan, "AI Safety & Regulation")],
    )

    sections, _writer_inputs = _assemble_writer_inputs(plan, work_dir)

    # The plan theme has zero resolved articles, so it is omitted -- the
    # orphan text must NOT have been folded into it.
    section_names = [theme for theme, _texts in sections]
    assert section_names == ["AI Safety & Regulation"]
    assert "AI & Machine Learning" not in section_names
    assert (
        "AI safety article text"
        in _section_texts(sections, "AI Safety & Regulation")[0]
    )


def test_assembly_deduplicates_a_repeated_plan_theme(tmp_path):
    """plan.themes comes from an LLM and has no uniqueness constraint.

    A repeated theme name would otherwise emit the section -- and every
    article in it -- twice in the writer prompt.
    """
    work_dir = tmp_path / "work"
    headline = "Alpha Story About Widgets"
    _write_stub(work_dir, headline, "Alpha article text")
    plan = _plan_multi(
        themes=["Alpha", "Alpha"],
        directives=[(headline, "Alpha")],
    )

    sections, _writer_inputs = _assemble_writer_inputs(plan, work_dir)

    assert [theme for theme, _texts in sections] == ["Alpha"]
    assert sum(len(texts) for _theme, texts in sections) == 1
