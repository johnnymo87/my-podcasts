# Generic Source Adapters + arXiv Report Support

Date: 2026-06-16
Issue: my-podcasts-i83
Status: Approved (design)

## Context

One-off episodes are produced today by `pipeline/substack.py` (a single-source
ingester: `resolve_post(url)` → `SubstackPost`, plus `html_to_clean_text()` for
report-mode normalization) wired through the `substack` CLI command. The report
writer, `pipeline/substack_writer.generate_report(body, subject)`, has a prompt
tuned specifically for interview/transcript posts.

We want to turn additional source types into one-off episodes — starting with
arXiv HTML papers (immediate driver:
`https://arxiv.org/html/2407.16314v1`, *"Capital as Artificial Intelligence"*).
A paper is a different document shape (title, authors, abstract, sections, math,
figures, references) and a spoken *report* on a paper needs a different prompt
than an interview briefing.

## Goals

- A small, explicit **source-adapter interface**: each adapter knows how to
  recognize a URL and fetch+normalize it into a shared `Document`.
- An **arXiv adapter** that produces a paper-style report.
- A report writer that selects its **prompt by style** declared on the
  `Document` (`interview` vs `paper`), not by source identity.
- A single generic **`episode`** CLI command that dispatches by URL and
  **replaces** the `substack` command, carrying forward `--script-file` and
  `--dry-run`.

## Non-Goals

- No adapter auto-discovery/plugin framework, no config-driven source registry
  (YAGNI). The registry is a small in-code list.
- arXiv **read mode is not supported** (papers are report-only).
- No new feed infrastructure; the operator passes `--feed-slug` as today.

## Architecture

Three seams:

### 1. `pipeline/sources.py` — Document + dispatch

```python
@dataclass(frozen=True)
class Document:
    title: str
    byline: str            # authors / subtitle (may be "")
    canonical_url: str     # source <link> + feed source_url
    description: str       # for show notes (abstract / subtitle / description)
    report_text: str       # clean text fed to report-mode writer
    read_html: str | None  # HTML for read mode; None => read unsupported
    slug: str              # dry-run filename + episode slug hint
    style: str             # "interview" | "paper"
    wordcount: int
```

Adapter contract (structural — a small class or module with two callables):

```python
def matches(url: str) -> bool: ...
def resolve(url: str) -> Document: ...
```

Dispatcher:

```python
def resolve_document(url: str, *, source: str | None = None) -> Document:
    # source overrides matching; otherwise first adapter whose matches() is True.
    # Raises ValueError if no adapter matches.
```

`ADAPTERS` is an ordered in-code list (arxiv, substack). Substack is the
permissive fallback for `.../p/<slug>` and `.../p-<id>` URLs.

### 2. Adapters

**substack** — thin wrapper over the existing `substack.py` (no behavior
change). Maps `SubstackPost` → `Document`:
`style="interview"`, `report_text=html_to_clean_text(body_html)`,
`read_html=body_html`, `byline=subtitle`, `description=subtitle or description`.

**`pipeline/arxiv.py`** (new):
- **id/URL parsing** — accept `/html/<id>`, `/abs/<id>`, `/pdf/<id>`, or a bare
  id (`2407.16314`, optional `vN`). Keep the versioned id for the HTML fetch.
- **metadata** via the arXiv Atom API
  (`http://export.arxiv.org/api/query?id_list=<id>`): title, author names
  (joined into `byline`), `summary` → `description`, canonical `/abs/` URL,
  primary category. Robust to LaTeXML HTML changes.
- **body** via the HTML rendering (`https://arxiv.org/html/<versioned-id>`):
  - Keep the abstract (`div.ltx_abstract`).
  - Walk `section.ltx_section`: emit each section heading
    (`h2/h3.ltx_title`) followed by its paragraph text.
  - **Drop** the bibliography (`section.ltx_bibliography`, `ul.ltx_biblist`).
  - Drop figure/table bodies; **keep captions** (`figcaption`,
    `caption`/`ltx_caption`).
  - Strip `<math>` nodes (this paper's math is decorative; spoken reports
    summarize from prose). Keep short `alttext` only if trivially inline —
    default is to drop.
  - `style="paper"`, `read_html=None`.
  - If `/html/<id>` 404s (papers without an HTML rendering), raise a clear
    error pointing at the PDF-only situation.

