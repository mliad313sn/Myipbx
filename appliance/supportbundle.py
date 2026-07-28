"""Everything a remote engineer needs, in one file, with nothing they must not have.

When an appliance misbehaves at a site nobody can reach, the exchange that
follows is a dozen messages long: what does the hardware section say, what is
in the log, which trunk is it, what does the ruleset actually contain, what
version is this. Each round trip costs a day, and each answer arrives as a
screenshot of part of a screen.

This produces the whole of it at once. An operator downloads one file and
sends it, and the person reading it has the same view of the machine as
somebody standing in front of it.

What is deliberately not in it is as important as what is. The bundle carries
no telephone password, no carrier account password, no administrator
credential, no private key, and no session token, because a support bundle is
the single most casually forwarded file an appliance produces: it goes to a
vendor, into a ticket, onto a shared drive, and it stays there. Everything
placed in it is chosen by name rather than swept up, and the names come from
the schema rather than from a list kept here, so a secret field added to the
schema next year is withheld without anybody remembering to add it. The
redaction is asserted by a test that reads the finished bytes and looks for the
material.

It does carry things a site may consider sensitive in another sense: the
addresses of its carriers, the numbers of its extensions, the shape of its
network. That is the point of it, and the file says so at the top so that
whoever forwards it knows what they are forwarding.
"""

from __future__ import annotations

import io
import json
import platform
import tarfile
import time
from typing import Any

from . import entities, PRODUCT_FULL_NAME
from .logging_setup import get_logger

__all__ = ["create", "REDACTED", "WITHHELD_FIELDS"]

_LOG = get_logger("supportbundle")

#: What a withheld value is replaced by, rather than being dropped. An absent
#: field reads as "this appliance has none", which is a different and more
#: misleading statement than "this was not put in the bundle".
REDACTED = "withheld from the support bundle"

#: Names this module knows about on its own: material that never appears in
#: the telephony schema but does appear in settings, in state, and in whatever
#: a future collector reaches for.
_ALWAYS_WITHHELD = frozenset(
    {
        "password",
        "credential",
        "token",
        "private_key",
        "tls_private_key",
        "cookie",
        "passphrase",
        "api_key",
        "authorization",
    }
)


def _schema_secret_names() -> frozenset[str]:
    """Every field the telephony schema itself marks as a secret.

    Read rather than restated. The first version of this file listed the names
    by hand and its own docstring claimed that a field added elsewhere could
    not arrive here by accident. That was not true: the list happened to match
    the schema on the day it was written, and adding one secret field to the
    schema the following year would have put its value into a bundle destined
    for a vendor's inbox. Asking the schema makes the claim true.
    """
    names = set()
    for spec in entities.ENTITY_SPECS.values():
        for item in spec.fields:
            if item.secret:
                names.add(item.name)
    return frozenset(names)


#: Fields removed wherever they appear, at any depth: everything the schema
#: calls a secret, and everything this module knows to be one.
WITHHELD_FIELDS = _ALWAYS_WITHHELD | _schema_secret_names()

#: How much of each log to take. Enough to see what led to a fault, bounded so
#: that the bundle stays something a person can send in a message.
_LOG_LINES = 400


