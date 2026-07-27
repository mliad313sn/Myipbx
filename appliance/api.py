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

from . import numerals
from .confstore import DriftDetected
from .httpd import Request, Response, Router
from .logging_setup import get_logger
from .trunks import TrunkState

__all__ = ["build_router", "SESSION_COOKIE_NAME"]

_LOG = get_logger("api")

SESSION_COOKIE_NAME = "myipbx_session"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def build_router(context: Any) -> Router:
    """Build the complete route table for an appliance context."""
    router = Router()
    guard = _Guard(context)

    # -- unauthenticated -------------------------------------------------
    router.get("/api/health", lambda request: _health(context, request))
    router.post("/api/session", lambda request: _sign_in(context, request))

    # -- authenticated reads ---------------------------------------------
    router.get("/api/state", guard.read(lambda request: _state(context)))
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


def _state(context: Any) -> Response:
    payload = context.state.snapshot()
    payload["socket"] = context.hub.snapshot()
    payload["engine"] = context.manager.status() if context.manager else None
    payload["spelled"] = _spelled_summary(payload)
    return Response.json(payload)


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
