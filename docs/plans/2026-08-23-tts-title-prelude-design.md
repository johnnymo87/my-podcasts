# Spoken Title Prelude for Newsletter Episodes

**Date:** 2026-08-23
**Status:** approved, not implemented

## Problem

No feed speaks its episode title. The TTS input is the article body (or the
generated script) and nothing else; `episode_title` is computed only to populate
the `Episode` DB row (`pipeline/processor.py:108`, used at `:155`).

The Matt Levine feed appeared to be an exception. It is not. Verified against
three real archived Levine emails pulled from R2: the cleaned body opens

```
Money Stuff

 View in browser Subscribe to Bloomberg.com for unlimited access ...
```

and the actual headline (e.g. `Bilateral OTC Goat Hedge`) **does not appear in
the body at all** — `body.find(headline) == -1` in 3 of 3 samples. What a
listener hears today is the brand word and Bloomberg's boilerplate, never the
episode's own title. The feature therefore adds something no feed currently has,
rather than propagating something one feed already had.

## Goal

Every newsletter-derived episode's audio opens by speaking its episode title,
matching the written feed title.

## Scope

**In:** the per-article feeds — email (levine, silver, chinatalk, yglesias,
general), the Aaronson blog poller, and one-off `episode` / `publish-script`
episodes (substack, arxiv, papers).

**Out:** The Rundown and FP Digest. Their titles are hardcoded boilerplate
(`f"{date_str} - The Rundown"`, `f"{date_str} - Foreign Policy Digest"`) and
their writer prompts already instruct a self-announcing opening
(`pipeline/rundown_writer.py:26`). Verified against 11 real FP scripts: they open
`"Good morning. It is Wednesday, August nineteenth... your daily foreign policy
briefing."` and the string "Foreign Policy Digest" never appears — so a
containment-based dedupe cannot suppress the redundancy there either. Prepending
would yield 10 doubled openings a week for no gain. Left alone deliberately.

**The exclusion needs a code guard, because the automated path is not the only
one.** `things_happen_processor` and `fp_processor` never call `publish_script`,
so wiring `publish_script` does not touch normal daily runs. But the documented
consumer-down recovery — `--dry-run`, then `publish-script`, then `jobs complete`
— publishes those exact scripts *through* `publish_script`. An operator passing
`--title "2026-08-21 - The Rundown"` would get precisely the doubled opening this
section excludes, on the path taken during an incident, when nobody is listening
for it. So `publish_script` skips the prelude when `feed_slug` is `the-rundown`
or `fp-digest`.

**Accepted redundancy: one-off report mode.** Report-mode scripts often name
their own subject early. A real archived papers script opens "Today I want to
walk you through... It's called 'Capital as Artificial Intelligence'..." — so the
audio becomes "Report: Capital as Artificial Intelligence. Today I want to walk
you through...". That second mention is conversational rather than a repeated
header, and these episodes are operator-reviewed before publishing. Accepted
knowingly, not overlooked.

## Design

New leaf module `pipeline/title_prelude.py`, importing only `re` (same
no-`pipeline`-imports discipline as `pipeline/article_resolver.py`, so every
caller can use it without an import cycle).

```
spoken_title(episode_title) -> str
  1. remove r'\d{4}-\d{2}-\d{2}\s*-\s*' anywhere in the string
     (so "Report: 2026-08-17 - ChinaTalk - Foo" works, not just a leading date)
  2. replace remaining ' - ' with ': '
  3. collapse whitespace, strip

prepend_title(episode_title, body) -> str
  spoken = spoken_title(episode_title)
  if not spoken: return body
  if _already_states(spoken, body): return body
  terminator = '' if spoken[-1] in '.?!' else '.'
  return f"{spoken}{terminator}\n\n{body}"

_already_states(spoken, body)
  norm = casefold, non-alphanumeric -> single space, strip
  compare TOKEN LISTS: norm(body[:300]).split()[:n] == norm(spoken).split()
```

Transformations, verified against all 790 real episode titles in the DB:

| Input | Output |
|---|---|
| `2026-08-17 - Money Stuff - Bilateral OTC Goat Hedge` | `Money Stuff: Bilateral OTC Goat Hedge` |
| `Report: 2026-08-19 - ChinaTalk - North Korean Messiah` | `Report: ChinaTalk: North Korean Messiah` |
| `2026-08-11 - Slow Boring - Why does everyone hate data centers?` | `Slow Boring: Why does everyone hate data centers?` |

The `Report: ` prefix is spoken verbatim — it distinguishes an AI briefing from a
literal read, and keeps the audio matching the written title.

**Punctuation-aware terminator, not an unconditional `.`.** Real titles end in
`?` and `.`; an unconditional append produces `?.` and `..`. nltk tolerates both
but TTS prosody on `?.` is a gamble.

