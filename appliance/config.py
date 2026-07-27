"""Appliance configuration.

Configuration is loaded from a single structured document on disk, overlaid
with environment variables so that a field technician can override one value
without editing the document.  Every field carries a default that is safe on a
machine with no telephony hardware attached, which is what allows the control
plane to run and be tested away from the target appliance.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

__all__ = ["ApplianceConfig", "ConfigError", "ENVIRONMENT_PREFIX"]

ENVIRONMENT_PREFIX = "MYIPBX_"


class ConfigError(ValueError):
    """Raised when the configuration document cannot be honoured."""


#: The transport security versions this appliance will negotiate.  Everything
#: older is excluded here rather than at the listener, so that a document
#: asking for it is refused at load time with a reason an operator can read.
_PERMITTED_TLS_VERSIONS = frozenset({"TLSv1.2", "TLSv1.3"})


@dataclass
class ApplianceConfig:
    """The complete runtime configuration of the appliance control plane."""

    # --- Presentation and control surface -------------------------------
    #: The address the console binds.  It is the loopback address rather than
    #: every interface because an appliance that can reboot the machine and
    #: rewrite its firewall should not appear on a network nobody chose.  The
    #: installer writes the management address it was given here; when it was
    #: given none it leaves this default in place and says so on the console,
    #: so that an unreachable appliance is a stated decision rather than a
    #: silent exposure discovered later by somebody else's scanner.
    listen_address: str = "127.0.0.1"
    listen_port: int = 8088
    web_root: str = "/opt/myipbx/web"
    allowed_origins: tuple[str, ...] = ()

    # --- Transport security -----------------------------------------------
    #: The console carries the administrator's password and its session cookie,
    #: and the operations it authorises reach as far as rebooting the machine
    #: and recompiling kernel modules.  None of that may cross a site network
    #: in the clear, so the secured listener is the shipped default and turning
    #: it off is a deliberate act by somebody who has read this line.
    tls_enabled: bool = True
    tls_certificate: str = "/etc/myipbx/tls/appliance.crt"
    tls_private_key: str = "/etc/myipbx/tls/appliance.key"
    #: Everything below this version has a published attack against it.  The
    #: setting exists so that a site may raise the floor, not lower it; the
    #: validator refuses anything older.
    tls_minimum_version: str = "TLSv1.2"
    #: A second listener that serves nothing at all.  An operator who types the
    #: appliance's address without a scheme reaches plain transport, and a port
    #: that answers with a redirect sends them to the secured one instead of
    #: leaving them at a refused connection wondering which of the two things
    #: they typed was wrong.  It never returns content and never sets a cookie.
    plain_http_redirect_port: int = 8080

    # --- Persistent state ------------------------------------------------
    state_directory: str = "/var/lib/myipbx"
    configuration_document: str = "/etc/myipbx/appliance.json"
    asterisk_configuration_directory: str = "/etc/asterisk"
    log_file: str = "/var/log/myipbx/appliance.log"
    privileged_helper: str = "/opt/myipbx/bin/myipbx-privileged-helper.sh"

    # Where the daemon that holds the privilege is listening.  The control
    # plane holds none of its own and reaches every system operation through
    # this socket, so an appliance whose daemon listens elsewhere -- a second
    # appliance on one machine, or a test standing one up in a temporary
    # directory -- could not be configured at all while this was fixed in code.
    privileged_socket: str = "/run/myipbx/helper.sock"
    call_record_file: str = "/var/log/asterisk/cdr-csv/Master.csv"
    log_level: str = "INFO"

    # --- Telephony engine manager interface ------------------------------
    manager_host: str = "127.0.0.1"
    manager_port: int = 5038
    manager_username: str = "myipbx"
    manager_secret: str = ""
    manager_connect_timeout_seconds: float = 5.0
    manager_action_timeout_seconds: float = 10.0
    manager_reconnect_base_seconds: float = 1.0
    manager_reconnect_ceiling_seconds: float = 60.0

    # --- Persistent bidirectional socket ---------------------------------
    heartbeat_interval_seconds: float = 5.0
    heartbeat_missed_limit: int = 3
    socket_maximum_message_bytes: int = 262144
    socket_maximum_connections: int = 256
    socket_send_queue_limit: int = 512
    #: How long a state change may be held so that the changes arriving behind
    #: it can ride out on the same frame.  A busy engine produces several
    #: hundred events a second and no operator can read a dashboard that
    #: redraws that often, so the appliance would be spending the loop it needs
    #: for the engine on frames nobody can perceive.  Zero disables the
    #: behaviour entirely and restores one frame per event, which is what a
    #: laboratory measurement of the event path wants.
    broadcast_coalesce_milliseconds: int = 150

    # --- Trunk registration layer ----------------------------------------
    trunk_registration_base_seconds: float = 2.0
    trunk_registration_ceiling_seconds: float = 300.0
    trunk_registration_jitter_ratio: float = 0.25
    trunk_registration_timeout_seconds: float = 30.0

    # --- Security ---------------------------------------------------------
    session_idle_seconds: int = 1800
    session_maximum: int = 64
    login_attempt_limit: int = 5
    login_lockout_seconds: int = 300
    password_iterations: int = 240000
    #: Withholds the session cookie from any request that is not carried over a
    #: secured transport.  The appliance serves its console over one by default,
    #: so the attribute costs nothing and removes the case where a single plain
    #: request — a mistyped scheme, a bookmark from before this was true — hands
    #: the session token to whatever is listening on the wire.
    session_cookie_secure: bool = True

    # --- Automated task execution ----------------------------------------
    health_sweep_interval_seconds: float = 30.0
    hardware_rescan_interval_seconds: float = 300.0
    worker_pool_size: int = 4
    task_default_timeout_seconds: float = 120.0

    # --- Constraint One ----------------------------------------------------
    # Refusing to start on a detected address allocation server is the
    # shipped default.  The override exists only so that a laboratory machine
    # that legitimately runs one can host the test suite; it is never set on
    # an appliance and the installer does not write it.
    fail_on_address_allocation_server: bool = True

    extra: dict[str, Any] = field(default_factory=dict)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ApplianceConfig":
        """Build a configuration from a mapping, coercing to declared types."""
        known = {item.name: item for item in fields(cls) if item.name != "extra"}
        accepted: dict[str, Any] = {}
        surplus: dict[str, Any] = {}

        for key, value in values.items():
            target = known.get(key)
            if target is None:
                surplus[key] = value
                continue
            accepted[key] = _coerce(key, value, target.type)

        instance = cls(**accepted)
        instance.extra.update(surplus)
        instance.validate()
        return instance

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> "ApplianceConfig":
        """Load the configuration document, then overlay environment values.

        A missing document is not an error: the defaults are a working
        configuration.  A malformed document is an error, because silently
        falling back to defaults would hide an operator's mistake.
        """
        environment = os.environ if environment is None else environment
        document_path = Path(
            path
            or environment.get(f"{ENVIRONMENT_PREFIX}CONFIGURATION_DOCUMENT")
            or cls.configuration_document
        )

        values: dict[str, Any] = {}
        if document_path.is_file():
            try:
                raw = json.loads(document_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ConfigError(
                    f"the configuration document at {document_path} is not valid: {error}"
                ) from error
            if not isinstance(raw, dict):
                raise ConfigError("the configuration document must be a mapping")
            values.update(raw.get("appliance", raw))

        known = {item.name for item in fields(cls) if item.name != "extra"}
        for name in known:
            variable = f"{ENVIRONMENT_PREFIX}{name.upper()}"
            if variable in environment:
                values[name] = environment[variable]

        instance = cls.from_mapping(values)
        instance.extra["configuration_document_path"] = str(document_path)
        return instance

    # -- derived paths -----------------------------------------------------

    @property
    def state_path(self) -> Path:
        return Path(self.state_directory)

    @property
    def receipts_path(self) -> Path:
        return self.state_path / "receipts"

    @property
    def digest_path(self) -> Path:
        return self.state_path / "rendered-digests.json"

    @property
    def credentials_path(self) -> Path:
        return self.state_path / "credentials.json"

    @property
    def journal_path(self) -> Path:
        """Where the record of who changed what is kept."""
        return self.state_path / "operations.jsonl"

    # -- validation --------------------------------------------------------

    def validate(self) -> None:
        """Reject configurations that cannot produce a healthy appliance."""
        # A listening port of zero is a deliberate request for an ephemeral
        # port assigned by the kernel.  The appliance reports the port it
        # actually bound, so this is usable rather than merely tolerated.
        if not 0 <= self.listen_port <= 65535:
            raise ConfigError("the listening port must be a valid port number")
        if not 1 <= self.manager_port <= 65535:
            raise ConfigError("the manager interface port must be a valid port number")
        if self.tls_enabled and not (self.tls_certificate and self.tls_private_key):
            raise ConfigError(
                "a secured listener needs both a certificate and a private key; "
                "name them, or set the transport security setting to false and "
                "accept that the administrator password crosses the network in "
                "the clear"
            )
        if self.tls_minimum_version not in _PERMITTED_TLS_VERSIONS:
            raise ConfigError(
                "the minimum transport security version must be one of "
                + ", ".join(sorted(_PERMITTED_TLS_VERSIONS))
            )
        # A redirect port of zero asks for an ephemeral one, exactly as the
        # console port does, which is what lets a test bind both without
        # choosing numbers that might already be in use.
        if not 0 <= self.plain_http_redirect_port <= 65535:
            raise ConfigError("the redirect port must be a valid port number")
        if (
            self.plain_http_redirect_port
            and self.plain_http_redirect_port == self.listen_port
        ):
            raise ConfigError(
                "the redirect port cannot be the port the secured console binds"
            )
        if self.heartbeat_interval_seconds <= 0:
            raise ConfigError("the heartbeat interval must be greater than zero")
        if self.heartbeat_missed_limit < 1:
            raise ConfigError("at least one missed heartbeat must be tolerated")
        if self.socket_maximum_message_bytes < 1024:
            raise ConfigError("the maximum socket message size is implausibly small")
        if self.socket_maximum_connections < 1:
            raise ConfigError("at least one socket connection must be permitted")
        if self.socket_send_queue_limit < 1:
            raise ConfigError("a connection must be allowed to queue at least one message")
        # The ceiling is a second of held state.  Beyond that an operator would
        # begin to perceive the dashboard as lagging the telephone in front of
        # them, which is the one impression this appliance cannot afford.
        if not 0 <= self.broadcast_coalesce_milliseconds <= 1000:
            raise ConfigError(
                "the broadcast coalescing window must fall between zero and one "
                "thousand milliseconds"
            )
        if self.session_idle_seconds < 60:
            raise ConfigError("the session idle expiry must be at least sixty seconds")
        if self.password_iterations < 100000:
            raise ConfigError("the key derivation iteration count is too low to ship")
        if self.worker_pool_size < 1:
            raise ConfigError("the worker pool must have at least one worker")
        if not 0.0 <= self.trunk_registration_jitter_ratio < 1.0:
            raise ConfigError("the retry jitter ratio must fall between zero and one")
        if self.trunk_registration_base_seconds <= 0:
            raise ConfigError("the retry base interval must be greater than zero")
        if self.trunk_registration_ceiling_seconds < self.trunk_registration_base_seconds:
            raise ConfigError("the retry ceiling cannot be below the retry base interval")

    def redacted(self) -> dict[str, Any]:
        """A representation safe to log or to serve over the interface."""
        exposed: dict[str, Any] = {}
        for item in fields(self):
            if item.name == "extra":
                continue
            value = getattr(self, item.name)
            if "secret" in item.name or "password" in item.name:
                value = "redacted" if value else "unset"
            exposed[item.name] = list(value) if isinstance(value, tuple) else value
        return exposed


_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "disabled"})


def _coerce(name: str, value: Any, declared: Any) -> Any:
    """Coerce a loaded value to the type declared on the dataclass field.

    Declared types arrive as strings because the module uses postponed
    annotation evaluation, so the comparison is textual by design.
    """
    text = declared if isinstance(declared, str) else getattr(declared, "__name__", str(declared))

    try:
        if text == "bool":
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in _TRUE_WORDS:
                return True
            if lowered in _FALSE_WORDS:
                return False
            raise ValueError("expected a truth value")
        if text == "int":
            return int(value)
        if text == "float":
            return float(value)
        if text == "str":
            return str(value)
        if text.startswith("tuple"):
            if isinstance(value, str):
                return tuple(part.strip() for part in value.split(",") if part.strip())
            return tuple(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"the setting named {name} is not usable: {error}") from error

    return value
