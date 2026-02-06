"""SQLite persistence layer for scheduled jobs.

Refactored from task_store.py to work with the new ScheduledJob model.
Also provides JSON file export for human-readable viewing.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
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

logger = logger.bind(module="scheduler.store")


class JobStore:
    """SQLite-based job persistence with JSON export support.
    
    Provides both SQLite storage (for reliability) and JSON file export
    (for human-readable viewing and debugging).
    """

    def __init__(self, db_path: str | Path, json_path: str | Path | None = None):
        """Initialize job store.

        Args:
            db_path: Path to SQLite database
            json_path: Optional path to JSON file for human-readable export
        """
        self.db_path = Path(db_path)
        self.json_path = Path(json_path) if json_path else self.db_path.with_suffix(".json")
        self._connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)

        # Create jobs table with new schema
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                agent_id TEXT DEFAULT 'main',
                name TEXT,
                description TEXT,
                enabled INTEGER DEFAULT 1,
                schedule TEXT NOT NULL,
                payload TEXT NOT NULL,
                max_retries INTEGER DEFAULT 3,
                retry_delay_ms INTEGER DEFAULT 60000,
                on_complete TEXT DEFAULT '[]',
                state TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
        """)

        # Create indexes for common queries
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON scheduled_jobs(user_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON scheduled_jobs(status)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_enabled ON scheduled_jobs(enabled)"
        )

        await self._connection.commit()
        logger.info(f"Job store initialized at {self.db_path}")

        # Initialize runs table
        await self._init_runs_table()

    async def _init_runs_table(self) -> None:
        """Initialize the job_runs table for execution history."""
        if not self._connection:
            return

        # Create job_runs table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS job_runs (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL,
                finished_at_ms INTEGER,
                status TEXT NOT NULL DEFAULT 'ok',
                result TEXT,
                error TEXT,
                duration_ms INTEGER DEFAULT 0,
                FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE
            )
        """)

        # Create indexes for job_runs
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_job_id ON job_runs(job_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_started_at ON job_runs(started_at_ms)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_status ON job_runs(status)"
        )

        await self._connection.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def save(self, job: ScheduledJob) -> None:
        """Save or update a job."""
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        job.updated_at_ms = int(datetime.now().timestamp() * 1000)

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO scheduled_jobs (
                id, user_id, agent_id, name, description, enabled,
                schedule, payload, max_retries, retry_delay_ms,
                on_complete, state, status, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.user_id,
                job.agent_id,
                job.name,
                job.description,
                1 if job.enabled else 0,
                json.dumps(job.schedule.to_dict()),
                json.dumps(job.payload.to_dict()),
                job.max_retries,
                job.retry_delay_ms,
                json.dumps([p.to_dict() for p in job.on_complete]),
                json.dumps(job.state.to_dict()),
                job.status.value,
                job.created_at_ms,
                job.updated_at_ms,
            ),
        )
        await self._connection.commit()

    async def get(self, job_id: str) -> ScheduledJob | None:
        """Get a job by ID."""
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        async with self._connection.execute(
            "SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_job(row, cursor.description)
        return None

    async def delete(self, job_id: str) -> bool:
        """Permanently delete a job."""
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        result = await self._connection.execute(
            "DELETE FROM scheduled_jobs WHERE id = ?", (job_id,)
        )
        await self._connection.commit()
        return result.rowcount > 0

    async def list_jobs(
        self,
        user_id: str | None = None,
        status: JobStatus | None = None,
        include_disabled: bool = False,
        limit: int = 100,
    ) -> list[ScheduledJob]:
        """List jobs with optional filters."""
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        query = "SELECT * FROM scheduled_jobs WHERE 1=1"
        params: list[Any] = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if status:
            query += " AND status = ?"
            params.append(status.value)

        if not include_disabled:
            query += " AND enabled = 1"

        query += " ORDER BY created_at_ms DESC LIMIT ?"
        params.append(limit)

        jobs = []
        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                jobs.append(self._row_to_job(row, cursor.description))

        return jobs

    async def get_due_jobs(self, before_ms: int) -> list[ScheduledJob]:
        """Get jobs that are due to run before the given timestamp.

        Args:
            before_ms: Timestamp in milliseconds

        Returns:
            List of jobs that should run
        """
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        # Query jobs where next_run_at_ms <= before_ms
        query = """
            SELECT * FROM scheduled_jobs
            WHERE enabled = 1
            AND status = 'active'
            AND json_extract(state, '$.next_run_at_ms') <= ?
            ORDER BY json_extract(state, '$.next_run_at_ms') ASC
        """

        jobs = []
        async with self._connection.execute(query, (before_ms,)) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                jobs.append(self._row_to_job(row, cursor.description))

        return jobs

    async def get_next_run_time(self) -> int | None:
        """Get the earliest next_run_at_ms among all active jobs.

        Returns:
            Earliest next run timestamp in ms, or None if no jobs
        """
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        query = """
            SELECT MIN(json_extract(state, '$.next_run_at_ms'))
            FROM scheduled_jobs
            WHERE enabled = 1
            AND status = 'active'
            AND json_extract(state, '$.next_run_at_ms') IS NOT NULL
        """

        async with self._connection.execute(query) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return int(row[0])

        return None

    async def count_by_status(self) -> dict[str, int]:
        """Count jobs by status.

        Returns:
            Dict mapping status to count
        """
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        query = """
            SELECT status, COUNT(*) FROM scheduled_jobs
            GROUP BY status
        """

        counts: dict[str, int] = {}
        async with self._connection.execute(query) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                counts[row[0]] = row[1]

        return counts

    def _row_to_job(self, row: Any, description: Any) -> ScheduledJob:
        """Convert a database row to a ScheduledJob."""
        columns = [col[0] for col in description]
        data = dict(zip(columns, row))

        # Parse JSON fields
        schedule_data = json.loads(data.get("schedule", "{}"))
        payload_data = json.loads(data.get("payload", "{}"))
        state_data = json.loads(data.get("state", "{}"))
        on_complete_data = json.loads(data.get("on_complete", "[]"))

        job = ScheduledJob(
            id=data.get("id", ""),
            user_id=data.get("user_id", ""),
            agent_id=data.get("agent_id", "main"),
            name=data.get("name", ""),
            description=data.get("description", ""),
            enabled=bool(data.get("enabled", 1)),
            schedule=schedule_from_dict(schedule_data),
            payload=payload_from_dict(payload_data),
            max_retries=data.get("max_retries", 3),
            retry_delay_ms=data.get("retry_delay_ms", 60000),
            on_complete=[TaskChainPayload.from_dict(p) for p in on_complete_data],
            state=JobState.from_dict(state_data),
            status=JobStatus(data.get("status", "pending")),
            created_at_ms=data.get("created_at_ms", 0),
            updated_at_ms=data.get("updated_at_ms", 0),
        )

        return job

    # ============== JSON File Export ==============

    async def export_to_json(self) -> None:
        """Export all jobs to JSON file for human-readable viewing."""
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        try:
            # Get all jobs
            jobs = await self.list_jobs(include_disabled=True, limit=10000)
            
            # Get recent runs
            recent_runs, _ = await self.get_runs(limit=100)
            
            # Build export data
            export_data = {
                "exported_at": datetime.now().isoformat(),
                "total_jobs": len(jobs),
                "jobs": [self._job_to_export_dict(job) for job in jobs],
                "recent_runs": [run.to_dict() for run in recent_runs],
            }

            # Write to JSON file
            self.json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"Exported {len(jobs)} jobs to {self.json_path}")

        except Exception as e:
            logger.error(f"Failed to export to JSON: {e}")

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

    async def import_from_json(self, json_path: str | Path | None = None) -> int:
        """Import jobs from JSON file.
        
        Args:
            json_path: Path to JSON file, defaults to self.json_path
            
        Returns:
            Number of jobs imported
        """
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

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
                await self.save(job)
                imported += 1

            logger.info(f"Imported {imported} jobs from {path}")
            return imported

        except Exception as e:
            logger.error(f"Failed to import from JSON: {e}")
            return 0

    async def get_json_path(self) -> Path:
        """Get the path to the JSON export file."""
        return self.json_path

    # ============== Job Runs (Execution History) ==============

    async def save_run(self, run: JobRun) -> None:
        """Save a job run record."""
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        # Generate ID if not set
        if not run.id:
            run.id = f"run_{uuid4().hex[:12]}"

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO job_runs (
                id, job_id, started_at_ms, finished_at_ms,
                status, result, error, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.job_id,
                run.started_at_ms,
                run.finished_at_ms,
                run.status.value,
                run.result,
                run.error,
                run.duration_ms,
            ),
        )
        await self._connection.commit()

    async def get_runs(
        self,
        job_id: str | None = None,
        status: RunStatus | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[JobRun], int]:
        """Get job runs with filters.

        Args:
            job_id: Filter by job ID
            status: Filter by run status
            since_ms: Filter runs after this timestamp
            until_ms: Filter runs before this timestamp
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            Tuple of (runs list, total count)
        """
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        # Build query
        where_clauses = []
        params: list[Any] = []

        if job_id:
            where_clauses.append("job_id = ?")
            params.append(job_id)

        if status:
            where_clauses.append("status = ?")
            params.append(status.value)

        if since_ms:
            where_clauses.append("started_at_ms >= ?")
            params.append(since_ms)

        if until_ms:
            where_clauses.append("started_at_ms <= ?")
            params.append(until_ms)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # Get total count
        count_query = f"SELECT COUNT(*) FROM job_runs WHERE {where_sql}"
        async with self._connection.execute(count_query, params) as cursor:
            row = await cursor.fetchone()
            total = row[0] if row else 0

        # Get runs
        query = f"""
            SELECT * FROM job_runs
            WHERE {where_sql}
            ORDER BY started_at_ms DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        runs = []
        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            for row in rows:
                data = dict(zip(columns, row))
                runs.append(JobRun(
                    id=data.get("id", ""),
                    job_id=data.get("job_id", ""),
                    started_at_ms=data.get("started_at_ms", 0),
                    finished_at_ms=data.get("finished_at_ms"),
                    status=RunStatus(data.get("status", "ok")),
                    result=data.get("result"),
                    error=data.get("error"),
                    duration_ms=data.get("duration_ms", 0),
                ))

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
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        stats = JobStats(job_id=job_id)

        # Get counts and aggregates
        query = """
            SELECT
                COUNT(*) as total_runs,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status IN ('failed', 'timeout') THEN 1 ELSE 0 END) as failure_count,
                AVG(duration_ms) as avg_duration,
                MAX(duration_ms) as max_duration,
                MIN(duration_ms) as min_duration
            FROM job_runs
            WHERE job_id = ?
        """

        async with self._connection.execute(query, (job_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                stats.total_runs = row[0] or 0
                stats.success_count = row[1] or 0
                stats.failure_count = row[2] or 0
                stats.avg_duration_ms = float(row[3] or 0)
                stats.max_duration_ms = row[4] or 0
                stats.min_duration_ms = row[5] or 0

                if stats.total_runs > 0:
                    stats.success_rate = stats.success_count / stats.total_runs

        # Get last run info
        last_run_query = """
            SELECT started_at_ms, status FROM job_runs
            WHERE job_id = ?
            ORDER BY started_at_ms DESC
            LIMIT 1
        """

        async with self._connection.execute(last_run_query, (job_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                stats.last_run_at_ms = row[0]
                stats.last_status = row[1]

        return stats

    async def get_runs_stats_today(self) -> dict[str, Any]:
        """Get run statistics for today."""
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        # Calculate start of today in ms
        now = datetime.now()
        start_of_day = datetime(now.year, now.month, now.day)
        start_of_day_ms = int(start_of_day.timestamp() * 1000)

        query = """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status IN ('failed', 'timeout') THEN 1 ELSE 0 END) as failed,
                AVG(duration_ms) as avg_duration
            FROM job_runs
            WHERE started_at_ms >= ?
        """

        async with self._connection.execute(query, (start_of_day_ms,)) as cursor:
            row = await cursor.fetchone()
            if row:
                total = row[0] or 0
                success = row[1] or 0
                failed = row[2] or 0
                avg_duration = float(row[3] or 0)
                success_rate = success / total if total > 0 else 0.0

                return {
                    "total": total,
                    "success": success,
                    "failed": failed,
                    "success_rate": success_rate,
                    "avg_duration_ms": avg_duration,
                }

        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "success_rate": 0.0,
            "avg_duration_ms": 0.0,
        }

    async def delete_old_runs(self, before_ms: int) -> int:
        """Delete runs older than the given timestamp.

        Args:
            before_ms: Delete runs started before this timestamp

        Returns:
            Number of deleted runs
        """
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        result = await self._connection.execute(
            "DELETE FROM job_runs WHERE started_at_ms < ?",
            (before_ms,)
        )
        await self._connection.commit()
        return result.rowcount

    async def get_upcoming_jobs(
        self,
        within_ms: int,
        limit: int = 20,
    ) -> list[ScheduledJob]:
        """Get jobs scheduled to run within the given time window.

        Args:
            within_ms: Time window in milliseconds from now
            limit: Maximum number of jobs to return

        Returns:
            List of upcoming jobs sorted by next_run_at_ms
        """
        if not self._connection:
            raise RuntimeError("JobStore not initialized")

        now_ms = int(datetime.now().timestamp() * 1000)
        until_ms = now_ms + within_ms

        query = """
            SELECT * FROM scheduled_jobs
            WHERE enabled = 1
            AND status = 'active'
            AND json_extract(state, '$.next_run_at_ms') IS NOT NULL
            AND json_extract(state, '$.next_run_at_ms') <= ?
            ORDER BY json_extract(state, '$.next_run_at_ms') ASC
            LIMIT ?
        """

        jobs = []
        async with self._connection.execute(query, (until_ms, limit)) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                jobs.append(self._row_to_job(row, cursor.description))

        return jobs
