"""Firewall rules, generated from the appliance's own configuration.

A telephony system reachable from an untrusted network is scanned continuously
and within hours of being visible. Leaving the machine's firewall to the site
is defensible for a component; it is not defensible for an appliance, which is
supposed to arrive ready.

So the appliance generates a complete ruleset from what the administrator
declared, and asks its helper to load it. The ruleset is deny by default: the
loopback interface, established traffic, and the diagnostic control messages a
network genuinely needs are allowed, and nothing else is open unless a rule
says so.

Two properties are worth stating because they are what make this safe to
operate from a browser. The ruleset always permits the management interface
from the sources the administrator named, so applying a firewall cannot lock
the administrator out of the appliance that applied it. And the generated file
is written by the control plane and loaded by the helper, so nothing an
operator types is ever assembled into a rule by string concatenation without
having passed the schema first.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Iterable, Mapping

from . import numerals
from .logging_setup import get_logger

__all__ = [
    "SERVICES",
    "render_ruleset",
    "describe_services",
    "summarise",
    "FirewallError",
]

_LOG = get_logger("firewall")


class FirewallError(ValueError):
    """A ruleset could not be generated from what was declared."""


#: The services a rule may open, and what each means on the wire. A service
#: absent from here cannot be opened, which keeps the firewall from becoming a
#: general purpose port editor reachable from a browser.
SERVICES: dict[str, dict[str, Any]] = {
    "management": {
        "description": "this appliance's own web console",
        "protocols": ("tcp",),
        "ports": (),  # filled from the appliance's configured listening port
        "essential": True,
    },
    "session protocol": {
        "description": "call signalling, in the clear",
        "protocols": ("udp", "tcp"),
        "ports": (5060,),
        "essential": False,
    },
    "secure session protocol": {
        "description": "call signalling, encrypted",
        "protocols": ("tcp",),
        "ports": (5061,),
        "essential": False,
    },
    "media": {
        "description": "the audio of calls in progress",
        "protocols": ("udp",),
        "ports": ((10000, 20000),),
        "essential": False,
    },
    "secure shell": {
        "description": "administrative access at the command line",
        "protocols": ("tcp",),
        "ports": (22,),
        "essential": False,
    },
    "name resolution": {
        "description": "answering name lookups, which this appliance does not do",
        "protocols": ("udp", "tcp"),
        "ports": (53,),
        "essential": False,
    },
}

#: The table and chain the appliance owns. Nothing outside this table is
#: touched, so a site with its own rules keeps them.
_TABLE = "myipbx"


def describe_services() -> list[dict[str, Any]]:
    """Describe the openable services for the interface."""
    return [
        {
            "name": name,
            "description": service["description"],
            "essential": service["essential"],
        }
        for name, service in SERVICES.items()
    ]


def _validate_source(source: str) -> str:
    """Accept a network in prefix notation, or the word naming everywhere."""
    text = str(source or "").strip().lower()
    if text in ("any", "anywhere", "0.0.0.0/0", ""):
        return "0.0.0.0/0"
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError as error:
        raise FirewallError(
            f"the source named {source} is not a network in prefix notation"
        ) from error
    if network.version != 4:
        raise FirewallError("only version four networks are supported by this ruleset")
    return str(network)


def _port_expression(ports: Iterable[Any]) -> str:
    pieces: list[str] = []
    for port in ports:
        if isinstance(port, tuple):
            pieces.append(f"{port[0]}-{port[1]}")
        else:
            pieces.append(str(port))
    return "{ " + ", ".join(pieces) + " }" if len(pieces) > 1 else pieces[0]


def render_ruleset(
    rules: Iterable[Mapping[str, Any]],
    management_port: int,
    management_sources: Iterable[str] = ("0.0.0.0/0",),
) -> str:
    """Generate the complete ruleset.

    The management service is always emitted, from the sources given, before
    any declared rule. That ordering is deliberate: an administrator cannot
    write a ruleset that locks them out of the console they wrote it in.
    """
    if not 1 <= int(management_port) <= 65535:
        raise FirewallError("the management port is not a valid port number")

    accepted_sources = [_validate_source(source) for source in management_sources] or [
        "0.0.0.0/0"
    ]

    lines: list[str] = [
        "#!/usr/sbin/nft -f",
        "#",
        "# Legacy-to-Modern IPBX Appliance -- firewall ruleset",
        "# generated by the appliance control plane -- do not edit by hand",
        "#",
        "# This ruleset denies by default. Only the services declared in the",
        "# console are open, and only from the sources declared with them.",
        "#",
        "# Note the absence of any address allocation service port. This",
        "# appliance assigns no addresses, so there is nothing to open.",
        "",
        f"table inet {_TABLE} {{",
        "    chain input {",
        "        type filter hook input priority 0; policy drop;",
        "",
        "        # Traffic this machine itself started, and its own loopback.",
        "        ct state established,related accept",
        "        iif lo accept",
        "        ct state invalid drop",
        "",
        "        # The diagnostic messages a network genuinely needs. Refusing",
        "        # these breaks path discovery and makes the appliance look",
        "        # broken in ways that are hard to diagnose.",
        "        ip protocol icmp icmp type { echo-request, destination-unreachable, "
        "time-exceeded, parameter-problem } accept",
        "",
        "        # The management console, always, from the declared sources.",
        "        # This rule comes first so that no later rule can remove the",
        "        # administrator's own way back in.",
    ]

    for source in accepted_sources:
        lines.append(
            f"        ip saddr {source} tcp dport {int(management_port)} accept "
            f"comment \"the appliance console\""
        )

    lines.append("")
    lines.append("        # Declared rules follow, in the order they were written.")

    emitted = 0
    for rule in rules:
        if not rule.get("enabled", True):
            continue

        name = str(rule.get("name", "an unnamed rule"))
        service_name = str(rule.get("service", "")).strip().lower()
        service = SERVICES.get(service_name)
        if service is None:
            raise FirewallError(f"the rule named {name} opens an unrecognised service")

        source = _validate_source(str(rule.get("source", "any")))
        ports = service["ports"] or ((int(management_port),) if service["essential"] else ())
        if not ports:
            continue

        for protocol in service["protocols"]:
            lines.append(
                f"        ip saddr {source} {protocol} dport "
                f"{_port_expression(ports)} accept comment \"{name}\""
            )
        emitted += 1

    if not emitted:
        lines.append("        # No service beyond the console has been opened.")

    lines.extend(
        [
            "",
            "        # Anything not named above is dropped by the policy.",
            "    }",
            "",
            "    chain forward {",
            "        # This appliance is not a router and forwards nothing.",
            "        type filter hook forward priority 0; policy drop;",
            "    }",
            "",
            "    chain output {",
            "        type filter hook output priority 0; policy accept;",
            "    }",
            "}",
            "",
        ]
    )

    return "\n".join(lines)


def summarise(
    rules: Iterable[Mapping[str, Any]], management_port: int
) -> dict[str, Any]:
    """Describe the ruleset for the interface, with every figure spelled."""
    declared = list(rules)
    active = [rule for rule in declared if rule.get("enabled", True)]
    exposed = [
        rule
        for rule in active
        if _safe_source(rule.get("source", "any")) == "0.0.0.0/0"
    ]

    return {
        "rule_count": numerals.spell_integer(len(declared)),
        "active_count": numerals.spell_integer(len(active)),
        "open_to_anywhere_count": numerals.spell_integer(len(exposed)),
        "management_port_is_always_open": True,
        "default_policy": "everything not named is dropped",
        "services": describe_services(),
        "advice": (
            "a rule open to anywhere exposes that service to the whole network "
            "this appliance can reach; prefer naming the networks that need it"
        )
        if exposed
        else "every open service is limited to declared networks",
    }


def _safe_source(source: Any) -> str:
    try:
        return _validate_source(str(source))
    except FirewallError:
        return "invalid"
