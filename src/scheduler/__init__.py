"""Scheduler module for natural language scheduled tasks.

This module provides a unified scheduler system with:
- Natural language task parsing
- Multiple schedule types (at/every/cron)
- Task chaining
- JSON file persistence (easy to view and edit)
- asyncio-based timer
"""
# Core types
from .types import (
    # Schedule types
    ScheduleKind,
    AtSchedule,
    EverySchedule,
    CronSchedule,
    Schedule,
    schedule_from_dict,
    # Payload types
    PayloadKind,
    SystemEventPayload,
    AgentTurnPayload,
    WebhookPayload,
    TaskChainPayload,
    Payload,
    payload_from_dict,
    # Status types
    JobStatus,
    RunStatus,
    HookPoint,
    # Result types
    SchedulerEvent,
    RunResult,
    RemoveResult,
    SchedulerStatus,
    # Monitoring types
    JobRun,
    JobStats,
    SchedulerStats,
    BatchResult,
)

# Models
from .models import (
    ScheduledJob,
    JobState,
    JobCreate,
    JobPatch,
    # Legacy compatibility
    ScheduledTask,
    TaskStatus,
    TaskType,
)

# Schedule utilities
from .schedule import (
    compute_next_run_at_ms,
    schedule_to_human,
    cron_to_human,
    interval_to_human,
    validate_cron_expression,
    now_ms,
)

# Service
from .service import SchedulerService

# Executor
from .executor import JobExecutor

# Tools
from .tools import (
    init_scheduler_tools,
    # Tool definitions
    CREATE_SCHEDULED_JOB_TOOL,
    LIST_SCHEDULED_JOBS_TOOL,
    DELETE_SCHEDULED_JOB_TOOL,
    PAUSE_JOB_TOOL,
    RESUME_JOB_TOOL,
    CREATE_TASK_CHAIN_TOOL,
    ALL_SCHEDULER_TOOLS,
    TOOL_IMPLEMENTATIONS,
    # Tool functions
    create_scheduled_job_tool,
    list_scheduled_jobs_tool,
    delete_scheduled_job_tool,
    pause_scheduled_job_tool,
    resume_scheduled_job_tool,
    create_task_chain_tool,
)

__all__ = [
    # Core types
    "ScheduleKind",
    "AtSchedule",
    "EverySchedule",
    "CronSchedule",
    "Schedule",
    "schedule_from_dict",
    "PayloadKind",
    "SystemEventPayload",
    "AgentTurnPayload",
    "WebhookPayload",
    "TaskChainPayload",
    "Payload",
    "payload_from_dict",
    "JobStatus",
    "RunStatus",
    "HookPoint",
    "SchedulerEvent",
    "RunResult",
    "RemoveResult",
    "SchedulerStatus",
    # Monitoring types
    "JobRun",
    "JobStats",
    "SchedulerStats",
    "BatchResult",
    # Models
    "ScheduledJob",
    "JobState",
    "JobCreate",
    "JobPatch",
    "ScheduledTask",
    "TaskStatus",
    "TaskType",
    # Schedule utilities
    "compute_next_run_at_ms",
    "schedule_to_human",
    "cron_to_human",
    "interval_to_human",
    "validate_cron_expression",
    "now_ms",
    # Service
    "SchedulerService",
    # Executor
    "JobExecutor",
    # Tools
    "init_scheduler_tools",
    "CREATE_SCHEDULED_JOB_TOOL",
    "LIST_SCHEDULED_JOBS_TOOL",
    "DELETE_SCHEDULED_JOB_TOOL",
    "PAUSE_JOB_TOOL",
    "RESUME_JOB_TOOL",
    "CREATE_TASK_CHAIN_TOOL",
    "ALL_SCHEDULER_TOOLS",
    "TOOL_IMPLEMENTATIONS",
    "create_scheduled_job_tool",
    "list_scheduled_jobs_tool",
    "delete_scheduled_job_tool",
    "pause_scheduled_job_tool",
    "resume_scheduled_job_tool",
    "create_task_chain_tool",
]