**The terminator is required, not cosmetic.** `ttsjoin` tokenizes with
`nltk.sent_tokenize` and packs sentences into 4096-char chunks joined by a single
space; blank lines are not chunk boundaries and `\n\n` is inert. Without terminal
punctuation the title merges into the body's first sentence.

**Dedupe direction.** Prefix match on the opening, not containment anywhere in
the body. Containment was considered and rejected during review: it is what
would be needed to suppress the self-announcing daily digests, and it does not
even work for them (see Scope), while being loose enough to drop a legitimate
title.

**The prefix must be token-aligned.** A raw `startswith` on normalized text
matches a partial final token: the real Aaronson title `Better than gold` is
suppressed by a body opening "Better than golden retrievers...", dropping a
title the body never stated. Compare token lists instead.

Be honest about what dedupe is worth here: it currently has **no known
beneficiary**. It will not fire on Levine, because the headline is absent from
the body entirely — which is correct, the prelude is exactly what Levine is
missing. The `general` feed, the other candidate, has zero episodes ever. It is
kept as a cheap guard against a future body that opens by stating its own
headline, not because a measured case demands it.

**A fully non-ASCII title disables the prelude.** `_normalize` keeps only
`[a-z0-9]`, so a Chinese-only ChinaTalk title normalizes to empty and is skipped.
That is a safe degradation, but note it makes a *third* normalization family
alongside the two `slugify` families `AGENTS.md` documents — and unlike the
article family, this one deliberately drops non-ASCII alphanumerics.

## Call sites

Prelude enters the **TTS input only**. Archived and published script artifacts
(`script.txt`, show notes, R2 script objects) are untouched.

| File | Change |
|---|---|
| `pipeline/processor.py:133` | `body = prepend_title(episode_title, body)` before the write at `:134`. Sits after `maybe_rewrite_transcript` (`:123`), so it uses the final `Report: ` title. Covers levine, silver, chinatalk, yglesias, general. |
| `pipeline/script_processor.py:174` | apply to `tts_text` after `strip_markdown_for_tts` (`:173`), before the write at `:192`, **guarded by `feed_slug not in {"the-rundown", "fp-digest"}`**. Covers one-off `episode` and `publish-script`. |
| `pipeline/__main__.py:850` | same change in the `publish-script --dry-run` TTS reimplementation, so dry-run audio matches published audio. |
| `pipeline/blog_poller.py:150` | apply to `adapted_text` before the write at `:151`, passing **`post.title`**, not `episode_title`. |

The blog poller detail matters: `episode_title` there is
`f"{parsed_pub_date.strftime('%b %-d')} - {post.title}"` (`:200-201`), e.g.
`Aug 22 - Anthropic's LLM watermarking`. That date shape does not match the
ISO regex, so routing `episode_title` through `spoken_title` would speak
`"Aug 22: ..."` — violating the strip-the-date decision — and would also poison
dedupe, since a body can never start with `aug 22`. Using `post.title` needs no
hoist of the `parsed_pub_date` block.

## Idempotency

No path can double-prepend. Each builds its TTS input fresh in a tempdir from an
in-memory string (`processor.py:130-134`, `script_processor.py:190-192`,
`blog_poller.py:149-151`), and retries re-parse the raw email or reread
`script.txt` — neither of which ever carries a prelude, because of the
TTS-input-only rule above.

## Failure mode

An empty or unparseable title returns the body unchanged. The prelude never
blocks a publish. A title whose normalized form is empty (all punctuation) hits
`startswith("")`, which is True, so the prelude is skipped — accidentally
correct, and pinned by a test.

## Testing

Unit tests on `spoken_title`: ISO date strip, brand separator, `Report:` prefix,
`?`- and `.`-terminated titles, empty and punctuation-only input.

Unit tests on `prepend_title`: dedupe hit on a body that opens with its headline,
dedupe miss on a real Levine-shaped body (boilerplate opening, headline absent),
case and punctuation variance, no double terminator.

Per-call-site tests asserting the TTS input starts with the spoken title and the
archived script artifact does not. Fixtures use the real shapes found during
review: the Levine `Money Stuff\n\nView in browser...` opening, an `Aug 22 - `
blog title, a `Report: <date> - <brand> - <subject>` transcript title.

## Follow-up, not in scope

Levine's `View in browser / Subscribe to Bloomberg.com` boilerplate is currently
audible and the prelude makes it more prominent. Stripping it belongs in
`LevineAdapter.clean_body` (`pipeline/source_adapters.py`) as separate work.
ChinaTalk bodies have a milder version of the same shape (they open with contest
promo text); expect it in listening tests and do not misread it as a dedupe bug.
