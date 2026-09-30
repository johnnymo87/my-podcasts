"""Normalize script text and ASR transcripts into comparable word tokens.

Changing output here changes verifier policy: bump ``verify.VERIFIER_VERSION``.

Both sides pass through the same function. Numbers are expanded to their usual
spoken English reading by the bounded grammar below. That matters because the
ASR writes digits ("2026", "30th", "$580") while our writers often spell them
out ("twenty twenty-six", "thirtieth"), and number formatting was the dominant
source of alignment noise in the 2026-09-28 evaluation.

Supported, in the order applied:
  times            4:30 -> four thirty, 9:05 -> nine oh five, 10:00 -> ten
  currency         $580, $1,000, $2.45 (dollars and cents), $1.8 billion
  percent          7%, 5.5 %
  ordinals         1st, 30th, 21st
  decades          1990s, 90s
  signed numbers   -3 -> minus three (only when "-" follows start/space/"(")
  numbers          grouped (1,800,000) or plain integers, decimals (2.45);
                   a bare 4-digit 1100-2099 not followed by a magnitude word is
                   read as a year (2026 -> twenty twenty six)
Anything else numeric (above 999 trillion, version strings like 3.5.1) is left
as digits: visible noise, never a guess.
"""

from __future__ import annotations

import re
import unicodedata


_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = (
    (10**12, "trillion"),
    (10**9, "billion"),
    (10**6, "million"),
    (10**3, "thousand"),
)
MAX_CARDINAL = 10**15 - 1
_ORDINAL_IRREGULAR = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_MAGNITUDE = r"(?:thousand|million|billion|trillion)"
_INT = r"(?:\d{1,3}(?:,\d{3})+|\d+)"


def cardinal(n: int) -> str:
    if n < 0:
        return "minus " + cardinal(-n)
    if n > MAX_CARDINAL:
        raise ValueError(f"{n} is outside the supported range")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else " " + _ONES[ones])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        tail = "" if rest == 0 else " " + cardinal(rest)
        return _ONES[hundreds] + " hundred" + tail
    for value, name in _SCALES:
        if n >= value:
            head, rest = divmod(n, value)
            tail = "" if rest == 0 else " " + cardinal(rest)
            return cardinal(head) + " " + name + tail
    raise AssertionError("unreachable")


def _ordinalize_word(word: str) -> str:
    if word in _ORDINAL_IRREGULAR:
        return _ORDINAL_IRREGULAR[word]
    if word.endswith("y"):
        return word[:-1] + "ieth"
    return word + "th"


def ordinal(n: int) -> str:
    *head, last = cardinal(n).split()
    return " ".join([*head, _ordinalize_word(last)])


def _pluralize_last(words: str) -> str:
    *head, last = words.split()
    last = last[:-1] + "ies" if last.endswith("y") else last + "s"
    return " ".join([*head, last])


def year_words(n: int) -> str:
    if not 1100 <= n <= 2099:
        raise ValueError(f"{n} is not a supported year")
    if 2000 <= n <= 2009:
        return "two thousand" + ("" if n == 2000 else " " + _ONES[n - 2000])
    high, low = divmod(n, 100)
    if low == 0:
        return cardinal(high) + " hundred"
    if low < 10:
        return cardinal(high) + " oh " + _ONES[low]
    return cardinal(high) + " " + cardinal(low)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _number(int_part: str, frac: str | None) -> str:
    words = cardinal(_int(int_part))
    if frac:
        words += " point " + " ".join(_ONES[int(d)] for d in frac)
    return words


def _safe(fn):
    """Apply ``fn`` to a match; on an unsupported value keep the original text."""

    def sub(m: re.Match) -> str:
        try:
            return " " + fn(m) + " "
        except ValueError:
            return m.group(0)

    return sub


def _time(m: re.Match) -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute == 0:
        return cardinal(hour)
    if minute < 10:
        return cardinal(hour) + " oh " + _ONES[minute]
    return cardinal(hour) + " " + cardinal(minute)


