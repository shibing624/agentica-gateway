"""Timer management for the scheduler.

Handles scheduling wake-ups and running due jobs.
"""
import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger

from ..schedule import compute_next_run_at_ms, now_ms
from ..types import JobStatus, RunStatus, RunResult, JobRun
from .events import emit_job_event, EventTypes

if TYPE_CHECKING:
    from .service import SchedulerService

logger = logger.bind(module="scheduler.timer")


async def arm_timer(service: "SchedulerService") -> None:
    """Arm the timer to wake up at the next scheduled job time.

    This function calculates the next wake time and schedules
    an asyncio task to run due jobs at that time.
    """
    async with service.state.lock:
        # Get the next run time from the store
        next_run_ms = await service.store.get_next_run_time()

        if next_run_ms is None:
            # No scheduled jobs, clear wake time
            service.state.next_wake_at_ms = None
            logger.debug("No jobs scheduled, timer not armed")
            return

        service.state.next_wake_at_ms = next_run_ms

        # Signal the timer loop to check for new wake time
        service.state.wake_event.set()

        logger.debug(f"Timer armed for {next_run_ms}")


async def timer_loop(service: "SchedulerService") -> None:
    """Main timer loop that runs due jobs.

    This loop runs continuously and:
    1. Calculates sleep time until next job
    2. Sleeps until then (or until woken early)
    3. Runs all due jobs
    4. Repeats
    """
    logger.info("Timer loop started")

    while service.state.running:
        try:
            current_ms = now_ms()

            # Get next wake time
            next_wake_ms = service.state.next_wake_at_ms

            if next_wake_ms is None:
                # No jobs scheduled, wait for signal
                service.state.wake_event.clear()
                try:
                    await asyncio.wait_for(
                        service.state.wake_event.wait(),
                        timeout=60.0,  # Check every minute even if no jobs
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            # Calculate sleep duration
            sleep_ms = max(0, next_wake_ms - current_ms)
            sleep_seconds = sleep_ms / 1000.0

            if sleep_seconds > 0:
                # Wait until next wake time or until signaled
                service.state.wake_event.clear()
                try:
                    await asyncio.wait_for(
                        service.state.wake_event.wait(),
                        timeout=sleep_seconds,
                    )
                    # If we were woken early, re-calculate
                    continue
                except asyncio.TimeoutError:
                    pass  # Time to run jobs

            # Run due jobs
            await run_due_jobs(service)

            # Re-arm timer for next wake
            await arm_timer(service)

        except asyncio.CancelledError:
            logger.info("Timer loop cancelled")
            break
        except Exception as e:
            logger.error(f"Timer loop error: {e}")
            await asyncio.sleep(1)  # Avoid tight loop on errors

    logger.info("Timer loop stopped")


async def run_due_jobs(service: "SchedulerService") -> list[RunResult]:
    """Run all jobs that are due.

    Args:
        service: The scheduler service

    Returns:
        List of run results
    """
    current_ms = now_ms()
    results: list[RunResult] = []

    # Get due jobs from store
    due_jobs = await service.store.get_due_jobs(current_ms)

    if not due_jobs:
        return results

    logger.info(f"Running {len(due_jobs)} due jobs")

    for job in due_jobs:
        try:
            result = await run_single_job(service, job.id)
            results.append(result)
        except Exception as e:
            logger.error(f"Failed to run job {job.id}: {e}")
            results.append(RunResult(
                job_id=job.id,
                status=RunStatus.FAILED,
                started_at_ms=current_ms,
                finished_at_ms=now_ms(),
                error=str(e),
            ))

    return results


async def run_single_job(
    service: "SchedulerService",
    job_id: str,
    force: bool = False,
) -> RunResult:
    """Run a single job.

    Args:
        service: The scheduler service
        job_id: ID of the job to run
        force: If True, run even if not due

    Returns:
        Run result
    """
    started_at_ms = now_ms()

    # Get the job
    job = await service.store.get(job_id)
    if not job:
        return RunResult(
            job_id=job_id,
            status=RunStatus.FAILED,
            started_at_ms=started_at_ms,
            finished_at_ms=now_ms(),
            error="Job not found",
        )

    # Check if job is enabled and active
    if not job.enabled or job.status != JobStatus.ACTIVE:
        if not force:
            return RunResult(
                job_id=job_id,
                status=RunStatus.SKIPPED,
                started_at_ms=started_at_ms,
                finished_at_ms=now_ms(),
                error="Job not active",
            )

    # Emit before run event
    emit_job_event(service.events, EventTypes.JOB_STARTED, job_id)

    # Create run record
    run = JobRun(
        id=f"run_{uuid4().hex[:12]}",
        job_id=job_id,
        started_at_ms=started_at_ms,
        status=RunStatus.OK,
    )

    try:
        # Execute the job
        result = None
        if service.deps.executor:
            # Support both callable and object with execute method
            if callable(service.deps.executor):
                result = await service.deps.executor(job)
            else:
                result = await service.deps.executor.execute(job)

        # Update job state
        job.state.last_run_at_ms = now_ms()
        job.state.run_count += 1
        job.state.last_status = RunStatus.OK
        job.state.last_error = None
        job.state.consecutive_failures = 0

        # Calculate next run time
        next_run = compute_next_run_at_ms(job.schedule, now_ms(), job.state.last_run_at_ms)
        job.state.next_run_at_ms = next_run

        # Mark completed if one-time job
        if next_run is None:
            job.status = JobStatus.COMPLETED
            job.enabled = False

        # Save updated state
        await service.store.save(job)

        # Update and save run record
        finished_at_ms = now_ms()
        run.finished_at_ms = finished_at_ms
        run.duration_ms = finished_at_ms - started_at_ms
        run.status = RunStatus.OK
        run.result = str(result)[:1000] if result else None
        await service.store.save_run(run)

        # Emit completion event
        emit_job_event(
            service.events,
            EventTypes.JOB_COMPLETED,
            job_id,
            {"result": str(result)[:500] if result else None},
        )

        # Trigger task chains
        await trigger_chains(service, job, RunStatus.OK)

        return RunResult(
            job_id=job_id,
            status=RunStatus.OK,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            result=result,
        )

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Job {job_id} failed: {error_msg}")

        # Update failure state
        job.state.last_run_at_ms = now_ms()
        job.state.run_count += 1
        job.state.failure_count += 1
        job.state.consecutive_failures += 1
        job.state.last_status = RunStatus.FAILED
        job.state.last_error = error_msg[:500]

        # Check if max retries exceeded
        if job.state.consecutive_failures >= job.max_retries:
            job.status = JobStatus.FAILED
            job.enabled = False
        else:
            # Schedule retry
            job.state.next_run_at_ms = now_ms() + job.retry_delay_ms

        await service.store.save(job)

        # Update and save run record
        finished_at_ms = now_ms()
        run.finished_at_ms = finished_at_ms
        run.duration_ms = finished_at_ms - started_at_ms
        run.status = RunStatus.FAILED
        run.error = error_msg[:1000]
        await service.store.save_run(run)

        # Emit failure event
        emit_job_event(
            service.events,
            EventTypes.JOB_FAILED,
            job_id,
            {"error": error_msg[:500]},
        )

        # Trigger failure chains
        await trigger_chains(service, job, RunStatus.FAILED)

        return RunResult(
            job_id=job_id,
            status=RunStatus.FAILED,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            error=error_msg,
        )


async def trigger_chains(
    service: "SchedulerService",
    job: Any,  # ScheduledJob type
    status: RunStatus,
) -> None:
    """Trigger task chains based on job completion status.

    Args:
        service: The scheduler service
        job: The completed job
        status: The completion status
    """
    for chain in job.on_complete:
        # Check if status matches trigger condition
        if status.value in chain.on_status:
            logger.info(f"Triggering chain: {job.id} -> {chain.next_job_id}")

            # Emit chain event
            emit_job_event(
                service.events,
                EventTypes.CHAIN_TRIGGERED,
                job.id,
                {"next_job_id": chain.next_job_id},
            )

            try:
                # Run the chained job
                await run_single_job(service, chain.next_job_id, force=True)
            except Exception as e:
                logger.error(f"Chain trigger failed: {e}")
