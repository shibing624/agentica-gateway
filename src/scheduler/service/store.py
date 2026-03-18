"""Hybrid YAML config + SQLite state store for scheduled jobs.

Architecture:
- YAML file (scheduler.yaml): Job definitions (user-editable config)
  Contains: id, name, description, enabled, schedule, payload, target, etc.
- SQLite database (scheduler_state.db): Runtime state + run history (program-owned)
  Contains: job status, next_run_at, run_count, failure_count, run records

This separation ensures:
1. Users can freely edit YAML to add/modify/remove scheduled tasks
2. Runtime state writes (high-frequency) go to SQLite with proper transactions
3. No concurrent write issues — YAML is read-mostly, SQLite handles concurrency natively
"""
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from loguru import logger

from ..models import ScheduledJob, JobState
from ..types import (
    JobStatus,
    RunStatus,
    JobRun,
    JobStats,
    SessionTarget,
    TaskChainPayload,
    schedule_from_dict,
    payload_from_dict,
)

logger = logger.bind(module="scheduler.store")

# ============== SQL Schema ==============

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS job_state (
    job_id        TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending',
    next_run_at_ms INTEGER,
    last_run_at_ms INTEGER,
    last_status   TEXT,
    run_count     INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    status        TEXT NOT NULL DEFAULT 'ok',
    result        TEXT,
    error         TEXT,
    duration_ms   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_job_id ON job_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON job_runs(started_at_ms);
CREATE INDEX IF NOT EXISTS idx_runs_status ON job_runs(status);
"""


def _job_to_yaml_dict(job: ScheduledJob) -> dict[str, Any]:
    """Convert a ScheduledJob to a YAML-friendly dict (config only, no runtime state)."""
    d: dict[str, Any] = {
        "id": job.id,
        "name": job.name,
    }
    if job.description:
        d["description"] = job.description
    d["enabled"] = job.enabled
    if job.user_id:
        d["user_id"] = job.user_id
    if job.agent_id and job.agent_id != "main":
        d["agent_id"] = job.agent_id

    # Schedule
    d["schedule"] = job.schedule.to_dict()

    # Payload
    d["payload"] = job.payload.to_dict()

    # Target (only if non-default)
    target_dict = job.target.to_dict()
    if target_dict.get("kind") != "isolated":
        d["target"] = target_dict

    # Execution settings (only if non-default)
    if job.max_retries != 3:
        d["max_retries"] = job.max_retries
    if job.retry_delay_ms != 60000:
        d["retry_delay_ms"] = job.retry_delay_ms

    # Task chain
    if job.on_complete:
        d["on_complete"] = [p.to_dict() for p in job.on_complete]

    return d


def _yaml_dict_to_job(d: dict[str, Any]) -> ScheduledJob:
    """Parse a YAML dict into a ScheduledJob (config fields only)."""
    job = ScheduledJob(
        id=d.get("id", str(uuid4())),
        name=d.get("name", ""),
        description=d.get("description", ""),
        enabled=d.get("enabled", True),
        user_id=d.get("user_id", ""),
        agent_id=d.get("agent_id", "main"),
        max_retries=d.get("max_retries", 3),
        retry_delay_ms=d.get("retry_delay_ms", 60000),
    )

    if d.get("schedule"):
        job.schedule = schedule_from_dict(d["schedule"])
    if d.get("payload"):
        job.payload = payload_from_dict(d["payload"])
    if d.get("target"):
        job.target = SessionTarget.from_dict(d["target"])
    if d.get("on_complete"):
        job.on_complete = [TaskChainPayload.from_dict(p) for p in d["on_complete"]]

    return job


class JobStore:
    """Hybrid YAML config + SQLite state store.

    Thread-safety: SQLite handles its own locking. YAML writes are
    infrequent (only on job create/update/delete via API) and
    serialized by the GIL + single event loop.
    """

    def __init__(self, data_dir: str | Path):
        """Initialize store.

        Args:
            data_dir: Directory to store scheduler.yaml and scheduler_state.db
        """
        self.data_dir = Path(data_dir).expanduser()
        self.yaml_path = self.data_dir / "scheduler.yaml"
        self.db_path = self.data_dir / "scheduler_state.db"
        self._jobs: dict[str, ScheduledJob] = {}
        self._db: sqlite3.Connection | None = None

    # ============== Lifecycle ==============

    async def initialize(self) -> None:
        """Initialize store: load YAML config, open SQLite, reconcile."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._open_db)
        self._load_yaml()
        self._reconcile_state()
        logger.info(
            f"Store initialized: {len(self._jobs)} jobs from {self.yaml_path}"
        )

    def _open_db(self) -> None:
        """Open SQLite connection (sync, called via to_thread)."""
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_INIT_SQL)

    async def close(self) -> None:
        """Close store."""
        if self._db:
            self._db.close()
            self._db = None

    # ============== YAML I/O ==============

    def _load_yaml(self) -> None:
        """Load job definitions from YAML file."""
        self._jobs = {}
        if not self.yaml_path.exists():
            self._write_yaml()
            return

        try:
            with open(self.yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            for job_data in data.get("jobs", []):
                job = _yaml_dict_to_job(job_data)
                self._jobs[job.id] = job

            logger.info(f"Loaded {len(self._jobs)} jobs from {self.yaml_path}")
        except Exception as e:
            logger.error(f"Failed to load YAML: {e}")

    def _write_yaml(self) -> None:
        """Write job definitions to YAML file (atomic)."""
        jobs_list = sorted(
            self._jobs.values(),
            key=lambda j: j.created_at_ms,
        )

        data = {
            "jobs": [_job_to_yaml_dict(job) for job in jobs_list],
        }

        temp_path = self.yaml_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write("# Agentica Scheduler Configuration\n")
            f.write("# Edit this file to add/modify/remove scheduled tasks.\n")
            f.write("# Changes take effect after restarting the service.\n\n")
            yaml.dump(
                data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        temp_path.rename(self.yaml_path)

    # ============== SQLite State ==============

    def _reconcile_state(self) -> None:
        """Apply SQLite state to in-memory jobs loaded from YAML."""
        assert self._db is not None
        cursor = self._db.execute("SELECT * FROM job_state")
        state_rows = {row["job_id"]: row for row in cursor.fetchall()}

        for job_id, job in self._jobs.items():
            row = state_rows.get(job_id)
            if row:
                job.status = JobStatus(row["status"])
                job.state = JobState(
                    next_run_at_ms=row["next_run_at_ms"],
                    last_run_at_ms=row["last_run_at_ms"],
                    last_status=RunStatus(row["last_status"]) if row["last_status"] else None,
                    run_count=row["run_count"],
                    failure_count=row["failure_count"],
                    consecutive_failures=row["consecutive_failures"],
                    last_error=row["last_error"],
                )
                job.created_at_ms = row["created_at_ms"]
                job.updated_at_ms = row["updated_at_ms"]
            else:
                # New job from YAML, insert initial state
                now_ms = int(datetime.now().timestamp() * 1000)
                job.created_at_ms = now_ms
                job.updated_at_ms = now_ms
                self._upsert_state(job)

        # Clean up orphaned state rows (jobs removed from YAML)
        orphan_ids = set(state_rows.keys()) - set(self._jobs.keys())
        if orphan_ids:
            placeholders = ",".join("?" * len(orphan_ids))
            self._db.execute(
                f"DELETE FROM job_state WHERE job_id IN ({placeholders})",
                list(orphan_ids),
            )
            self._db.commit()
            logger.info(f"Cleaned up {len(orphan_ids)} orphaned state rows")

    def _upsert_state(self, job: ScheduledJob) -> None:
        """Insert or update job state in SQLite."""
        assert self._db is not None
        self._db.execute(
            """INSERT INTO job_state
               (job_id, status, next_run_at_ms, last_run_at_ms, last_status,
                run_count, failure_count, consecutive_failures, last_error,
                created_at_ms, updated_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                next_run_at_ms=excluded.next_run_at_ms,
                last_run_at_ms=excluded.last_run_at_ms,
                last_status=excluded.last_status,
                run_count=excluded.run_count,
                failure_count=excluded.failure_count,
                consecutive_failures=excluded.consecutive_failures,
                last_error=excluded.last_error,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                job.id,
                job.status.value,
                job.state.next_run_at_ms,
                job.state.last_run_at_ms,
                job.state.last_status.value if job.state.last_status else None,
                job.state.run_count,
                job.state.failure_count,
                job.state.consecutive_failures,
                job.state.last_error,
                job.created_at_ms,
                job.updated_at_ms,
            ),
        )
        self._db.commit()

    # ============== Job CRUD ==============

    async def save(self, job: ScheduledJob) -> None:
        """Save or update a job."""
        job.updated_at_ms = int(datetime.now().timestamp() * 1000)
        is_new = job.id not in self._jobs
        self._jobs[job.id] = job
        await asyncio.to_thread(self._upsert_state, job)
        if is_new:
            await asyncio.to_thread(self._write_yaml)
            logger.debug(f"Saved new job {job.id} to YAML + SQLite")
        else:
            logger.debug(f"Updated job {job.id} state in SQLite")

    async def get(self, job_id: str) -> ScheduledJob | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    async def delete(self, job_id: str) -> bool:
        """Permanently delete a job."""
        if job_id not in self._jobs:
            return False
        del self._jobs[job_id]
        await asyncio.to_thread(self._delete_from_db, job_id)
        await asyncio.to_thread(self._write_yaml)
        return True

    def _delete_from_db(self, job_id: str) -> None:
        """Remove job state and runs from SQLite (sync)."""
        assert self._db is not None
        self._db.execute("DELETE FROM job_state WHERE job_id = ?", (job_id,))
        self._db.execute("DELETE FROM job_runs WHERE job_id = ?", (job_id,))
        self._db.commit()

    async def list_jobs(
        self,
        user_id: str | None = None,
        status: JobStatus | None = None,
        include_disabled: bool = False,
        limit: int = 100,
    ) -> list[ScheduledJob]:
        """List jobs with optional filters."""
        jobs = list(self._jobs.values())

        if user_id:
            jobs = [j for j in jobs if j.user_id == user_id]
        if status:
            jobs = [j for j in jobs if j.status == status]
        if not include_disabled:
            jobs = [j for j in jobs if j.enabled]

        jobs.sort(key=lambda j: j.created_at_ms, reverse=True)
        return jobs[:limit]

    # ============== Timer Queries ==============

    async def get_due_jobs(self, before_ms: int) -> list[ScheduledJob]:
        """Get jobs that are due to run before the given timestamp."""
        due = [
            job for job in self._jobs.values()
            if (job.enabled
                and job.status == JobStatus.ACTIVE
                and job.state.next_run_at_ms
                and job.state.next_run_at_ms <= before_ms)
        ]
        due.sort(key=lambda j: j.state.next_run_at_ms or 0)
        return due

    async def get_next_run_time(self) -> int | None:
        """Get the earliest next_run_at_ms among all active jobs."""
        min_time = None
        for job in self._jobs.values():
            if (job.enabled
                    and job.status == JobStatus.ACTIVE
                    and job.state.next_run_at_ms):
                if min_time is None or job.state.next_run_at_ms < min_time:
                    min_time = job.state.next_run_at_ms
        return min_time

    async def get_upcoming_jobs(
        self,
        within_ms: int,
        limit: int = 20,
    ) -> list[ScheduledJob]:
        """Get jobs scheduled to run within the given time window."""
        now_ms = int(datetime.now().timestamp() * 1000)
        until_ms = now_ms + within_ms

        upcoming = [
            job for job in self._jobs.values()
            if (job.enabled
                and job.status == JobStatus.ACTIVE
                and job.state.next_run_at_ms
                and job.state.next_run_at_ms <= until_ms)
        ]
        upcoming.sort(key=lambda j: j.state.next_run_at_ms or 0)
        return upcoming[:limit]

    # ============== Statistics ==============

    async def count_by_status(self) -> dict[str, int]:
        """Count jobs by status."""
        counts: dict[str, int] = {}
        for job in self._jobs.values():
            s = job.status.value
            counts[s] = counts.get(s, 0) + 1
        return counts

    # ============== Job Runs (SQLite) ==============

    async def save_run(self, run: JobRun) -> None:
        """Save a job run record."""
        if not run.id:
            run.id = f"run_{uuid4().hex[:12]}"
        await asyncio.to_thread(self._save_run_sync, run)

    def _save_run_sync(self, run: JobRun) -> None:
        """Insert or replace a job run in SQLite (sync)."""
        assert self._db is not None
        self._db.execute(
            """INSERT OR REPLACE INTO job_runs
               (id, job_id, started_at_ms, finished_at_ms, status, result, error, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.id, run.job_id, run.started_at_ms, run.finished_at_ms,
                run.status.value, run.result, run.error, run.duration_ms,
            ),
        )
        self._db.commit()

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
        return await asyncio.to_thread(
            self._get_runs_sync, job_id, status, since_ms, until_ms, limit, offset
        )

    def _get_runs_sync(
        self,
        job_id: str | None,
        status: RunStatus | None,
        since_ms: int | None,
        until_ms: int | None,
        limit: int,
        offset: int,
    ) -> tuple[list[JobRun], int]:
        """Query job runs from SQLite (sync)."""
        assert self._db is not None
        conditions = []
        params: list[Any] = []

        if job_id:
            conditions.append("job_id = ?")
            params.append(job_id)
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if since_ms:
            conditions.append("started_at_ms >= ?")
            params.append(since_ms)
        if until_ms:
            conditions.append("started_at_ms <= ?")
            params.append(until_ms)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        count_row = self._db.execute(
            f"SELECT COUNT(*) as cnt FROM job_runs {where}", params
        ).fetchone()
        total = count_row["cnt"] if count_row else 0

        rows = self._db.execute(
            f"SELECT * FROM job_runs {where} ORDER BY started_at_ms DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        runs = [
            JobRun(
                id=row["id"],
                job_id=row["job_id"],
                started_at_ms=row["started_at_ms"],
                finished_at_ms=row["finished_at_ms"],
                status=RunStatus(row["status"]),
                result=row["result"],
                error=row["error"],
                duration_ms=row["duration_ms"],
            )
            for row in rows
        ]
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
            status=RunStatus.FAILED, since_ms=since_ms, limit=limit,
        )
        return runs

    async def get_job_stats(self, job_id: str) -> JobStats:
        """Get statistics for a single job."""
        return await asyncio.to_thread(self._get_job_stats_sync, job_id)

    def _get_job_stats_sync(self, job_id: str) -> JobStats:
        """Query job statistics from SQLite (sync)."""
        assert self._db is not None
        stats = JobStats(job_id=job_id)

        row = self._db.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status IN ('failed','timeout') THEN 1 ELSE 0 END) as failed,
                AVG(duration_ms) as avg_dur,
                MAX(duration_ms) as max_dur,
                MIN(duration_ms) as min_dur
               FROM job_runs WHERE job_id = ?""",
            (job_id,),
        ).fetchone()

        if row and row["total"] > 0:
            stats.total_runs = row["total"]
            stats.success_count = row["success"]
            stats.failure_count = row["failed"]
            stats.success_rate = row["success"] / row["total"]
            stats.avg_duration_ms = row["avg_dur"] or 0.0
            stats.max_duration_ms = row["max_dur"] or 0
            stats.min_duration_ms = row["min_dur"] or 0

        # Last run
        last = self._db.execute(
            "SELECT started_at_ms, status FROM job_runs WHERE job_id = ? ORDER BY started_at_ms DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if last:
            stats.last_run_at_ms = last["started_at_ms"]
            stats.last_status = last["status"]

        return stats

    async def get_runs_stats_today(self) -> dict[str, Any]:
        """Get run statistics for today."""
        return await asyncio.to_thread(self._get_runs_stats_today_sync)

    def _get_runs_stats_today_sync(self) -> dict[str, Any]:
        """Query today's run stats from SQLite (sync)."""
        assert self._db is not None
        now = datetime.now()
        start_of_day_ms = int(
            datetime(now.year, now.month, now.day).timestamp() * 1000
        )

        row = self._db.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status IN ('failed','timeout') THEN 1 ELSE 0 END) as failed,
                AVG(duration_ms) as avg_dur
               FROM job_runs WHERE started_at_ms >= ?""",
            (start_of_day_ms,),
        ).fetchone()

        total = row["total"] if row else 0
        success = row["success"] if row else 0
        failed = row["failed"] if row else 0
        avg_dur = row["avg_dur"] if row else 0.0

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": success / total if total > 0 else 0.0,
            "avg_duration_ms": avg_dur or 0.0,
        }

    async def delete_old_runs(self, before_ms: int) -> int:
        """Delete runs older than the given timestamp."""
        return await asyncio.to_thread(self._delete_old_runs_sync, before_ms)

    def _delete_old_runs_sync(self, before_ms: int) -> int:
        """Delete old runs from SQLite (sync)."""
        assert self._db is not None
        cursor = self._db.execute(
            "DELETE FROM job_runs WHERE started_at_ms < ?", (before_ms,)
        )
        self._db.commit()
        return cursor.rowcount

    # ============== Export/Import (compatibility) ==============

    async def export_to_yaml(self) -> None:
        """Force write YAML config to disk."""
        await asyncio.to_thread(self._write_yaml)

    async def get_yaml_path(self) -> Path:
        """Get the path to the YAML config file."""
        return self.yaml_path

    async def reload_yaml(self) -> int:
        """Reload jobs from YAML file (hot-reload).

        Returns:
            Number of jobs loaded
        """
        await asyncio.to_thread(self._load_yaml)
        await asyncio.to_thread(self._reconcile_state)
        return len(self._jobs)
