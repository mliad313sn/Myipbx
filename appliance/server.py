"""Composition root.

Everything the appliance is made of is constructed here and nowhere else, so
that every component can be built in isolation by a test with its own doubles.
This module owns startup ordering, the bridge from the engine event stream to
the live state and the socket hub, the socket upgrade path, and shutdown.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import signal
import time
from typing import Any

from . import api, hardware as hardware_module, httpd, numerals, wsproto
from .ami import ManagerClient, ManagerMessage
from .config import ApplianceConfig
from .confstore import ConfigurationStore, DriftDetected
from .diagnostics import CallRecordReader, LogReader
from .entities import SecretStore
from . import firewall as firewall_module
from .httpd import HttpServer, Request, Response
from .logging_setup import configure_logging, get_logger
from .audit import AuditJournal
from .netaudit import AddressAllocationAudit, AddressAllocationDetected
from .security import CredentialStore, LoginThrottle, PasswordHasher, SessionStore
from .state import ApplianceState
from .sysops import PrivilegedOperations, SystemStatus
from .tasks import TaskScheduler
from .trunks import TrunkRegistry
from .wsserver import SocketConnection, SocketHub

__all__ = ["Appliance", "run"]

_LOG = get_logger("server")


class Appliance:
    """The assembled appliance control plane."""

    def __init__(self, config: ApplianceConfig | None = None) -> None:
        self.config = config or ApplianceConfig()
        self.started_at = time.time()
        self.audit_findings: list[Any] = []

        # -- constraint enforcement ---------------------------------------
        self.audit = AddressAllocationAudit()

        # -- persistence ---------------------------------------------------
        # Secrets live apart from the source of truth and are read only when an
        # engine configuration file is actually rendered.
        self.secrets = SecretStore(self.config.state_path / "secrets.json")
        self.store = ConfigurationStore(
            document_path=self.config.configuration_document,
            output_directory=self.config.asterisk_configuration_directory,
            digest_path=self.config.digest_path,
            secrets=self.secrets,
        )

        # -- security ------------------------------------------------------
        hasher = PasswordHasher(self.config.password_iterations)
        self.credentials = CredentialStore(self.config.credentials_path, hasher)
        self.sessions = SessionStore(
            idle_seconds=self.config.session_idle_seconds,
            lifetime_seconds=self.config.session_lifetime_seconds,
            maximum=self.config.session_maximum,
        )
        self.throttle = LoginThrottle(
            attempt_limit=self.config.login_attempt_limit,
            lockout_seconds=self.config.login_lockout_seconds,
        )
        # Who changed what, and from where. Written by the guard that fronts
        # every write route, so a route added later is recorded without anybody
        # having to remember to record it.
        self.journal = AuditJournal(self.config.journal_path)

        # -- live model ----------------------------------------------------
        self.state = ApplianceState()
        self.hardware = hardware_module.HardwareInventory()

        # -- the machine, and the operations the interface may perform on it -
        self.system = SystemStatus()
        self.operations = PrivilegedOperations(
            self.config.privileged_helper, self.config.privileged_socket
        )
        self.logs = LogReader()
        self.calls = CallRecordReader(self.config.call_record_file)

        # -- transport -----------------------------------------------------
        self.hub = SocketHub(
            heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
            missed_limit=self.config.heartbeat_missed_limit,
            maximum_connections=self.config.socket_maximum_connections,
            state_provider=self.state.snapshot,
            coalesce_milliseconds=self.config.broadcast_coalesce_milliseconds,
        )
        self.state.publisher = self._publish

        # -- engine --------------------------------------------------------
        self.manager = ManagerClient(
            host=self.config.manager_host,
            port=self.config.manager_port,
            username=self.config.manager_username,
            secret=self.config.manager_secret,
            connect_timeout_seconds=self.config.manager_connect_timeout_seconds,
            action_timeout_seconds=self.config.manager_action_timeout_seconds,
            reconnect_base_seconds=self.config.manager_reconnect_base_seconds,
            reconnect_ceiling_seconds=self.config.manager_reconnect_ceiling_seconds,
        )
        self.manager.add_event_handler(self._on_engine_event)

        self.trunks = TrunkRegistry(
            manager=self.manager,
            publisher=self._publish,
            base_seconds=self.config.trunk_registration_base_seconds,
            ceiling_seconds=self.config.trunk_registration_ceiling_seconds,
            jitter_ratio=self.config.trunk_registration_jitter_ratio,
            attempt_timeout_seconds=self.config.trunk_registration_timeout_seconds,
        )

        # -- automation ----------------------------------------------------
        self.tasks = TaskScheduler(
            publisher=self._publish,
            worker_pool_size=self.config.worker_pool_size,
            default_timeout_seconds=self.config.task_default_timeout_seconds,
        )
        self._register_tasks()

        # -- interface -----------------------------------------------------
        self.router = api.build_router(self)
        self.http = HttpServer(
            router=self.router,
            host=self.config.listen_address,
            port=self.config.listen_port,
            upgrade_handler=self._handle_upgrade,
        )
        # The transport security material is read at start rather than here, so
        # that a certificate a technician is still installing produces a refusal
        # to serve with a reason attached rather than an appliance object that
        # cannot be constructed at all.
        self.redirect: httpd.RedirectServer | None = None
        self.tls_fingerprint = ""

        self._running = False
        self._connection_counter = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bring the appliance up in dependency order."""
        _LOG.info("the appliance control plane is starting")

        # Constraint One is checked before anything else binds a socket, so a
        # machine that allocates addresses never reaches a serving state.
        self._enforce_address_allocation_exclusion()

        # The transport is secured before the credential is generated, because
        # the credential is printed on the console in the same breath as the
        # certificate's fingerprint and an operator needs both together.
        self._configure_transport_security()

        self._ensure_credentials()
        self._load_declared_trunks()

        self.state.hardware = self.hardware.scan()
        _LOG.info("hardware enumeration reports that %s", self.state.hardware["summary"])

        await self.manager.start()
        self.trunks.start_all()
        self.tasks.start()
        self.hub.start_heartbeat()
        await self.http.start()
        await self._start_redirect_listener()

        self._running = True
        self.state.touch()
        _LOG.info(
            "the appliance control plane is serving on the address %s at port number %d",
            self.config.listen_address,
            self.config.listen_port,
        )

    async def stop(self) -> None:
        """Take the appliance down in reverse dependency order."""
        if not self._running:
            return
        self._running = False
        _LOG.info("the appliance control plane is stopping")

        if self.redirect is not None:
            await self.redirect.stop()
            self.redirect = None
        await self.http.stop()
        await self.hub.stop()
        await self.tasks.stop()
        await self.trunks.stop_all()
        await self.manager.stop()
        _LOG.info("the appliance control plane has stopped")

    # -- constraint one -----------------------------------------------------

    def _enforce_address_allocation_exclusion(self) -> None:
        try:
            self.audit_findings = list(
                self.audit.enforce(fatal=self.config.fail_on_address_allocation_server)
            )
        except AddressAllocationDetected:
            _LOG.error(
                "the appliance refuses to start because this machine runs an address "
                "allocation service, which the product specification forbids"
            )
            raise

        for finding in self.audit_findings:
            self.state.raise_alarm(
                f"address-allocation-{finding.source}",
                finding.severity,
                finding.subject,
                finding.detail,
            )

    # -- transport security --------------------------------------------------

    def _configure_transport_security(self) -> None:
        """Build the secured listener's context, or refuse to serve.

        Refusing is the whole point.  An appliance that quietly fell back to
        plain transport when its certificate was missing would put the
        administrator's password on the wire on exactly the machines where
        somebody had already got the installation half right, and nobody would
        find out until it mattered.
        """
        if not self.config.tls_enabled:
            _LOG.warning(
                "transport security is switched off in the configuration document; "
                "the administrator password and the session cookie will cross this "
                "network in the clear"
            )
            self.http.tls_context = None
            return

        # A failure here propagates.  The caller turns it into a refusal to
        # start with the message intact, because the message names the file.
        self.http.tls_context = httpd.build_tls_context(
            certificate=self.config.tls_certificate,
            private_key=self.config.tls_private_key,
            minimum_version=self.config.tls_minimum_version,
        )
        # The fingerprint is deliberately kept out of the log. Constraint Two
        # sanitises every numeral in a log line into words, which would render
        # a digest unrecognisable against the one a browser displays, and the
        # whole value of a fingerprint is that the two can be compared
        # character by character. It goes to the console instead, beside the
        # password, which is the same reason the password goes there.
        self.tls_fingerprint = httpd.certificate_fingerprint(self.config.tls_certificate)
        _LOG.info(
            "the console is secured by the certificate at %s",
            self.config.tls_certificate,
        )

    async def _start_redirect_listener(self) -> None:
        """Answer the plain port with the secured address, if one is configured.

        A failure to bind this port is reported and then tolerated.  It carries
        no content and no session, so an appliance without it is inconvenient
        rather than broken, and refusing to serve the console over a courtesy
        redirect would be the wrong trade.
        """
        port = self.config.plain_http_redirect_port
        if not self.config.tls_enabled or not port:
            return

        redirect = httpd.RedirectServer(
            host=self.config.listen_address,
            port=port,
            secure_port=self.http.bound_port or self.config.listen_port,
        )
        try:
            await redirect.start()
        except OSError as error:
            _LOG.warning(
                "the plain listener at port number %d could not be bound, so a browser "
                "reaching this appliance without a scheme will not be redirected: %s",
                port,
                error,
            )
            return
        self.redirect = redirect

    # -- startup helpers ----------------------------------------------------

    def _ensure_credentials(self) -> None:
        """Create an initial administrator credential if none exists."""
        if self.credentials.exists():
            return
        password = _generated_password()
        self.credentials.save("administrator", password)
        # This is the one and only time the password is rendered.  It is
        # written to the console rather than to the log, because the log is a
        # durable artefact and a credential must not be one.
        #
        # The certificate's fingerprint goes here too.  This appliance signs
        # its own certificate, so the first browser to reach it will warn; the
        # only way an operator can tell that warning apart from an interception
        # is to have seen the fingerprint somewhere the network was not
        # involved, and this console is that somewhere.
        lines = [
            "",
            "  an initial administrator credential has been generated for this appliance",
            "  user name: administrator",
            f"  password:  {password}",
            "  this password is shown once and is not written to any log; record it now",
        ]
        if self.tls_fingerprint:
            lines.extend(
                [
                    "",
                    "  this appliance signed its own certificate, so the first connection",
                    "  will warn. compare what the browser shows against this fingerprint",
                    "  before accepting it:",
                    f"  {self.tls_fingerprint}",
                ]
            )
        print("\n".join(lines) + "\n", flush=True)

    def _load_declared_trunks(self) -> None:
        try:
            document = self.store.load()
        except DriftDetected as error:
            _LOG.error("the source of truth document could not be read: %s", error)
            self.state.raise_alarm(
                "configuration-unreadable",
                "critical",
                "the source of truth document could not be read",
                str(error),
            )
            return

        declared = self.trunks.declare_many(document.get("trunks", []) or [])
        _LOG.info("the configuration declares %d trunk or trunks", len(declared))

    def _register_tasks(self) -> None:
        self.tasks.register(
            "health-sweep",
            self._task_health_sweep,
            "verify the engine, the hardware, and the address allocation exclusion",
            interval_seconds=self.config.health_sweep_interval_seconds,
        )
        self.tasks.register(
            "hardware-rescan",
            self._task_hardware_rescan,
            "re-enumerate interface cards and spans",
            interval_seconds=self.config.hardware_rescan_interval_seconds,
        )
        self.tasks.register(
            "render-configuration",
            self._task_render_configuration,
            "render the engine configuration from the source of truth",
        )
        self.tasks.register(
            "reload-engine",
            self._task_reload_engine,
            "ask the engine to reload its configuration",
        )
        self.tasks.register(
            "render-firewall",
            self._task_render_firewall,
            "generate the firewall ruleset from the declared rules",
        )
        self.tasks.register(
            "backup-state",
            self._task_backup_state,
            "write a timestamped copy of the source of truth to the state directory",
            blocking=True,
        )

    # -- tasks --------------------------------------------------------------

    async def _task_health_sweep(self) -> dict[str, Any]:
        findings = self.audit.scan()
        critical = [item for item in findings if item.severity == "critical"]
        if critical:
            self.state.raise_alarm(
                "address-allocation-detected",
                "critical",
                "an address allocation service appeared on this machine",
                "; ".join(item.subject for item in critical),
            )
        else:
            self.state.clear_alarm("address-allocation-detected")

        engine_reachable = await self.manager.ping() if self.manager.connected else False
        if not engine_reachable:
            self.state.raise_alarm(
                "engine-unreachable",
                "critical" if not self.manager.connected else "warning",
                "the telephony engine did not answer a health probe",
                self.manager.last_error or "no further detail is available",
            )
        else:
            self.state.clear_alarm("engine-unreachable")

        self.sessions.purge_expired()
        return {
            "engine_reachable": engine_reachable,
            "address_allocation_findings": len(findings),
            "socket_connections": self.hub.connection_count,
        }

    async def _task_hardware_rescan(self) -> dict[str, Any]:
        inventory = self.hardware.scan()
        self.state.hardware = inventory
        self.state.touch()
        for span in inventory.get("spans", []):
            key = f"span-alarm-{span['number']}"
            if span["healthy"]:
                self.state.clear_alarm(key)
            else:
                self.state.raise_alarm(
                    key,
                    "warning",
                    f"the span numbered {span['number']} reports an alarm",
                    str(span["alarm"]),
                )
        return {"card_count": inventory["card_count"], "span_count": inventory["span_count"]}

    async def _task_render_configuration(self, force: bool = False) -> dict[str, Any]:
        try:
            outcome = self.store.render(force=force)
        except DriftDetected as error:
            self.state.raise_alarm(
                "configuration-drift",
                "warning",
                "a generated configuration file was modified outside the appliance",
                str(error),
            )
            raise
        self.state.clear_alarm("configuration-drift")
        return {"written": outcome["written"], "forced": outcome["forced"]}

    async def _task_reload_engine(self) -> dict[str, Any]:
        if not self.manager.connected:
            raise RuntimeError("the telephony engine manager interface is not connected")
        response = await self.manager.send_action("Reload")
        return {"accepted": bool(response.is_success)}

    async def _task_render_firewall(self) -> dict[str, Any]:
        """Write the ruleset the helper will load.

        The control plane generates it and the helper loads it, so no value an
        operator typed is ever turned into a rule by the privileged side.
        """
        document = self.store.load()
        rules = document.get("firewall_rules", []) or []

        # Both console ports are named. The secured one carries the console;
        # the plain one carries only the redirect to it, and an administrator
        # who reaches for the appliance the way they always have would find
        # nothing listening if a ruleset closed it.
        redirect_port = (
            self.redirect.bound_port if self.redirect is not None else 0
        ) or self.config.plain_http_redirect_port

        ruleset = firewall_module.render_ruleset(
            rules,
            management_port=self.http.bound_port or self.config.listen_port,
            management_sources=document.get("firewall_management_sources", ["0.0.0.0/0"]),
            redirect_port=redirect_port if self.config.tls_enabled else 0,
        )

        destination = self.config.state_path / "firewall.nft"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        temporary.write_text(ruleset, encoding="utf-8")
        temporary.replace(destination)

        _LOG.info(
            "the firewall ruleset was generated from %d declared rule or rules",
            len(rules),
        )
        return {
            "path": str(destination),
            "rule_count": len(rules),
            "next_step": "apply the ruleset to load it",
        }

    def _task_backup_state(self) -> dict[str, Any]:
        """Blocking by design: dispatched to the worker pool, not the loop."""
        destination = self.config.state_path / "backups"
        destination.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        target = destination / f"appliance-{stamp}.json"
        document = self.store.load()
        target.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        return {"path": str(target)}

    # -- engine bridge ------------------------------------------------------

    def _on_engine_event(self, message: ManagerMessage) -> None:
        """Advance every consumer of the engine event stream."""
        name = (message.event or "").lower()

        if name == "appliancemanagerconnected":
            self.state.apply_engine_event(message)
            self.trunks.start_all()
            return

        changed = self.state.apply_engine_event(message)
        self.trunks.apply_engine_event(message)

        if not changed and name in {"newchannel", "newstate", "hangup"}:
            # A channel event that changed nothing usually means the appliance
            # missed the channel's creation, which is worth knowing about.
            _LOG.debug("the engine event named %s did not match any known channel", name)

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Offer an update to every connected dashboard.

        The hub decides whether the message goes out now or rides the next
        coalescing flush; see the topic sets in the transport module for which
        is which.  Nothing is offered at all when no dashboard is connected.
        """
        if topic == "trunk.transition":
            self._note_trunk_health(payload)

        if self.hub.connection_count:
            self.hub.publish(topic, payload)

    #: Trunk states in which the appliance cannot place or receive a call on
    #: that trunk.  Registering is deliberately absent: a trunk on its way up
    #: is not a fault, and alarming on it would alarm on every start.
    _TRUNK_DOWN_STATES = frozenset({"retrying", "failed", "unknown", "unconfigured"})

    def _note_trunk_health(self, payload: dict[str, Any]) -> None:
        """Raise or clear an alarm as a trunk leaves or regains registration.

        A carrier dropping is the commonest real telephony outage there is, and
        it was the one condition the appliance treated as unremarkable: the
        transition was published, the console redrew a table, and nothing else
        happened. No alarm existed for it, so the dashboard's alarm panel stayed
        empty and the health route went on reporting the appliance healthy while
        the site had no dialtone. Anything polling that route -- which is the
        obvious way to monitor an appliance -- saw green through a total loss of
        service.
        """
        name = str(payload.get("trunk", "")).strip()
        if not name:
            return

        key = f"trunk-down-{name}"
        state = str(payload.get("to", "")).strip().lower()
        reason = str(payload.get("reason", "")).strip()

        if state in self._TRUNK_DOWN_STATES:
            self.state.raise_alarm(
                key,
                "critical",
                f"the trunk named {name} is not registered",
                (
                    f"calls over this trunk cannot be placed or received; the "
                    f"engine reported the state {state}"
                    + (f" because {reason}" if reason else "")
                ),
            )
        else:
            self.state.clear_alarm(key)

    # -- socket upgrade -----------------------------------------------------

    async def _handle_upgrade(
        self, request: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Authenticate, complete the handshake, and serve the connection."""
        refusal = self._refuse_upgrade(request)
        if refusal is not None:
            writer.write(refusal.serialise(keep_alive=False))
            await writer.drain()
            writer.close()
            return

        session = self.sessions.validate(request.cookie(api.SESSION_COOKIE_NAME))
        response = httpd.build_handshake_response(request)
        writer.write(httpd.serialise_handshake(response))
        await writer.drain()
        if response.status != 101:
            writer.close()
            return

        self._connection_counter += 1
        connection = SocketConnection(
            identifier=f"connection-{self._connection_counter}-{secrets.token_hex(4)}",
            reader=reader,
            writer=writer,
            maximum_message_bytes=self.config.socket_maximum_message_bytes,
            send_queue_limit=self.config.socket_send_queue_limit,
            username=session.username if session else "unknown",
            peer=request.peer,
        )

        if not self.hub.add(connection):
            await connection.close(
                wsproto.CLOSE_POLICY_VIOLATION,
                "the appliance has reached its connected dashboard limit",
            )
            return

        connection.send_message("welcome", self._welcome_payload(connection))
        try:
            await connection.serve(self._on_socket_message)
        finally:
            self.hub.remove(connection)

    def _refuse_upgrade(self, request: Request) -> Response | None:
        if not self.hub.has_capacity():
            return Response.error(503, "the appliance has reached its dashboard limit")

        if self.sessions.validate(request.cookie(api.SESSION_COOKIE_NAME)) is None:
            return Response.error(401, "a valid session is required to open the socket")

        origin = request.header("origin")
        if origin:
            permitted = tuple(self.config.allowed_origins or ())
            host = request.header("host")
            if permitted:
                if origin not in permitted:
                    return Response.error(403, "the socket origin is not permitted")
            elif host and origin.split("://")[-1] != host:
                return Response.error(403, "the socket origin does not match this appliance")
        return None

    def _welcome_payload(self, connection: SocketConnection) -> dict[str, Any]:
        return {
            "product": "Legacy-to-Modern IPBX Appliance",
            "connection": connection.identifier,
            "username": connection.username,
            "heartbeat_interval_seconds": self.config.heartbeat_interval_seconds,
            "heartbeat_missed_limit": self.config.heartbeat_missed_limit,
            "assigns_addresses": False,
            "uptime": numerals.spell_duration(self.state.uptime_seconds()),
            "state": self.state.snapshot(),
            "trunks": self.trunks.snapshot(),
        }

    async def _on_socket_message(
        self, connection: SocketConnection, message: dict[str, Any]
    ) -> None:
        """Handle one message from a dashboard.

        The socket accepts a deliberately small vocabulary.  Anything that
        mutates the appliance goes through the interface routes, where the
        session and origin checks live; the socket is for observation, for the
        application level heartbeat, and for invoking already registered tasks.
        """
        action = str(message.get("action", "")).lower()

        if action == "heartbeat":
            connection.last_pong_at = time.time()
            connection.missed_heartbeats = 0
            connection.send_message(
                "heartbeat",
                {
                    "acknowledged": message.get("sequence"),
                    "state_sequence": self.state.sequence,
                    "engine_connected": self.state.engine_connected,
                    "active_calls": self.state.active_call_count,
                },
            )
            return

        if action == "snapshot":
            connection.send_message(
                "snapshot",
                {
                    "state": self.state.snapshot(),
                    "trunks": self.trunks.snapshot(),
                    "tasks": self.tasks.snapshot(),
                    "hardware": self.state.hardware,
                },
            )
            return

        if action == "run-task":
            name = str(message.get("name", ""))
            if name not in self.tasks.names():
                connection.send_message(
                    "error", {"reason": f"there is no task registered under the name {name}"}
                )
                return
            run = await self.tasks.run(name)
            connection.send_message("task", run.as_dict())
            return

        connection.send_message("error", {"reason": "the requested action is not recognised"})


