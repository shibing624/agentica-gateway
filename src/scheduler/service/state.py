"""State management for the scheduler service.

Contains dependency injection and runtime state management.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class JobExecutor(Protocol):
    """Protocol for job execution."""

    async def execute(self, job: Any) -> Any:
        """Execute a job and return the result."""
        ...


@dataclass
class SchedulerServiceDeps:
    """Dependencies for the scheduler service.

    This allows for dependency injection of external services.
    """
    executor: JobExecutor | None = None
    event_handler: Callable[[Any], None] | None = None


@dataclass
class SchedulerServiceState:
    """Runtime state of the scheduler service."""
    running: bool = False
    timer_task: asyncio.Task | None = None
    next_wake_at_ms: int | None = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Lock for concurrent access
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset(self) -> None:
        """Reset state to initial values."""
        self.running = False
        self.timer_task = None
        self.next_wake_at_ms = None
        self.wake_event.clear()
