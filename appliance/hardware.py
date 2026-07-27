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

#: Recognised interface cards, named in plain language for the dashboard.  An
#: identifier absent from this table is still reported as a Digium card; the
#: table improves the description, it does not gate detection.
_CARD_CATALOGUE: dict[int, tuple[str, str, str]] = {
    0x0205: ("Wildcard TDM400P", "wctdm", "a four port analogue interface card"),
    0x8005: ("Wildcard TDM410P", "wctdm24xxp", "a four port analogue interface card"),
    0x8002: ("Wildcard TDM800P", "wctdm24xxp", "an eight port analogue interface card"),
    0x8003: ("Wildcard TDM2400P", "wctdm24xxp", "a twenty four port analogue interface card"),
    0x8000: ("Wildcard TE110P", "wcte11xp", "a single span digital interface card"),
    0x8001: ("Wildcard TE120P", "wcte12xp", "a single span digital interface card"),
    0x0405: ("Wildcard TE405P", "wct4xxp", "a four span digital interface card"),
    0x2400: ("Wildcard TE410P", "wct4xxp", "a four span digital interface card"),
    0x800A: ("Wildcard TE220", "wct4xxp", "a two span digital interface card"),
    0x800C: ("Wildcard TE420", "wct4xxp", "a four span digital interface card"),
    0x1820: ("Wildcard AEX800", "wctdm24xxp", "an eight port analogue interface card"),
}

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

    The driver appends parenthesised annotations to the span header.  Some of
    them are faults and some of them merely describe the span's role, so the
    tokens are classified rather than being taken wholesale as an alarm.  An
    unrecognised token is reported as an alarm, because failing towards
    visibility is the correct bias for a fault indicator.
    """
    tokens: list[str] = []
    for annotation in _ANNOTATION.findall(trailer or ""):
        tokens.extend(part.strip().upper() for part in annotation.split(",") if part.strip())

    alarms = [token for token in tokens if token not in _ROLE_TOKENS]
    if not alarms:
        return "no alarm"
    return " ".join(alarms)


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
