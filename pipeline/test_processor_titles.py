from __future__ import annotations

from pipeline.source_adapters import LevineAdapter, SubstackAdapter


def test_levine_money_stuff_title_is_normalized() -> None:
    title = LevineAdapter().format_title(
        date_str="2026-02-12",
        subject_raw="Money Stuff: Insider Trading on War",
        subject_slug="Money-Stuff-Insider-Trading-on-War",
    )
    assert title == "2026-02-12 - Money Stuff - Insider Trading on War"


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
