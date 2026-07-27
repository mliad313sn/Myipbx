"""Legacy Digium hardware bridging layer — read only enumeration.

Enumeration is strictly separated from provisioning.  This module only reads:
it inspects the peripheral bus inventory for interface cards carrying the
Digium vendor identifier, and it reads the driver's own exported device tree
for span and channel state.  Nothing here loads a module, compiles anything, or
mutates the machine; that is the staging scripts' responsibility.

On a machine with no such hardware, enumeration returns an empty inventory and
reports the absence as a normal condition.  That is deliberate — it is what
allows the entire control plane to be developed, tested, and demonstrated away
from the target appliance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .logging_setup import get_logger

__all__ = [
    "DIGIUM_VENDOR_IDENTIFIER",
    "InterfaceCard",
    "Span",
    "SpanChannel",
    "HardwareInventory",
    "describe_card",
    "parse_span_document",
]

_LOG = get_logger("hardware")

#: The peripheral bus vendor identifier assigned to Digium.
DIGIUM_VENDOR_IDENTIFIER = 0xD161

#: Where the identifiers actually live. One file, read by this module and by
#: the pre-flight script, because there were two hand-written tables before it
#: and they disagreed on ten of the eleven identifiers they shared. The file
#: records which driver source its rows were read out of; see the note at the
#: top of it.
#:
#: Looked for in more than one place, because the repository and the installed
#: appliance are laid out differently. The first draft resolved one path
#: relative to this module and so found the file in the repository, where every
#: test runs, and found nothing on a real machine -- where the installer copies
#: the modules and nothing beside them. Every card would have read "an
#: unrecognised Digium interface card" in service while the suite stayed green.
_CATALOGUE_LOCATIONS: tuple[Path, ...] = (
    Path(__file__).resolve().parent.parent / "share" / "digium-cards.tsv",
    Path(__file__).resolve().parent / "share" / "digium-cards.tsv",
    Path("/opt/myipbx/share/digium-cards.tsv"),
    Path("/usr/share/myipbx/digium-cards.tsv"),
)


def _catalogue_file() -> Path:
    for candidate in _CATALOGUE_LOCATIONS:
        if candidate.is_file():
            return candidate
    return _CATALOGUE_LOCATIONS[0]


_CATALOGUE_FILE = _catalogue_file()


def _load_catalogue(path: Path) -> dict[int, tuple[str, str, str]]:
    """Read the shared table.

    A missing or damaged file degrades the description of a detected card; it
    never stops one being detected. An appliance that refused to enumerate its
    hardware because a text file was missing would be a worse appliance than
    one that says "an unrecognised Digium interface card" and carries on.
    """
    catalogue: dict[int, tuple[str, str, str]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        _LOG.warning("the interface card catalogue could not be read: %s", error)
        return catalogue

    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] == "device":
            continue
        try:
            identifier = int(parts[0], 16)
        except ValueError:
            continue
        catalogue[identifier] = (parts[1], parts[2], parts[3])
    return catalogue


#: Recognised interface cards, named in plain language for the dashboard.  An
#: identifier absent from this table is still reported as a Digium card; the
#: table improves the description, it does not gate detection.
_CARD_CATALOGUE: dict[int, tuple[str, str, str]] = _load_catalogue(_CATALOGUE_FILE)

_SPAN_HEADER = re.compile(
    r'^Span\s+(?P<number>\d+):\s*(?P<identifier>\S+)\s*"(?P<description>[^"]*)"(?P<trailer>.*)$'
)

_CHANNEL_LINE = re.compile(
    r"^\s+(?P<number>\d+)\s+(?P<name>\S+)\s+(?P<signalling>\S+)?(?P<trailer>.*)$"
)

_ANNOTATION = re.compile(r"\(([^)]*)\)")

#: Annotations the driver appends that genuinely indicate a fault.
_ALARM_TOKENS = frozenset(
    {
        "RED", "YELLOW", "BLUE", "LOOPBACK", "NOTOPEN", "RECOVERING",
        "RECOVER", "UNCONFIGURED", "LOS", "LFA", "LMFA", "ALARM",
    }
)

#: The line coding and framing a span was configured with, such as
#: ``HDB3/CCS/CRC4`` or ``B8ZS/ESF``.  It appears on every healthy span and
#: says nothing about health, so it must not be read as a fault.
_SIGNALLING_DESCRIPTION = re.compile(r"^[A-Z0-9]+(/[A-Z0-9]+)+$")

#: Annotations that describe the span's role rather than its health.  The
#: master span of every machine carries one, so mistaking these for alarms
#: would raise a false alarm on essentially every real appliance.
_ROLE_TOKENS = frozenset({"MASTER", "SLAVE", "OK", "SYNC"})


@dataclass(frozen=True)
class InterfaceCard:
    """One detected interface card on the peripheral bus."""

    slot: str
    vendor_identifier: int
    device_identifier: int
    model: str
    driver_module: str
    description: str
    driver_bound: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "model": self.model,
            "description": self.description,
            "driver_module": self.driver_module,
            "driver_bound": self.driver_bound,
            "vendor_identifier": f"{self.vendor_identifier:04x}",
            "device_identifier": f"{self.device_identifier:04x}",
            "recognised": self.device_identifier in _CARD_CATALOGUE,
        }


@dataclass(frozen=True)
class SpanChannel:
    """One channel within a span."""

    number: int
    name: str
    signalling: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "signalling": self.signalling,
            "status": self.status,
            "in_use": self.status.lower() == "in use",
        }


@dataclass
class Span:
    """One span exported by the interface driver."""

    number: int
    identifier: str
    description: str
    alarm: str = "no alarm"
    channels: list[SpanChannel] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.alarm.strip().upper() in {"OK", "NO ALARM", "NO ALARMS", ""}

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "identifier": self.identifier,
            "description": self.description,
            "alarm": self.alarm,
            "healthy": self.healthy,
            "channel_count": len(self.channels),
            "channels": [channel.as_dict() for channel in self.channels],
        }


def describe_card(device_identifier: int) -> tuple[str, str, str]:
    """Return the model name, driver module, and plain language description."""
    return _CARD_CATALOGUE.get(
        device_identifier,
        (
            f"an unrecognised Digium interface card bearing the device identifier "
            f"{device_identifier:04x}",
            "dahdi",
            "the card is a Digium device that this appliance does not have a "
            "catalogue entry for; the generic interface driver will be attempted",
        ),
    )


def parse_span_document(text: str) -> Span | None:
    """Parse one span document as exported by the interface driver.

    The document format is a header line naming the span, followed by one
    indented line per channel.  Anything that does not match is ignored rather
    than raising, because driver versions differ in their trailing annotations
    and a cosmetic difference must not blind the dashboard to a working span.
    """
    span: Span | None = None
    for line in text.splitlines():
        if not line.strip():
            continue

        header = _SPAN_HEADER.match(line.strip()) or _SPAN_HEADER.match(line)
        if header is not None:
            alarm = _classify_annotations(header.group("trailer"))
            span = Span(
                number=int(header.group("number")),
                identifier=header.group("identifier"),
                description=header.group("description").strip(),
                alarm=alarm,
            )
            continue

        if span is None:
            continue

        channel = _CHANNEL_LINE.match(line)
        if channel is None:
            continue
        trailer = (channel.group("trailer") or "").strip()
        status = trailer.strip("() ").strip() or "idle"
        span.channels.append(
            SpanChannel(
                number=int(channel.group("number")),
                name=channel.group("name"),
                signalling=channel.group("signalling") or "unknown",
                status=status,
            )
        )
    return span


class HardwareInventory:
    """Read only enumeration of interface cards and spans."""

    def __init__(self, root: str | Path = "/") -> None:
        self.root = Path(root)
        self.last_error: str | None = None

    # -- peripheral bus ----------------------------------------------------

    def enumerate_cards(self) -> list[InterfaceCard]:
        """Return every Digium interface card present on the peripheral bus."""
        devices = self.root / "sys/bus/pci/devices"
        if not devices.is_dir():
            self.last_error = "the peripheral bus inventory is not available on this machine"
            return []

        cards: list[InterfaceCard] = []
        try:
            entries = sorted(devices.iterdir())
        except OSError as error:
            self.last_error = str(error)
            return []

        for entry in entries:
            vendor = _read_identifier(entry / "vendor")
            if vendor != DIGIUM_VENDOR_IDENTIFIER:
                continue
            device = _read_identifier(entry / "device")
            if device is None:
                continue

            model, module, description = describe_card(device)
            cards.append(
                InterfaceCard(
                    slot=entry.name,
                    vendor_identifier=vendor,
                    device_identifier=device,
                    model=model,
                    driver_module=module,
                    description=description,
                    driver_bound=_read_bound_driver(entry),
                )
            )

        self.last_error = None
        return cards

    # -- driver exported spans ---------------------------------------------

    def enumerate_spans(self) -> list[Span]:
        """Return every span the interface driver currently exports."""
        directory = self.root / "proc/dahdi"
        if not directory.is_dir():
            return []

        spans: list[Span] = []
        try:
            documents = sorted(
                (item for item in directory.iterdir() if item.name.isdigit()),
                key=lambda item: int(item.name),
            )
        except OSError as error:
            self.last_error = str(error)
            return []

        for document in documents:
            try:
                text = document.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            span = parse_span_document(text)
            if span is not None:
                spans.append(span)
        return spans

    def driver_loaded(self) -> bool:
        """Report whether the interface driver is present in the kernel."""
        modules = self.root / "proc/modules"
        if not modules.is_file():
            return False
        try:
            content = modules.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return any(line.split(" ")[0] == "dahdi" for line in content.splitlines())

    # -- combined ----------------------------------------------------------

    def scan(self) -> dict[str, Any]:
        """Produce the complete hardware inventory for the interface layer."""
        cards = self.enumerate_cards()
        spans = self.enumerate_spans()
        channel_count = sum(len(span.channels) for span in spans)
        alarmed = [span for span in spans if not span.healthy]

        if not cards:
            summary = (
                "no legacy Digium interface card was detected on this machine; the "
                "appliance is running without telephony hardware"
            )
        else:
            summary = (
                f"{len(cards)} Digium interface card or cards detected, exporting "
                f"{len(spans)} span or spans and {channel_count} channel or channels"
            )

        return {
            "cards": [card.as_dict() for card in cards],
            "spans": [span.as_dict() for span in spans],
            "card_count": len(cards),
            "span_count": len(spans),
            "channel_count": channel_count,
            "alarmed_span_count": len(alarmed),
            "driver_loaded": self.driver_loaded(),
            "hardware_present": bool(cards),
            "summary": summary,
            "last_error": self.last_error,
        }


def _classify_annotations(trailer: str) -> str:
    """Separate genuine span alarms from role annotations.

    The whole trailer is read, not only the parenthesised part of it, and that
    distinction is the entire point of this function.

    The driver writes the span's role in parentheses and its alarm bare:

        Span 1: TE4/0/1 "T4XXP (PCI) Card 0 Span 1" (MASTER) HDB3/CCS/CRC4 RED

    An earlier version scanned only inside parentheses.  It therefore saw
    ``MASTER``, classified it as a role, and reported "no alarm" -- on a span
    in RED alarm, which is a span with no line on it.  A technician fitting a
    card saw every span green and concluded the cabling was good.  An appliance
    that asserts health on a dead line is worse than one that says nothing.

    The set of alarm tokens below existed the whole time and was never
    consulted, and the test fixture wrote the alarm inside parentheses, so the
    suite agreed with the defect.  Both are corrected.

    An unrecognised token is reported as an alarm, because failing towards
    visibility is the correct bias for a fault indicator.
    """
    text = trailer or ""
    tokens: list[str] = []

    # Inside parentheses first, then everything outside them, so that a role
    # marker and a bare alarm on the same line are both seen.
    for annotation in _ANNOTATION.findall(text):
        tokens.extend(part.strip().upper() for part in annotation.split(",") if part.strip())

    outside = _ANNOTATION.sub(" ", text)
    for part in re.split(r"[\s,]+", outside):
        cleaned = part.strip().upper()
        if cleaned:
            tokens.append(cleaned)

    # The signalling description is not a health report.  It names the line
    # coding and framing the span was configured with, and it appears on every
    # healthy span, so it is not evidence of anything.
    alarms = [
        token
        for token in tokens
        if token not in _ROLE_TOKENS and not _SIGNALLING_DESCRIPTION.match(token)
    ]
    if not alarms:
        return "no alarm"

    # Ordered so the reader sees the same words the driver used, without
    # repeating one that appeared both inside and outside the parentheses.
    seen: set[str] = set()
    ordered = [token for token in alarms if not (token in seen or seen.add(token))]
    return " ".join(ordered)


def _read_identifier(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def _read_bound_driver(entry: Path) -> str | None:
    link = entry / "driver"
    try:
        if link.is_symlink():
            return Path(link.readlink()).name
        if link.exists():
            return link.resolve().name
    except OSError:
        return None
    return None


def iterate_catalogue() -> Iterable[tuple[int, str, str, str]]:
    """Yield the recognised card catalogue.  Used by documentation and tests."""
    for identifier, (model, module, description) in sorted(_CARD_CATALOGUE.items()):
        yield identifier, model, module, description
