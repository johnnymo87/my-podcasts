from __future__ import annotations

from pipeline.processor import _format_episode_title


def test_levine_money_stuff_title_is_normalized() -> None:
    title = _format_episode_title(
        date_str="2026-02-12",
        subject_raw="Money Stuff: Insider Trading on War",
        subject_slug="Money-Stuff-Insider-Trading-on-War",
        feed_slug="levine",
    )
    assert title == "2026-02-12 - Money Stuff - Insider Trading on War"


def test_non_levine_title_uses_raw_subject() -> None:
    title = _format_episode_title(
        date_str="2026-02-12",
        subject_raw="Slow Boring: Housing Policy",
        subject_slug="Slow-Boring-Housing-Policy",
        feed_slug="yglesias",
    )
    assert title == "Slow Boring: Housing Policy"
