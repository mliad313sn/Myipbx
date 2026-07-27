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

#: Shapes that are identifiers rather than quantities, and are left alone.
#:
#: The rule this encodes is the difference between a number an operator reads
#: and a number an operator uses.  "Twelve active calls" is a quantity: spelling
#: it costs nothing and reads better.  An address, a port, a version, a device
#: name or a protocol response code is an identifier: the operator has to type
#: it, compare it against a label on a cable, or search for it, and spelling it
#: does not make it clearer — it makes it unusable.
#:
#: This was not a theoretical worry.  The preflight check told a technician to
#: set the target interface to "ethzero", when the name of the interface is
#: eth0 and nothing called ethzero exists on any machine.
#:
#: Order matters: the longest and most specific shapes come first, so that an
#: address inside a prefix is not matched as a bare address and left half
#: spelled.
_IDENTIFIER_SHAPES: tuple[re.Pattern[str], ...] = (
    # An address with a prefix length, then a bare address, then an address
    # with a port. Internet Protocol version six is matched loosely on purpose:
    # anything that is plausibly one should be left alone rather than mangled.
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}\b"),
    # A hardware address, and a peripheral bus identifier.
    re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"),
    re.compile(r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]\b"),
    re.compile(r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\b"),
    # A date, a time, and the two joined.
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?!\d)"),
    re.compile(r"\b\d{2}:\d{2}(?::\d{2})?(?!\d)"),
    # A version, with or without its leading letter.
    re.compile(r"\bv?\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]+)?\b"),
    # A device or interface name: letters then digits, with no space between.
    re.compile(r"\b(?:eth|en[a-z0-9]*|wl[a-z0-9]*|lo|tty[A-Za-z]*|sd[a-z]|nvme|dahdi|span|zap)\d+\b"),
    # A filesystem path containing digits, and a bare digit-bearing filename.
    re.compile(r"(?:/[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*)+"),
    # A protocol response code, named as one.
    re.compile(r"\b(?:SIP|HTTP|status|code|error)\s+\d{3}\b", re.IGNORECASE),
    # An error number, which an engineer looks up. The operating system's own
    # numbering is the clearest case: "Errno one hundred eleven" is not
    # something anybody can search for.
    re.compile(r"\b(?:errno|error\s+number)\s+\d+\b", re.IGNORECASE),
    # The address and port pair the networking layer prints when a connection
    # fails. The port inside it is an identifier like any other.
    re.compile(r"\(\s*'[^']*'\s*,\s*\d{1,5}\s*\)"),
    # A file mode. An operator types it into a command; "zero seven
    # hundred fifty" is not something chmod accepts.
    re.compile(r"\b(?:chmod|mode|permissions)\s+[0-7]{3,4}\b", re.IGNORECASE),
    # A port, named as one. An operator opens a port in a firewall and types it
    # into a browser, so it is something they use rather than something they
    # count.
    re.compile(r"\bport(?:\s+number)?\s+\d{1,5}\b", re.IGNORECASE),
    # An interface card model, as printed on the card itself. A technician
    # holds the card and reads the silkscreen; "TDM four one zero P" is not
    # what it says.
    #
    # This pattern was written with its escapes doubled and so had never
    # matched anything: every card model this appliance logged came out spelled
    # while the browser and the installer, whose copies were written correctly,
    # kept the digits. Three implementations that are meant to be identical
    # were not, and the test holding them together did not sample a card model.
    # It does now.
    re.compile(r"\b(?:TDM|TCE|TC|TE|AEX|HA|HB|A|B)\d+(?:-\d+|[A-Z])?\b"),
    # A telephone number the appliance routes to: an extension, a dial string.
    # These keep their leading zeros and are matched against a handset label.
    re.compile(r"\bextension\s+\d+\b", re.IGNORECASE),
)

#: A private placeholder that cannot occur in real text, used to hold an
#: identifier's place while the quantities around it are spelled.
#:
#: The index inside the marker is written in letters rather than digits, which
#: is not decoration: the marker passes through the very function that turns
#: digits into words, and a numeric index would be spelled along with
#: everything else and the identifier could never be put back.
_GUARD_OPEN = "\x00\x01"
_GUARD_CLOSE = "\x01\x00"
_GUARD_FINDER = re.compile(r"\x00\x01([a-z]+)\x01\x00")


def _guard_label(index: int) -> str:
    """A purely alphabetic label for a held identifier."""
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("a") + remainder) + label
    return label


def _guard_index(label: str) -> int:
    index = 0
    for character in label:
        index = index * 26 + (ord(character) - ord("a") + 1)
    return index - 1


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


def identifier_spans(text: str) -> list[tuple[int, int]]:
    """Find the parts of ``text`` that are identifiers rather than quantities.

    Returned spans never overlap: where two shapes match the same region the
    longer one wins, so an address inside a prefix is protected whole rather
    than half spelled.
    """
    found: list[tuple[int, int]] = []
    for pattern in _IDENTIFIER_SHAPES:
        for match in pattern.finditer(text):
            found.append(match.span())

    found.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    merged: list[tuple[int, int]] = []
    for start, end in found:
        if merged and start < merged[-1][1]:
            # Overlapping: extend the one already held if this reaches further.
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
            continue
        merged.append((start, end))
    return merged


def sanitize(text: str, spell_identifiers: bool = False) -> str:
    """Spell the quantities in ``text``, leaving its identifiers alone.

    Each maximal run of digits is rendered independently as a whole number.
    Word boundaries are preserved: a space is introduced where a replacement
    would otherwise be welded onto an adjacent letter.

    Identifiers are exempt, and the distinction is the point.  A quantity is a
    number the operator reads — "twelve active calls" — and spelling it costs
    nothing.  An identifier is a number the operator uses: an address they must
    type, a port they must open, a device name they must match against a label
    on a cable, a response code they must search for.  Spelling those does not
    make them clearer, it makes them unusable, and the appliance was doing it:
    it told a technician to configure the interface named "ethzero" when the
    interface is called eth0.

    Pass ``spell_identifiers`` to get the older behaviour, in which nothing at
    all survives as digits.  Nothing in the appliance asks for that; it exists
    so a test can still exercise the unconditional form.

    This is the function the logging formatter applies to every emitted line,
    which is what keeps Constraint Two a property of the system rather than a
    convention that individual call sites are trusted to remember.
    """
    if not isinstance(text, str):
        text = str(text)
    if not _ANY_DIGIT.search(text):
        return text

    if not spell_identifiers:
        spans = identifier_spans(text)
        if spans:
            # Hold each identifier's place with a marker carrying no digits of
            # its own, spell what is left, and put the identifiers back.
            held: list[str] = []
            rebuilt: list[str] = []
            cursor = 0
            for start, end in spans:
                rebuilt.append(text[cursor:start])
                rebuilt.append(_GUARD_OPEN + _guard_label(len(held)) + _GUARD_CLOSE)
                held.append(text[start:end])
                cursor = end
            rebuilt.append(text[cursor:])
            spelled = sanitize("".join(rebuilt), spell_identifiers=True)
            return _GUARD_FINDER.sub(lambda m: held[_guard_index(m.group(1))], spelled)

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
