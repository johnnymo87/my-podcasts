"""Rewrite podcast-transcript newsletter bodies into spoken briefings.

Several newsletters occasionally ship the verbatim transcript of a podcast
conversation as the email body. Reading that aloud is 60-80 minutes of
narration nobody asked for, so for a *confirmed* transcript we generate a
spoken briefing about the conversation instead and prefix the episode title
with ``Report: ``.

One registry, one gate, one writer. This module replaces what used to be four
per-feed modules (``chinatalk_writer``/``chinatalk_report`` and
``yglesias_writer``/``yglesias_report``) with a single
``feed_slug -> prompt template`` registry plus one detect-and-rewrite gate.
Feeds differ *only* by prompt template, and those templates are kept as
separate string constants -- ``_CHINATALK_TEMPLATE``, ``_YGLESIAS_TEMPLATE``,
and so on -- specifically so an edit aimed at tuning one feed's prompt cannot
accidentally reach another's.

Detection is deterministic and content-only
(``pipeline.transcript_detect.looks_like_transcript``) -- no LLM, no network,
so it cannot silently fail during a remote outage the way an earlier Gemini
classifier did on 2026-05-26 (it quietly returned NO for the whole outage
window and an 80-minute transcript shipped as a literal read).
"""

from __future__ import annotations

import logging

from pipeline.report_engine import ReportOutput, run_report_prompt
from pipeline.transcript_detect import looks_like_transcript


logger = logging.getLogger(__name__)

# A briefing generated here REPLACES the entire episode body, unlike the
# one-off report_writer path where a human reviews output before publishing.
# report_engine.run_report_prompt's min_chars floor exists precisely for a
# publish path like this one: the 2026-08-18 incident's placeholder script
# was 3 characters, not empty, so an emptiness-only check would have missed
# it here too.
#
# 2000, not 500. Every prompt below demands 800-1500 words -- roughly
# 4400-9000 chars -- so 2000 sits well below a legitimately terse briefing
# while still catching anything that could only be a refusal or a
# truncation. The original 500 was copied from rundown_writer.py's own
# _MIN_SCRIPT_CHARS without re-deriving it for this call site: that module's
# failure path is bounded (retry backoff -> errored -> alert), so a
# too-permissive floor there is eventually caught. A failure on this path
# means the email is redelivered UNBOUNDED -- there is no backoff, errored
# state, or alert for this queue -- so a floor that lets 500-4000 chars of
# refusal/truncation prose through would ship it as a full "Report:" episode
# replacing the entire post, and keep doing so on every redelivery.
_MIN_SCRIPT_CHARS = 2000

_CHINATALK_TEMPLATE = """\
You are writing a spoken briefing about a podcast conversation that ran
in the ChinaTalk newsletter today. Your listener does NOT want to hear
the transcript read aloud — they want a clear, structured report on
what was said.

Subject line: {subject}

Below is the full transcript. Read it, then produce a 5–10 minute
spoken briefing (roughly 800–1500 words) covering:

- Who participated (host and guests, with affiliations if stated).
- What topics were discussed, in the order that best illuminates the
  conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("Schneider argued...", "the guest pushed back,
  saying...").
- Any notable disagreements or points of tension.
- Concrete details, numbers, and examples that gave the conversation
  weight.

Write for the ear: plain spoken English, no markdown, no bullet
points, no headers. Use natural transitions. You are a smart friend
explaining what a podcast got into, not reading a summary out loud.
Do not editorialize beyond what the participants themselves said,
and do not invent facts.

TRANSCRIPT:

{body}
"""

_YGLESIAS_TEMPLATE = """\
You are writing a spoken briefing about an episode of The Argument, a
debate-and-conversation podcast hosted by Jerusalem Demsas. It ran in
the Slow Boring newsletter today. Your listener does NOT want to hear
the transcript read aloud — they want a clear, structured report on
what was said.

The post may open with a short editor's framing before the transcript
begins; use it for context but focus your report on the conversation
itself.

Subject line: {subject}

Below is the full transcript. Read it, then produce a 5–10 minute
spoken briefing (roughly 800–1500 words) covering:

- Who participated (host and guests, with affiliations if stated).
- What topics and questions were debated, in the order that best
  illuminates the conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("Demsas argued...", "Piper pushed back, saying...").
- Where they disagreed and where they found common ground.
- Concrete details, numbers, and examples that gave the conversation
  weight.

Write for the ear: plain spoken English, no markdown, no bullet
points, no headers. Use natural transitions. You are a smart friend
explaining what a debate got into, not reading a summary out loud.
Do not editorialize beyond what the participants themselves said,
and do not invent facts.

TRANSCRIPT:

{body}
"""

