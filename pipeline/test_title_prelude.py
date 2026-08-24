import pytest

from pipeline.title_prelude import spoken_title


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
