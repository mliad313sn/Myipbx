"""Automated task execution.

A registry of named operations — render the configuration, reload the engine,
rescan the hardware, sweep the health, back up the state — that can be invoked
on a schedule or on demand from the dashboard.

Three properties matter here.  Tasks are single flight per name, so a slow task
cannot be stacked on top of itself by an impatient operator or by a schedule
that fires faster than the work completes.  Tasks carry a timeout, so a wedged
operation is reported rather than leaked.  Blocking work is dispatched to a
bounded worker pool rather than being called inline, so the event loop keeps
serving the socket while a driver compilation or a filesystem synchronisation
runs.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .logging_setup import get_logger

__all__ = ["TaskDefinition", "TaskRun", "TaskScheduler", "TaskError"]

_LOG = get_logger("tasks")

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed out"
STATUS_REJECTED = "rejected"
STATUS_RUNNING = "running"


class TaskError(RuntimeError):
    """Raised when a task cannot be accepted or cannot complete."""


@dataclass
class TaskDefinition:
    """One registered task."""

    name: str
    handler: Callable[..., Any]
    description: str = ""
    interval_seconds: float | None = None
    timeout_seconds: float | None = None
    blocking: bool = False

    #: Bookkeeping, maintained by the scheduler.
    run_count: int = 0
    failure_count: int = 0
    last_status: str | None = None
    last_detail: str = ""
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_duration_seconds: float | None = None

    def as_dict(self, now: float, running: bool) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "interval_seconds": self.interval_seconds,
            "timeout_seconds": self.timeout_seconds,
            "running": running,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "last_status": self.last_status,
            "last_detail": self.last_detail,
            "seconds_since_last_run": int(now - self.last_finished_at)
            if self.last_finished_at
            else None,
            "last_duration_seconds": self.last_duration_seconds,
        }


@dataclass
class TaskRun:
    """The outcome of one task invocation."""

    name: str
    status: str
    detail: str = ""
    result: Any = None
    started_at: float = 0.0
    finished_at: float = 0.0
    arguments: Mapping[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_SUCCEEDED

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "succeeded": self.succeeded,
            "duration_seconds": round(self.duration_seconds, 3),
            "result": self.result if _is_publishable(self.result) else str(self.result),
        }


class TaskScheduler:
    """Owns the task registry, the worker pool, and the interval loops."""

    def __init__(
        self,
        publisher: Callable[[str, dict[str, Any]], Any] | None = None,
        worker_pool_size: int = 4,
        default_timeout_seconds: float = 120.0,
        clock: Callable[[], float] | None = None,
        history_limit: int = 100,
    ) -> None:
        if worker_pool_size < 1:
            raise ValueError("the worker pool must have at least one worker")
        self._publisher = publisher
        self._default_timeout = default_timeout_seconds
        self._clock = clock or time.time
        self._history_limit = history_limit

        self._definitions: dict[str, TaskDefinition] = {}
        self._running: dict[str, asyncio.Task[TaskRun]] = {}
        self._schedules: dict[str, asyncio.Task[None]] = {}
        self._history: list[TaskRun] = []
        self._pool = ThreadPoolExecutor(
            max_workers=worker_pool_size, thread_name_prefix="crossbar-worker"
        )
        self._stopping = False

    # -- registration ------------------------------------------------------

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        description: str = "",
        interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
        blocking: bool = False,
    ) -> TaskDefinition:
        if name in self._definitions:
            raise TaskError(f"a task named {name} is already registered")
        definition = TaskDefinition(
            name=name,
            handler=handler,
            description=description,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds or self._default_timeout,
            blocking=blocking,
        )
        self._definitions[name] = definition
        return definition

    def names(self) -> list[str]:
        return sorted(self._definitions)

    def is_running(self, name: str) -> bool:
        task = self._running.get(name)
        return task is not None and not task.done()

    # -- invocation --------------------------------------------------------

    async def run(self, name: str, arguments: Mapping[str, Any] | None = None) -> TaskRun:
        """Invoke a task once, respecting single flight and the timeout."""
        definition = self._definitions.get(name)
        if definition is None:
            raise TaskError(f"there is no task registered under the name {name}")

        if self.is_running(name):
            run = TaskRun(
                name=name,
                status=STATUS_REJECTED,
                detail="the task is already running and will not be started again",
                started_at=self._clock(),
                finished_at=self._clock(),
                arguments=dict(arguments or {}),
            )
            self._record(run)
            return run

        task = asyncio.create_task(
            self._execute(definition, dict(arguments or {})), name=f"task-{name}"
        )
        self._running[name] = task
        try:
            return await task
        finally:
            if self._running.get(name) is task:
                del self._running[name]

    async def _execute(
        self, definition: TaskDefinition, arguments: dict[str, Any]
    ) -> TaskRun:
        started = self._clock()
        definition.last_started_at = started
        definition.run_count += 1
        self._publish("task.started", {"name": definition.name, "arguments": arguments})

        status = STATUS_SUCCEEDED
        detail = ""
        result: Any = None

        try:
            result = await asyncio.wait_for(
                self._invoke(definition, arguments),
                timeout=definition.timeout_seconds,
            )
        except asyncio.TimeoutError:
            status = STATUS_TIMED_OUT
            detail = "the task exceeded its timeout and was abandoned"
            definition.failure_count += 1
            _LOG.error("the task named %s timed out", definition.name)
        except asyncio.CancelledError:
            status = STATUS_FAILED
            detail = "the task was cancelled"
            definition.failure_count += 1
            raise
        except Exception as error:  # noqa: BLE001 - a task failure is data, not a crash
            status = STATUS_FAILED
            detail = str(error) or error.__class__.__name__
            definition.failure_count += 1
            _LOG.error(
                "the task named %s failed: %s\n%s",
                definition.name,
                detail,
                traceback.format_exc(limit=5),
            )
        finally:
            finished = self._clock()
            definition.last_finished_at = finished
            definition.last_duration_seconds = round(max(0.0, finished - started), 3)
            definition.last_status = status
            definition.last_detail = detail

        run = TaskRun(
            name=definition.name,
            status=status,
            detail=detail,
            result=result,
            started_at=started,
            finished_at=self._clock(),
            arguments=arguments,
        )
        self._record(run)
        self._publish("task.finished", run.as_dict())
        return run

    async def _invoke(self, definition: TaskDefinition, arguments: dict[str, Any]) -> Any:
        handler = definition.handler
        if definition.blocking:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._pool, lambda: handler(**arguments) if arguments else handler()
            )
        outcome = handler(**arguments) if arguments else handler()
        if asyncio.iscoroutine(outcome):
            return await outcome
        return outcome

    # -- scheduling --------------------------------------------------------

    def start(self) -> int:
        """Start an interval loop for every task that declares one."""
        self._stopping = False
        started = 0
        for definition in self._definitions.values():
            if not definition.interval_seconds:
                continue
            existing = self._schedules.get(definition.name)
            if existing is not None and not existing.done():
                continue
            self._schedules[definition.name] = asyncio.create_task(
                self._schedule(definition), name=f"schedule-{definition.name}"
            )
            started += 1
        return started

    async def _schedule(self, definition: TaskDefinition) -> None:
        interval = float(definition.interval_seconds or 0)
        # The first run is delayed by one interval so that startup is not a
        # thundering herd of every scheduled task at once.
        while not self._stopping:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if self._stopping:
                return
            try:
                await self.run(definition.name)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a schedule must survive
                _LOG.error(
                    "the scheduled task named %s could not be invoked: %s",
                    definition.name,
                    error,
                )

    async def stop(self) -> None:
        """Stop every schedule, cancel running tasks, and close the pool."""
        self._stopping = True
        pending = list(self._schedules.values()) + list(self._running.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._schedules.clear()
        self._running.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- bookkeeping -------------------------------------------------------

    def _record(self, run: TaskRun) -> None:
        self._history.append(run)
        if len(self._history) > self._history_limit:
            del self._history[: -self._history_limit]

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher(topic, payload)
        except Exception as error:  # noqa: BLE001 - publication is best effort
            _LOG.error("a task event could not be published: %s", error)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [run.as_dict() for run in self._history[-limit:]][::-1]

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "tasks": [
                definition.as_dict(now, self.is_running(name))
                for name, definition in sorted(self._definitions.items())
            ],
            "running": [name for name in self._definitions if self.is_running(name)],
            "history": self.history(),
        }


def _is_publishable(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_publishable(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_publishable(item) for key, item in value.items()
        )
    return False
