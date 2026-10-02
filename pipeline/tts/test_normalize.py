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
        # Currency reads as the bare number; "dollar(s)" is dropped (v4).
        ("$580", "five hundred eighty"),
        ("$750 million", "seven hundred fifty million"),
        ("$1.8 billion", "one point eight billion"),
        ("$2.45", "two point four five"),
        ("$1", "one"),
        ("$1,000", "one thousand"),
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
        ("$20-30 a barrel", ["twenty", "thirty", "a barrel"]),
        ("$5-10 million", ["five", "ten million"]),
        ("3:30-5 pm", ["three thirty", "five", "pm"]),
        ("1st-10 place", ["first", "ten", "place"]),
        ("$3.5-4 billion", ["three point five", "four billion"]),
        ("$20-30", ["twenty", "thirty"]),
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
        ("-$5", "minus five"),
        ("(-3)", "minus three"),
        ("down -3", "down minus three"),
    ],
)
def test_real_minus_signs_survive_padding_fix(text, expected):
    assert toks(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$11bn", "eleven billion"),
        ("$11 billion", "eleven billion"),
        ("$1.8-billion deal", "one point eight billion deal"),
        ("$1.8 billion", "one point eight billion"),
        ("$750 million", "seven hundred fifty million"),
        ("$2tn", "two trillion"),
        ("$5M", "five million"),
        ("$3mm", "three million"),
        ("$9k", "nine thousand"),
        ("11bn", "eleven billion"),
        ("1.5B", "one point five billion"),
        ("4k", "four thousand"),
        ("$20-30", "twenty thirty"),
    ],
)
def test_glued_and_hyphenated_magnitudes(text, expected):
    assert toks(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5 mm", "five mm"),
        ("$11 bn", "eleven bn"),
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


# --- currency convergence (my-podcasts-9p3.15, verifier v4) -----------------
# Gemini ASR is inconsistent about "$" on the same audio, so every way a
# transcript can write a dollar amount must normalize like the script's "$".


@pytest.mark.parametrize(
    ("script", "transcript"),
    [
        ("$15.51", "15.51"),
        ("$15.51", "15.51 dollars"),
        ("$15.51", "$15.51"),
        ("$580", "580 dollars"),
        ("$580", "580"),
        ("$1", "1 dollar"),
        ("$1.8 billion", "1.8 billion dollars"),
        ("$1.8 billion", "1.8 billion"),
        ("$11bn", "11 billion"),
        ("costs $2.45 a share", "costs 2.45 a share"),
        ("$1,000", "one thousand dollars"),
    ],
)
def test_dollar_sign_dropped_by_asr_still_aligns(script, transcript):
    assert toks(script) == toks(transcript)


def test_price_list_without_dollar_signs_has_no_deficit():
    """The 9p3.15 case: a 4-price list whose transcript drops every "$"."""
    script = "Prices were $15.51, $22.10, $9.99 and $101.25 on the day."
    heard = "Prices were 15.51, 22.10, 9.99 and 101.25 on the day."
    assert normalize_tokens(script) == normalize_tokens(heard)


def test_dollar_words_are_dropped_everywhere():
    assert toks("a dollar store sells Dollars") == "a store sells"


def test_cents_are_kept_as_a_word():
    assert toks("51 cents") == "fifty one cents"


@pytest.mark.parametrize(
    "form",
    [
        "$107.35",
        "107.35",
        "107.35 dollars",
        "107 dollars and 35 cents",
        "one hundred seven dollars and thirty-five cents",
    ],
)
def test_every_way_to_write_dollars_and_cents_converges(form):
    """Writers spell prices out, ASR writes digits with or without "$"."""
    assert toks(f"It closed at {form} today") == toks("It closed at $107.35 today")


@pytest.mark.parametrize(
    ("spelled", "digits"),
    [
        ("two dollars and ninety cents", "$2.90"),
        ("one dollar and one cent", "$1.01"),
        ("three dollars and five cents", "$3.05"),
        ("a dollar and fifty cents", "a $1.50"),
    ],
)
def test_spelled_cents_become_two_decimal_digits(spelled, digits):
    if spelled.startswith("a dollar"):
        # "a" is not a number word; only the cents part is rewritten.
        assert toks(spelled) == "a point five zero"
        return
    assert toks(spelled) == toks(digits)


@pytest.mark.parametrize(
    "text",
    [
        "fifty cents",
        "dollars and cents",
        "ten dollars and a hundred cents",
        "five dollars and change",
    ],
)
def test_cents_without_a_full_dollars_and_cents_shape_are_left_alone(text):
    assert "point" not in toks(text).split()
