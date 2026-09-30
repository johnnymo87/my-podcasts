from __future__ import annotations

import pytest

from pipeline.tts.chunker import chunk_text


def _tokens(chunks: list[str]) -> list[str]:
    return " ".join(chunks).split()


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("Hello there.\n\nSecond paragraph.") == [
        "Hello there.\n\nSecond paragraph."
    ]


def test_whitespace_only_is_empty() -> None:
    assert chunk_text("  \n\n \n") == []


def test_packs_paragraphs_under_target_and_breaks_between_them() -> None:
    para = "word " * 39 + "end."  # 199 chars
    text = "\n\n".join([para] * 10)
    chunks = chunk_text(text, target=500, ceiling=4096)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) == 5  # two 199-char paragraphs (+2 sep) per chunk
    assert all(c.count("\n\n") == 1 for c in chunks)
    assert _tokens(chunks) == text.split()


def test_oversize_paragraph_splits_on_sentences() -> None:
    sentence = "This sentence is about forty characters. "
    para = (sentence * 30).strip()  # ~1230 chars, one paragraph
    chunks = chunk_text(para, target=300, ceiling=4096)
    assert all(len(c) <= 300 for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    assert _tokens(chunks) == para.split()


def test_sentence_split_keeps_closing_quote_with_sentence() -> None:
    para = ('He said "stop." ' * 40).strip()
    chunks = chunk_text(para, target=100, ceiling=4096)
    assert all(c.endswith('stop."') for c in chunks)


def test_oversize_sentence_splits_on_whitespace() -> None:
    sentence = " ".join(["word"] * 400) + "."  # ~2000 chars, no sentence break
    chunks = chunk_text(sentence, target=300, ceiling=4096)
    assert all(len(c) <= 300 for c in chunks)
    assert _tokens(chunks) == sentence.split()


def test_unbroken_string_is_hard_cut() -> None:
    blob = "x" * 1000
    chunks = chunk_text(blob, target=300, ceiling=4096)
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks) == blob


def test_target_above_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_text("hi", target=5000, ceiling=4096)


def test_real_shaped_text_respects_openai_ceiling() -> None:
    paras = [f"Sentence number {i} is here. " * 60 for i in range(12)]
    text = "\n\n".join(p.strip() for p in paras)
    chunks = chunk_text(text)
    assert all(len(c) <= 3000 for c in chunks)
    assert _tokens(chunks) == text.split()
