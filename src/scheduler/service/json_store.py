"""JSON file persistence layer for scheduled jobs.

Simple file-based storage for scheduled jobs, making it easy for users
to view and modify jobs directly.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from ..models import ScheduledJob, JobState
from ..types import (
    JobStatus,
    RunStatus,
    JobRun,
    JobStats,
    schedule_from_dict,
    payload_from_dict,
    TaskChainPayload,
)

logger = logger.bind(module="scheduler.json_store")


class JsonJobStore:
    """JSON file-based job persistence.
    
    Stores jobs in a human-readable JSON file that users can easily
    view and modify.
    """

    def __init__(self, json_path: str | Path):
        """Initialize JSON job store.

        Args:
            json_path: Path to JSON file for storage
        """
        self.json_path = Path(json_path).expanduser()
        self._jobs: dict[str, ScheduledJob] = {}
        self._runs: list[JobRun] = []
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize store by loading from JSON file."""
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        
        if self.json_path.exists():
            await self._load_from_file()
        else:
            # Create empty file
            await self._save_to_file()
        
        self._initialized = True
        logger.info(f"JSON job store initialized at {self.json_path}")

    async def close(self) -> None:
        """Close store (save final state)."""
        if self._initialized:
            await self._save_to_file()

    async def _load_from_file(self) -> None:
        """Load jobs from JSON file."""
        try:
            async with self._lock:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                self._jobs = {}
                for job_data in data.get("jobs", []):
                    # Remove human-readable fields before parsing
                    for key in ["created_at_human", "updated_at_human", 
                               "next_run_at_human", "last_run_at_human"]:
                        job_data.pop(key, None)
                    
                    job = ScheduledJob.from_dict(job_data)
                    self._jobs[job.id] = job
                
                # Load runs
                self._runs = []
                for run_data in data.get("recent_runs", []):
                    self._runs.append(JobRun(
                        id=run_data.get("id", ""),
                        job_id=run_data.get("job_id", ""),
                        started_at_ms=run_data.get("started_at_ms", 0),
                        finished_at_ms=run_data.get("finished_at_ms"),
                        status=RunStatus(run_data.get("status", "ok")),
                        result=run_data.get("result"),
                        error=run_data.get("error"),
                        duration_ms=run_data.get("duration_ms", 0),
                    ))
                
                logger.info(f"Loaded {len(self._jobs)} jobs from {self.json_path}")
        except Exception as e:
            logger.error(f"Failed to load from JSON: {e}")
            self._jobs = {}
            self._runs = []

    async def _save_to_file(self) -> None:
        """Save jobs to JSON file."""
        try:
            async with self._lock:
                # Build export data
                jobs_list = sorted(
                    self._jobs.values(),
                    key=lambda j: j.created_at_ms,
                    reverse=True
                )
                
                export_data = {
                    "exported_at": datetime.now().isoformat(),
                    "total_jobs": len(self._jobs),
                    "jobs": [self._job_to_export_dict(job) for job in jobs_list],
                    "recent_runs": [run.to_dict() for run in self._runs[-100:]],
                }

                # Write atomically (write to temp, then rename)
                temp_path = self.json_path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(export_data, f, ensure_ascii=False, indent=2)
                temp_path.rename(self.json_path)

                logger.debug(f"Saved {len(self._jobs)} jobs to {self.json_path}")
        except Exception as e:
            logger.error(f"Failed to save to JSON: {e}")

    def _job_to_export_dict(self, job: ScheduledJob) -> dict[str, Any]:
        """Convert job to human-readable export format."""
        data = job.to_dict()
        
        # Add human-readable timestamps
        data["created_at_human"] = datetime.fromtimestamp(
            job.created_at_ms / 1000
        ).strftime("%Y-%m-%d %H:%M:%S")
        data["updated_at_human"] = datetime.fromtimestamp(
            job.updated_at_ms / 1000
        ).strftime("%Y-%m-%d %H:%M:%S")
        
        if job.state.next_run_at_ms:
            data["next_run_at_human"] = datetime.fromtimestamp(
                job.state.next_run_at_ms / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
        
        if job.state.last_run_at_ms:
            data["last_run_at_human"] = datetime.fromtimestamp(
                job.state.last_run_at_ms / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")

        return data

    # ============== Job CRUD ==============

    async def save(self, job: ScheduledJob) -> None:
        """Save or update a job."""
        job.updated_at_ms = int(datetime.now().timestamp() * 1000)
        self._jobs[job.id] = job
        await self._save_to_file()

    async def get(self, job_id: str) -> ScheduledJob | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    async def delete(self, job_id: str) -> bool:
        """Permanently delete a job."""
        if job_id in self._jobs:
            del self._jobs[job_id]
            await self._save_to_file()
            return True
        return False

    async def list_jobs(
        self,
        user_id: str | None = None,
        status: JobStatus | None = None,
        include_disabled: bool = False,
        limit: int = 100,
    ) -> list[ScheduledJob]:
        """List jobs with optional filters."""
        jobs = list(self._jobs.values())
        
        # Apply filters
        if user_id:
            jobs = [j for j in jobs if j.user_id == user_id]
        
        if status:
            jobs = [j for j in jobs if j.status == status]
        
        if not include_disabled:
            jobs = [j for j in jobs if j.enabled]
        
        # Sort by created_at descending
        jobs.sort(key=lambda j: j.created_at_ms, reverse=True)
        
        return jobs[:limit]

    async def get_due_jobs(self, before_ms: int) -> list[ScheduledJob]:
        """Get jobs that are due to run before the given timestamp."""
        due_jobs = []
        for job in self._jobs.values():
            if (job.enabled and 
                job.status == JobStatus.ACTIVE and
                job.state.next_run_at_ms and
                job.state.next_run_at_ms <= before_ms):
                due_jobs.append(job)
        
        # Sort by next_run_at_ms
        due_jobs.sort(key=lambda j: j.state.next_run_at_ms or 0)
        return due_jobs

    async def get_next_run_time(self) -> int | None:
        """Get the earliest next_run_at_ms among all active jobs."""
        min_time = None
        for job in self._jobs.values():
            if (job.enabled and 
                job.status == JobStatus.ACTIVE and
                job.state.next_run_at_ms):
                if min_time is None or job.state.next_run_at_ms < min_time:
                    min_time = job.state.next_run_at_ms
        return min_time

    async def count_by_status(self) -> dict[str, int]:
        """Count jobs by status."""
        counts: dict[str, int] = {}
        for job in self._jobs.values():
            status = job.status.value
            counts[status] = counts.get(status, 0) + 1
        return counts

    async def get_upcoming_jobs(
        self,
        within_ms: int,
        limit: int = 20,
    ) -> list[ScheduledJob]:
        """Get jobs scheduled to run within the given time window."""
        now_ms = int(datetime.now().timestamp() * 1000)
        until_ms = now_ms + within_ms
        
        upcoming = []
        for job in self._jobs.values():
            if (job.enabled and 
                job.status == JobStatus.ACTIVE and
                job.state.next_run_at_ms and
                job.state.next_run_at_ms <= until_ms):
                upcoming.append(job)
        
        upcoming.sort(key=lambda j: j.state.next_run_at_ms or 0)
        return upcoming[:limit]

    # ============== Job Runs ==============

    async def save_run(self, run: JobRun) -> None:
        """Save a job run record."""
        if not run.id:
            run.id = f"run_{uuid4().hex[:12]}"
        
        # Keep last 500 runs
        self._runs.append(run)
        if len(self._runs) > 500:
            self._runs = self._runs[-500:]
        
        await self._save_to_file()

    async def get_runs(
        self,
        job_id: str | None = None,
        status: RunStatus | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[JobRun], int]:
        """Get job runs with filters."""
        runs = self._runs.copy()
        
        # Apply filters
        if job_id:
            runs = [r for r in runs if r.job_id == job_id]
        
        if status:
            runs = [r for r in runs if r.status == status]
        
        if since_ms:
            runs = [r for r in runs if r.started_at_ms >= since_ms]
        
        if until_ms:
            runs = [r for r in runs if r.started_at_ms <= until_ms]
        
        # Sort by started_at descending
        runs.sort(key=lambda r: r.started_at_ms, reverse=True)
        
        total = len(runs)
        runs = runs[offset:offset + limit]
        
        return runs, total

    async def get_recent_runs(
        self,
        limit: int = 20,
        since_ms: int | None = None,
    ) -> list[JobRun]:
        """Get recent job runs across all jobs."""
        runs, _ = await self.get_runs(since_ms=since_ms, limit=limit)
        return runs

    async def get_failed_runs(
        self,
        since_ms: int | None = None,
        limit: int = 20,
    ) -> list[JobRun]:
        """Get failed job runs."""
        runs, _ = await self.get_runs(
            status=RunStatus.FAILED,
            since_ms=since_ms,
            limit=limit,
        )
        return runs

    async def get_job_stats(self, job_id: str) -> JobStats:
        """Get statistics for a single job."""
        stats = JobStats(job_id=job_id)
        
        job_runs = [r for r in self._runs if r.job_id == job_id]
        if not job_runs:
            return stats
        
        stats.total_runs = len(job_runs)
        stats.success_count = sum(1 for r in job_runs if r.status == RunStatus.OK)
        stats.failure_count = sum(1 for r in job_runs if r.status in (RunStatus.FAILED, RunStatus.TIMEOUT))
        
        if stats.total_runs > 0:
            stats.success_rate = stats.success_count / stats.total_runs
        
        durations = [r.duration_ms for r in job_runs if r.duration_ms]
        if durations:
            stats.avg_duration_ms = sum(durations) / len(durations)
            stats.max_duration_ms = max(durations)
            stats.min_duration_ms = min(durations)
        
        # Last run info
        job_runs.sort(key=lambda r: r.started_at_ms, reverse=True)
        if job_runs:
            stats.last_run_at_ms = job_runs[0].started_at_ms
            stats.last_status = job_runs[0].status.value
        
        return stats

    async def get_runs_stats_today(self) -> dict[str, Any]:
        """Get run statistics for today."""
        now = datetime.now()
        start_of_day = datetime(now.year, now.month, now.day)
        start_of_day_ms = int(start_of_day.timestamp() * 1000)
        
        today_runs = [r for r in self._runs if r.started_at_ms >= start_of_day_ms]
        
        total = len(today_runs)
        success = sum(1 for r in today_runs if r.status == RunStatus.OK)
        failed = sum(1 for r in today_runs if r.status in (RunStatus.FAILED, RunStatus.TIMEOUT))
        
        durations = [r.duration_ms for r in today_runs if r.duration_ms]
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        success_rate = success / total if total > 0 else 0.0
        
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": success_rate,
            "avg_duration_ms": avg_duration,
        }

    async def delete_old_runs(self, before_ms: int) -> int:
        """Delete runs older than the given timestamp."""
        original_count = len(self._runs)
        self._runs = [r for r in self._runs if r.started_at_ms >= before_ms]
        deleted = original_count - len(self._runs)
        if deleted > 0:
            await self._save_to_file()
        return deleted

    # ============== JSON Export (compatibility) ==============

    async def export_to_json(self) -> None:
        """Export all jobs to JSON file (no-op, already using JSON)."""
        await self._save_to_file()

    async def import_from_json(self, json_path: str | Path | None = None) -> int:
        """Import jobs from JSON file."""
        path = Path(json_path) if json_path else self.json_path
        if not path.exists():
            logger.warning(f"JSON file not found: {path}")
            return 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            jobs_data = data.get("jobs", [])
            imported = 0

            for job_data in jobs_data:
                # Remove human-readable fields
                for key in ["created_at_human", "updated_at_human", 
                           "next_run_at_human", "last_run_at_human"]:
                    job_data.pop(key, None)
                
                job = ScheduledJob.from_dict(job_data)
                self._jobs[job.id] = job
                imported += 1

            await self._save_to_file()
            logger.info(f"Imported {imported} jobs from {path}")
            return imported

        except Exception as e:
            logger.error(f"Failed to import from JSON: {e}")
            return 0

    async def get_json_path(self) -> Path:
        """Get the path to the JSON file."""
        return self.json_path
