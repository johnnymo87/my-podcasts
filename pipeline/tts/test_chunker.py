from __future__ import annotations

import pytest

from pipeline.tts.chunker import OPENAI_MAX_CHARS, chunk_text


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


def test_real_shaped_text_respects_default_target_and_openai_ceiling() -> None:
    paras = [f"Sentence number {i} is here. " * 60 for i in range(12)]
    text = "\n\n".join(p.strip() for p in paras)
    chunks = chunk_text(text)
    assert all(len(c) <= 3000 for c in chunks)
    assert all(len(c) <= OPENAI_MAX_CHARS for c in chunks)
    assert _tokens(chunks) == text.split()


@pytest.mark.parametrize("target", [0, -1, -300])
def test_non_positive_target_is_rejected(target: int) -> None:
    with pytest.raises(ValueError):
        chunk_text("some text to chunk", target=target, ceiling=4096)


def test_hard_cut_blob_between_words_stays_whole_tokens() -> None:
    blob = "x" * 1000
    text = f"a {blob} b"
    chunks = chunk_text(text, target=300, ceiling=4096)
    assert all(len(c) <= 300 for c in chunks)
    tokens = _tokens(chunks)
    assert tokens[0] == "a"
    assert tokens[-1] == "b"
    x_pieces = tokens[1:-1]
    assert all(set(piece) == {"x"} for piece in x_pieces)
    assert "".join(x_pieces) == blob


def test_crlf_paragraph_breaks_normalize_to_double_newline() -> None:
    assert chunk_text("a\r\n\r\nb\r\n\r\nc") == ["a\n\nb\n\nc"]


def test_empty_string_is_empty() -> None:
    assert chunk_text("") == []


def test_chunk_exactly_target_long_is_allowed() -> None:
    single = "x" * 300
    assert chunk_text(single, target=300, ceiling=4096) == [single]
    joined = "a" * 149 + "\n\n" + "b" * 149  # exactly 300 with separator
    assert len(joined) == 300
    assert chunk_text(joined, target=300, ceiling=4096) == [joined]


def test_abbreviations_and_numbers_stay_intact() -> None:
    text = "Mr. Smith paid 5.5% of $1.2 million. " * 40
    chunks = chunk_text(text, target=200, ceiling=4096)
    assert all(len(c) <= 200 for c in chunks)
    tokens = _tokens(chunks)
    assert tokens.count("5.5%") == 40
    assert tokens.count("$1.2") == 40
    assert tokens == text.split()
