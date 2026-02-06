"""Core operations for the scheduler service.

Contains the main business logic for job management.
"""
from datetime import datetime
from typing import Optional, List

from loguru import logger

from ..models import ScheduledJob, JobCreate, JobPatch
from ..schedule import compute_next_run_at_ms, now_ms
from ..types import (
    JobStatus,
    RemoveResult,
    RunResult,
    SchedulerStatus,
    TaskChainPayload,
)
from .events import EventEmitter, emit_job_event, EventTypes
from .store import JobStore
from .state import SchedulerServiceState, SchedulerServiceDeps

logger = logger.bind(module="scheduler.ops")


async def add_job(
    store: JobStore,
    events: EventEmitter,
    job_create: JobCreate,
) -> ScheduledJob:
    """Add a new scheduled job.

    Args:
        store: Job store
        events: Event emitter
        job_create: Job creation request

    Returns:
        Created job with computed next_run_at_ms
    """
    # Create job from request
    job = ScheduledJob(
        user_id=job_create.user_id,
        agent_id=job_create.agent_id,
        name=job_create.name,
        description=job_create.description,
        enabled=job_create.enabled,
        schedule=job_create.schedule,
        payload=job_create.payload,
        target=job_create.target,
        max_retries=job_create.max_retries,
    )

    # Compute initial next_run_at_ms
    job.state.next_run_at_ms = compute_next_run_at_ms(job.schedule, now_ms())

    # Set status based on whether there's a next run
    if job.enabled and job.state.next_run_at_ms:
        job.status = JobStatus.ACTIVE
    elif not job.state.next_run_at_ms:
        job.status = JobStatus.COMPLETED
    else:
        job.status = JobStatus.PENDING

    # Save to store
    await store.save(job)

    # Emit event
    emit_job_event(events, EventTypes.JOB_ADDED, job.id)

    logger.info(f"Added job {job.id}: {job.name}")
    return job


async def update_job(
    store: JobStore,
    events: EventEmitter,
    job_id: str,
    patch: JobPatch,
) -> ScheduledJob | None:
    """Update an existing job.

    Args:
        store: Job store
        events: Event emitter
        job_id: ID of job to update
        patch: Update request

    Returns:
        Updated job, or None if not found
    """
    job = await store.get(job_id)
    if not job:
        return None

    # Apply patch
    patch.apply(job)

    # Recompute next_run_at_ms if schedule changed
    if patch.schedule is not None:
        job.state.next_run_at_ms = compute_next_run_at_ms(
            job.schedule, now_ms(), job.state.last_run_at_ms
        )

    # Update status if enabled changed
    if patch.enabled is not None:
        if patch.enabled and job.state.next_run_at_ms:
            job.status = JobStatus.ACTIVE
        elif not patch.enabled:
            job.status = JobStatus.PAUSED

    # Save
    await store.save(job)

    # Emit event
    emit_job_event(events, EventTypes.JOB_UPDATED, job.id)

    logger.info(f"Updated job {job.id}")
    return job


async def remove_job(
    store: JobStore,
    events: EventEmitter,
    job_id: str,
) -> RemoveResult:
    """Remove a scheduled job.

    Args:
        store: Job store
        events: Event emitter
        job_id: ID of job to remove

    Returns:
        Remove result
    """
    job = await store.get(job_id)
    if not job:
        return RemoveResult(
            job_id=job_id,
            removed=False,
            reason="Job not found",
        )

    # Delete from store
    success = await store.delete(job_id)

    if success:
        # Emit event
        emit_job_event(events, EventTypes.JOB_REMOVED, job_id)
        logger.info(f"Removed job {job_id}")

    return RemoveResult(
        job_id=job_id,
        removed=success,
        reason="" if success else "Delete failed",
    )


async def pause_job(
    store: JobStore,
    events: EventEmitter,
    job_id: str,
) -> ScheduledJob | None:
    """Pause a scheduled job.

    Args:
        store: Job store
        events: Event emitter
        job_id: ID of job to pause

    Returns:
        Updated job, or None if not found
    """
    job = await store.get(job_id)
    if not job:
        return None

    job.enabled = False
    job.status = JobStatus.PAUSED
    job.updated_at_ms = int(datetime.now().timestamp() * 1000)

    await store.save(job)

    emit_job_event(events, EventTypes.JOB_PAUSED, job_id)
    logger.info(f"Paused job {job_id}")

    return job


async def resume_job(
    store: JobStore,
    events: EventEmitter,
    job_id: str,
) -> ScheduledJob | None:
    """Resume a paused job.

    Args:
        store: Job store
        events: Event emitter
        job_id: ID of job to resume

    Returns:
        Updated job, or None if not found
    """
    job = await store.get(job_id)
    if not job:
        return None

    # Recompute next run time
    job.state.next_run_at_ms = compute_next_run_at_ms(
        job.schedule, now_ms(), job.state.last_run_at_ms
    )

    if job.state.next_run_at_ms:
        job.enabled = True
        job.status = JobStatus.ACTIVE
    else:
        # No more runs, mark completed
        job.status = JobStatus.COMPLETED

    job.updated_at_ms = int(datetime.now().timestamp() * 1000)

    await store.save(job)

    emit_job_event(events, EventTypes.JOB_RESUMED, job_id)
    logger.info(f"Resumed job {job_id}")

    return job


async def chain_jobs(
    store: JobStore,
    events: EventEmitter,
    job_id: str,
    next_job_id: str,
    on_status: Optional[List[str]] = None,
) -> ScheduledJob | None:
    """Chain two jobs together.

    Args:
        store: Job store
        events: Event emitter
        job_id: ID of source job
        next_job_id: ID of job to trigger on completion
        on_status: List of statuses that trigger the chain (default: ["ok"])

    Returns:
        Updated source job, or None if not found
    """
    job = await store.get(job_id)
    if not job:
        return None

    # Verify next job exists
    next_job = await store.get(next_job_id)
    if not next_job:
        logger.warning(f"Chain target job {next_job_id} not found")
        return None

    # Add chain payload
    chain = TaskChainPayload(
        next_job_id=next_job_id,
        on_status=on_status or ["ok"],
    )

    # Avoid duplicates
    existing_ids = [c.next_job_id for c in job.on_complete]
    if next_job_id not in existing_ids:
        job.on_complete.append(chain)

    job.updated_at_ms = int(datetime.now().timestamp() * 1000)

    await store.save(job)

    logger.info(f"Chained job {job_id} -> {next_job_id}")
    return job


async def get_status(
    store: JobStore,
    state: SchedulerServiceState,
) -> SchedulerStatus:
    """Get scheduler status.

    Args:
        store: Job store
        state: Service state

    Returns:
        Scheduler status
    """
    counts = await store.count_by_status()

    return SchedulerStatus(
        running=state.running,
        jobs_total=sum(counts.values()),
        jobs_active=counts.get(JobStatus.ACTIVE.value, 0),
        jobs_paused=counts.get(JobStatus.PAUSED.value, 0),
        next_run_at_ms=state.next_wake_at_ms,
    )