def _currency(m: re.Match) -> str:
    sign, int_part, frac, magnitude = m.group(1), m.group(2), m.group(3), m.group(4)
    prefix = "minus " if sign else ""
    if magnitude:
        return prefix + _number(int_part, frac) + " " + magnitude.lower() + " dollars"
    dollars = _int(int_part)
    unit = "dollar" if dollars == 1 else "dollars"
    if frac and len(frac) == 2:
        cents = int(frac)
        words = cardinal(dollars) + " " + unit
        if cents:
            words += " and " + cardinal(cents) + (" cent" if cents == 1 else " cents")
        return prefix + words
    if frac:
        return prefix + _number(int_part, frac) + " dollars"
    return prefix + cardinal(dollars) + " " + unit


def _percent(m: re.Match) -> str:
    prefix = "minus " if m.group(1) else ""
    return prefix + _number(m.group(2), m.group(3)) + " percent"


def _ordinal(m: re.Match) -> str:
    return ordinal(_int(m.group(1)))


def _decade(m: re.Match) -> str:
    digits = m.group(1)
    n = int(digits)
    if len(digits) == 4:
        return _pluralize_last(year_words(n))
    return _pluralize_last(cardinal(n))


def _plain(m: re.Match) -> str:
    sign, int_part, frac, following = m.group(1), m.group(2), m.group(3), m.group(4)
    prefix = "minus " if sign else ""
    is_year = (
        not sign
        and frac is None
        and "," not in int_part
        and len(int_part) == 4
        and 1100 <= int(int_part) <= 2099
        and not following
    )
    if is_year:
        return year_words(int(int_part))
    return prefix + _number(int_part, frac)


_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_SIGN = r"(?:(?<=^)|(?<=[\s(]))"
_CURRENCY_RE = re.compile(
    rf"{_SIGN}(-)?\$\s?({_INT})(?:\.(\d+))?(?:\s?({_MAGNITUDE})\b)?", re.IGNORECASE
)
_CURRENCY_NOSIGN_RE = re.compile(
    rf"()\$\s?({_INT})(?:\.(\d+))?(?:\s?({_MAGNITUDE})\b)?", re.IGNORECASE
)
_PERCENT_RE = re.compile(rf"(?:{_SIGN}(-))?(?<![\d.])({_INT})(?:\.(\d+))?\s?%")
_ORDINAL_RE = re.compile(rf"(?<![\d.])({_INT})(?:st|nd|rd|th)\b", re.IGNORECASE)
_DECADE_RE = re.compile(r"(?<![\d.])(\d0|\d{3}0)s\b")
_PLAIN_RE = re.compile(
    rf"(?:{_SIGN}(-))?(?<!\d)(?<!\d\.)({_INT})(?:\.(\d+))?(?!\d)(?=(\s+{_MAGNITUDE}\b)?)",
    re.IGNORECASE,
)
_INITIALISM_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}")
_INNER_APOSTROPHE_RE = re.compile(r"(?<=\w)'(?=\w)")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(
        {
            0x2018: "'",
            0x2019: "'",
            0x02BC: "'",
            0x201C: '"',
            0x201D: '"',
            0x2013: " ",
            0x2014: " ",
            0x2212: "-",
        }
    )
    text = _INITIALISM_RE.sub(lambda m: m.group(0).replace(".", ""), text)
    text = text.replace("&", " and ")
    text = _INNER_APOSTROPHE_RE.sub("", text)
    text = _TIME_RE.sub(_safe(_time), text)
    text = _CURRENCY_RE.sub(_safe(_currency), text)
    text = _CURRENCY_NOSIGN_RE.sub(_safe(_currency), text)
    text = _PERCENT_RE.sub(_safe(_percent), text)
    text = _ORDINAL_RE.sub(_safe(_ordinal), text)
    text = _DECADE_RE.sub(_safe(_decade), text)
    text = _PLAIN_RE.sub(_safe(_plain), text)
    return text.lower()


def normalize_tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_text(text))
