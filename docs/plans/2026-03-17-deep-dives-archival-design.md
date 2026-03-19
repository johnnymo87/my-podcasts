# Deep-Dives Script Archival and Dog Cancer Episode Design

## Problem

The `publish-script` flow successfully publishes one-off episodes to feeds like `deep-dives`, but it does not persist the source script and optional show notes under `/persist/my-podcasts/scripts/`. That makes it harder to audit, revise, or reuse past one-off episodes. We also want to publish a new `deep-dives` episode on the story about a tech entrepreneur using ChatGPT and collaborators to create a bespoke cancer vaccine for his dog.

## Recommended Approach

Keep the existing `publish-script` CLI as the one-off publishing path, and add automatic archival of the source files as part of a successful publish. This keeps the workflow simple for future one-off episodes, matches the existing pattern used by daily scripted feeds, and avoids introducing a second manual archival step that can be forgotten.

## Alternatives Considered

### 1. Automatic file archival during `publish-script` (recommended)

- Archive the input script markdown to `/persist/my-podcasts/scripts/<feed-slug>/<date>-<slug>.md`
- Archive optional show notes to `/persist/my-podcasts/scripts/<feed-slug>/<date>-<slug>-show-notes.md`
- Only write archives after a successful publish so archived artifacts correspond to real episodes

Pros: automatic, easy to inspect, consistent with existing persisted script behavior.

Cons: adds a little more logic to the publish path.

### 2. Separate archival command

- Keep `publish-script` unchanged
- Add a second CLI command to copy source files into persistent storage

Pros: smaller change to the publish path.

Cons: operationally fragile because it depends on someone remembering a second command.

### 3. Store source markdown in SQLite

- Add columns for raw script and raw show notes in `episodes`

Pros: all metadata in one place.

Cons: unnecessary schema growth, worse ergonomics for long markdown documents, and less aligned with how persisted scripts are already handled elsewhere.

## Archival Design

Add a small helper in `pipeline/script_processor.py` that computes a persistent script directory from the feed slug and episode slug, creates the directory if needed, and copies the source files there.

Archive naming:

- Script: `/persist/my-podcasts/scripts/<feed-slug>/<date>-<title-slug>.md`
- Show notes: `/persist/my-podcasts/scripts/<feed-slug>/<date>-<title-slug>-show-notes.md`

Behavior:

- Read the original script file as today for TTS preprocessing
- Publish audio to R2 and insert the `Episode` row as today
- After successful publish steps, persist the original markdown artifacts
- If no show notes file is provided, only archive the script

## Error Handling

- Publishing failures should behave exactly as they do now: no archive written if TTS/upload/DB insertion fails
- If archival itself fails after publish succeeds, surface the exception so the operator knows the episode published but the source artifact needs attention
- Keep the implementation minimal; do not add retries or backup locations yet

## Testing Strategy

- Extend `pipeline/test_script_processor.py` with a focused test that publishes a script episode and verifies the expected archived files exist with the original markdown contents
- Add a second test for the no-show-notes case to ensure only the script is archived
- Preserve existing publish and feed tests as regression coverage

## Dog Cancer Episode Plan

Publish the new episode to the existing `deep-dives` feed with category `Technology`.

Working framing:

- Topic: whether this dog cancer vaccine story is a real biomedical breakthrough, a promising anecdote, or mostly a story about AI-assisted navigation of existing scientific infrastructure
- Tone: skeptical but curious, emphasizing what actually happened, what AI contributed, what humans and institutions contributed, and what would have to be true for this to generalize
- Inputs: the enriched Semafor/Fortune context already present in the cache plus any supporting citations needed for show notes

## Files Involved

| File | Purpose |
|------|---------|
| `pipeline/script_processor.py` | Add archive helper and integrate it into successful publish flow |
| `pipeline/test_script_processor.py` | Add archival tests |
| `pipeline/__main__.py` | Likely unchanged unless we decide to expose archive path configuration later |
| `/persist/my-podcasts/scripts/deep-dives/` | New storage location for future one-off script artifacts |

## Non-Goals

- No migration of already-published one-off episodes
- No new feed or new episode type
- No database schema changes for raw source markdown
- No changes to daily digest pipelines
