"""One reading of what an Internet Protocol address is.

There were three, and they disagreed.

The system operations module matched four groups of up to three digits and
checked each was between zero and two hundred fifty-five, reading each group
with the interpreter's own integer conversion. The firewall renderer handed the
text to the standard library's network parser. The telephony schema matched
four groups of up to three digits and checked nothing at all, so a source of
nine hundred ninety-nine dot one dot one dot one was saved into the source of
truth and refused later, at render time, by a different component with a
different message.

The interesting disagreement is the leading zero. In this language ``int("010")``
is ten. In the C library every C program on the machine uses to read an
address, ``010`` is octal and means eight. So ``010.0.0.1`` is one address to
the appliance's own check and a different address to the kernel, the firewall
tool, and the telephony engine. A firewall rule written to admit one host would
admit another. The standard library refuses leading zeros outright for exactly
this reason, and this module refuses them too, everywhere, rather than in one
of the three places.

Nothing here allocates an address. This module reads text that names one.
"""

from __future__ import annotations

import ipaddress
import re

__all__ = [
    "AddressRefused",
    "parse_address",
    "parse_network",
    "is_address",
    "is_network",
]


class AddressRefused(ValueError):
    """The text does not name an address this appliance will act on."""


#: Four groups of digits, and nothing else. The range and the leading zero are
#: checked afterwards, because a pattern that tried to express both would be
#: unreadable and would still not agree with the C library on the edge cases.
_DOTTED = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _refuse_leading_zeros(text: str) -> None:
    for part in text.split("."):
        if len(part) > 1 and part[0] == "0":
            raise AddressRefused(
                f"the address {text} has a group written with a leading zero, "
                f"which this appliance reads as one number and the system "
                f"underneath it reads as another; write it without"
            )


def parse_address(text: str) -> str:
    """Return the address in its one canonical spelling, or refuse it."""
    candidate = str(text or "").strip()
    if not _DOTTED.match(candidate):
        raise AddressRefused(f"the text {text} is not an address in dotted form")
    _refuse_leading_zeros(candidate)
    try:
        parsed = ipaddress.IPv4Address(candidate)
    except ValueError as error:
        raise AddressRefused(f"the text {text} is not a valid address") from error
    return str(parsed)


def parse_network(text: str) -> str:
    """Return the network in its one canonical spelling, or refuse it.

    A bare address is a network of one host, which is how an operator naming a
    single machine in a firewall rule expects it to be read.
    """
    candidate = str(text or "").strip().lower()
    if candidate in ("", "any", "anywhere"):
        return "0.0.0.0/0"

    address, separator, prefix = candidate.partition("/")
    if ":" in address:
        # Named rather than lumped in with the malformed, because a version six
        # network is a thing an operator meant, and "this is not a network"
        # would send them looking for a typing mistake that is not there.
        raise AddressRefused(
            "only version four networks are understood by this appliance"
        )
    if not _DOTTED.match(address):
        raise AddressRefused(
            f"the text {text} is not a network in prefix notation"
        )
    _refuse_leading_zeros(address)
    if separator and (not prefix.isdigit() or len(prefix) > 2):
        raise AddressRefused(
            f"the prefix length in {text} is not a number between zero and "
            f"thirty-two"
        )

    try:
        network = ipaddress.ip_network(candidate, strict=False)
    except ValueError as error:
        raise AddressRefused(
            f"the text {text} is not a network in prefix notation"
        ) from error
    if network.version != 4:
        raise AddressRefused(
            "only version four networks are understood by this appliance"
        )
    return str(network)


def is_address(text: str) -> bool:
    try:
        parse_address(text)
    except AddressRefused:
        return False
    return True


def is_network(text: str) -> bool:
    try:
        parse_network(text)
    except AddressRefused:
        return False
    return True
