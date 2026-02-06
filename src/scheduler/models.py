"""Data models for scheduled jobs.

Refactored to use the new Schedule and Payload types from types.py.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
import uuid

from .types import (
    Schedule,
    Payload,
    JobStatus,
    RunStatus,
    AtSchedule,
    EverySchedule,
    CronSchedule,
    AgentTurnPayload,
    SystemEventPayload,
    TaskChainPayload,
    SessionTarget,
    SessionTargetKind,
    schedule_from_dict,
    payload_from_dict,
)


# Keep old enums for backwards compatibility during migration
class TaskStatus(str, Enum):
    """Deprecated: Use JobStatus instead."""
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"


class TaskType:
    """Deprecated: Use PayloadKind instead."""
    AGENT_RUN = "agent_turn"
    NOTIFICATION = "system_event"
    WEBHOOK = "webhook"
    SYSTEM_COMMAND = "system_event"


@dataclass
class JobState:
    """Runtime state of a scheduled job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: RunStatus | None = None
    run_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_run_at_ms": self.next_run_at_ms,
            "last_run_at_ms": self.last_run_at_ms,
            "last_status": self.last_status.value if self.last_status else None,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        return cls(
            next_run_at_ms=data.get("next_run_at_ms"),
            last_run_at_ms=data.get("last_run_at_ms"),
            last_status=RunStatus(data["last_status"]) if data.get("last_status") else None,
            run_count=data.get("run_count", 0),
            failure_count=data.get("failure_count", 0),
            consecutive_failures=data.get("consecutive_failures", 0),
            last_error=data.get("last_error"),
        )


