from __future__ import annotations

from pipeline.source_adapters import LevineAdapter, SubstackAdapter


def test_levine_money_stuff_title_is_normalized() -> None:
    title = LevineAdapter().format_title(
        date_str="2026-02-12",
        subject_raw="Money Stuff: Insider Trading on War",
        subject_slug="Money-Stuff-Insider-Trading-on-War",
    )
    assert title == "2026-02-12 - Money Stuff - Insider Trading on War"


def _levine_clean(body: str) -> str:
    return LevineAdapter().clean_body(raw_email=b"", body=body)


def test_levine_body_drops_read_in_browser_link() -> None:
    # The shape of a cleaned Money Stuff body since 2026-10-26.
    body = "Anthropic Pops\n\nRead in browser\n\nAnthropic\n\nThere are two ways."
    assert _levine_clean(body) == "Anthropic Pops\n\nAnthropic\n\nThere are two ways."


def test_levine_body_drops_view_in_browser_link() -> None:
    # The shape before 2026-10-26 (leading space from the HTML).
    body = "Money Stuff\n\n View in browser\n\nClass C\n\nThe basic deal."
    assert _levine_clean(body) == "Money Stuff\n\nClass C\n\nThe basic deal."


def test_levine_body_keeps_the_phrase_inside_prose() -> None:
    body = "Title\n\nYou can read in browser tabs all day, he said."
    assert _levine_clean(body) == body


def test_levine_body_drops_zero_width_characters() -> None:
    body = "Title\n\nDek. \u200c \u200c \u200b\u2060\ufeff\n\nBody text."
    assert _levine_clean(body) == "Title\n\nDek.\n\nBody text."


def test_levine_body_unchanged_without_boilerplate() -> None:
    body = "Programming note: normal service resumes Monday.\n\nGoats."
    assert _levine_clean(body) == body


def test_yglesias_title_uses_slow_boring_format() -> None:
    title = SubstackAdapter(
        brand_name="Slow Boring", domain="slowboring.com"
    ).format_title(
        date_str="2026-02-12",
        subject_raw="Slow Boring: Housing Policy",
        subject_slug="Slow-Boring-Housing-Policy",
    )
    assert title == "2026-02-12 - Slow Boring - Housing Policy"


def test_silver_bulletin_title_uses_brand_format() -> None:
    adapter = SubstackAdapter(brand_name="Silver Bulletin", domain="natesilver.net")
    title = adapter.format_title(
        date_str="2026-02-28",
        subject_raw="Silver Bulletin: The Forecast Was Wrong",
        subject_slug="Silver-Bulletin-The-Forecast-Was-Wrong",
    )
    assert title == "2026-02-28 - Silver Bulletin - The Forecast Was Wrong"
