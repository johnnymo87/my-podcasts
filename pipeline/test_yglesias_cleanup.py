from __future__ import annotations

from pipeline import source_adapters as adapters


def test_clean_yglesias_body_prefers_plaintext_and_removes_substack_noise() -> None:
    raw = (
        b"Subject: Democrats need to think bigger on utilities\n"
        b"Content-Type: multipart/alternative; boundary=\"x\"\n\n"
        b"--x\n"
        b"Content-Type: text/plain; charset=utf-8\n\n"
        b"View this post on the web at https://www.slowboring.com/p/demo\n\n"
        b"Paragraph one [ https://substack.com/redirect/abc ] text.\n"
        b"READ IN APP\n"
        b"Unsubscribe https://substack.com/redirect/unsub\n"
        b"--x--\n"
    )
    cleaned = adapters._clean_yglesias_body(raw, "fallback")
    assert "View this post on the web" not in cleaned
    assert "substack.com/redirect" not in cleaned
    assert "READ IN APP" not in cleaned
    assert "Unsubscribe" not in cleaned
    assert cleaned == "Paragraph one text."
