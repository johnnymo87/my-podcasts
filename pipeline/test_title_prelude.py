import pytest

from pipeline.title_prelude import prepend_title, spoken_title


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "2026-08-17 - Money Stuff - Bilateral OTC Goat Hedge",
            "Money Stuff: Bilateral OTC Goat Hedge",
        ),
        (
            "Report: 2026-08-19 - ChinaTalk - North Korean Messiah",
            "Report: ChinaTalk: North Korean Messiah",
        ),
        (
            "2026-08-11 - Slow Boring - Why does everyone hate data centers?",
            "Slow Boring: Why does everyone hate data centers?",
        ),
        ("Anthropic's LLM watermarking", "Anthropic's LLM watermarking"),
        ("2026-08-17 - The Rundown", "The Rundown"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_spoken_title(raw: str, expected: str) -> None:
    assert spoken_title(raw) == expected


# Real cleaned-body opening from an archived Levine email. The headline does
# not appear anywhere in the body, so dedupe must NOT fire here.
LEVINE_BODY = (
    "Money Stuff\n\n View in browser Subscribe to Bloomberg.com for "
    "unlimited access to all our coverage.\n\nProgramming note: this "
    "column will be off tomorrow.\n"
)


def test_prepends_when_body_does_not_state_title() -> None:
    result = prepend_title("2026-08-17 - Money Stuff - Goat Hedge", LEVINE_BODY)
    assert result.startswith("Money Stuff: Goat Hedge.\n\n")
    assert result.endswith(LEVINE_BODY)


def test_skips_when_body_already_opens_with_title() -> None:
    body = "Bilateral OTC Goat Hedge\n\nSome article text.\n"
    assert prepend_title("2026-08-17 - Bilateral OTC Goat Hedge", body) == body


def test_dedupe_ignores_case_and_punctuation() -> None:
    body = "money stuff -- goat hedge!\n\nSome article text.\n"
    assert prepend_title("2026-08-17 - Money Stuff - Goat Hedge", body) == body


def test_dedupe_requires_whole_token_match() -> None:
    """ "Better than gold" is not stated by "Better than golden retrievers"."""
    body = "Better than golden retrievers, honestly.\n\nText.\n"
    result = prepend_title("Better than gold", body)
    assert result.startswith("Better than gold.\n\n")


def test_dedupe_only_looks_at_the_opening() -> None:
    """A title mentioned deep in the body is not an opening statement."""
    body = "Unrelated lede.\n\n" + ("filler. " * 60) + "Goat Hedge\n"
    result = prepend_title("2026-08-17 - Goat Hedge", body)
    assert result.startswith("Goat Hedge.\n\n")


@pytest.mark.parametrize(
    "title",
    [
        "2026-08-11 - Slow Boring - Why does everyone hate data centers?",
        "2026-08-11 - Slow Boring - Woke 1 is dead. We've learned nothing.",
        "2026-08-11 - Slow Boring - Stop!",
    ],
)
def test_no_double_terminator(title: str) -> None:
    result = prepend_title(title, "Body text.\n")
    first_line = result.split("\n", 1)[0]
    assert first_line[-2:] not in {"?.", "..", "!."}
    assert first_line[-1] in ".?!"


def test_empty_title_returns_body_unchanged() -> None:
    assert prepend_title("", "Body text.\n") == "Body text.\n"


def test_punctuation_only_title_returns_body_unchanged() -> None:
    """Normalizes to empty, so the guard must not emit a bare '.' prelude."""
    assert prepend_title("---", "Body text.\n") == "Body text.\n"


def test_date_strip_only_eats_the_first_date() -> None:
    """_ISO_DATE_PREFIX strips only the first date-shaped substring (the one
    generated date prefix), so a second date-shaped substring that is real
    title content -- e.g. "1999-12-31 - Y2K Retrospective" -- survives."""
    raw = "2026-08-17 - 1999-12-31 - Y2K Retrospective"
    assert spoken_title(raw) == "1999-12-31: Y2K Retrospective"


def test_normalize_drops_accented_characters_rather_than_transliterating() -> None:
    """Unlike article_resolver.slugify (which keeps non-ASCII alphanumerics,
    see AGENTS.md), _normalize's [^a-z0-9]+ strips accents outright instead
    of folding them to ASCII. "Café" normalizes to "caf", not "cafe", so it
    will not dedupe against an ASCII-spelled "Cafe" in the body -- a safe
    degradation (prelude added anyway) but not a match."""
    result = prepend_title("Café Talk", "Cafe Talk is great.\n")
    assert result == "Café Talk.\n\nCafe Talk is great.\n"
