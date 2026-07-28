"""What a call cost, from a table an operator can read and edit.

Nothing here guesses. A call has a cost only when the operator has declared a
rate that covers the number it was dialled to, and a call with no matching rate
is reported as unrated rather than as free -- because those are different facts
and a total that quietly counts one as the other is wrong in the direction
somebody notices at the end of the quarter.

Three rules, all of them the field's:

**Only answered calls are charged.** A carrier bills for a conversation, not
for a ring.

**Only outbound calls are charged.** An inbound call costs the caller, and a
call between two extensions on this machine costs nobody.

**Time is billed in whole increments.** A carrier selling per-minute billing
charges a full minute for a call of four seconds, and a report that divided the
four seconds by sixty would be out by an order of magnitude on exactly the
calls a site makes most of.

Money is held as ``Decimal`` throughout and never as a floating point number.
A tenth of a penny cannot be represented in binary, and a quarter of a million
calls a year is enough for that to show.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

from . import numerals

__all__ = [
    "Tariff",
    "load_tariffs",
    "match_tariff",
    "charge",
    "currencies_of",
    "spell_money",
    "AMOUNT_PATTERN",
]

#: What an amount may look like when it is typed in. Deliberately narrow: a
#: rate is money, and the shapes money is written in vary by country in ways a
#: permissive pattern would silently accept and misread -- a comma is a
#: thousands separator in one place and a decimal point in another.
AMOUNT_PATTERN = re.compile(r"^\d{1,9}(\.\d{1,6})?$")

#: The unit an amount is rounded to when it is presented or exported. Kept at
#: four places rather than two because a per-minute rate is routinely quoted in
#: thousandths, and rounding the rate to the nearest penny would make every
#: rate under a penny a minute read as free.
_PRESENTED = Decimal("0.0001")


@dataclass(frozen=True)
class Tariff:
    """One rate, covering the numbers that begin with one prefix."""

    name: str
    prefix: str
    currency: str
    connection_fee: Decimal
    per_minute: Decimal
    increment_seconds: int
    minimum_seconds: int

    def describe(self) -> str:
        where = f"numbers beginning {self.prefix}" if self.prefix else "every other number"
        return f"{self.name}, covering {where}"


def _amount(value: Any) -> Decimal:
    """One typed amount, or zero.

    An unreadable amount becomes zero rather than raising, because this is
    reached from a report and a report that will not draw at all because one
    row of a rate table has a typographical error in it is worse than a report
    that shows that row as costing nothing. The validation that keeps such a
    row out of the table in the first place lives with the entity.
    """
    text = str(value or "0").strip()
    if not AMOUNT_PATTERN.match(text):
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def load_tariffs(records: Iterable[Mapping[str, Any]]) -> list[Tariff]:
    """The enabled rates, longest prefix first.

    Sorted here rather than at each lookup so that "longest prefix wins" is a
    property of the table rather than something every caller has to remember to
    do -- and getting it wrong means a national rate quietly charged at the
    international one, which is the expensive direction.
    """
    tariffs: list[Tariff] = []
    for record in records or ():
        if not record.get("enabled", True):
            continue
        name = str(record.get("name", "")).strip()
        if not name:
            continue
        tariffs.append(Tariff(
            name=name,
            prefix=str(record.get("prefix", "") or "").strip(),
            currency=str(record.get("currency", "") or "").strip(),
            connection_fee=_amount(record.get("connection_fee")),
            per_minute=_amount(record.get("per_minute")),
            increment_seconds=max(1, int(record.get("increment_seconds") or 60)),
            minimum_seconds=max(0, int(record.get("minimum_seconds") or 0)),
        ))
    tariffs.sort(key=lambda tariff: (-len(tariff.prefix), tariff.prefix))
    return tariffs


def match_tariff(tariffs: Sequence[Tariff], number: str) -> Tariff | None:
    """The rate covering this number, or none at all.

    None means unrated, which the report says out loud. It does not mean free.
    """
    dialled = str(number or "").strip()
    if not dialled:
        return None
    for tariff in tariffs:
        if not tariff.prefix or dialled.startswith(tariff.prefix):
            return tariff
    return None


def charge(tariff: Tariff, billable_seconds: int) -> Decimal:
    """What one answered call cost under one rate.

    A call that was never answered has no billable seconds and so no cost, and
    that falls out of the arithmetic rather than needing a special case.
    """
    seconds = max(0, int(billable_seconds or 0))
    if seconds <= 0:
        return Decimal("0")

    seconds = max(seconds, tariff.minimum_seconds)
    increment = tariff.increment_seconds
    # Rounded up to the next whole increment, which is what a carrier does.
    units = -(-seconds // increment)
    billed = units * increment

    return tariff.connection_fee + (
        tariff.per_minute * Decimal(billed) / Decimal(60)
    )


def currencies_of(tariffs: Iterable[Tariff]) -> list[str]:
    """Every distinct currency the table names, in the order first seen."""
    seen: list[str] = []
    for tariff in tariffs:
        currency = tariff.currency or ""
        if currency and currency not in seen:
            seen.append(currency)
    return seen


def round_money(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(_PRESENTED, rounding=ROUND_HALF_UP)


def spell_money(amount: Decimal, currency: str = "") -> str:
    """An amount, spelled, with its currency named after it.

    Constraint Two applies: an amount of money is a quantity, not an
    identifier, so it is written in words on every screen. The exported file
    carries the digits, as every export does, because that is the file somebody
    adds up.

    Trailing zeroes are dropped before spelling, so a round amount reads as
    "four" rather than "four point zero zero zero zero".
    """
    value = round_money(amount)
    places = max(0, -value.as_tuple().exponent)
    text = value.normalize()
    normalised_places = max(0, -text.as_tuple().exponent)
    spelled = numerals.spell_decimal(float(value), min(places, max(normalised_places, 0)))
    return f"{spelled} {currency}".strip() if currency else spelled
