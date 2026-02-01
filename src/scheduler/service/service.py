"""Main Scheduler Service class.

This is the unified entry point for all scheduler operations.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Optional, List

from loguru import logger

from ..models import ScheduledJob, JobCreate, JobPatch
from ..types import (
    JobStatus,
    RemoveResult,
    RunResult,
    SchedulerStatus,
)
from .state import SchedulerServiceDeps, SchedulerServiceState
from .store import JobStore
from .events import EventEmitter, emit_job_event, EventTypes
from . import ops
from . import timer

logger = logger.bind(module="scheduler.service")


class SchedulerService:
    """Unified scheduler service for managing scheduled jobs.

    This replaces both the legacy Scheduler class and the APScheduler-based
    SchedulerService with a simpler asyncio-based implementation.
    """

    def __init__(
        self,
        db_path: str | Path = "~/.agentica/scheduler.db",
        executor: Any = None,
        notification_sender: Any = None,
    ):
        """Initialize scheduler service.

        Args:
            db_path: Path to SQLite database for persistence
            executor: Job executor (implements execute(job) -> Any)
            notification_sender: Notification sender (implements send(channel, chat_id, msg) -> bool)
        """
        db_path = Path(db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.store = JobStore(db_path)
        self.events = EventEmitter()
        self.deps = SchedulerServiceDeps(
            executor=executor,
            notification_sender=notification_sender,
        )
        self.state = SchedulerServiceState()

    async def start(self) -> None:
        """Start the scheduler service."""
        if self.state.running:
            logger.warning("Scheduler already running")
            return

        # Initialize store
        await self.store.initialize()

        # Activate pending jobs
        await self._activate_jobs()

        # Start timer loop
        self.state.running = True
        self.state.timer_task = asyncio.create_task(timer.timer_loop(self))

        # Arm timer for first wake
        await timer.arm_timer(self)

        emit_job_event(self.events, EventTypes.SCHEDULER_STARTED, "")
        logger.info("Scheduler service started")

    async def stop(self) -> None:
        """Stop the scheduler service gracefully."""
        if not self.state.running:
            return

        self.state.running = False

        # Cancel timer task
        if self.state.timer_task:
            self.state.timer_task.cancel()
            try:
                await self.state.timer_task
            except asyncio.CancelledError:
                pass

        # Close store
        await self.store.close()

        self.state.reset()

        emit_job_event(self.events, EventTypes.SCHEDULER_STOPPED, "")
        logger.info("Scheduler service stopped")

    async def status(self) -> SchedulerStatus:
        """Get scheduler status.

        Returns:
            Current scheduler status
        """
        return await ops.get_status(self.store, self.state)

    # ============== Job Management ==============

    async def add(self, job: JobCreate) -> ScheduledJob:
        """Add a new scheduled job.

        Args:
            job: Job creation request

        Returns:
            Created job
        """
        created_job = await ops.add_job(self.store, self.events, job)

        # Re-arm timer if needed
        if self.state.running:
            await timer.arm_timer(self)

        return created_job

    async def update(self, job_id: str, patch: JobPatch) -> ScheduledJob | None:
        """Update an existing job.

        Args:
            job_id: ID of job to update
            patch: Update request

        Returns:
            Updated job, or None if not found
        """
        updated_job = await ops.update_job(self.store, self.events, job_id, patch)

        # Re-arm timer if schedule changed
        if updated_job and self.state.running and patch.schedule is not None:
            await timer.arm_timer(self)

        return updated_job

    async def remove(self, job_id: str) -> RemoveResult:
        """Remove a scheduled job.

        Args:
            job_id: ID of job to remove

        Returns:
            Remove result
        """
        result = await ops.remove_job(self.store, self.events, job_id)

        if result.removed and self.state.running:
            await timer.arm_timer(self)

        return result

    async def get(self, job_id: str) -> ScheduledJob | None:
        """Get a job by ID.

        Args:
            job_id: Job ID

        Returns:
            Job if found, None otherwise
        """
        return await self.store.get(job_id)

    async def list(
        self,
        user_id: str | None = None,
        include_disabled: bool = False,
        limit: int = 100,
    ) -> list[ScheduledJob]:
        """List jobs with optional filters.

        Args:
            user_id: Filter by user ID
            include_disabled: Include disabled jobs
            limit: Maximum number of jobs to return

        Returns:
            List of jobs
        """
        return await self.store.list_jobs(
            user_id=user_id,
            include_disabled=include_disabled,
            limit=limit,
        )

    async def pause(self, job_id: str) -> ScheduledJob | None:
        """Pause a scheduled job.

        Args:
            job_id: ID of job to pause

        Returns:
            Updated job, or None if not found
        """
        job = await ops.pause_job(self.store, self.events, job_id)

        if job and self.state.running:
            await timer.arm_timer(self)

        return job

    async def resume(self, job_id: str) -> ScheduledJob | None:
        """Resume a paused job.

        Args:
            job_id: ID of job to resume

        Returns:
            Updated job, or None if not found
        """
        job = await ops.resume_job(self.store, self.events, job_id)

        if job and self.state.running:
            await timer.arm_timer(self)

        return job

    async def run(self, job_id: str, mode: str = "due") -> RunResult:
        """Run a job immediately.

        Args:
            job_id: ID of job to run
            mode: Run mode ("due" = only if due, "force" = always)

        Returns:
            Run result
        """
        force = mode == "force"
        return await timer.run_single_job(self, job_id, force=force)

    async def chain(
        self,
        job_id: str,
        next_job_id: str,
        on_status: Optional[List[str]] = None,
    ) -> ScheduledJob | None:
        """Chain two jobs together.

        Args:
            job_id: ID of source job
            next_job_id: ID of job to trigger on completion
            on_status: List of statuses that trigger the chain

        Returns:
            Updated source job, or None if not found
        """
        return await ops.chain_jobs(
            self.store, self.events, job_id, next_job_id, on_status
        )

    # ============== Event Handling ==============

    def on_event(self, handler: Callable[[Any], None]) -> None:
        """Register an event handler.

        Args:
            handler: Function to call when events are emitted
        """
        self.events.add_handler(handler)

    def off_event(self, handler: Callable[[Any], None]) -> None:
        """Unregister an event handler.

        Args:
            handler: Handler to remove
        """
        self.events.remove_handler(handler)

    # ============== Internal ==============

    async def _activate_jobs(self) -> None:
        """Activate all pending jobs on startup."""
        jobs = await self.store.list_jobs(include_disabled=True)

        activated = 0
        for job in jobs:
            if job.status == JobStatus.PENDING and job.enabled:
                job.status = JobStatus.ACTIVE
                await self.store.save(job)
                activated += 1

        if activated:
            logger.info(f"Activated {activated} pending jobs")

    # ============== Legacy Compatibility ==============
    # These methods provide compatibility with the old API

    def get_status(self) -> dict:
        """Legacy: Get scheduler status as dict.

        Use status() instead for the new API.
        """
        return {
            "running": self.state.running,
            "tasks_total": 0,  # Will be populated async
            "tasks_enabled": 0,
        }

    def list_tasks(self) -> list[dict]:
        """Legacy: List tasks as dicts.

        Use list() instead for the new API.
        """
        # This is sync, so we can't actually query
        # Return empty for now, callers should use async list()
        return []
