import pytest

from pipeline.tts.normalize import cardinal, normalize_tokens, ordinal, year_words


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (0, "zero"),
        (7, "seven"),
        (19, "nineteen"),
        (20, "twenty"),
        (42, "forty two"),
        (100, "one hundred"),
        (580, "five hundred eighty"),
        (1000, "one thousand"),
        (1_800_000, "one million eight hundred thousand"),
        (12_000_000_000, "twelve billion"),
    ],
)
def test_cardinal(n, words):
    assert cardinal(n) == words


def test_cardinal_rejects_out_of_range():
    with pytest.raises(ValueError):
        cardinal(10**15)


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (1, "first"),
        (2, "second"),
        (3, "third"),
        (5, "fifth"),
        (8, "eighth"),
        (9, "ninth"),
        (12, "twelfth"),
        (20, "twentieth"),
        (30, "thirtieth"),
        (21, "twenty first"),
        (100, "one hundredth"),
    ],
)
def test_ordinal(n, words):
    assert ordinal(n) == words


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (2026, "twenty twenty six"),
        (2010, "twenty ten"),
        (2000, "two thousand"),
        (2005, "two thousand five"),
        (1984, "nineteen eighty four"),
        (1905, "nineteen oh five"),
        (1900, "nineteen hundred"),
    ],
)
def test_year_words(n, words):
    assert year_words(n) == words


def toks(s: str) -> str:
    return " ".join(normalize_tokens(s))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Currency: magnitude moves before "dollars"; cents; singular.
        ("$580", "five hundred eighty dollars"),
        ("$750 million", "seven hundred fifty million dollars"),
        ("$1.8 billion", "one point eight billion dollars"),
        ("$2.45", "two dollars and forty five cents"),
        ("$1", "one dollar"),
        ("$1,000", "one thousand dollars"),
        # Percent, decimals, grouped integers.
        ("7%", "seven percent"),
        ("5.5 %", "five point five percent"),
        ("2.45", "two point four five"),
        ("1,800,000 people", "one million eight hundred thousand people"),
        # Years vs quantities.
        ("in 2030.", "in twenty thirty"),
        ("2026", "twenty twenty six"),
        ("2030 million", "two thousand thirty million"),
        ("12345", "twelve thousand three hundred forty five"),
        # Ordinals and decades.
        ("September 30th", "september thirtieth"),
        ("the 1990s", "the nineteen nineties"),
        ("the 90s", "the nineties"),
        # Times.
        ("4:30 AM", "four thirty am"),
        ("9:05", "nine oh five"),
        ("10:00", "ten"),
        # Negative sign is kept; a hyphen inside a word is not a sign.
        ("fell -3%", "fell minus three percent"),
        ("GPT-6.1 Astra", "gpt six point one astra"),
        # Spelled-out and digit forms converge.
        ("twenty twenty-six", "twenty twenty six"),
    ],
)
def test_numbers(text, expected):
    assert toks(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The U.S. and A.I.", "the us and ai"),
        ("AT&T", "at and t"),
        ("it’s OpenAI’s", "its openais"),
        ("“quoted” — dash–dash", "quoted dash dash"),
        ("didn't", "didnt"),
        ("", ""),
        ("... !!", ""),
    ],
)
def test_text_normalization(text, expected):
    assert toks(text) == expected


def test_out_of_range_integer_stays_digits():
    assert toks("1234567890123456789") == "1234567890123456789"


# --- review follow-ups -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("$20-30 a barrel", ["twenty dollars", "thirty", "a barrel"]),
        ("$5-10 million", ["five dollars", "ten million"]),
        ("3:30-5 pm", ["three thirty", "five", "pm"]),
        ("1st-10 place", ["first", "ten", "place"]),
        ("$3.5-4 billion", ["three point five dollars", "four billion"]),
        ("$20-30", ["twenty dollars", "thirty"]),
    ],
)
def test_range_hyphen_is_not_a_minus_sign(text, numbers):
    out = toks(text)
    assert "minus" not in out.split()
    for piece in numbers:
        assert piece in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fell -3%", "fell minus three percent"),
        ("-$5", "minus five dollars"),
        ("(-3)", "minus three"),
        ("down -3", "down minus three"),
    ],
)
def test_real_minus_signs_survive_padding_fix(text, expected):
    assert toks(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$11bn", "eleven billion dollars"),
        ("$11 billion", "eleven billion dollars"),
        ("$1.8-billion deal", "one point eight billion dollars deal"),
        ("$1.8 billion", "one point eight billion dollars"),
        ("$750 million", "seven hundred fifty million dollars"),
        ("$2tn", "two trillion dollars"),
        ("$5M", "five million dollars"),
        ("$3mm", "three million dollars"),
        ("$9k", "nine thousand dollars"),
        ("11bn", "eleven billion"),
        ("1.5B", "one point five billion"),
        ("4k", "four thousand"),
        ("$20-30", "twenty dollars thirty"),
    ],
)
def test_glued_and_hyphenated_magnitudes(text, expected):
    assert toks(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5 mm", "five mm"),
        ("$11 bn", "eleven dollars bn"),
        ("5 m", "five m"),
    ],
)
def test_abbreviations_convert_only_when_glued_to_digits(text, expected):
    assert toks(text) == expected


def test_diacritics_are_folded():
    assert toks("Zürich Nestlé Pokémon") == "zurich nestle pokemon"


def test_version_string_is_partly_converted():
    # Documented behavior: only the leading "3.5" is read as a decimal.
    assert toks("3.5.1") == "three point five 1"
