"""Main Scheduler Service class.

This is the unified entry point for all scheduler operations.
Supports:
- Session target modes (main/isolated)
- Dependency injection callbacks
- JSON file export for human-readable viewing
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, List

from loguru import logger

from ..models import ScheduledJob, JobCreate, JobPatch
from ..types import (
    JobStatus,
    RunStatus,
    RemoveResult,
    RunResult,
    SchedulerStatus,
    SchedulerStats,
    JobRun,
    JobStats,
    BatchResult,
    SessionTarget,
)
from .state import SchedulerServiceDeps, SchedulerServiceState
from .store import JobStore
from .events import EventEmitter, emit_job_event, EventTypes
from . import ops
from . import timer

logger = logger.bind(module="scheduler.service")

# Type aliases for dependency injection callbacks
OnSystemEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
RunHeartbeatCallback = Callable[[str], Awaitable[None]]
ReportToMainCallback = Callable[[str, str, str], Awaitable[None]]


class SchedulerService:
    """Unified scheduler service for managing scheduled jobs.

    This replaces both the legacy Scheduler class and the APScheduler-based
    SchedulerService with a simpler asyncio-based implementation.
    
    Supports:
    - Session target modes (main/isolated)
    - Dependency injection for main mode callbacks
    - JSON file export for human-readable viewing
    """

    def __init__(
        self,
        db_path: str | Path = "~/.agentica/scheduler.db",
        json_path: str | Path | None = None,
        executor: Any = None,
        notification_sender: Any = None,
        # Dependency injection callbacks for main mode
        on_system_event: OnSystemEventCallback | None = None,
        run_heartbeat: RunHeartbeatCallback | None = None,
        report_to_main: ReportToMainCallback | None = None,
        # Auto export to JSON after changes
        auto_export_json: bool = True,
    ):
        """Initialize scheduler service.

        Args:
            db_path: Path to SQLite database for persistence
            json_path: Path to JSON file for human-readable export
            executor: Job executor (implements execute(job) -> Any)
            notification_sender: Notification sender (implements send(channel, chat_id, msg) -> bool)
            on_system_event: Callback to inject system event into main session
            run_heartbeat: Callback to trigger heartbeat in main session
            report_to_main: Callback to report isolated execution result to main session
            auto_export_json: Whether to auto-export to JSON after job changes
        """
        db_path = Path(db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.store = JobStore(db_path, json_path)
        self.events = EventEmitter()
        self.deps = SchedulerServiceDeps(
            executor=executor,
            notification_sender=notification_sender,
        )
        self.state = SchedulerServiceState()
        
        # Main mode callbacks
        self.on_system_event = on_system_event
        self.run_heartbeat = run_heartbeat
        self.report_to_main = report_to_main
        self.auto_export_json = auto_export_json

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
        
        # Initial JSON export
        if self.auto_export_json:
            await self.store.export_to_json()

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

        # Final JSON export
        if self.auto_export_json:
            await self.store.export_to_json()

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
        
        # Export to JSON
        if self.auto_export_json:
            await self.store.export_to_json()

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
        
        # Export to JSON
        if updated_job and self.auto_export_json:
            await self.store.export_to_json()

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
        
        # Export to JSON
        if result.removed and self.auto_export_json:
            await self.store.export_to_json()

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

    # ============== Monitoring & Statistics ==============

    async def get_stats(self) -> SchedulerStats:
        """Get comprehensive scheduler statistics.

        Returns:
            SchedulerStats with job counts and run statistics
        """
        stats = SchedulerStats(running=self.state.running)

        # Get job counts by status
        status_counts = await self.store.count_by_status()
        stats.total_jobs = sum(status_counts.values())
        stats.active_jobs = status_counts.get("active", 0)
        stats.paused_jobs = status_counts.get("paused", 0)
        stats.completed_jobs = status_counts.get("completed", 0)
        stats.failed_jobs = status_counts.get("failed", 0)

        # Get today's run stats
        runs_today = await self.store.get_runs_stats_today()
        stats.total_runs_today = runs_today.get("total", 0)
        stats.success_runs_today = runs_today.get("success", 0)
        stats.failed_runs_today = runs_today.get("failed", 0)
        stats.success_rate_today = runs_today.get("success_rate", 0.0)
        stats.avg_duration_ms_today = runs_today.get("avg_duration_ms", 0.0)

        # Get next run time
        stats.next_run_at_ms = await self.store.get_next_run_time()

        return stats

    async def get_job_stats(self, job_id: str) -> JobStats | None:
        """Get statistics for a single job.

        Args:
            job_id: ID of the job

        Returns:
            JobStats or None if job doesn't exist
        """
        job = await self.store.get(job_id)
        if not job:
            return None

        return await self.store.get_job_stats(job_id)

    async def get_job_runs(
        self,
        job_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[JobRun], int]:
        """Get execution history for a job.

        Args:
            job_id: ID of the job
            limit: Maximum number of runs to return
            offset: Offset for pagination

        Returns:
            Tuple of (runs list, total count)
        """
        return await self.store.get_runs(
            job_id=job_id,
            limit=limit,
            offset=offset,
        )

    async def get_recent_runs(
        self,
        limit: int = 20,
        since_ms: int | None = None,
    ) -> list[JobRun]:
        """Get recent job runs across all jobs.

        Args:
            limit: Maximum number of runs
            since_ms: Only get runs after this timestamp

        Returns:
            List of recent job runs
        """
        return await self.store.get_recent_runs(limit=limit, since_ms=since_ms)

    async def get_failed_runs(
        self,
        limit: int = 20,
        since_ms: int | None = None,
    ) -> list[JobRun]:
        """Get failed job runs.

        Args:
            limit: Maximum number of runs
            since_ms: Only get runs after this timestamp

        Returns:
            List of failed job runs
        """
        return await self.store.get_failed_runs(limit=limit, since_ms=since_ms)

    async def get_upcoming_jobs(
        self,
        within_minutes: int = 30,
        limit: int = 20,
    ) -> list[ScheduledJob]:
        """Get jobs scheduled to run within the given time window.

        Args:
            within_minutes: Time window in minutes
            limit: Maximum number of jobs

        Returns:
            List of upcoming jobs
        """
        within_ms = within_minutes * 60 * 1000
        return await self.store.get_upcoming_jobs(within_ms=within_ms, limit=limit)

    # ============== Batch Operations ==============

    async def batch_pause(self, job_ids: list[str]) -> BatchResult:
        """Pause multiple jobs.

        Args:
            job_ids: List of job IDs to pause

        Returns:
            BatchResult with success count and failures
        """
        result = BatchResult(success=True)

        for job_id in job_ids:
            try:
                job = await self.pause(job_id)
                if job:
                    result.processed += 1
                else:
                    result.failed_ids.append(job_id)
                    result.errors[job_id] = "Job not found"
            except Exception as e:
                result.failed_ids.append(job_id)
                result.errors[job_id] = str(e)

        result.success = len(result.failed_ids) == 0
        return result

    async def batch_resume(self, job_ids: list[str]) -> BatchResult:
        """Resume multiple jobs.

        Args:
            job_ids: List of job IDs to resume

        Returns:
            BatchResult with success count and failures
        """
        result = BatchResult(success=True)

        for job_id in job_ids:
            try:
                job = await self.resume(job_id)
                if job:
                    result.processed += 1
                else:
                    result.failed_ids.append(job_id)
                    result.errors[job_id] = "Job not found"
            except Exception as e:
                result.failed_ids.append(job_id)
                result.errors[job_id] = str(e)

        result.success = len(result.failed_ids) == 0
        return result

    async def batch_delete(self, job_ids: list[str]) -> BatchResult:
        """Delete multiple jobs.

        Args:
            job_ids: List of job IDs to delete

        Returns:
            BatchResult with success count and failures
        """
        result = BatchResult(success=True)

        for job_id in job_ids:
            try:
                remove_result = await self.remove(job_id)
                if remove_result.removed:
                    result.processed += 1
                else:
                    result.failed_ids.append(job_id)
                    result.errors[job_id] = remove_result.reason or "Failed to delete"
            except Exception as e:
                result.failed_ids.append(job_id)
                result.errors[job_id] = str(e)

        result.success = len(result.failed_ids) == 0
        return result

    async def clone_job(
        self,
        job_id: str,
        new_name: str | None = None,
    ) -> ScheduledJob | None:
        """Clone an existing job.

        Args:
            job_id: ID of job to clone
            new_name: Optional new name for the cloned job

        Returns:
            Newly created job, or None if source job not found
        """
        source_job = await self.store.get(job_id)
        if not source_job:
            return None

        # Create a new job based on the source
        job_create = JobCreate(
            user_id=source_job.user_id,
            agent_id=source_job.agent_id,
            name=new_name or f"{source_job.name} (copy)",
            description=source_job.description,
            schedule=deepcopy(source_job.schedule),
            payload=deepcopy(source_job.payload),
            max_retries=source_job.max_retries,
        )

        return await self.add(job_create)

    async def retry_job(self, job_id: str) -> RunResult:
        """Retry a job (force immediate execution).

        Args:
            job_id: ID of job to retry

        Returns:
            RunResult from the execution
        """
        return await self.run(job_id, mode="force")

    # ============== JSON Export ==============

    async def export_to_json(self) -> Path:
        """Export all jobs to JSON file for human-readable viewing.

        Returns:
            Path to the exported JSON file
        """
        await self.store.export_to_json()
        return await self.store.get_json_path()

    async def import_from_json(self, json_path: str | Path | None = None) -> int:
        """Import jobs from JSON file.

        Args:
            json_path: Path to JSON file, defaults to configured path

        Returns:
            Number of jobs imported
        """
        count = await self.store.import_from_json(json_path)
        
        # Re-arm timer after import
        if self.state.running and count > 0:
            await self._activate_jobs()
            await timer.arm_timer(self)
        
        return count

    async def get_json_path(self) -> Path:
        """Get the path to the JSON export file.

        Returns:
            Path to JSON file
        """
        return await self.store.get_json_path()

