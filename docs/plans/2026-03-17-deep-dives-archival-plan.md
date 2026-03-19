# Deep-Dives Script Archival and Dog Cancer Episode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist source script/show-notes artifacts for future `publish-script` episodes and publish a new `deep-dives` episode about the dog cancer vaccine story.

**Architecture:** Extend `pipeline/script_processor.py` so successful one-off publishes archive their original markdown inputs to `/persist/my-podcasts/scripts/<feed-slug>/`. Keep the existing `publish-script` CLI and `deep-dives` feed, then use that flow to publish a new episode with script and show notes derived from the enriched dog-cancer story context.

**Tech Stack:** Python, Click, pytest, tempfile/pathlib/shutil, SQLite, Cloudflare R2, `ttsjoin`, existing `publish-script` pipeline.

---

### Task 1: Add archival coverage tests for `publish_script`

**Files:**
- Modify: `pipeline/test_script_processor.py`

**Step 1: Write the failing test for script + show-notes archival**

Append a test that:

- creates temp `script.md` and `notes.md`
- monkeypatches the archive base path used by `pipeline.script_processor`
- runs `publish_script(...)`
- asserts archived files exist at:
  - `<archive-base>/deep-dives/2026-03-17-dog-story.md`
  - `<archive-base>/deep-dives/2026-03-17-dog-story-show-notes.md`
- asserts archived contents exactly match the original markdown files

Use this shape:

```python
def test_publish_script_archives_source_files(tmp_path, monkeypatch) -> None:
    store = StateStore(tmp_path / "test.sqlite3")
    r2_client = MagicMock()
    archive_root = tmp_path / "persisted-scripts"

    script_file = tmp_path / "script.md"
    script_file.write_text("# Dog Story\n\nBody text.", encoding="utf-8")
    show_notes_file = tmp_path / "notes.md"
    show_notes_file.write_text("## Episode Summary\n\nSummary.\n", encoding="utf-8")

    def fake_subprocess_run(cmd, **kwargs):
        ...

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr("pipeline.script_processor.regenerate_and_upload_feed", lambda s, r: None)
    monkeypatch.setattr("pipeline.script_processor.SCRIPT_ARCHIVE_ROOT", archive_root)

    publish_script(
        script_file=script_file,
        title="Dog Story",
        feed_slug="deep-dives",
        store=store,
        r2_client=r2_client,
        show_notes_file=show_notes_file,
        date_str="2026-03-17",
    )

    assert (archive_root / "deep-dives" / "2026-03-17-dog-story.md").read_text(encoding="utf-8") == "# Dog Story\n\nBody text."
    assert (archive_root / "deep-dives" / "2026-03-17-dog-story-show-notes.md").read_text(encoding="utf-8") == "## Episode Summary\n\nSummary.\n"
```