_SILVER_TEMPLATE = """\
You are writing a spoken briefing about a Silver Bulletin post by Nate
Silver's team that contains a recorded conversation. Your listener does
NOT want to hear the transcript read aloud — they want a clear,
structured report on what was said.

The post usually opens with several hundred words of the author's own
essay before the conversation begins. Cover that opening argument first,
in proportion to its length, then report the conversation itself.

Do not assume Nate Silver is one of the speakers; some conversations are
between other Silver Bulletin writers with no Nate Silver present. Take
the participants from the transcript, not from who usually writes the
newsletter.

Subject line: {subject}

Below is the full post. Read it, then produce a 5–10 minute spoken
briefing (roughly 800–1500 words) covering:

- The argument or setup in the opening essay, if there is one.
- Who participated in the conversation (with affiliations if stated).
- What topics were discussed, in the order that best illuminates the
  conversation (not necessarily the order they appeared).
- The key claims, arguments, and evidence each participant offered,
  with attribution ("the host argued...", "the guest pushed back,
  saying...").
- Any notable disagreements or points of tension.
- Concrete numbers, forecasts, and examples that gave the conversation
  weight. Silver Bulletin traffics in specific figures; keep them exact
  and do not round or invent them.

Write for the ear: plain spoken English, no markdown, no bullet
points, no headers. Use natural transitions. You are a smart friend
explaining what a conversation got into, not reading a summary out loud.
Do not editorialize beyond what the participants themselves said,
and do not invent facts.

POST:

{body}
"""

TRANSCRIPT_FEEDS: dict[str, str] = {
    "chinatalk": _CHINATALK_TEMPLATE,
    "yglesias": _YGLESIAS_TEMPLATE,
    "silver": _SILVER_TEMPLATE,
}


def build_report_prompt(*, body: str, subject: str, feed_slug: str) -> str:
    """Render the prompt for ``feed_slug``.

    Raises ``ValueError`` for a feed with no registered transcript prompt --
    a programming error at the call site (an unregistered feed should have
    been filtered out by ``maybe_rewrite_transcript`` before reaching here).
    """
    try:
        template = TRANSCRIPT_FEEDS[feed_slug]
    except KeyError:
        raise ValueError(f"No transcript prompt for feed: {feed_slug!r}") from None
    return template.format(subject=subject, body=body)


def generate_report(*, body: str, subject: str, feed_slug: str) -> ReportOutput:
    """Generate a spoken-briefing report on a transcript body."""
    prompt = build_report_prompt(body=body, subject=subject, feed_slug=feed_slug)
    instruction = (
        "Read the following transcript and produce the spoken briefing. "
        "First write a 2-3 sentence summary wrapped in <summary>...</summary> "
        "tags. Then write the full spoken script wrapped in "
        "<script>...</script> tags. Output nothing outside these tags.\n\n" + prompt
    )
    # require_tags=True: when the model emits no <script> tag at all, the
    # engine's fallback returns essentially the raw reply with the <summary>
    # block and stray tags stripped -- measured at 6522 chars in, 6521 out, so
    # cosmetic. On this path that means the model's own reasoning replaces the
    # post and is narrated to subscribers, and _MIN_SCRIPT_CHARS cannot catch
    # it because raw output is long. This module's contract is
    # re-raise-over-degrade (see maybe_rewrite_transcript), and a silent
    # raw-output fallback contradicts it. Refusing re-raises, the email goes
    # unacked, and redelivery regenerates from scratch.
    return run_report_prompt(
        instruction,
        label=feed_slug,
        min_chars=_MIN_SCRIPT_CHARS,
        require_tags=True,
    )


def maybe_rewrite_transcript(
    *,
    body: str,
    title: str,
    feed_slug: str,
    subject_raw: str,
) -> tuple[str, str]:
    """Rewrite a transcript body into a spoken briefing if applicable.

    Returns ``(body, title)`` unchanged when the feed has no registered
    transcript prompt or the body does not look like a transcript, so the
    caller falls back to a standard reading.

    For a *confirmed* transcript (a registered feed whose body passed
    ``looks_like_transcript``), a generation failure is logged and
    RE-RAISED -- never caught and downgraded to a literal read. This is
    load-bearing: re-raising bubbles out of
    ``pipeline.processor.process_email_bytes``, the consumer leaves the
    queue message unacked, and the email is reprocessed on redelivery.
    Silently falling back here would mean shipping the exact 60-80 minute
    narration this module exists to avoid, precisely when generation is
    broken and nobody would notice until a listener complained.
    """
    if feed_slug not in TRANSCRIPT_FEEDS:
        return body, title
    if not looks_like_transcript(body):
        return body, title
    try:
        report = generate_report(body=body, subject=subject_raw, feed_slug=feed_slug)
    except Exception:
        logger.exception(
            "%s report generation failed for a confirmed transcript; "
            "not shipping a literal read",
            feed_slug,
        )
        raise
    return report.script, f"Report: {title}"
