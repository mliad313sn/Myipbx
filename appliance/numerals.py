"""Numeral spelling subsystem.

Constraint Two of the appliance specification requires that every operator
facing surface renders numbers as words rather than as digit characters.  This
module is the single server side authority for that transformation.  A browser
side twin lives at ``web/js/numerals.js`` and is required to agree with this
module for every value the product can produce; ``tests/test_numerals.py``
enforces that agreement.

The public surface is deliberately small:

``spell_integer``   -- an exact integer rendered as words.
``spell_decimal``   -- a real number rendered as words, fraction digit by digit.
``spell_ordinal``   -- position words such as ``first`` or ``twenty-third``.
``spell_duration``  -- a second count rendered as a spoken duration.
``sanitize``        -- replace every digit run in arbitrary text with words.
``contains_digit``  -- the assertion used by the constraint tests.
"""

from __future__ import annotations

import re

__all__ = [
    "spell_integer",
    "spell_decimal",
    "spell_ordinal",
    "spell_duration",
    "sanitize",
    "contains_digit",
    "SpellingError",
]


class SpellingError(ValueError):
    """Raised when a value is outside the range this module can render."""


_SMALL = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)

_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)

# Short scale naming.  The empty first entry is the units group.
_SCALES = (
    "", "thousand", "million", "billion", "trillion", "quadrillion",
    "quintillion", "sextillion",
)

_GROUP = 1000

# Largest magnitude representable with the scale table above.
_LIMIT = _GROUP ** len(_SCALES)

_ORDINAL_SMALL = {
    "zero": "zeroth", "one": "first", "two": "second", "three": "third",
    "four": "fourth", "five": "fifth", "six": "sixth", "seven": "seventh",
    "eight": "eighth", "nine": "ninth", "ten": "tenth", "eleven": "eleventh",
    "twelve": "twelfth", "thirteen": "thirteenth", "fourteen": "fourteenth",
    "fifteen": "fifteenth", "sixteen": "sixteenth",
    "seventeen": "seventeenth", "eighteen": "eighteenth",
    "nineteen": "nineteenth", "twenty": "twentieth", "thirty": "thirtieth",
    "forty": "fortieth", "fifty": "fiftieth", "sixty": "sixtieth",
    "seventy": "seventieth", "eighty": "eightieth", "ninety": "ninetieth",
    "hundred": "hundredth", "thousand": "thousandth", "million": "millionth",
    "billion": "billionth", "trillion": "trillionth",
    "quadrillion": "quadrillionth", "quintillion": "quintillionth",
    "sextillion": "sextillionth",
}

_DIGIT_RUN = re.compile(r"\d+")
_ANY_DIGIT = re.compile(r"\d")
_WORD_EDGE = re.compile(r"[A-Za-z]")


