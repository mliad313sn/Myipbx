"""The representational state transfer interface.

Read routes describe the appliance; write routes change it.  Every write route
requires an authenticated session and a matching origin, and every route
returns a plain language reason on failure rather than a bare status.

The route table is built against a context object supplied by the composition
root, so the interface has no global state and can be exercised in tests
against a synthetic appliance.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import backup, entities, firewall, numerals, sysops
from .confstore import DriftDetected
from .httpd import Request, Response, Router
from .logging_setup import get_logger
from .trunks import TrunkState

VERSION = "one point two point zero"

__all__ = ["build_router", "SESSION_COOKIE_NAME"]

_LOG = get_logger("api")

SESSION_COOKIE_NAME = "myipbx_session"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: The privileged operations that make up interface card driver bring-up.  A
#: failure in one of these is published on the transport's bypass list rather
#: than left for the next state change, because an operator is standing at the
#: appliance waiting on the answer when they run one.
DRIVER_STAGE_VERBS = frozenset({"driver-rebuild", "span-generate"})


def build_router(context: Any) -> Router:
    """Build the complete route table for an appliance context."""
    router = Router()
    guard = _Guard(context)
    state_cache = _StateResponseCache(context)

    # -- unauthenticated -------------------------------------------------
    router.get("/api/health", lambda request: _health(context, request))
    router.get("/api/session", lambda request: _session_status(context, request))
    router.post("/api/session", lambda request: _sign_in(context, request))

    # -- authenticated reads ---------------------------------------------
    router.get("/api/state", guard.read(lambda request: state_cache.response()))
    router.get("/api/hardware", guard.read(lambda request: _hardware(context)))
    router.get("/api/trunks", guard.read(lambda request: _trunks(context)))
    router.get("/api/tasks", guard.read(lambda request: _tasks(context)))
    router.get("/api/sessions", guard.read(lambda request: _sessions(context)))
    router.get("/api/constraints", guard.read(lambda request: _constraints(context)))
    router.get("/api/configuration", guard.read(lambda request: _configuration(context)))
    router.get(
        "/api/configuration/drift", guard.read(lambda request: _drift(context))
    )

    # -- authenticated writes ---------------------------------------------
    router.post("/api/session/end", guard.write(lambda request: _sign_out(context, request)))
    router.post(
        "/api/configuration", guard.write(lambda request: _save_configuration(context, request))
    )
    router.post(
        "/api/configuration/render",
        guard.write(lambda request: _render_configuration(context, request)),
    )
    router.post(
        "/api/configuration/adopt",
        guard.write(lambda request: _adopt_artefact(context, request)),
    )
    router.post("/api/trunks/control", guard.write(lambda request: _control_trunk(context, request)))
    router.post("/api/tasks/run", guard.write(lambda request: _run_task(context, request)))

    # -- telephony objects, created and edited entirely from the interface --
    router.get("/api/schema", guard.read(lambda request: _schema(context)))
    router.get(
        "/api/entities/{kind}", guard.read(lambda request: _entities_list(context, request))
    )
    router.get(
        "/api/entities/{kind}/{key}", guard.read(lambda request: _entity_read(context, request))
    )
    router.post(
        "/api/entities/{kind}", guard.write(lambda request: _entity_create(context, request))
    )
    router.put(
        "/api/entities/{kind}/{key}",
        guard.write(lambda request: _entity_update(context, request)),
    )
    router.delete(
        "/api/entities/{kind}/{key}",
        guard.write(lambda request: _entity_delete(context, request)),
    )

    # -- the machine itself -------------------------------------------------
    router.get("/api/system", guard.read(lambda request: _system(context)))
    router.get("/api/system/operations", guard.read(lambda request: _operations(context)))
    router.post(
        "/api/system/operations/{verb}",
        guard.write(lambda request: _run_operation(context, request)),
    )

    # -- diagnostics --------------------------------------------------------
    router.get("/api/firewall", guard.read(lambda request: _firewall(context)))
    router.get("/api/logs", guard.read(lambda request: _log_catalogue(context)))
    router.get("/api/logs/{key}", guard.read(lambda request: _log_read(context, request)))
    router.get("/api/calls", guard.read(lambda request: _call_records(context, request)))

    # -- backup and restore -------------------------------------------------
    router.get("/api/backup", guard.read(lambda request: _backup(context)))
    router.post("/api/restore", guard.write(lambda request: _restore(context, request)))

    router.serve_static(context.config.web_root)
    return router


class _Guard:
    """Session and origin enforcement for interface routes."""

    def __init__(self, context: Any) -> None:
        self._context = context

    def read(self, handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
        def wrapped(request: Request) -> Any:
            refusal = self._authenticate(request)
            return refusal if refusal is not None else handler(request)

        return wrapped

    def write(self, handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
        def wrapped(request: Request) -> Any:
            refusal = self._authenticate(request)
            if refusal is not None:
                return refusal
            refusal = self._check_origin(request)
            return refusal if refusal is not None else handler(request)

        return wrapped

    def _authenticate(self, request: Request) -> Response | None:
        token = request.cookie(SESSION_COOKIE_NAME)
        session = self._context.sessions.validate(token)
        if session is None:
            return Response.error(401, "a valid session is required for this request")
        # Attach for handlers that need to know who is acting.
        setattr(request, "session", session)
        return None

    def _check_origin(self, request: Request) -> Response | None:
        origin = request.header("origin")
        if not origin:
            # A request with no origin header did not come from a browser page,
            # which is the only threat this check exists to defeat.
            return None

        permitted = tuple(self._context.config.allowed_origins or ())
        if permitted:
            if origin in permitted:
                return None
            return Response.error(403, "the request origin is not permitted")

        # With no explicit list configured, accept only an origin whose host
        # matches the host the request was addressed to.
        host = request.header("host")
        if host and origin.split("://")[-1] == host:
            return None
        return Response.error(403, "the request origin does not match this appliance")


# -- read handlers ---------------------------------------------------------


def _health(context: Any, request: Request) -> Response:
    """A summary safe to serve without a session, used by monitoring."""
    snapshot = context.state.snapshot()
    return Response.json(
        {
            "product": "Legacy-to-Modern IPBX Appliance",
            "healthy": bool(snapshot.get("engine_connected"))
            and not context.state.alarms,
            "engine_connected": snapshot.get("engine_connected"),
            "uptime": numerals.spell_duration(int(snapshot.get("uptime_seconds", 0))),
            "active_calls": numerals.spell_integer(int(snapshot.get("active_calls", 0))),
            "alarm_count": numerals.spell_integer(len(context.state.alarms)),
            "assigns_addresses": False,
            "address_allocation_findings": numerals.spell_integer(
                len(getattr(context, "audit_findings", ()) or ())
            ),
        }
    )


def _session_status(context: Any, request: Request) -> Response:
    """Report whether the caller already holds a session.

    This answers with success whether or not a session exists, because asking
    the question is not an error.  A dashboard that probed a protected route
    instead would make the browser log a failed request on every page load,
    which trains an operator to ignore the console exactly when it matters.
    """
    session = context.sessions.validate(request.cookie(SESSION_COOKIE_NAME))
    return Response.json(
        {
            "authenticated": session is not None,
            "username": session.username if session else None,
        }
    )


def _state(context: Any) -> Response:
    payload = context.state.snapshot()
    payload["socket"] = context.hub.snapshot()
    payload["engine"] = context.manager.status() if context.manager else None
    payload["spelled"] = _spelled_summary(payload)
    return Response.json(payload)


class _StateResponseCache:
    """The serialised body of the state route, reused within one window.

    Rebuilding this response means walking every live channel and every alarm,
    spelling the summary, and serialising the result.  With a hundred calls up
    that is the most expensive read the interface serves, and a dashboard for
    every operator on the floor asks for it.  Answering each of them by doing
    the same work again is what makes the interface feel slow at exactly the
    moment the appliance is busiest.

    Two things bound how stale the cached body may be.  A change to the live
    state advances the state sequence number, and a body built under an older
    sequence is discarded on sight, so a caller can never read a snapshot from
    before a change it could have observed.  The remaining fields — the socket
    population, the engine's own status, the elapsed durations — move without
    any state change behind them, so the body additionally expires once the
    coalescing window has passed.  A window of zero therefore expires the body
    immediately and restores the original behaviour exactly.

    The body is shared between callers, which is only sound because this route
    varies by nothing: the handler never reads the request, and a session
    carries no role that could narrow what it is shown.
    """

    def __init__(self, context: Any) -> None:
        self._context = context
        self._response: Response | None = None
        self._sequence: int | None = None
        self._built_at = 0.0

    def _window_seconds(self) -> float:
        window = getattr(self._context.config, "broadcast_coalesce_milliseconds", 0)
        return max(0, int(window)) / 1000.0

    def response(self) -> Response:
        sequence = getattr(self._context.state, "sequence", None)
        window = self._window_seconds()
        now = time.monotonic()

        fresh = (
            self._response is not None
            and self._sequence == sequence
            and now - self._built_at < window
        )
        if fresh:
            return self._response  # type: ignore[return-value]

        # Assembling the body never awaits, so no change can land part way
        # through it, and the sequence read afterwards is the one the body
        # genuinely describes.  Recording it after the fact rather than before
        # keeps that true even if the body ever grows an asynchronous step.
        response = _state(self._context)
        self._response = response
        self._sequence = getattr(self._context.state, "sequence", None)
        self._built_at = now
        return response


def _hardware(context: Any) -> Response:
    inventory = context.hardware.scan()
    context.state.hardware = inventory
    return Response.json(inventory)


def _trunks(context: Any) -> Response:
    snapshot = context.trunks.snapshot()
    snapshot["spelled"] = {
        "total": numerals.spell_integer(snapshot["total"]),
        "registered": numerals.spell_integer(snapshot["registered"]),
    }
    return Response.json(snapshot)


def _tasks(context: Any) -> Response:
    return Response.json(context.tasks.snapshot())


def _sessions(context: Any) -> Response:
    now = time.monotonic()
    return Response.json(
        {
            "sessions": [
                {
                    "identifier": session.identifier,
                    "username": session.username,
                    "source_address": session.source_address,
                    "age": numerals.spell_duration(session.age_seconds(now)),
                    "idle": numerals.spell_duration(session.idle_seconds(now)),
                }
                for session in context.sessions.active()
            ],
            "count": numerals.spell_integer(len(context.sessions)),
        }
    )


def _constraints(context: Any) -> Response:
    """Report the enforcement state of both absolute product constraints."""
    findings = context.audit.scan()
    critical = [item for item in findings if item.severity == "critical"]
    return Response.json(
        {
            "address_allocation": {
                "statement": (
                    "this appliance contains no address allocation service and never "
                    "assigns Internet Protocol addresses"
                ),
                "enforced_at": [
                    "build time, by staging scripts that install no allocation service",
                    "install time, by the audit script that fails the installation",
                    "run time, by this audit on startup and on every health sweep",
                    "repository time, by an automated test over the whole source tree",
                ],
                "findings": [item.as_dict() for item in findings],
                "finding_count": numerals.spell_integer(len(findings)),
                "critical_count": numerals.spell_integer(len(critical)),
                "satisfied": not critical,
            },
            "spelled_numerals": {
                "statement": (
                    "every operator facing surface renders numbers as words; the "
                    "logging formatter sanitises every emitted line unconditionally"
                ),
                "satisfied": True,
            },
        }
    )


def _configuration(context: Any) -> Response:
    document = context.store.load()
    return Response.json(
        {
            "document": document,
            "revision": numerals.spell_integer(int(document.get("revision", 0))),
            "drift": [report.as_dict() for report in context.store.inspect(document)],
        }
    )


def _drift(context: Any) -> Response:
    reports = context.store.inspect()
    diverged = [report for report in reports if report.diverged]
    return Response.json(
        {
            "reports": [report.as_dict() for report in reports],
            "diverged_count": numerals.spell_integer(len(diverged)),
            "reconciliation_required": bool(diverged),
            "explanation": (
                "a generated file was modified outside the appliance; choose to adopt "
                "the file on disk as authoritative, or to regenerate over it"
            )
            if diverged
            else "every generated file matches what the appliance last wrote",
        }
    )


# -- write handlers --------------------------------------------------------


def _sign_in(context: Any, request: Request) -> Response:
    source = request.peer

    if context.throttle.is_locked(source):
        remaining = context.throttle.remaining_lockout_seconds(source)
        return Response.error(
            429,
            "too many failed attempts from this address; try again in "
            f"{numerals.spell_duration(remaining)}",
        )

    payload = request.json() or {}
    if not isinstance(payload, dict):
        return Response.error(400, "the request body must be a mapping")

    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))

    if not context.credentials.verify(username, password):
        locked = context.throttle.record_failure(source)
        _LOG.warning("a sign in attempt from %s was refused", source)
        return Response.error(
            401,
            "the credentials were not accepted"
            + (
                "; this address is now locked out temporarily"
                if locked
                else ""
            ),
        )

    context.throttle.record_success(source)
    token = context.sessions.create(username, source)
    _LOG.info("the administrator named %s signed in from %s", username, source)

    attributes = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={context.config.session_idle_seconds}",
    ]
    if getattr(context.config, "session_cookie_secure", False):
        attributes.append("Secure")

    return Response.json(
        {"signed_in": True, "username": username},
        headers={"Set-Cookie": "; ".join(attributes)},
    )


def _sign_out(context: Any, request: Request) -> Response:
    token = request.cookie(SESSION_COOKIE_NAME)
    revoked = context.sessions.revoke(token)
    expired = f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    return Response.json({"signed_out": revoked}, headers={"Set-Cookie": expired})


def _save_configuration(context: Any, request: Request) -> Response:
    payload = request.json()
    if not isinstance(payload, dict):
        return Response.error(400, "the configuration document must be a mapping")

    document = context.store.save(payload)
    context.trunks.declare_many(document.get("trunks", []) or [])
    context.state.touch()
    return Response.json(
        {
            "saved": True,
            "revision": numerals.spell_integer(int(document.get("revision", 0))),
            "trunk_count": numerals.spell_integer(len(document.get("trunks", []) or [])),
        }
    )


def _render_configuration(context: Any, request: Request) -> Response:
    payload = request.json() or {}
    force = bool(payload.get("force", False)) if isinstance(payload, dict) else False

    try:
        outcome = context.store.render(force=force)
    except DriftDetected as error:
        return Response.json(
            {
                "rendered": False,
                "reconciliation_required": True,
                "reason": str(error),
                "reports": [report.as_dict() for report in error.reports],
            },
            status=409,
        )

    return Response.json(
        {
            "rendered": True,
            "written": outcome["written"],
            "written_count": numerals.spell_integer(len(outcome["written"])),
            "forced": outcome["forced"],
        }
    )


def _adopt_artefact(context: Any, request: Request) -> Response:
    payload = request.json() or {}
    name = str(payload.get("name", "")) if isinstance(payload, dict) else ""
    if not name:
        return Response.error(400, "the name of the file to adopt is required")
    try:
        report = context.store.adopt(name)
    except FileNotFoundError as error:
        return Response.error(404, str(error))
    return Response.json({"adopted": True, "report": report.as_dict()})


def _control_trunk(context: Any, request: Request) -> Response:
    payload = request.json() or {}
    if not isinstance(payload, dict):
        return Response.error(400, "the request body must be a mapping")

    name = str(payload.get("name", ""))
    action = str(payload.get("action", "")).lower()
    trunk = context.trunks.get(name)
    if trunk is None:
        return Response.error(404, f"there is no trunk named {name}")

    if action == "enable":
        context.trunks.declare(
            name=trunk.name,
            technology=trunk.technology,
            host=trunk.host,
            username=trunk.username,
            enabled=True,
        )
        started = context.trunks.start(name)
        return Response.json({"trunk": name, "enabled": True, "driver_started": started})

    if action == "disable":
        context.trunks.declare(
            name=trunk.name,
            technology=trunk.technology,
            host=trunk.host,
            username=trunk.username,
            enabled=False,
        )
        return Response.json({"trunk": name, "enabled": False})

    if action == "register":
        if trunk.state is TrunkState.REGISTERED:
            return Response.json({"trunk": name, "already_registered": True})
        started = context.trunks.start(name)
        return Response.json({"trunk": name, "driver_started": started})

    return Response.error(400, "the requested action is not recognised")


async def _run_task(context: Any, request: Request) -> Response:
    payload = request.json() or {}
    if not isinstance(payload, dict):
        return Response.error(400, "the request body must be a mapping")

    name = str(payload.get("name", ""))
    if name not in context.tasks.names():
        return Response.error(404, f"there is no task registered under the name {name}")

    run = await context.tasks.run(name, payload.get("arguments") or {})
    status = 200 if run.succeeded or run.status == "rejected" else 500
    return Response.json(run.as_dict(), status=status)


# -- telephony objects -----------------------------------------------------


def _schema(context: Any) -> Response:
    """The object schema the interface builds its forms from."""
    return Response.json(entities.schema())


def _entity_context(context: Any) -> tuple[dict[str, Any], entities.EntityStore]:
    document = context.store.load()
    return document, entities.EntityStore(document, context.secrets)


def _commit(context: Any, document: dict[str, Any]) -> dict[str, Any]:
    """Persist a mutated document and bring the running appliance into line."""
    saved = context.store.save(document)
    context.trunks.declare_many(saved.get("trunks", []) or [])
    context.state.touch()
    return saved


def _entities_list(context: Any, request: Request) -> Response:
    kind = request.parameter("kind")
    try:
        _, store = _entity_context(context)
        records = store.list(kind)
    except KeyError as error:
        return Response.error(404, str(error))

    spec = entities.ENTITY_SPECS[kind]
    return Response.json(
        {
            "kind": kind,
            "singular": spec.singular,
            "plural": spec.plural,
            "records": records,
            "count": numerals.spell_integer(len(records)),
        }
    )


def _entity_read(context: Any, request: Request) -> Response:
    kind = request.parameter("kind")
    key = request.parameter("key")
    try:
        _, store = _entity_context(context)
        record = store.get(kind, key)
    except KeyError as error:
        return Response.error(404, str(error))

    if record is None:
        return Response.error(404, f"there is nothing of that kind identified as {key}")
    return Response.json({"kind": kind, "record": record})


def _entity_create(context: Any, request: Request) -> Response:
    kind = request.parameter("kind")
    payload = request.json()
    if not isinstance(payload, dict):
        return Response.error(400, "the submission must be a mapping")

    try:
        document, store = _entity_context(context)
        record = store.create(kind, payload)
    except KeyError as error:
        return Response.error(404, str(error))
    except entities.ValidationError as error:
        return Response.json(
            {"accepted": False, "errors": error.errors}, status=422
        )

    _commit(context, document)
    return Response.json({"accepted": True, "record": record}, status=201)


def _entity_update(context: Any, request: Request) -> Response:
    kind = request.parameter("kind")
    key = request.parameter("key")
    payload = request.json()
    if not isinstance(payload, dict):
        return Response.error(400, "the submission must be a mapping")

    try:
        document, store = _entity_context(context)
        record = store.update(kind, key, payload)
    except KeyError as error:
        return Response.error(404, str(error))
    except entities.ValidationError as error:
        return Response.json({"accepted": False, "errors": error.errors}, status=422)

    _commit(context, document)
    return Response.json({"accepted": True, "record": record})


def _entity_delete(context: Any, request: Request) -> Response:
    kind = request.parameter("kind")
    key = request.parameter("key")

    try:
        document, store = _entity_context(context)
        removed = store.delete(kind, key)
    except KeyError as error:
        return Response.error(404, str(error))
    except entities.ValidationError as error:
        # A deletion that would leave something dangling is refused with an
        # explanation of exactly what still refers to it.
        return Response.json({"accepted": False, "errors": error.errors}, status=409)

    if not removed:
        return Response.error(404, f"there is nothing of that kind identified as {key}")

    _commit(context, document)
    return Response.json({"accepted": True, "deleted": key})


# -- the machine itself ----------------------------------------------------


def _system(context: Any) -> Response:
    payload = context.system.snapshot()
    payload["appliance"] = {
        "uptime": numerals.spell_duration(context.state.uptime_seconds()),
        "engine_connected": context.state.engine_connected,
        "dashboards_connected": numerals.spell_integer(context.hub.connection_count),
        "version": VERSION,
    }
    payload["privileged_operations"] = context.operations.describe()
    return Response.json(payload)


def _operations(context: Any) -> Response:
    return Response.json(context.operations.describe())


async def _run_operation(context: Any, request: Request) -> Response:
    verb = request.parameter("verb")
    payload = request.json() or {}
    if not isinstance(payload, dict):
        return Response.error(400, "the request body must be a mapping")

    session = getattr(request, "session", None)
    _LOG.warning(
        "the administrator named %s asked for the system operation named %s",
        session.username if session else "unknown",
        verb,
    )

    try:
        outcome = await context.operations.run(verb, payload)
    except sysops.OperationRefused as error:
        return Response.error(400, str(error))

    if verb in DRIVER_STAGE_VERBS and not outcome.succeeded:
        # Driver bring-up is the one place an operator is watching the console
        # and cannot proceed until they know, so the failure is put on the
        # transport's bypass list rather than folded into the next snapshot.
        _publish_urgently(
            context,
            "driver.stage-failed",
            {
                "verb": verb,
                "detail": getattr(outcome, "detail", "")
                or "the stage did not report a reason",
            },
        )

    return Response.json(outcome.as_dict(), status=200 if outcome.succeeded else 500)


def _publish_urgently(context: Any, topic: str, payload: dict[str, Any]) -> None:
    """Publish on a topic the transport is told never to hold back."""
    publisher = getattr(getattr(context, "state", None), "publisher", None)
    if publisher is None:
        return
    try:
        publisher(topic, payload)
    except Exception as error:  # noqa: BLE001 - publication is best effort
        _LOG.error("an urgent notice could not be published: %s", error)


def _firewall(context: Any) -> Response:
    """Describe the ruleset the appliance would generate, and its state."""
    document = context.store.load()
    rules = document.get("firewall_rules", []) or []
    port = context.http.bound_port or context.config.listen_port

    payload = firewall.summarise(rules, port)
    ruleset_path = context.config.state_path / "firewall.nft"
    payload["generated"] = ruleset_path.is_file()
    payload["explanation"] = (
        "the appliance generates the ruleset and its helper loads it, so nothing "
        "typed here is ever turned into a rule by the privileged side. the "
        "console remains reachable whatever else is declared, so a firewall "
        "cannot lock you out of the appliance that applied it"
    )

    try:
        payload["preview"] = firewall.render_ruleset(
            rules,
            management_port=port,
            management_sources=document.get("firewall_management_sources", ["0.0.0.0/0"]),
        )
    except firewall.FirewallError as error:
        payload["preview"] = ""
        payload["error"] = str(error)

    return Response.json(payload)


# -- diagnostics -----------------------------------------------------------


def _log_catalogue(context: Any) -> Response:
    return Response.json({"logs": context.logs.catalogue()})


def _log_read(context: Any, request: Request) -> Response:
    key = request.parameter("key")
    try:
        lines = int(request.query.get("lines", "200"))
    except ValueError:
        lines = 200

    try:
        payload = context.logs.read(
            key,
            lines=lines,
            search=request.query.get("search", ""),
            level=request.query.get("level", ""),
        )
    except KeyError as error:
        return Response.error(404, str(error))
    return Response.json(payload)


def _call_records(context: Any, request: Request) -> Response:
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        limit = 100
    return Response.json(
        context.calls.read(limit=limit, search=request.query.get("search", ""))
    )


# -- backup and restore ----------------------------------------------------


def _backup(context: Any) -> Response:
    """Produce a complete backup as a single downloadable archive."""
    try:
        payload, name = backup.create(context)
    except OSError as error:
        return Response.error(500, f"the backup could not be produced: {error}")

    return Response(
        status=200,
        body=payload,
        content_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            # A backup carries secrets, so it must never sit in a cache.
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
        },
    )


def _restore(context: Any, request: Request) -> Response:
    if not request.body:
        return Response.error(400, "the request carried no archive to restore")

    try:
        outcome = backup.restore(context, request.body)
    except backup.RestoreRefused as error:
        return Response.error(422, str(error))
    except OSError as error:
        return Response.error(500, f"the restore could not be completed: {error}")

    return Response.json(outcome)


# -- helpers ---------------------------------------------------------------


def _spelled_summary(snapshot: dict[str, Any]) -> dict[str, str]:
    """Pre-spell the headline figures so the dashboard cannot forget to."""
    return {
        "active_calls": numerals.spell_integer(int(snapshot.get("active_calls", 0))),
        "answered_calls": numerals.spell_integer(int(snapshot.get("answered_calls", 0))),
        "calls_started": numerals.spell_integer(int(snapshot.get("calls_started", 0))),
        "calls_completed": numerals.spell_integer(int(snapshot.get("calls_completed", 0))),
        "peak_concurrent_calls": numerals.spell_integer(
            int(snapshot.get("peak_concurrent_calls", 0))
        ),
        "uptime": numerals.spell_duration(int(snapshot.get("uptime_seconds", 0))),
    }