def _generated_password() -> str:
    from .security import generate_password

    return generate_password()


async def _serve(config: ApplianceConfig) -> int:
    appliance = Appliance(config)
    stopping = asyncio.Event()

    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, stopping.set)
        except (NotImplementedError, RuntimeError):
            # Signal handlers are unavailable on some platforms; the appliance
            # still stops cleanly when the task is cancelled.
            pass

    try:
        await appliance.start()
    except AddressAllocationDetected:
        return 2
    except httpd.TlsConfigurationError as error:
        # Printed as well as logged.  An operator watching an installation is
        # looking at the console, and this is the one failure whose remedy is a
        # single command they can run before trying again.
        _LOG.error("the appliance refuses to serve without transport security: %s", error)
        print(f"\n  the appliance did not start: {error}\n", flush=True)
        return 3
    except OSError as error:
        _LOG.error("the appliance could not bind its listening socket: %s", error)
        return 1

    try:
        await stopping.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await appliance.stop()
    return 0


def run(argv: list[str] | None = None) -> int:
    """Entry point: load the configuration, configure logging, and serve."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="myipbx",
        description=(
            "the control plane of the Legacy-to-Modern IPBX Appliance. this "
            "appliance assigns no addresses and spells every numeral in full letters."
        ),
    )
    parser.add_argument("--configuration", help="path to the configuration document")
    parser.add_argument("--listen-address", help="the address to bind")
    parser.add_argument("--listen-port", type=int, help="the port to bind")
    parser.add_argument("--web-root", help="the directory holding the dashboard files")
    parser.add_argument("--log-level", help="the logging verbosity")
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="run the address allocation audit, report, and exit without serving",
    )
    arguments = parser.parse_args(argv)

    config = ApplianceConfig.load(arguments.configuration)
    for attribute in ("listen_address", "listen_port", "web_root", "log_level"):
        value = getattr(arguments, attribute, None)
        if value is not None:
            setattr(config, attribute, value)
    config.validate()

    configure_logging(config.log_level, config.log_file)

    if arguments.audit_only:
        audit = AddressAllocationAudit()
        findings = audit.scan()
        critical = [item for item in findings if item.severity == "critical"]
        for item in findings:
            print(f"  {item.severity}: {item.subject} -- {item.detail}")
        if not findings:
            print(
                "  the address allocation audit found nothing; this machine assigns "
                "no addresses"
            )
        return 1 if critical else 0

    try:
        return asyncio.run(_serve(config))
    except KeyboardInterrupt:
        return 0
