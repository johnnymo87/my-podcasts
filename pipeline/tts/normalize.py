"""Normalize script text and ASR transcripts into comparable word tokens.

Changing output here changes verifier policy: bump ``verify.VERIFIER_VERSION``.

Both sides pass through the same function. Numbers are expanded to their usual
spoken English reading by the bounded grammar below. That matters because the
ASR writes digits ("2026", "30th", "$580") while our writers often spell them
out ("twenty twenty-six", "thirtieth"), and number formatting was the dominant
source of alignment noise in the 2026-09-28 evaluation.

Supported, in the order applied:
  times            4:30 -> four thirty, 9:05 -> nine oh five, 10:00 -> ten
  currency         $580, $1,000, $2.45, $1.8 billion -> the bare number (no
                   "dollars"; $2.45 reads "two point four five")
  percent          7%, 5.5 %
  ordinals         1st, 30th, 21st
  decades          1990s, 90s
  signed numbers   -3 -> minus three (only when "-" follows start/space/"(")
  numbers          grouped (1,800,000) or plain integers, decimals (2.45);
                   a bare 4-digit 1100-2099 not followed by a magnitude word is
                   read as a year (2026 -> twenty twenty six)
Anything else numeric is left as digits: visible noise, never a guess. That
means integers above 999 trillion, and the tail of a version string: "3.5.1"
reads as "three point five 1" (the leading decimal converts, the ".1" does not).

Currency (verifier v4, my-podcasts-9p3.15): a dollar amount normalizes to the
same tokens as the bare number, and the words "dollar"/"dollars" are dropped on
both sides. Gemini ASR writes "$15.51", "15.51" or "15.51 dollars" for the same
audio, so any reading that kept a currency word on one side only turned every
price into a 1-4 token deficit (a 4-price list reached net_missing 8, a false
omission). Our writers spell prices out ("fifteen dollars and fifty-one
cents"), so "<number> dollars and <1-99> cents" is rewritten to "<number> point
d d" at the token level: every written form of a dollars-and-cents amount
converges. The cost is that a dropped "dollars" is never counted as missing
speech, and a dollar amount whose integer part is not a plain number ("a
dollar and fifty cents") becomes "a point five zero" on both sides alike.

Also: glued magnitude abbreviations (11bn, 5M, 4k; bn tn mm m b k) convert only
when attached directly to digits, so "5 mm" is left alone. Diacritics are
folded (Zürich -> zurich).

Every replacement is wrapped in "|" separators, not spaces. The tokenizer drops
"|", and, unlike whitespace, it does not satisfy the signed-number lookbehind,
so a range hyphen after a converted number ("$20-30") is never read as a minus.
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
_ABBREV = r"(?:bn|tn|mm|m|b|k)"
_ABBREV_WORDS = {
    "bn": "billion",
    "tn": "trillion",
    "mm": "million",
    "m": "million",
    "b": "billion",
    "k": "thousand",
}
# Groups: (spelled magnitude, glued abbreviation). A spelled magnitude may follow
# a space or hyphen ("$1.8-billion"); an abbreviation must be glued ("$11bn").
_MAG_CAPTURE = rf"(?:[\s-]?({_MAGNITUDE})\b|({_ABBREV})\b)"
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
            return "|" + fn(m) + "|"
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


def _magnitude_word(spelled: str | None, abbrev: str | None) -> str | None:
    if spelled:
        return spelled.lower()
    if abbrev:
        return _ABBREV_WORDS[abbrev.lower()]
    return None


def _currency(m: re.Match) -> str:
    """A dollar amount reads as the bare number (see the module docstring)."""
    sign, int_part, frac = m.group(1), m.group(2), m.group(3)
    magnitude = _magnitude_word(m.group(4), m.group(5))
    prefix = "minus " if sign else ""
    words = _number(int_part, frac)
    if magnitude:
        words += " " + magnitude
    return prefix + words


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
    sign, int_part, frac, abbrev = m.group(1), m.group(2), m.group(3), m.group(4)
    following = m.group(5)
    prefix = "minus " if sign else ""
    if abbrev:
        return prefix + _number(int_part, frac) + " " + _ABBREV_WORDS[abbrev.lower()]
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
    rf"{_SIGN}(-)?\$\s?({_INT})(?:\.(\d+))?{_MAG_CAPTURE}?", re.IGNORECASE
)
_CURRENCY_NOSIGN_RE = re.compile(
    rf"()\$\s?({_INT})(?:\.(\d+))?{_MAG_CAPTURE}?", re.IGNORECASE
)
_PERCENT_RE = re.compile(rf"(?:{_SIGN}(-))?(?<![\d.])({_INT})(?:\.(\d+))?\s?%")
_ORDINAL_RE = re.compile(rf"(?<![\d.])({_INT})(?:st|nd|rd|th)\b", re.IGNORECASE)
_DECADE_RE = re.compile(r"(?<![\d.])(\d0|\d{3}0)s\b")
_PLAIN_RE = re.compile(
    rf"(?:{_SIGN}(-))?(?<!\d)(?<!\d\.)({_INT})(?:\.(\d+))?(?!\d)(?:({_ABBREV})\b)?(?=(\s+{_MAGNITUDE}\b)?)",
    re.IGNORECASE,
)
_INITIALISM_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}")
_INNER_APOSTROPHE_RE = re.compile(r"(?<=\w)'(?=\w)")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DOLLAR_WORDS = frozenset({"dollar", "dollars"})
_CENT_WORDS = frozenset({"cent", "cents"})
_ONES_INDEX = {w: i for i, w in enumerate(_ONES)}
_TENS_INDEX = {w: i for i, w in enumerate(_TENS) if w != "_"}


def _cents_at(tokens: list[str], i: int) -> tuple[int, int] | None:
    """Parse ``<1-99 in words> cent(s)`` at ``tokens[i:]``: (value, tokens used)."""
    first = tokens[i] if i < len(tokens) else None
    if first in _TENS_INDEX:
        value, used = 10 * _TENS_INDEX[first], 1
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt in _ONES_INDEX and 0 < _ONES_INDEX[nxt] < 10:
            value, used = value + _ONES_INDEX[nxt], 2
    elif first in _ONES_INDEX and _ONES_INDEX[first] > 0:
        value, used = _ONES_INDEX[first], 1
    else:
        return None
    if i + used < len(tokens) and tokens[i + used] in _CENT_WORDS:
        return value, used + 1
    return None


def _canonical_currency(tokens: list[str]) -> list[str]:
    """Drop "dollar(s)"; rewrite "dollars and N cents" as "point d d".

    So the spelled "one hundred seven dollars and thirty five cents" (how our
    writers phrase prices) reads like "$107.35" and "107.35" (how the ASR writes
    them, with or without the "$").
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _DOLLAR_WORDS:
            if i + 1 < len(tokens) and tokens[i + 1] == "and":
                cents = _cents_at(tokens, i + 2)
                if cents is not None:
                    value, used = cents
                    out += ["point", _ONES[value // 10], _ONES[value % 10]]
                    i += 2 + used
                    continue
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


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
    # Fold diacritics: NFKD splits "é" into "e" + a combining mark, which we drop.
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
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
    return _canonical_currency(_TOKEN_RE.findall(normalize_text(text)))
