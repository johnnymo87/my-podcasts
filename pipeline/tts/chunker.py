"""Lossless, paragraph-aware text partition for TTS.

Replaces tts-joinery's nltk sentence packing. No nltk: its tokenizer data is
fetched from the network at runtime (bead my-podcasts-4ld).
"""

from __future__ import annotations

import re


DEFAULT_TARGET_CHARS = 3000
OPENAI_MAX_CHARS = 4096

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
# Fixed-width lookbehinds only (re has no variable-width lookbehind).
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'’”)\]])\s+")


def chunk_text(
    text: str, *, target: int = DEFAULT_TARGET_CHARS, ceiling: int = OPENAI_MAX_CHARS
) -> list[str]:
    if target > ceiling:
        raise ValueError(f"target {target} exceeds provider ceiling {ceiling}")
    units: list[tuple[str, str]] = []  # (separator before unit, unit text)
    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for i, piece in enumerate(_fit(paragraph, target)):
            units.append(("\n\n" if i == 0 else " ", piece))

    chunks: list[str] = []
    current = ""
    for sep, unit in units:
        if not current:
            current = unit
        elif len(current) + len(sep) + len(unit) <= target:
            current = f"{current}{sep}{unit}"
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    assert all(len(c) <= ceiling for c in chunks)
    return chunks


def _fit(paragraph: str, limit: int) -> list[str]:
    """Split one paragraph into pieces each <= limit (sentences, then words)."""
    if len(paragraph) <= limit:
        return [paragraph]
    pieces: list[str] = []
    for sentence in _SENTENCE_BREAK.split(paragraph):
        if len(sentence) <= limit:
            pieces.append(sentence)
        else:
            pieces.extend(_split_words(sentence, limit))
    return pieces


def _split_words(sentence: str, limit: int) -> list[str]:
    out: list[str] = []
    current = ""
    for word in sentence.split():
        while len(word) > limit:  # unbroken string: hard cut
            if current:
                out.append(current)
                current = ""
            out.append(word[:limit])
            word = word[limit:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= limit:
            current = f"{current} {word}"
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out