### 3. `pipeline/report_writer.py` — style-keyed prompts

Generalize the writer: `generate_report(body, subject, *, style="interview")`
holds a small map of prompt templates:
- `interview` — the current substack/yglesias/chinatalk briefing prompt.
- `paper` — new: open with title + authors; cover the research question,
  core claims/contributions, method/model, key findings, significance, and
  notable caveats; spoken-briefing register; no slide-deck/bullet artifacts.

`pipeline/substack_writer.py` is migrated into / re-exported from
`report_writer.py` (substack command is going away). The 900s timeout,
empty-output rejection, and `ReportOutput` shape are preserved.

## CLI: `episode` (replaces `substack`)

```
uv run python -m pipeline episode \
  --url <url-or-id> \
  [--source {arxiv,substack}] \
  --mode {report,read} \
  --feed-slug <slug> \
  [--style {interview,paper}] \
  [--title ...] [--voice nova] [--category ...] [--date YYYY-MM-DD] \
  [--script-file PATH] [--dry-run]
```

Flow:
1. `doc = resolve_document(url, source=source)`.
2. `--mode read` and `doc.read_html is None` → `ClickException` (arXiv).
3. Script text:
   - `--script-file` → read verbatim (skips generation), as today.
   - report → `report_writer.generate_report(doc.report_text, doc.title,
     style=(style or doc.style))`.
   - read → `adapt_for_audio(doc.read_html, doc.title)`.
4. Title: report → `Report: <title>` (unless `--title`); read → `<title>`.
5. Show notes from `doc.description` + `doc.byline` + `[Original]({canonical_url})`.
6. `--dry-run` writes the script artifact and stops.
7. Otherwise `publish_script(..., source_url=doc.canonical_url or None)`.

The `substack` command is removed. `episode` auto-detects substack URLs, so the
only change for substack episodes is the command name.

## Error Handling

- Unknown URL with no matching adapter and no `--source` → `ValueError` →
  `ClickException`.
- arXiv `/html` 404 → explicit "no HTML rendering (PDF only)" error.
- arXiv API empty/missing entry → error.
- read mode on a report-only source → `ClickException` naming the source.
- Empty generated/normalized text → reuse the writer's existing empty-output
  rejection.

## Testing

- `pipeline/arxiv.py`: fixture-based (saved API XML + HTML sample). Assert
  Document fields (title, byline, canonical_url, `style="paper"`,
  `read_html is None`), that section headings appear in `report_text`, that the
  bibliography text is excluded, and id/URL parsing across `/html`, `/abs`,
  `/pdf`, bare id. `requests.get` mocked.
- `pipeline/sources.py`: dispatch routing (arxiv.org → arxiv; substack URL →
  substack; `--source` override; no-match raises).
- `pipeline/report_writer.py`: style selection (paper prompt contains
  paper-specific framing; interview prompt unchanged), empty-output rejection.
- `episode` CLI: mirror `test_substack_cli.py` — report title prefix +
  `source_url`, `--script-file` skips generation, `--dry-run` writes & doesn't
  publish, and arXiv `--mode read` errors. Migrate the existing substack CLI
  tests onto `episode`.

## Migration Notes (backward-incompatible)

- The `substack` CLI command is **removed**; use `episode` (URL auto-detected).
- `substack_writer` content moves to `report_writer`; update imports/tests.
- `pipeline/substack.py` (`resolve_post`, `html_to_clean_text`) stays — it's now
  the substack adapter's engine.

## Open Operational Items (not blocking design)

- Which feed slug should the arXiv "Capital as AI" episode publish to (new
  `arxiv`/`papers` feed vs an existing one)? Decide at publish time.
- Default `--category` for paper episodes (e.g. `Science`); keep a flag default
  and pass explicitly for now.
