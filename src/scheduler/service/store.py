"""SQLite persistence layer for scheduled jobs.

Refactored from task_store.py to work with the new ScheduledJob model.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from ..models import ScheduledJob, JobState
from ..types import (
    JobStatus,
    schedule_from_dict,
    payload_from_dict,
    TaskChainPayload,
)

logger = logger.bind(module="scheduler.store")


class JobStore:
    """SQLite-based job persistence."""

    def __init__(self, db_path: str | Path):
        """Initialize job store.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = Path(db_path)
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