def _redact(value: Any) -> Any:
    """Walk a structure and replace every withheld field by name."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if key in WITHHELD_FIELDS else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _member(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = int(time.time())
    archive.addfile(info, io.BytesIO(payload))


def _document(archive: tarfile.TarFile, name: str, value: Any) -> None:
    _member(
        archive,
        name,
        json.dumps(_redact(value), indent=2, default=str).encode("utf-8"),
    )


def _safely(producer: Any) -> Any:
    """Run one collector, and let a failure become a note rather than an abort.

    A bundle is asked for when something is already wrong. A collector that
    raises because the thing it reads is the broken thing must not take the
    other twenty with it -- the surviving twenty are what the engineer reads.
    """
    try:
        return producer()
    except Exception as error:  # noqa: BLE001 - a bundle is best effort by design
        return {
            "collected": False,
            "reason": f"this could not be collected: {type(error).__name__}: {error}",
        }


def create(context: Any) -> tuple[bytes, str]:
    """Build the bundle in memory and return it with a file name."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    config = context.config
    collected: list[str] = []

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        _member(
            archive,
            "READ-ME-FIRST.txt",
            (
                f"{PRODUCT_FULL_NAME} -- support bundle\n"
                "\n"
                "This file describes one appliance at one moment. It carries no\n"
                "telephone password, no carrier account password, no\n"
                "administrator credential and no private key: every such field\n"
                "is replaced by the words '" + REDACTED + "'.\n"
                "\n"
                "It does carry the addresses of this site's carriers, the\n"
                "numbers of its extensions, and the shape of its network,\n"
                "because that is what makes it useful. Send it to the people\n"
                "helping you and to nobody else.\n"
                "\n"
                "Produced at " + stamp + " in coordinated universal time.\n"
                "\n"
                "The documents beside this one are machine readable, so the\n"
                "quantities in them are written as numbers rather than spelled\n"
                "out. That is the same rule the appliance's own interface\n"
                "follows: the console spells what it shows a person, and the\n"
                "data underneath stays comparable. Read this file for the\n"
                "sentences and the others for the figures.\n"
            ).encode("utf-8"),
        )

        def add(name: str, producer: Any) -> None:
            _document(archive, name, _safely(producer))
            collected.append(name)

        add(
            "appliance.json",
            lambda: {
                "product": PRODUCT_FULL_NAME,
                "produced_at": stamp,
                "uptime_seconds": max(0, int(time.time() - context.started_at)),
                "python": platform.python_version(),
                "system": platform.platform(),
                "kernel": platform.release(),
                "machine": platform.machine(),
                "host_name": platform.node(),
            },
        )
        add("state.json", lambda: context.state.snapshot())
        add("trunks.json", lambda: context.trunks.snapshot())
        add("tasks.json", lambda: context.tasks.snapshot())
        add("hardware.json", lambda: context.state.hardware)
        add(
            "settings.json",
            lambda: {
                name: getattr(config, name)
                for name in (
                    "listen_address", "listen_port", "tls_enabled",
                    "tls_certificate", "manager_host", "manager_port",
                    "state_directory", "asterisk_configuration_directory",
                    "web_root", "log_file", "session_idle_seconds",
                    "session_lifetime_seconds", "session_maximum",
                    "fail_on_address_allocation_server",
                    "heartbeat_interval_seconds", "health_sweep_interval_seconds",
                )
                if hasattr(config, name)
            },
        )
        add(
            "configuration.json",
            lambda: context.store.load(),
        )
        add(
            "rendered-digests.json",
            lambda: json.loads(
                config.digest_path.read_text(encoding="utf-8")
            ) if config.digest_path.is_file() else {"present": False},
        )
        add(
            "address-allocation.json",
            lambda: {
                "assigns_addresses": False,
                "findings": [
                    finding.as_dict() if hasattr(finding, "as_dict") else str(finding)
                    for finding in (context.audit_findings or [])
                ],
            },
        )
        add(
            "journal.json",
            lambda: {
                "entries": context.journal.recent(limit=200)
                if getattr(context, "journal", None) is not None
                else [],
            },
        )

        # Logs last, because they are the bulk of it.
        logs = _safely(lambda: _collect_logs(context))
        _document(archive, "logs.json", logs)
        collected.append("logs.json")

        _document(
            archive,
            "manifest.json",
            {
                "produced_at": stamp,
                "members": collected,
                "withheld_fields": sorted(WITHHELD_FIELDS),
                "note": (
                    "every field named above is replaced by a fixed phrase "
                    "rather than removed, so that a reader can tell a value "
                    "that was withheld from one this appliance does not have"
                ),
            },
        )

    payload = buffer.getvalue()
    _LOG.info(
        "a support bundle was produced carrying %d document or documents",
        len(collected) + 1,
    )
    return payload, f"crossbar-support-{stamp}.tar.gz"


def _collect_logs(context: Any) -> dict[str, Any]:
    reader = getattr(context, "logs", None)
    if reader is None:
        return {"collected": False, "reason": "this appliance has no log reader"}

    gathered: dict[str, Any] = {}
    for entry in reader.catalogue():
        key = str(entry.get("key", ""))
        if not key:
            continue
        try:
            gathered[key] = reader.read(key, lines=_LOG_LINES)
        except Exception as error:  # noqa: BLE001
            gathered[key] = {
                "collected": False,
                "reason": f"{type(error).__name__}: {error}",
            }
    return gathered
