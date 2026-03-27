"""Scheduler routes: /api/scheduler/*"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from .. import deps
from ..models import JobCreateRequest, BatchJobsRequest, CloneJobRequest
from ..scheduler import SchedulerService, schedule_to_human

router = APIRouter(prefix="/api/scheduler")


# ============== Job CRUD ==============

@router.get("/jobs")
async def list_jobs(
    user_id: Optional[str] = None,
    include_disabled: bool = False,
    limit: int = 100,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    jobs = await svc.list(user_id=user_id, include_disabled=include_disabled, limit=limit)
    return {
        "jobs": [
            {
                "id": j.id,
                "name": j.name,
                "description": j.description,
                "user_id": j.user_id,
                "schedule": schedule_to_human(j.schedule),
                "status": j.status.value,
                "enabled": j.enabled,
                "next_run_at_ms": j.state.next_run_at_ms,
                "last_run_at_ms": j.state.last_run_at_ms,
                "run_count": j.state.run_count,
            }
            for j in jobs
        ],
        "total": len(jobs),
    }


@router.get("/jobs/upcoming")
async def get_upcoming_jobs(
    within_minutes: int = 30,
    limit: int = 20,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    jobs = await svc.get_upcoming_jobs(within_minutes=within_minutes, limit=limit)
    return {
        "jobs": [
            {
                "id": j.id,
                "name": j.name,
                "next_run_at_ms": j.state.next_run_at_ms,
                "schedule": schedule_to_human(j.schedule),
            }
            for j in jobs
        ],
        "within_minutes": within_minutes,
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    job = await svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job": {
            "id": job.id,
            "name": job.name,
            "description": job.description,
            "schedule": schedule_to_human(job.schedule),
            "status": job.status.value,
            "enabled": job.enabled,
            "state": job.state.to_dict(),
            "on_complete": [c.to_dict() for c in job.on_complete],
            "created_at_ms": job.created_at_ms,
            "updated_at_ms": job.updated_at_ms,
        }
    }


@router.post("/jobs")
async def create_job(
    request: JobCreateRequest,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    import json
    from ..scheduler import create_scheduled_job_tool

    result_str = await create_scheduled_job_tool(
        name=request.name,
        prompt=request.prompt,
        user_id=request.user_id,
        cron_expression=request.cron_expression,
        interval_seconds=request.interval_seconds,
        run_at_iso=request.run_at_iso,
        timezone=request.timezone,
    )
    result = json.loads(result_str)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to create job"))
    return result


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    user_id: str,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    job = await svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Permission denied")
    result = await svc.remove(job_id)
    if not result.removed:
        raise HTTPException(status_code=400, detail=result.reason or "Delete failed")
    return {"status": "deleted", "job_id": job_id}


# ============== Job actions ==============

@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: str,
    mode: str = "force",
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    job = await svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = await svc.run(job_id, mode=mode)
    return {
        "job_id": result.job_id,
        "status": result.status.value,
        "started_at_ms": result.started_at_ms,
        "finished_at_ms": result.finished_at_ms,
        "result": str(result.result)[:500] if result.result else None,
        "error": result.error,
    }


@router.post("/jobs/{job_id}/pause")
async def pause_job(
    job_id: str,
    user_id: str,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    job = await svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Permission denied")
    updated = await svc.pause(job_id)
    if not updated:
        raise HTTPException(status_code=400, detail="Pause failed")
    return {"status": "paused", "job_id": job_id}


@router.post("/jobs/{job_id}/resume")
async def resume_job(
    job_id: str,
    user_id: str,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    job = await svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Permission denied")
    updated = await svc.resume(job_id)
    if not updated:
        raise HTTPException(status_code=400, detail="Resume failed")
    return {"status": "resumed", "job_id": job_id, "next_run_at_ms": updated.state.next_run_at_ms}


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    job = await svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = await svc.retry_job(job_id)
    return {
        "job_id": result.job_id,
        "status": result.status.value,
        "started_at_ms": result.started_at_ms,
        "finished_at_ms": result.finished_at_ms,
        "result": str(result.result)[:500] if result.result else None,
        "error": result.error,
    }


@router.post("/jobs/{job_id}/clone")
async def clone_job(
    job_id: str,
    request: CloneJobRequest,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    new_job = await svc.clone_job(job_id, new_name=request.new_name)
    if not new_job:
        raise HTTPException(status_code=404, detail="Source job not found")
    return {
        "success": True,
        "job": {
            "id": new_job.id,
            "name": new_job.name,
            "schedule": schedule_to_human(new_job.schedule),
            "status": new_job.status.value,
            "next_run_at_ms": new_job.state.next_run_at_ms,
        },
    }


# ============== Monitoring ==============

@router.get("/stats")
async def get_scheduler_stats(svc: SchedulerService = Depends(deps.get_scheduler)):
    stats = await svc.get_stats()
    return stats.to_dict()


@router.get("/jobs/{job_id}/stats")
async def get_job_stats(
    job_id: str,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    stats = await svc.get_job_stats(job_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Job not found")
    return stats.to_dict()


@router.get("/jobs/{job_id}/runs")
async def get_job_runs(
    job_id: str,
    limit: int = 20,
    offset: int = 0,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    job = await svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    runs, total = await svc.get_job_runs(job_id, limit=limit, offset=offset)
    return {
        "job_id": job_id,
        "runs": [r.to_dict() for r in runs],
        "total": total,
        "has_more": offset + len(runs) < total,
    }


@router.get("/runs/recent")
async def get_recent_runs(
    limit: int = 20,
    since_ms: Optional[int] = None,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    runs = await svc.get_recent_runs(limit=limit, since_ms=since_ms)
    return {"runs": [r.to_dict() for r in runs], "total": len(runs)}


@router.get("/runs/failed")
async def get_failed_runs(
    limit: int = 20,
    since_ms: Optional[int] = None,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    runs = await svc.get_failed_runs(limit=limit, since_ms=since_ms)
    return {"runs": [r.to_dict() for r in runs], "total": len(runs)}


# ============== Batch ==============

@router.post("/jobs/batch/pause")
async def batch_pause(
    request: BatchJobsRequest,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    result = await svc.batch_pause(request.job_ids)
    return {"success": result.success, "paused": result.processed, "failed": result.failed_ids, "errors": result.errors}


@router.post("/jobs/batch/resume")
async def batch_resume(
    request: BatchJobsRequest,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    result = await svc.batch_resume(request.job_ids)
    return {"success": result.success, "resumed": result.processed, "failed": result.failed_ids, "errors": result.errors}


@router.post("/jobs/batch/delete")
async def batch_delete(
    request: BatchJobsRequest,
    svc: SchedulerService = Depends(deps.get_scheduler),
):
    result = await svc.batch_delete(request.job_ids)
    return {"success": result.success, "deleted": result.processed, "failed": result.failed_ids, "errors": result.errors}
