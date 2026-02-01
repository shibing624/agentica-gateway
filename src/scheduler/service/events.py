"""Event system for the scheduler.

Emits events for job lifecycle changes.
"""
import time
from typing import Any, Callable

from loguru import logger

from ..types import SchedulerEvent, HookPoint

logger = logger.bind(module="scheduler.events")


# Type alias for event handlers
EventHandler = Callable[[SchedulerEvent], None]


class EventEmitter:
    """Event emitter for scheduler events."""

    def __init__(self):
        self._handlers: list[EventHandler] = []

    def add_handler(self, handler: EventHandler) -> None:
        """Add an event handler."""
        self._handlers.append(handler)

    def remove_handler(self, handler: EventHandler) -> None:
        """Remove an event handler."""
        if handler in self._handlers:
            self._handlers.remove(handler)

    def emit(self, event: SchedulerEvent) -> None:
        """Emit an event to all handlers."""
        for handler in self._handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Event handler error: {e}")


def emit_job_event(
    emitter: EventEmitter,
    event_type: str,
    job_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a job-related event.

    Args:
        emitter: Event emitter instance
        event_type: Type of event (e.g., "job.started", "job.completed")
        job_id: ID of the job
        payload: Additional event payload
    """
    event = SchedulerEvent(
        type=event_type,
        job_id=job_id,
        timestamp_ms=int(time.time() * 1000),
        payload=payload or {},
    )
    emitter.emit(event)


def emit_hook_event(
    emitter: EventEmitter,
    hook_point: HookPoint,
    job_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a hook-related event.

    Args:
        emitter: Event emitter instance
        hook_point: The hook point (before_run, after_run, etc.)
        job_id: ID of the job
        payload: Additional event payload
    """
    event = SchedulerEvent(
        type=f"hook.{hook_point.value}",
        job_id=job_id,
        timestamp_ms=int(time.time() * 1000),
        payload=payload or {},
    )
    emitter.emit(event)


# Event type constants
class EventTypes:
    """Constants for event types."""

    # Scheduler lifecycle
    SCHEDULER_STARTED = "scheduler.started"
    SCHEDULER_STOPPED = "scheduler.stopped"

    # Job lifecycle
    JOB_ADDED = "job.added"
    JOB_UPDATED = "job.updated"
    JOB_REMOVED = "job.removed"
    JOB_PAUSED = "job.paused"
    JOB_RESUMED = "job.resumed"

    # Job execution
    JOB_STARTED = "job.started"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    JOB_TIMEOUT = "job.timeout"
    JOB_RETRYING = "job.retrying"

    # Task chain
    CHAIN_TRIGGERED = "chain.triggered"
    CHAIN_COMPLETED = "chain.completed"