@dataclass
class ScheduledJob:
    """Represents a scheduled job.

    This is the new unified model that replaces ScheduledTask.
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    agent_id: str = "main"

    # Definition
    name: str = ""
    description: str = ""
    enabled: bool = True

    # Schedule configuration
    schedule: Schedule = field(default_factory=lambda: AtSchedule())

    # Payload configuration
    payload: Payload = field(default_factory=lambda: AgentTurnPayload())

    # Session target (main/isolated)
    target: SessionTarget = field(default_factory=SessionTarget)

    # Execution settings
    max_retries: int = 3
    retry_delay_ms: int = 60000  # 1 minute

    # Task chain: jobs to trigger on completion
    on_complete: list[TaskChainPayload] = field(default_factory=list)

    # Runtime state
    state: JobState = field(default_factory=JobState)
    status: JobStatus = JobStatus.PENDING

    # Timestamps
    created_at_ms: int = field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    updated_at_ms: int = field(default_factory=lambda: int(datetime.now().timestamp() * 1000))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "schedule": self.schedule.to_dict(),
            "payload": self.payload.to_dict(),
            "target": self.target.to_dict(),
            "max_retries": self.max_retries,
            "retry_delay_ms": self.retry_delay_ms,
            "on_complete": [p.to_dict() for p in self.on_complete],
            "state": self.state.to_dict(),
            "status": self.status.value,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledJob":
        """Create from dictionary."""
        job = cls(
            id=data.get("id", str(uuid.uuid4())),
            user_id=data.get("user_id", ""),
            agent_id=data.get("agent_id", "main"),
            name=data.get("name", ""),
            description=data.get("description", ""),
            enabled=data.get("enabled", True),
            max_retries=data.get("max_retries", 3),
            retry_delay_ms=data.get("retry_delay_ms", 60000),
            status=JobStatus(data.get("status", "pending")),
            created_at_ms=data.get("created_at_ms", int(datetime.now().timestamp() * 1000)),
            updated_at_ms=data.get("updated_at_ms", int(datetime.now().timestamp() * 1000)),
        )

        # Parse schedule
        if data.get("schedule"):
            job.schedule = schedule_from_dict(data["schedule"])

        # Parse payload
        if data.get("payload"):
            job.payload = payload_from_dict(data["payload"])

        # Parse target
        if data.get("target"):
            job.target = SessionTarget.from_dict(data["target"])

        # Parse state
        if data.get("state"):
            job.state = JobState.from_dict(data["state"])

        # Parse on_complete chain
        if data.get("on_complete"):
            job.on_complete = [
                TaskChainPayload.from_dict(p) for p in data["on_complete"]
            ]

        return job


@dataclass
class JobCreate:
    """Request to create a new job."""
    user_id: str
    name: str = ""
    description: str = ""
    schedule: Schedule = field(default_factory=lambda: AtSchedule())
    payload: Payload = field(default_factory=lambda: AgentTurnPayload())
    target: SessionTarget = field(default_factory=SessionTarget)
    agent_id: str = "main"
    max_retries: int = 3
    enabled: bool = True


@dataclass
class JobPatch:
    """Request to update an existing job."""
    name: str | None = None
    description: str | None = None
    schedule: Schedule | None = None
    payload: Payload | None = None
    enabled: bool | None = None
    max_retries: int | None = None

    def apply(self, job: ScheduledJob) -> None:
        """Apply patch to a job."""
        if self.name is not None:
            job.name = self.name
        if self.description is not None:
            job.description = self.description
        if self.schedule is not None:
            job.schedule = self.schedule
        if self.payload is not None:
            job.payload = self.payload
        if self.enabled is not None:
            job.enabled = self.enabled
        if self.max_retries is not None:
            job.max_retries = self.max_retries
        job.updated_at_ms = int(datetime.now().timestamp() * 1000)


# ============== Legacy Compatibility ==============
# Keep ScheduledTask as an alias during migration

@dataclass
class ScheduledTask:
    """Deprecated: Use ScheduledJob instead.

    This class is kept for backwards compatibility during migration.
    """
    # Core fields
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""

    # Task definition (legacy fields)
    task_type: str = "agent_run"
    prompt: str = ""
    parsed_action: str = ""

    # Schedule definition (legacy - supports multiple formats)
    cron_expression: str | None = None
    interval_seconds: int | None = None
    run_at: datetime | None = None
    timezone: str = "Asia/Shanghai"

    # Notification settings
    notify_channel: str = "telegram"
    notify_chat_id: str = ""

    # Execution settings
    max_retries: int = 3
    retry_delay_seconds: int = 60
    timeout_seconds: int = 300

    # Metadata
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    failure_count: int = 0
    last_error: str | None = None

    # APScheduler job reference (legacy)
    job_id: str | None = None

    # Additional context
    context: dict[str, Any] = field(default_factory=dict)

    def to_job(self) -> ScheduledJob:
        """Convert to new ScheduledJob model."""
        # Determine schedule type
        schedule: Schedule
        if self.cron_expression:
            schedule = CronSchedule(
                expression=self.cron_expression,
                timezone=self.timezone,
            )
        elif self.interval_seconds:
            schedule = EverySchedule.from_seconds(self.interval_seconds)
        elif self.run_at:
            schedule = AtSchedule.from_datetime(self.run_at)
        else:
            schedule = AtSchedule()

        # Create payload
        payload = AgentTurnPayload(
            prompt=self.parsed_action or self.prompt,
            agent_id="main",
            context=self.context,
            notify_channel=self.notify_channel,
            notify_chat_id=self.notify_chat_id,
            timeout_seconds=self.timeout_seconds,
        )

        # Create state
        state = JobState(
            next_run_at_ms=int(self.next_run_at.timestamp() * 1000) if self.next_run_at else None,
            last_run_at_ms=int(self.last_run_at.timestamp() * 1000) if self.last_run_at else None,
            run_count=self.run_count,
            failure_count=self.failure_count,
            last_error=self.last_error,
        )

        # Map status
        status_map = {
            TaskStatus.PENDING: JobStatus.PENDING,
            TaskStatus.ACTIVE: JobStatus.ACTIVE,
            TaskStatus.PAUSED: JobStatus.PAUSED,
            TaskStatus.COMPLETED: JobStatus.COMPLETED,
            TaskStatus.FAILED: JobStatus.FAILED,
        }
        job_status = status_map.get(self.status, JobStatus.PENDING)

        return ScheduledJob(
            id=self.id,
            user_id=self.user_id,
            name=self.parsed_action[:50] if self.parsed_action else self.prompt[:50],
            description=self.prompt,
            enabled=self.status not in (TaskStatus.PAUSED, TaskStatus.COMPLETED, TaskStatus.FAILED),
            schedule=schedule,
            payload=payload,
            max_retries=self.max_retries,
            retry_delay_ms=self.retry_delay_seconds * 1000,
            state=state,
            status=job_status,
            created_at_ms=int(self.created_at.timestamp() * 1000),
            updated_at_ms=int(self.updated_at.timestamp() * 1000),
        )

    @classmethod
    def from_job(cls, job: ScheduledJob) -> "ScheduledTask":
        """Create from new ScheduledJob model."""
        task = cls(
            id=job.id,
            user_id=job.user_id,
            max_retries=job.max_retries,
            retry_delay_seconds=job.retry_delay_ms // 1000,
            created_at=datetime.fromtimestamp(job.created_at_ms / 1000),
            updated_at=datetime.fromtimestamp(job.updated_at_ms / 1000),
        )

        # Extract schedule
        if isinstance(job.schedule, CronSchedule):
            task.cron_expression = job.schedule.expression
            task.timezone = job.schedule.timezone
        elif isinstance(job.schedule, EverySchedule):
            task.interval_seconds = job.schedule.interval_ms // 1000
        elif isinstance(job.schedule, AtSchedule):
            task.run_at = datetime.fromtimestamp(job.schedule.at_ms / 1000)

        # Extract payload
        if isinstance(job.payload, AgentTurnPayload):
            task.task_type = "agent_run"
            task.prompt = job.description or job.payload.prompt
            task.parsed_action = job.name or job.payload.prompt
            task.notify_channel = job.payload.notify_channel
            task.notify_chat_id = job.payload.notify_chat_id
            task.timeout_seconds = job.payload.timeout_seconds
            task.context = job.payload.context
        elif isinstance(job.payload, SystemEventPayload):
            task.task_type = "notification"
            task.prompt = job.payload.message
            task.parsed_action = job.payload.message
            task.notify_channel = job.payload.channel
            task.notify_chat_id = job.payload.chat_id

        # Extract state
        if job.state.next_run_at_ms:
            task.next_run_at = datetime.fromtimestamp(job.state.next_run_at_ms / 1000)
        if job.state.last_run_at_ms:
            task.last_run_at = datetime.fromtimestamp(job.state.last_run_at_ms / 1000)
        task.run_count = job.state.run_count
        task.failure_count = job.state.failure_count
        task.last_error = job.state.last_error

        # Map status
        status_map = {
            JobStatus.PENDING: TaskStatus.PENDING,
            JobStatus.ACTIVE: TaskStatus.ACTIVE,
            JobStatus.PAUSED: TaskStatus.PAUSED,
            JobStatus.COMPLETED: TaskStatus.COMPLETED,
            JobStatus.FAILED: TaskStatus.FAILED,
        }
        task.status = status_map.get(job.status, TaskStatus.PENDING)  # type: ignore[arg-type]

        return task

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "task_type": self.task_type,
            "prompt": self.prompt,
            "parsed_action": self.parsed_action,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "run_at": self.run_at.isoformat() if self.run_at else None,
            "timezone": self.timezone,
            "notify_channel": self.notify_channel,
            "notify_chat_id": self.notify_chat_id,
            "max_retries": self.max_retries,
            "retry_delay_seconds": self.retry_delay_seconds,
            "timeout_seconds": self.timeout_seconds,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "job_id": self.job_id,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        """Create from dictionary."""
        task = cls(
            id=data.get("id", str(uuid.uuid4())),
            user_id=data.get("user_id", ""),
            task_type=data.get("task_type", "agent_run"),
            prompt=data.get("prompt", ""),
            parsed_action=data.get("parsed_action", ""),
            cron_expression=data.get("cron_expression"),
            interval_seconds=data.get("interval_seconds"),
            timezone=data.get("timezone", "Asia/Shanghai"),
            notify_channel=data.get("notify_channel", "telegram"),
            notify_chat_id=data.get("notify_chat_id", ""),
            max_retries=data.get("max_retries", 3),
            retry_delay_seconds=data.get("retry_delay_seconds", 60),
            timeout_seconds=data.get("timeout_seconds", 300),
            status=TaskStatus(data.get("status", "pending")),
            run_count=data.get("run_count", 0),
            failure_count=data.get("failure_count", 0),
            last_error=data.get("last_error"),
            job_id=data.get("job_id"),
            context=data.get("context", {}),
        )

        # Parse datetime fields
        if data.get("run_at"):
            task.run_at = datetime.fromisoformat(data["run_at"])
        if data.get("created_at"):
            task.created_at = datetime.fromisoformat(data["created_at"])
        if data.get("updated_at"):
            task.updated_at = datetime.fromisoformat(data["updated_at"])
        if data.get("last_run_at"):
            task.last_run_at = datetime.fromisoformat(data["last_run_at"])
        if data.get("next_run_at"):
            task.next_run_at = datetime.fromisoformat(data["next_run_at"])

        return task