**Step 2: Run the new test to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py::test_publish_script_archives_source_files -v`
Expected: FAIL because `pipeline.script_processor` does not yet define archive behavior.

**Step 3: Write the failing test for no-show-notes archival**

Append a second test that publishes without `show_notes_file` and asserts:

- the script archive exists
- the `-show-notes.md` file does not exist

**Step 4: Run the second test to verify it fails**

Run: `uv run pytest pipeline/test_script_processor.py::test_publish_script_archives_script_without_show_notes -v`
Expected: FAIL because no archive exists yet.

---

### Task 2: Implement source artifact archival in `script_processor`

**Files:**
- Modify: `pipeline/script_processor.py`

**Step 1: Add a configurable archive root constant**

Near the existing defaults, add:

```python
SCRIPT_ARCHIVE_ROOT = Path("/persist/my-podcasts/scripts")
```

**Step 2: Add a helper to archive publish inputs**

Add a helper like:

```python
def _archive_publish_inputs(
    *,
    script_file: Path,
    show_notes_file: Path | None,
    feed_slug: str,
    episode_slug: str,
) -> None:
    archive_dir = SCRIPT_ARCHIVE_ROOT / feed_slug
    archive_dir.mkdir(parents=True, exist_ok=True)

    archive_script = archive_dir / f"{episode_slug}.md"
    archive_script.write_text(script_file.read_text(encoding="utf-8"), encoding="utf-8")

    if show_notes_file is not None:
        archive_notes = archive_dir / f"{episode_slug}-show-notes.md"
        archive_notes.write_text(
            show_notes_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
```

Keep it simple; no extra abstraction beyond this helper.

**Step 3: Call the helper only after successful publish steps**

In `publish_script(...)`, after:

- TTS succeeds
- upload succeeds
- episode row is inserted
- feed regeneration succeeds

call `_archive_publish_inputs(...)`.

This preserves the rule that only successfully published episodes get persisted source artifacts.

**Step 4: Run the two new tests**

Run: `uv run pytest pipeline/test_script_processor.py::test_publish_script_archives_source_files pipeline/test_script_processor.py::test_publish_script_archives_script_without_show_notes -v`
Expected: PASS

**Step 5: Run existing script processor tests for regression coverage**

Run: `uv run pytest pipeline/test_script_processor.py -v`
Expected: PASS

---

### Task 3: Verify archival behavior against real path conventions

**Files:**
- Modify: none

**Step 1: Check the existing persisted scripts directory layout**

Run: `ls /persist/my-podcasts/scripts`

Expected: existing per-feed directories such as `the-rundown`, `fp-digest`, or `things-happen`.

**Step 2: Dry-run the one-off publish path locally if needed**

Prepare a tiny throwaway script file and run:

`uv run python -m pipeline publish-script --script-file /tmp/test-script.md --title "Archive Smoke Test" --feed-slug deep-dives --dry-run`

Expected: MP3 generation succeeds locally and no DB/R2 changes are made.

**Step 3: Note the limitation of dry-run**

Document for yourself that dry-run does not exercise archival because archival only happens on successful publish.

---

### Task 4: Draft the dog cancer deep-dives episode assets

**Files:**
- Create: `/tmp/dog-cancer-deep-dive-script.md`
- Create: `/tmp/dog-cancer-deep-dive-show-notes.md`
- Reference: `/persist/my-podcasts/semafor-cache/2026-03-16-australian-tech-entrepreneur-ai-cancer-vaccine-dog.md`

**Step 1: Re-read the enriched story context**

Pull facts from the Semafor/Fortune cache and keep the framing precise:

- Rosie had persistent tumors after surgery and chemotherapy
- Conyngham used ChatGPT for planning and AlphaFold for target work
- UNSW and Pall Thordarson's team developed the bespoke mRNA vaccine
- some tumors shrank, some did not
- this is a single anecdotal case, not clinical proof

**Step 2: Write the episode script**

Write a script with a skeptical deep-dive structure:

- hook: why the story spread
- what actually happened
- what AI did versus what domain experts/institutions did
- what counts as evidence here
- what would need to happen for this to matter broadly in medicine

Keep the tone analytical, not boosterish.

**Step 3: Write markdown show notes**

Include:

- `## Episode Summary`
- key claims and caveats
- primary links/citations
- broader context on personalized cancer vaccines and AlphaFold/AI drug design

**Step 4: Sanity-check the text**

Read both files once for factual overreach, ambiguity, and TTS-unfriendly formatting.

---

### Task 5: Publish the new `deep-dives` episode

**Files:**
- Create in persist on success: `/persist/my-podcasts/scripts/deep-dives/2026-03-17-<slug>.md`
- Create in persist on success: `/persist/my-podcasts/scripts/deep-dives/2026-03-17-<slug>-show-notes.md`

**Step 1: Run the publish command**

Run:

```bash
uv run python -m pipeline publish-script \
  --script-file /tmp/dog-cancer-deep-dive-script.md \
  --title "Did AI Really Help Create a Cancer Vaccine for a Dog?" \
  --feed-slug deep-dives \
  --show-notes /tmp/dog-cancer-deep-dive-show-notes.md \
  --category Technology \
  --date 2026-03-17
```

Expected: TTS succeeds, episode row is inserted, feed is regenerated, and archive files are created.

**Step 2: Verify the DB row**

Run:

```bash
uv run python -c "import sqlite3; conn=sqlite3.connect('/persist/my-podcasts/state.sqlite3'); conn.row_factory=sqlite3.Row; rows=conn.execute(\"SELECT title, feed_slug, pub_date, r2_key FROM episodes WHERE feed_slug='deep-dives' ORDER BY created_at DESC LIMIT 3\").fetchall(); [print(dict(r)) for r in rows]"
```

Expected: the new dog-cancer episode appears first.

**Step 3: Verify archived source artifacts**

Run:

`ls /persist/my-podcasts/scripts/deep-dives`

Expected: the new script and show-notes files are present.

**Step 4: Verify the live feed**

Run:

`curl -I https://podcast.mohrbacher.dev/feeds/deep-dives.xml`

Expected: `200`

**Step 5: Verify the new item appears in the feed**

Fetch `https://podcast.mohrbacher.dev/feeds/deep-dives.xml` and confirm the episode title and `content:encoded` show notes are present.

---

### Task 6: Final verification

**Files:**
- Modify: none

**Step 1: Run targeted tests again**

Run: `uv run pytest pipeline/test_script_processor.py -v`
Expected: PASS

**Step 2: Check git status**

Run: `git status --short`

Expected: only the intended code/docs changes plus any intentional local script assets if still present.

**Step 3: Update the bead with completion notes**

Run:

`bd update my-podcasts-323 --notes "DONE: automatic source archival for publish-script and published dog cancer deep-dives episode. VERIFIED: targeted pytest, DB row, persisted script/show-notes, live deep-dives feed." --json`

Expected: bead notes updated for handoff.