def _spell_group(value: int) -> list[str]:
    """Render a value below one thousand as a list of words.

    An input of zero renders as an empty list so that group assembly can skip
    empty groups; the caller is responsible for the standalone zero case.
    """
    words: list[str] = []
    if value >= 100:
        words.append(_SMALL[value // 100])
        words.append("hundred")
        value %= 100
    if value >= 20:
        tens_word = _TENS[value // 10]
        remainder = value % 10
        if remainder:
            words.append(f"{tens_word}-{_SMALL[remainder]}")
        else:
            words.append(tens_word)
    elif value > 0:
        words.append(_SMALL[value])
    return words


def spell_integer(value: int) -> str:
    """Render an exact integer as words.

    Negative values are prefixed with ``negative``.  Grouping follows the short
    scale with no comma and no conjunction, so one thousand and twenty four
    renders as ``one thousand twenty-four``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpellingError(f"an exact integer is required, received {type(value).__name__}")

    if value == 0:
        return _SMALL[0]

    negative = value < 0
    magnitude = -value if negative else value

    if magnitude >= _LIMIT:
        raise SpellingError("the value exceeds the largest representable magnitude")

    # Split into groups of three, least significant group first.
    groups: list[int] = []
    while magnitude:
        groups.append(magnitude % _GROUP)
        magnitude //= _GROUP

    words: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        group_value = groups[index]
        if group_value == 0:
            continue
        words.extend(_spell_group(group_value))
        if index:
            words.append(_SCALES[index])

    rendered = " ".join(words)
    return f"negative {rendered}" if negative else rendered


def spell_decimal(value: float, places: int = 2) -> str:
    """Render a real number as words.

    The integral part is rendered as a whole number and the fractional part is
    rendered digit by digit after the word ``point``, which is how a number is
    read aloud.  A value whose fractional part rounds away renders as a plain
    whole number rather than trailing a run of zero words.
    """
    if isinstance(value, bool):
        raise SpellingError("a real number is required, received a boolean")
    if not isinstance(value, (int, float)):
        raise SpellingError(f"a real number is required, received {type(value).__name__}")
    if places < 0:
        raise SpellingError("the requested place count cannot be negative")
    if value != value or value in (float("inf"), float("-inf")):
        raise SpellingError("the value is not a finite real number")

    negative = value < 0
    text = f"{abs(float(value)):.{places}f}"
    whole_text, _, fraction_text = text.partition(".")

    fraction_text = fraction_text.rstrip("0")
    words = spell_integer(int(whole_text))
    if fraction_text:
        spoken = " ".join(_SMALL[int(character)] for character in fraction_text)
        words = f"{words} point {spoken}"
    return f"negative {words}" if negative and (int(whole_text) or fraction_text) else words


def spell_ordinal(value: int) -> str:
    """Render a position as words, such as ``first`` or ``one hundredth``."""
    cardinal = spell_integer(value)
    head, _, tail = cardinal.rpartition(" ")

    if "-" in tail:
        tens_part, _, units_part = tail.partition("-")
        tail = f"{tens_part}-{_ORDINAL_SMALL[units_part]}"
    else:
        tail = _ORDINAL_SMALL[tail]

    return f"{head} {tail}" if head else tail


def spell_duration(seconds: int) -> str:
    """Render a second count as a spoken duration.

    Zero renders as ``zero seconds``.  Units that are empty are omitted, so an
    exact hour renders as ``one hour`` rather than naming empty minutes and
    seconds.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise SpellingError("an exact second count is required")
    if seconds < 0:
        raise SpellingError("a duration cannot be negative")
    if seconds == 0:
        return "zero seconds"

    remaining = seconds
    parts: list[str] = []
    for unit_seconds, singular, plural in (
        (86400, "day", "days"),
        (3600, "hour", "hours"),
        (60, "minute", "minutes"),
        (1, "second", "seconds"),
    ):
        count = remaining // unit_seconds
        remaining %= unit_seconds
        if count:
            name = singular if count == 1 else plural
            parts.append(f"{spell_integer(count)} {name}")
    return " ".join(parts)


def sanitize(text: str) -> str:
    """Replace every run of digit characters in ``text`` with its words.

    Each maximal run of digits is rendered independently as a whole number, so
    a dotted address renders as words separated by the original punctuation.
    Word boundaries are preserved: a space is introduced where a replacement
    would otherwise be welded onto an adjacent letter.

    This is the function the logging formatter applies to every emitted line,
    which is what makes Constraint Two unconditional rather than a convention
    that individual call sites are trusted to remember.
    """
    if not isinstance(text, str):
        text = str(text)
    if not _ANY_DIGIT.search(text):
        return text

    pieces: list[str] = []
    cursor = 0
    for match in _DIGIT_RUN.finditer(text):
        start, end = match.span()
        pieces.append(text[cursor:start])

        run = match.group()
        # Leading zeros carry meaning in identifiers, so they are spoken.
        stripped = run.lstrip("0")
        if stripped:
            leading = ["zero"] * (len(run) - len(stripped))
            try:
                spelled = " ".join(leading + [spell_integer(int(stripped))])
            except SpellingError:
                # Beyond the scale table, fall back to digit by digit.
                spelled = " ".join(_SMALL[int(character)] for character in run)
        else:
            spelled = " ".join(["zero"] * len(run))

        # Keep the replacement from fusing with adjacent letters.
        if pieces and pieces[-1] and _WORD_EDGE.search(pieces[-1][-1]):
            spelled = " " + spelled
        if end < len(text) and _WORD_EDGE.search(text[end]):
            spelled = spelled + " "

        pieces.append(spelled)
        cursor = end

    pieces.append(text[cursor:])
    return "".join(pieces)


def contains_digit(text: str) -> bool:
    """Report whether any digit character survives in ``text``."""
    return bool(_ANY_DIGIT.search(text))
