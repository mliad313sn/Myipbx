"""Telephony objects: their schema, their validation, and their storage.

The product's central claim is that every operation can be performed from the
graphical interface. The way that claim is kept true over time is this file:
one declarative schema that the server validates against and that the interface
generates its forms from. Neither side can drift from the other, because
neither side has its own copy of what a valid extension looks like.

Adding a field here makes it appear in the interface, makes it validated on the
way in, and makes it render into the engine configuration. There is no fourth
place to remember.

Secrets are deliberately not held in the source of truth document. They live in
a separate owner readable file and are never returned by any read path, so a
configuration export, a backup inspection, or a screenshot cannot disclose one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import addresses
from .logging_setup import get_logger

#: Readings a field may name. Each one is the single place this appliance
#: decides what a piece of text means, so that no two components can disagree
#: about the same value.
_READINGS = {
    "network": addresses.parse_network,
    "address": addresses.parse_address,
}

__all__ = [
    "Field",
    "EntitySpec",
    "ENTITY_SPECS",
    "ValidationError",
    "EntityStore",
    "SecretStore",
]

_LOG = get_logger("entities")

_NUMBER_PATTERN = re.compile(r"^[0-9]{1,10}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
#: A dialled prefix a rate covers. Empty is permitted and means "everything no
#: other tariff claims", which is what gives a rate table a floor.
_PREFIX_PATTERN = re.compile(r"^(\+?[0-9]{1,15})?$")
#: An amount of money as it is typed. Narrow on purpose: a comma is a thousands
#: separator in one country and a decimal point in another, and a pattern that
#: accepted both would silently misread one of them by a factor of a thousand.
_AMOUNT_PATTERN = re.compile(r"^[0-9]{1,9}(\.[0-9]{1,6})?$")
#: A currency written as it is said rather than as a symbol, because every
#: figure on these screens is read out in words.
_CURRENCY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z ]{0,23}$")
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")
_PATTERN_PATTERN = re.compile(r"^[0-9NXZ._\[\]!+*-]{1,32}$")
_ELECTRONIC_MAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
#: A menu's options, written as a comma separated list of key equals
#: destination pairs -- for example "one=two hundred one" is written 1=201,2=600
#: because both sides are dialled values rather than prose.
_MAP_PATTERN = re.compile(r"^\s*[0-9*#]\s*=\s*[A-Za-z0-9_-]+\s*(,\s*[0-9*#]\s*=\s*[A-Za-z0-9_-]+\s*)*$")
_SOUND_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_-]{0,127}$")
#: A network in prefix notation, or the word naming everywhere.
#:
#: This is the coarse shape only. The pattern accepted four groups of up to
#: three digits and checked nothing about them, so nine hundred ninety-nine dot
#: one dot one dot one saved into the source of truth here and was refused
#: later, at render time, by the firewall with a different message from a
#: different component. The reading that decides is the shared one below, so
#: what is accepted on the way in is what can be rendered on the way out.
_SOURCE_PATTERN = re.compile(r"^(any|anywhere|(\d{1,3}\.){3}\d{1,3}(/([0-9]|[12][0-9]|3[0-2]))?)$", re.IGNORECASE)


class ValidationError(ValueError):
    """One or more submitted values were not acceptable.

    Carries a field keyed mapping so that the interface can mark the offending
    input rather than showing one message about the whole form.
    """

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        summary = "; ".join(f"{name}: {reason}" for name, reason in self.errors.items())
        super().__init__(summary or "the submission was not acceptable")


@dataclass(frozen=True)
class Field:
    """One field of one telephony object."""

    name: str
    label: str
    kind: str = "text"          # text, number, secret, boolean, choice, list, time, map
    required: bool = False
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    pattern_help: str = ""
    maximum: int | None = None
    minimum: int | None = None
    #: A secret is accepted on the way in and never returned on the way out.
    secret: bool = False
    #: Names another entity kind whose members this field must reference.
    references: str | None = None
    #: Named check applied after the pattern, for a value whose validity
    #: cannot be written as a pattern without disagreeing with the component
    #: that will act on it later.
    reading: str = ""
    #: True when the value is a name rather than a quantity -- a number that is
    #: dialled, matched, or read aloud digit by digit. The spelling rule turns
    #: quantities into words, and a quantity is something you could add one to.
    #: You cannot add one to an extension: two hundred forty-one is not a
    #: telephone, 241 is, and an operator reading "two billion fifteen million
    #: five hundred fifty thousand one hundred" cannot dial it back. Fields
    #: marked here keep their digits wherever they are displayed.
    identifier: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "default": self.default,
            "help": self.help,
            "choices": list(self.choices),
            "references": self.references,
            "secret": self.secret,
            "identifier": self.identifier,
        }


@dataclass(frozen=True)
class EntitySpec:
    """One kind of telephony object."""

    kind: str
    singular: str
    plural: str
    key: str
    description: str
    fields: tuple[Field, ...]
    #: Other kinds that may refer to this one, checked before a deletion.
    referenced_by: tuple[tuple[str, str], ...] = ()

    def field(self, name: str) -> Field | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "singular": self.singular,
            "plural": self.plural,
            "key": self.key,
            "description": self.description,
            "fields": [item.as_dict() for item in self.fields],
        }


_DESTINATION_KINDS = (
    "extension", "ring group", "queue", "menu", "conference room",
    "voicemail", "hang up",
)

ENTITY_SPECS: dict[str, EntitySpec] = {}


def _register(spec: EntitySpec) -> EntitySpec:
    ENTITY_SPECS[spec.kind] = spec
    return spec


_register(
    EntitySpec(
        kind="extensions",
        singular="extension",
        plural="extensions",
        key="number",
        description="a telephone on this system",
        fields=(
            # A dial string, not a quantity: kept as text so that a leading
            # zero survives, which an integer would silently discard.
            Field("number", "extension number", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="an extension number is one to ten digits",
                  help="the number a caller dials to reach this telephone"),
            Field("name", "display name", required=True, pattern=_NAME_PATTERN,
                  pattern_help="a display name uses letters, digits, spaces, and the marks period, underscore, and hyphen",
                  help="shown to other telephones on this system"),
            Field("secret", "password", "secret", secret=True,
                  help="set once; it is stored as a secret and never shown again"),
            Field("technology", "technology", "choice", default="PJSIP",
                  choices=("PJSIP", "SIP", "DAHDI"),
                  help="the channel technology this telephone registers with"),
            Field("voicemail", "voicemail", "boolean", default=True,
                  help="whether unanswered calls are offered a mailbox"),
            Field("voicemail_password", "voicemail password", "secret", secret=True,
                  help="the code the user enters to collect messages"),
            Field("electronic_mail", "electronic mail address", pattern=_ELECTRONIC_MAIL_PATTERN,
                  pattern_help="an electronic mail address must contain one at sign and a domain",
                  help="messages are announced to this address when set"),
            Field("ring_seconds", "ring time", "number", default=20, minimum=5, maximum=300,
                  help="how long this telephone rings before the call moves on"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
        referenced_by=(("ring_groups", "members"), ("inbound_routes", "destination_value")),
    )
)

_register(
    EntitySpec(
        kind="trunks",
        singular="trunk",
        plural="trunks",
        key="name",
        description="a connection to a carrier or to another system",
        fields=(
            Field("name", "trunk name", required=True, pattern=_IDENTIFIER_PATTERN,
                  pattern_help="a trunk name uses letters, digits, and the marks period, underscore, and hyphen",
                  help="how this trunk is named throughout the appliance"),
            Field("technology", "technology", "choice", default="PJSIP",
                  choices=("PJSIP", "SIP", "DAHDI"),
                  help="the channel technology used to reach the carrier"),
            Field("host", "carrier address", required=True, pattern=_HOST_PATTERN,
                  pattern_help="a carrier address is a host name or an address",
                  help="the carrier's host name or address"),
            Field("username", "account name", pattern=_IDENTIFIER_PATTERN,
                  pattern_help="an account name uses letters, digits, and the marks period, underscore, and hyphen",
                  help="the account name the carrier issued"),
            Field("secret", "account password", "secret", secret=True,
                  help="stored as a secret and never shown again"),
            Field("register", "register with the carrier", "boolean", default=True,
                  help="whether this appliance registers, or the carrier trusts the address"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
        referenced_by=(("outbound_routes", "trunk"),),
    )
)

_register(
    EntitySpec(
        kind="ring_groups",
        singular="ring group",
        plural="ring groups",
        key="number",
        description="a set of telephones that ring together",
        fields=(
            # A dial string, not a quantity; see the note on extensions.
            Field("number", "group number", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a group number is one to ten digits"),
            Field("name", "description", required=True, pattern=_NAME_PATTERN,
                  pattern_help="a description uses letters, digits, spaces, and the marks period, underscore, and hyphen"),
            Field("members", "members", "list", required=True, identifier=True,
                  references="extensions",
                  help="the extensions that ring when this group is called"),
            Field("strategy", "ring strategy", "choice", default="ring all",
                  choices=("ring all", "in order", "least recently called"),
                  help="whether every telephone rings at once or one at a time"),
            Field("ring_seconds", "ring time", "number", default=25, minimum=5, maximum=300),
            Field("enabled", "enabled", "boolean", default=True),
        ),
        referenced_by=(("inbound_routes", "destination_value"),),
    )
)

_register(
    EntitySpec(
        kind="inbound_routes",
        singular="inbound route",
        plural="inbound routes",
        key="did",
        description="where a call arriving from a carrier is sent",
        fields=(
            Field("did", "number dialled", required=True, identifier=True,
                  pattern=_PATTERN_PATTERN,
                  pattern_help="a dialled number may contain digits and the pattern marks N, X, Z, period, and brackets",
                  help="the number the caller dialled; use the pattern mark period to match anything"),
            Field("description", "description", pattern=_NAME_PATTERN,
                  pattern_help="a description uses letters, digits, spaces, and the marks period, underscore, and hyphen"),
            Field("destination_kind", "send the call to", "choice", required=True,
                  default="extension", choices=_DESTINATION_KINDS),
            Field("destination_value", "destination", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a destination is the number of an extension or a ring group",
                  help="the extension or ring group number that answers"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
    )
)

_register(
    EntitySpec(
        kind="outbound_routes",
        singular="outbound route",
        plural="outbound routes",
        key="name",
        description="which trunk carries a call to a dialled number",
        fields=(
            Field("name", "route name", required=True, pattern=_IDENTIFIER_PATTERN,
                  pattern_help="a route name uses letters, digits, and the marks period, underscore, and hyphen"),
            Field("pattern", "dialled pattern", required=True, identifier=True,
                  pattern=_PATTERN_PATTERN,
                  pattern_help="a pattern may contain digits and the pattern marks N, X, Z, period, and brackets",
                  help="the pattern a dialled number must match for this route to carry it"),
            Field("trunk", "carried by", "choice", required=True, references="trunks",
                  help="the trunk this route sends the call to"),
            Field("strip_digits", "digits to remove", "number", default=0, minimum=0, maximum=20,
                  help="how many leading digits to remove before dialling"),
            Field("prepend_digits", "digits to add", identifier=True,
                  pattern=re.compile(r"^[0-9+]{0,16}$"),
                  pattern_help="digits to add may contain only digits and a leading plus sign",
                  help="digits placed in front of the number before dialling"),
            Field("priority", "order", "number", default=10, minimum=1, maximum=999,
                  help="routes are tried in this order; the lowest number is tried first"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
    )
)

_register(
    EntitySpec(
        kind="time_conditions",
        singular="time condition",
        plural="time conditions",
        key="name",
        description="sends calls to different places inside and outside business hours",
        fields=(
            Field("name", "condition name", required=True, pattern=_IDENTIFIER_PATTERN,
                  pattern_help="a condition name uses letters, digits, and the marks period, underscore, and hyphen"),
            Field("starts_at", "opens at", "time", required=True, default="09:00",
                  pattern=_TIME_PATTERN,
                  pattern_help="a time is written as hours and minutes separated by a colon"),
            Field("ends_at", "closes at", "time", required=True, default="17:30",
                  pattern=_TIME_PATTERN,
                  pattern_help="a time is written as hours and minutes separated by a colon"),
            Field("days", "days", "list", required=True,
                  default=["mon", "tue", "wed", "thu", "fri"],
                  choices=("mon", "tue", "wed", "thu", "fri", "sat", "sun")),
            Field("open_destination", "when open, send to", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a destination is the number of an extension or a ring group"),
            Field("closed_destination", "when closed, send to", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a destination is the number of an extension or a ring group"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
    )
)


_register(
    EntitySpec(
        kind="ivr_menus",
        singular="menu",
        plural="menus",
        key="number",
        description="plays a greeting and sends the caller where they choose",
        fields=(
            Field("number", "menu number", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a menu number is one to ten digits"),
            Field("name", "description", required=True, pattern=_NAME_PATTERN,
                  pattern_help="a description uses letters, digits, spaces, and the marks period, underscore, and hyphen"),
            Field("greeting", "greeting recording", default="vm-enter-num-to-call",
                  pattern=_SOUND_PATTERN,
                  pattern_help="a recording name uses letters, digits, and the marks slash, underscore, and hyphen",
                  help="the recording played when the caller arrives"),
            Field("options", "options", "map", required=True, identifier=True,
                  pattern=_MAP_PATTERN,
                  pattern_help="options are written as a key, an equals sign, and a destination, separated by commas",
                  help="what each key the caller presses leads to, such as one equals an extension"),
            Field("wait_seconds", "wait time", "number", default=10, minimum=1, maximum=60,
                  help="how long to wait for the caller to choose"),
            Field("timeout_destination", "if nobody chooses, send to", identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a destination is the number of an extension, a group, or a queue",
                  help="left empty, the call is hung up"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
        referenced_by=(("inbound_routes", "destination_value"),),
    )
)

_register(
    EntitySpec(
        kind="queues",
        singular="queue",
        plural="queues",
        key="number",
        description="holds callers in order until somebody is free to answer",
        fields=(
            Field("number", "queue number", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a queue number is one to ten digits"),
            Field("name", "description", required=True, pattern=_NAME_PATTERN,
                  pattern_help="a description uses letters, digits, spaces, and the marks period, underscore, and hyphen"),
            Field("members", "who answers", "list", required=True, identifier=True,
                  references="extensions",
                  help="the extensions that take calls from this queue"),
            Field("strategy", "how calls are offered", "choice", default="ring all",
                  choices=("ring all", "least recent", "fewest calls", "random",
                           "round robin memory"),
                  help="which member a waiting call is offered to next"),
            Field("ring_seconds", "ring time", "number", default=20, minimum=5, maximum=300,
                  help="how long one member rings before the call moves on"),
            Field("maximum_waiting", "most callers waiting", "number", default=0,
                  minimum=0, maximum=999,
                  help="callers beyond this are sent to the overflow destination; zero means no limit"),
            # The promise this queue is reported against. Per queue rather than
            # global, because a switchboard and an out-of-hours line are not
            # held to the same one.
            Field("service_level_seconds", "answer within", "number", default=20,
                  minimum=1, maximum=600,
                  help="the queue report says what share of answered calls were "
                       "picked up inside this many seconds"),
            Field("overflow_destination", "when full or timed out, send to", identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a destination is the number of an extension, a group, or a menu"),
            Field("music_class", "music while waiting", default="default",
                  pattern=_SOUND_PATTERN,
                  pattern_help="a music class name uses letters, digits, and the marks slash, underscore, and hyphen"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
        referenced_by=(("inbound_routes", "destination_value"),),
    )
)

_register(
    EntitySpec(
        kind="conferences",
        singular="conference room",
        plural="conference rooms",
        key="number",
        description="a room several callers can be in at once",
        fields=(
            Field("number", "room number", required=True, identifier=True,
                  pattern=_NUMBER_PATTERN,
                  pattern_help="a room number is one to ten digits"),
            Field("name", "description", required=True, pattern=_NAME_PATTERN,
                  pattern_help="a description uses letters, digits, spaces, and the marks period, underscore, and hyphen"),
            Field("pin", "entry code", "secret", secret=True,
                  help="callers must enter this to join; leave empty for an open room"),
            Field("announce_arrivals", "announce arrivals and departures", "boolean",
                  default=True),
            Field("music_class", "music while alone", default="default",
                  pattern=_SOUND_PATTERN,
                  pattern_help="a music class name uses letters, digits, and the marks slash, underscore, and hyphen"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
        referenced_by=(("inbound_routes", "destination_value"),),
    )
)


_register(
    EntitySpec(
        kind="tariffs",
        singular="tariff",
        plural="tariffs",
        key="name",
        description=(
            "what a call to a range of numbers costs, so the reports can total it"
        ),
        fields=(
            Field("name", "tariff name", required=True, pattern=_IDENTIFIER_PATTERN,
                  pattern_help="a tariff name uses letters, digits, and the marks period, underscore, and hyphen",
                  help="how this rate is named in the reports"),
            # The prefix is dialled digits, so it is an identifier and keeps
            # them. Left empty it covers everything no other tariff claims,
            # which is how a table is given a floor.
            Field("prefix", "numbers beginning", identifier=True,
                  pattern=_PREFIX_PATTERN,
                  pattern_help="a prefix is up to fifteen digits, or the plus sign followed by digits",
                  help="the longest matching prefix wins; leave this empty for the "
                       "rate that covers everything no other tariff claims"),
            Field("currency", "currency", required=True, default="pounds",
                  pattern=_CURRENCY_PATTERN,
                  pattern_help="a currency is one or two words of letters, written as it should be read aloud",
                  help="written as it is said rather than as a symbol, because "
                       "every figure on these screens is read out in words"),
            Field("connection_fee", "charge to connect", default="0",
                  pattern=_AMOUNT_PATTERN,
                  pattern_help="an amount is digits, optionally with a decimal point and up to six places",
                  help="charged once when the call is answered"),
            Field("per_minute", "charge a minute", default="0",
                  pattern=_AMOUNT_PATTERN,
                  pattern_help="an amount is digits, optionally with a decimal point and up to six places",
                  help="charged for the conversation, billed in whole increments"),
            Field("increment_seconds", "billed in blocks of", "number", default=60,
                  minimum=1, maximum=3600,
                  help="a carrier selling by the minute charges a whole minute "
                       "for a call of four seconds; set this to one to bill by the second"),
            Field("minimum_seconds", "shortest charged call", "number", default=0,
                  minimum=0, maximum=3600,
                  help="a call shorter than this is charged as though it lasted this long"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
    )
)


_register(
    EntitySpec(
        kind="firewall_rules",
        singular="firewall rule",
        plural="firewall rules",
        key="name",
        description="which service is reachable, and from where",
        fields=(
            Field("name", "rule name", required=True, pattern=_IDENTIFIER_PATTERN,
                  pattern_help="a rule name uses letters, digits, and the marks period, underscore, and hyphen"),
            Field("service", "service", "choice", required=True,
                  choices=("session protocol", "secure session protocol", "media",
                           "secure shell", "name resolution"),
                  help="what this rule opens; the console itself is always reachable"),
            Field("source", "reachable from", required=True, default="any",
                  pattern=_SOURCE_PATTERN, reading="network",
                  pattern_help="a source is a network in prefix notation, or the word any",
                  help="name the networks that need it rather than opening it to everywhere"),
            Field("enabled", "enabled", "boolean", default=True),
        ),
    )
)


class SecretStore:
    """Secrets, held apart from the configuration and never read back out."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _LOG.error("the secret store could not be read")
            return {}
        return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}

    def _save(self, secrets: Mapping[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".partial")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(secrets), indent=2, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    @staticmethod
    def reference(kind: str, key: str, field_name: str) -> str:
        return f"{kind}:{key}:{field_name}"

    def set(self, kind: str, key: str, field_name: str, value: str) -> None:
        secrets = self._load()
        secrets[self.reference(kind, key, field_name)] = value
        self._save(secrets)

    def get(self, kind: str, key: str, field_name: str) -> str | None:
        """Read a secret. Used only when rendering engine configuration."""
        return self._load().get(self.reference(kind, key, field_name))

    def has(self, kind: str, key: str, field_name: str) -> bool:
        return self.reference(kind, key, field_name) in self._load()

    def forget(self, kind: str, key: str) -> int:
        secrets = self._load()
        prefix = f"{kind}:{key}:"
        removed = [name for name in secrets if name.startswith(prefix)]
        for name in removed:
            del secrets[name]
        if removed:
            self._save(secrets)
        return len(removed)

    def rename(self, kind: str, old_key: str, new_key: str) -> None:
        if old_key == new_key:
            return
        secrets = self._load()
        prefix = f"{kind}:{old_key}:"
        moved = {
            f"{kind}:{new_key}:{name[len(prefix):]}": value
            for name, value in secrets.items()
            if name.startswith(prefix)
        }
        if not moved:
            return
        for name in list(secrets):
            if name.startswith(prefix):
                del secrets[name]
        secrets.update(moved)
        self._save(secrets)


@dataclass
class EntityStore:
    """Create, read, update, and delete telephony objects.

    The store operates on the source of truth document. It does not write it —
    the caller saves, so that a batch of changes lands atomically.

    Secrets are held back the same way, and for the same reason. They used to
    be written to disk the moment a record was validated, before the document
    that refers to them was saved. When the save then failed — a full disk, a
    document the guard refused, a permission that had changed — the secret
    stayed behind with nothing pointing at it: a carrier's password or a
    telephone's, never displayed, never reachable, never cleaned up, and
    carried into every backup taken afterwards. The mirror case was worse: a
    deletion forgot the secret first, so a failed save left the record in place
    having silently lost its password.

    The pending operations below are applied by ``commit_secrets``, which the
    caller invokes only once the document is safely written.
    """

    document: dict[str, Any]
    secrets: SecretStore | None = None
    _errors: dict[str, str] = field(default_factory=dict)
    #: Secret operations earned by the changes made so far, not yet applied.
    #: Each is a callable taking the secret store.
    _pending_secrets: list[Callable[[SecretStore], None]] = field(default_factory=list)
    #: Which secrets those operations will set and clear, as (kind, key, name).
    #: A record has to report a password the operator has just typed as set,
    #: even though it is deliberately not on disk until the document is.
    _pending_set: set[tuple[str, str, str]] = field(default_factory=set)
    _pending_forgotten: set[tuple[str, str]] = field(default_factory=set)

    def commit_secrets(self) -> int:
        """Apply the held secret operations. Call only after the save."""
        if not self.secrets:
            self._pending_secrets.clear()
            return 0
        applied = 0
        for operation in self._pending_secrets:
            operation(self.secrets)
            applied += 1
        self._pending_secrets.clear()
        self._pending_set.clear()
        self._pending_forgotten.clear()
        return applied

    def discard_secrets(self) -> int:
        """Throw the held operations away, because the save did not happen."""
        count = len(self._pending_secrets)
        self._pending_secrets.clear()
        self._pending_set.clear()
        self._pending_forgotten.clear()
        return count

    # -- reading -----------------------------------------------------------

    def list(self, kind: str) -> list[dict[str, Any]]:
        spec = self._spec(kind)
        records = self.document.get(kind) or []
        return [self._present(spec, dict(record)) for record in records]

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        spec = self._spec(kind)
        for record in self.document.get(kind) or []:
            if str(record.get(spec.key, "")) == str(key):
                return self._present(spec, dict(record))
        return None

    def _present(self, spec: EntitySpec, record: dict[str, Any]) -> dict[str, Any]:
        """Prepare a record for the interface, withholding every secret."""
        key = str(record.get(spec.key, ""))
        for item in spec.fields:
            if not item.secret:
                continue
            record.pop(item.name, None)
            configured = bool(
                self.secrets and self.secrets.has(spec.kind, key, item.name)
            )
            if (spec.kind, key) in self._pending_forgotten:
                configured = False
            if (spec.kind, key, item.name) in self._pending_set:
                configured = True
            record[f"{item.name}_configured"] = configured
        return record

    # -- writing -----------------------------------------------------------

    def create(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        spec = self._spec(kind)
        cleaned, secrets = self._validate(spec, payload, existing_key=None)

        key = str(cleaned[spec.key])
        if self._find_index(spec, key) is not None:
            raise ValidationError(
                {spec.key: f"a {spec.singular} with that value already exists"}
            )

        self.document.setdefault(kind, []).append(cleaned)
        self._store_secrets(spec, key, secrets)
        _LOG.info("a %s identified as %s was created", spec.singular, key)
        return self._present(spec, dict(cleaned))

    def update(self, kind: str, key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        spec = self._spec(kind)
        index = self._find_index(spec, key)
        if index is None:
            raise KeyError(f"there is no {spec.singular} identified as {key}")

        existing = dict((self.document.get(kind) or [])[index])
        merged = {**existing, **dict(payload)}
        cleaned, secrets = self._validate(spec, merged, existing_key=str(key))

        new_key = str(cleaned[spec.key])
        if new_key != str(key) and self._find_index(spec, new_key) is not None:
            raise ValidationError(
                {spec.key: f"a {spec.singular} with that value already exists"}
            )

        self.document[kind][index] = cleaned
        if new_key != str(key):
            if self.secrets:
                self._pending_secrets.append(
                    lambda store, kind=spec.kind, old=str(key), new=new_key:
                    store.rename(kind, old, new)
                )
            self._repoint_references(spec, str(key), new_key)
        self._store_secrets(spec, new_key, secrets)
        _LOG.info("the %s identified as %s was updated", spec.singular, new_key)
        return self._present(spec, dict(cleaned))

    def delete(self, kind: str, key: str) -> bool:
        spec = self._spec(kind)
        index = self._find_index(spec, key)
        if index is None:
            return False

        blocking = self._references_to(spec, str(key))
        if blocking:
            raise ValidationError(
                {
                    spec.key: (
                        f"this {spec.singular} is still used by "
                        + ", ".join(blocking)
                        + "; change or remove those first"
                    )
                }
            )

        del self.document[kind][index]
        if self.secrets:
            self._pending_secrets.append(
                lambda store, kind=spec.kind, key=str(key): store.forget(kind, key)
            )
            self._pending_forgotten.add((spec.kind, str(key)))
        _LOG.info("the %s identified as %s was deleted", spec.singular, key)
        return True

    # -- validation --------------------------------------------------------

    def _validate(
        self, spec: EntitySpec, payload: Mapping[str, Any], existing_key: str | None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        cleaned: dict[str, Any] = {}
        secrets: dict[str, str] = {}
        errors: dict[str, str] = {}

        for item in spec.fields:
            raw = payload.get(item.name, None)

            if item.secret:
                # An absent secret on an update means "leave it as it was".
                if raw is not None and str(raw) != "":
                    text = str(raw)
                    if len(text) < 8:
                        errors[item.name] = "a password must be at least eight characters"
                    elif len(text) > 128:
                        errors[item.name] = "a password may be at most one hundred twenty-eight characters"
                    else:
                        secrets[item.name] = text
                elif item.required and existing_key is None:
                    errors[item.name] = "a password is required"
                continue

            if raw is None or raw == "":
                if item.required:
                    errors[item.name] = f"{item.label} is required"
                    continue
                if item.default is not None:
                    cleaned[item.name] = (
                        list(item.default) if isinstance(item.default, list) else item.default
                    )
                continue

            try:
                cleaned[item.name] = self._coerce(spec, item, raw)
            except ValidationError as error:
                errors.update(error.errors)

        if errors:
            raise ValidationError(errors)
        return cleaned, secrets

    def _coerce(self, spec: EntitySpec, item: Field, raw: Any) -> Any:
        if item.kind == "boolean":
            if isinstance(raw, bool):
                return raw
            lowered = str(raw).strip().lower()
            if lowered in ("true", "yes", "on", "1"):
                return True
            if lowered in ("false", "no", "off", "0"):
                return False
            raise ValidationError({item.name: f"{item.label} must be yes or no"})

        if item.kind == "number":
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                raise ValidationError({item.name: f"{item.label} must be a whole number"}) from None
            if item.minimum is not None and value < item.minimum:
                raise ValidationError(
                    {item.name: f"{item.label} is below the smallest permitted value"}
                )
            if item.maximum is not None and value > item.maximum:
                raise ValidationError(
                    {item.name: f"{item.label} is above the largest permitted value"}
                )
            if item.pattern is not None and not item.pattern.match(str(value)):
                raise ValidationError({item.name: item.pattern_help or f"{item.label} is not valid"})
            return value

        if item.kind == "list":
            values = raw if isinstance(raw, (list, tuple)) else [
                part.strip() for part in str(raw).split(",") if part.strip()
            ]
            values = [str(value).strip() for value in values if str(value).strip()]
            if not values and item.required:
                raise ValidationError({item.name: f"{item.label} needs at least one entry"})
            if item.choices:
                unknown = [value for value in values if value not in item.choices]
                if unknown:
                    raise ValidationError(
                        {item.name: f"{item.label} contains an unrecognised entry"}
                    )
            if item.references:
                missing = [value for value in values if not self._reference_exists(item.references, value)]
                if missing:
                    raise ValidationError(
                        {
                            item.name: (
                                f"{item.label} refers to something that does not exist: "
                                + ", ".join(missing)
                            )
                        }
                    )
            return values

        if item.kind == "map":
            text = str(raw).strip()
            if item.pattern is not None and not item.pattern.match(text):
                raise ValidationError({item.name: item.pattern_help or f"{item.label} is not valid"})
            # A key may appear only once, or the dialplan would silently take
            # whichever entry happened to be rendered first.
            keys = [pair.split("=", 1)[0].strip() for pair in text.split(",")]
            if len(keys) != len(set(keys)):
                raise ValidationError(
                    {item.name: f"{item.label} names the same key more than once"}
                )
            return ",".join(
                f"{pair.split('=', 1)[0].strip()}={pair.split('=', 1)[1].strip()}"
                for pair in text.split(",")
            )

        text = str(raw).strip()

        if item.kind == "choice" and item.choices and text not in item.choices:
            raise ValidationError({item.name: f"{item.label} is not one of the permitted values"})

        if item.references and not self._reference_exists(item.references, text):
            raise ValidationError(
                {item.name: f"{item.label} refers to something that does not exist"}
            )

        if item.pattern is not None and not item.pattern.match(text):
            raise ValidationError({item.name: item.pattern_help or f"{item.label} is not valid"})

        if item.reading:
            reader = _READINGS.get(item.reading)
            if reader is not None:
                try:
                    return reader(text)
                except addresses.AddressRefused as error:
                    raise ValidationError({item.name: str(error)}) from error

        return text

    # -- referential integrity ---------------------------------------------

    def _reference_exists(self, kind: str, value: str) -> bool:
        spec = ENTITY_SPECS.get(kind)
        if spec is None:
            return False
        return any(
            str(record.get(spec.key, "")) == str(value)
            for record in self.document.get(kind) or []
        )

    def _references_to(self, spec: EntitySpec, key: str) -> list[str]:
        """Describe everything that would be left dangling by a deletion."""
        blocking: list[str] = []
        for other_kind, field_name in spec.referenced_by:
            other = ENTITY_SPECS.get(other_kind)
            if other is None:
                continue
            for record in self.document.get(other_kind) or []:
                value = record.get(field_name)
                matched = (
                    key in [str(item) for item in value]
                    if isinstance(value, list)
                    else str(value or "") == key
                )
                if matched:
                    blocking.append(
                        f"the {other.singular} identified as {record.get(other.key, 'unnamed')}"
                    )
        return blocking

    def _repoint_references(self, spec: EntitySpec, old_key: str, new_key: str) -> None:
        """Follow a rename through everything that referred to the old value."""
        for other_kind, field_name in spec.referenced_by:
            for record in self.document.get(other_kind) or []:
                value = record.get(field_name)
                if isinstance(value, list):
                    record[field_name] = [
                        new_key if str(item) == old_key else item for item in value
                    ]
                elif str(value or "") == old_key:
                    record[field_name] = new_key

    # -- helpers -----------------------------------------------------------

    def _spec(self, kind: str) -> EntitySpec:
        spec = ENTITY_SPECS.get(kind)
        if spec is None:
            raise KeyError(f"there is no kind of object named {kind}")
        return spec

    def _find_index(self, spec: EntitySpec, key: str) -> int | None:
        for index, record in enumerate(self.document.get(spec.kind) or []):
            if str(record.get(spec.key, "")) == str(key):
                return index
        return None

    def _store_secrets(self, spec: EntitySpec, key: str, secrets: Mapping[str, str]) -> None:
        if not self.secrets:
            return
        for name, value in secrets.items():
            self._pending_secrets.append(
                lambda store, kind=spec.kind, key=key, name=name, value=value:
                store.set(kind, key, name, value)
            )
            self._pending_set.add((spec.kind, key, name))
            self._pending_forgotten.discard((spec.kind, key))


def schema() -> dict[str, Any]:
    """The complete schema, as the interface consumes it to build its forms."""
    return {
        "kinds": [spec.as_dict() for spec in ENTITY_SPECS.values()],
        "explanation": (
            "the interface builds its forms from this schema and the appliance "
            "validates against the same schema, so the two cannot disagree about "
            "what is acceptable"
        ),
    }


def iterate_specs() -> Iterable[EntitySpec]:
    return ENTITY_SPECS.values()
