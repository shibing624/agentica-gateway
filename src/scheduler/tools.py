"""Agent tools for creating and managing scheduled jobs.

These tools are designed to be used by an AI agent to handle natural language
requests for scheduled tasks.
"""
from typing import Any, Optional, List

from .models import JobCreate
from .types import (
    CronSchedule,
    EverySchedule,
    AtSchedule,
    AgentTurnPayload,
)
from .schedule import schedule_to_human
from .task_parser import TaskParser
from .service import SchedulerService


# Singleton instances (initialized by daemon)
_scheduler_service: SchedulerService | None = None
_task_parser: TaskParser | None = None


def init_scheduler_tools(
    scheduler_service: SchedulerService,
    task_parser: TaskParser | None = None,
) -> None:
    """Initialize the scheduler tools with service instances.

    Call this during daemon startup.
    """
    global _scheduler_service, _task_parser
    _scheduler_service = scheduler_service
    _task_parser = task_parser or TaskParser()


# ============== Tool Definitions ==============
# These follow a common tool schema pattern compatible with OpenAI/Anthropic

CREATE_SCHEDULED_JOB_TOOL = {
    "name": "create_scheduled_job",
    "description": """创建一个新的定时任务。

用户可以用自然语言描述任务，例如：
- "每天早上9点提醒我看新闻"
- "每周一下午3点发送周报"
- "明天上午10点提醒我开会"
- "每隔30分钟检查一下服务器状态"

工具会自动解析时间表达式并创建相应的定时任务。""",
    "parameters": {
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "用户的自然语言任务描述，包含时间和要执行的动作"
            },
            "user_id": {
                "type": "string",
                "description": "用户ID"
            },
            "notify_channel": {
                "type": "string",
                "description": "通知渠道：feishu, telegram, discord, slack, webhook, gradio",
                "enum": ["feishu", "telegram", "discord", "slack", "webhook", "gradio"],
                "default": "feishu"
            },
            "notify_chat_id": {
                "type": "string",
                "description": "通知的目标chat/channel ID"
            },
            "timezone": {
                "type": "string",
                "description": "用户时区，默认 Asia/Shanghai",
                "default": "Asia/Shanghai"
            }
        },
        "required": ["task_description", "user_id"]
    }
}

LIST_SCHEDULED_JOBS_TOOL = {
    "name": "list_scheduled_jobs",
    "description": "列出用户的所有定时任务，可以按状态筛选。",
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "用户ID"
            },
            "include_disabled": {
                "type": "boolean",
                "description": "是否包含已暂停的任务",
                "default": False
            },
            "limit": {
                "type": "integer",
                "description": "返回数量限制",
                "default": 20
            }
        },
        "required": ["user_id"]
    }
}

DELETE_SCHEDULED_JOB_TOOL = {
    "name": "delete_scheduled_job",
    "description": "删除一个定时任务。",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "要删除的任务ID"
            },
            "user_id": {
                "type": "string",
                "description": "用户ID（用于权限验证）"
            }
        },
        "required": ["job_id", "user_id"]
    }
}

PAUSE_JOB_TOOL = {
    "name": "pause_scheduled_job",
    "description": "暂停一个定时任务。暂停后任务不会执行，但可以恢复。",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "要暂停的任务ID"
            },
            "user_id": {
                "type": "string",
                "description": "用户ID（用于权限验证）"
            }
        },
        "required": ["job_id", "user_id"]
    }
}

RESUME_JOB_TOOL = {
    "name": "resume_scheduled_job",
    "description": "恢复一个已暂停的定时任务。",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "要恢复的任务ID"
            },
            "user_id": {
                "type": "string",
                "description": "用户ID（用于权限验证）"
            }
        },
        "required": ["job_id", "user_id"]
    }
}

CREATE_TASK_CHAIN_TOOL = {
    "name": "create_task_chain",
    "description": "创建任务链，让一个任务完成后自动触发另一个任务。",
    "parameters": {
        "type": "object",
        "properties": {
            "source_job_id": {
                "type": "string",
                "description": "源任务ID（完成后触发下一个任务）"
            },
            "target_job_id": {
                "type": "string",
                "description": "目标任务ID（被触发的任务）"
            },
            "on_status": {
                "type": "array",
                "items": {"type": "string", "enum": ["ok", "failed"]},
                "description": "触发条件：任务在什么状态下触发（默认成功时触发）",
                "default": ["ok"]
            },
            "user_id": {
                "type": "string",
                "description": "用户ID（用于权限验证）"
            }
        },
        "required": ["source_job_id", "target_job_id", "user_id"]
    }
}


# ============== Tool Implementations ==============

async def create_scheduled_job_tool(
    task_description: str,
    user_id: str,
    notify_channel: str = "telegram",
    notify_chat_id: str = "",
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Tool implementation: Create a scheduled job from natural language.

    Args:
        task_description: Natural language task description
        user_id: User ID
        notify_channel: Notification channel
        notify_chat_id: Target chat/channel ID
        timezone: User timezone

    Returns:
        Dict with job creation result
    """
    if not _scheduler_service or not _task_parser:
        return {
            "success": False,
            "error": "Scheduler service not initialized"
        }

    try:
        # Parse natural language
        parsed = await _task_parser.parse(
            text=task_description,
            user_timezone=timezone,
        )

        # Check parsing confidence
        if parsed.confidence < 0.5:
            return {
                "success": False,
                "error": "无法理解任务时间，请更明确地描述。",
                "confidence": parsed.confidence,
                "parsed_action": parsed.parsed_action,
            }

        # Determine schedule type
        if parsed.cron_expression:
            schedule = CronSchedule(
                expression=parsed.cron_expression,
                timezone=timezone,
            )
        elif parsed.interval_seconds:
            schedule = EverySchedule.from_seconds(parsed.interval_seconds)
        elif parsed.run_at:
            schedule = AtSchedule.from_datetime(parsed.run_at)
        else:
            return {
                "success": False,
                "error": "无法确定执行时间",
            }

        # Create payload
        payload = AgentTurnPayload(
            prompt=parsed.parsed_action,
            notify_channel=notify_channel,
            notify_chat_id=notify_chat_id,
        )

        # Create job
        job_create = JobCreate(
            user_id=user_id,
            name=parsed.parsed_action[:50],
            description=task_description,
            schedule=schedule,
            payload=payload,
        )

        # Add to scheduler
        job = await _scheduler_service.add(job_create)

        return {
            "success": True,
            "job": {
                "id": job.id,
                "name": job.name,
                "schedule": schedule_to_human(job.schedule),
                "next_run_at_ms": job.state.next_run_at_ms,
                "status": job.status.value,
            },
            "message": f"已创建定时任务：{job.name}，{schedule_to_human(job.schedule)}"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


async def list_scheduled_jobs_tool(
    user_id: str,
    include_disabled: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Tool implementation: List user's scheduled jobs.

    Args:
        user_id: User ID
        include_disabled: Include disabled/paused jobs
        limit: Max number of jobs to return

    Returns:
        Dict with jobs list
    """
    if not _scheduler_service:
        return {
            "success": False,
            "error": "Scheduler service not initialized"
        }

    try:
        jobs = await _scheduler_service.list(
            user_id=user_id,
            include_disabled=include_disabled,
            limit=limit,
        )

        job_list = []
        for job in jobs:
            job_list.append({
                "id": job.id,
                "name": job.name,
                "schedule": schedule_to_human(job.schedule),
                "status": job.status.value,
                "next_run_at_ms": job.state.next_run_at_ms,
                "run_count": job.state.run_count,
                "last_run_at_ms": job.state.last_run_at_ms,
            })

        return {
            "success": True,
            "jobs": job_list,
            "total": len(job_list),
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


async def delete_scheduled_job_tool(
    job_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Tool implementation: Delete a scheduled job.

    Args:
        job_id: Job ID to delete
        user_id: User ID for permission check

    Returns:
        Dict with deletion result
    """
    if not _scheduler_service:
        return {
            "success": False,
            "error": "Scheduler service not initialized"
        }

    try:
        # Get job first to check ownership
        job = await _scheduler_service.get(job_id)

        if not job:
            return {
                "success": False,
                "error": f"找不到任务 {job_id}"
            }

        if job.user_id != user_id:
            return {
                "success": False,
                "error": "无权删除此任务"
            }

        # Delete the job
        result = await _scheduler_service.remove(job_id)

        if result.removed:
            return {
                "success": True,
                "message": f"已删除任务：{job.name}"
            }
        else:
            return {
                "success": False,
                "error": result.reason or "删除失败"
            }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


async def pause_scheduled_job_tool(
    job_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Tool implementation: Pause a scheduled job.

    Args:
        job_id: Job ID to pause
        user_id: User ID for permission check

    Returns:
        Dict with pause result
    """
    if not _scheduler_service:
        return {
            "success": False,
            "error": "Scheduler service not initialized"
        }

    try:
        # Get job first to check ownership
        job = await _scheduler_service.get(job_id)

        if not job:
            return {
                "success": False,
                "error": f"找不到任务 {job_id}"
            }

        if job.user_id != user_id:
            return {
                "success": False,
                "error": "无权暂停此任务"
            }

        # Pause the job
        updated_job = await _scheduler_service.pause(job_id)

        if updated_job:
            return {
                "success": True,
                "message": f"已暂停任务：{job.name}"
            }
        else:
            return {
                "success": False,
                "error": "暂停失败"
            }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


async def resume_scheduled_job_tool(
    job_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Tool implementation: Resume a paused job.

    Args:
        job_id: Job ID to resume
        user_id: User ID for permission check

    Returns:
        Dict with resume result
    """
    if not _scheduler_service:
        return {
            "success": False,
            "error": "Scheduler service not initialized"
        }

    try:
        # Get job first to check ownership
        job = await _scheduler_service.get(job_id)

        if not job:
            return {
                "success": False,
                "error": f"找不到任务 {job_id}"
            }

        if job.user_id != user_id:
            return {
                "success": False,
                "error": "无权恢复此任务"
            }

        # Resume the job
        updated_job = await _scheduler_service.resume(job_id)

        if updated_job:
            return {
                "success": True,
                "message": f"已恢复任务：{job.name}",
                "next_run_at_ms": updated_job.state.next_run_at_ms,
            }
        else:
            return {
                "success": False,
                "error": "恢复失败"
            }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


async def create_task_chain_tool(
    source_job_id: str,
    target_job_id: str,
    user_id: str,
    on_status: Optional[List[str]] = None,
) -> dict[str, Any]:
    """Tool implementation: Create a task chain.

    Args:
        source_job_id: Source job ID
        target_job_id: Target job ID to trigger
        user_id: User ID for permission check
        on_status: Trigger conditions

    Returns:
        Dict with chain creation result
    """
    if not _scheduler_service:
        return {
            "success": False,
            "error": "Scheduler service not initialized"
        }

    try:
        # Verify source job ownership
        source_job = await _scheduler_service.get(source_job_id)
        if not source_job:
            return {
                "success": False,
                "error": f"找不到源任务 {source_job_id}"
            }

        if source_job.user_id != user_id:
            return {
                "success": False,
                "error": "无权修改此任务"
            }

        # Verify target job exists and belongs to same user
        target_job = await _scheduler_service.get(target_job_id)
        if not target_job:
            return {
                "success": False,
                "error": f"找不到目标任务 {target_job_id}"
            }

        if target_job.user_id != user_id:
            return {
                "success": False,
                "error": "目标任务不属于当前用户"
            }

        # Create the chain
        updated_job = await _scheduler_service.chain(
            source_job_id,
            target_job_id,
            on_status or ["ok"],
        )

        if updated_job:
            return {
                "success": True,
                "message": f"已创建任务链：{source_job.name} -> {target_job.name}",
            }
        else:
            return {
                "success": False,
                "error": "创建任务链失败"
            }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# Export all tools as a list for easy registration
ALL_SCHEDULER_TOOLS = [
    CREATE_SCHEDULED_JOB_TOOL,
    LIST_SCHEDULED_JOBS_TOOL,
    DELETE_SCHEDULED_JOB_TOOL,
    PAUSE_JOB_TOOL,
    RESUME_JOB_TOOL,
    CREATE_TASK_CHAIN_TOOL,
]

# Map tool names to implementations
TOOL_IMPLEMENTATIONS = {
    "create_scheduled_job": create_scheduled_job_tool,
    "list_scheduled_jobs": list_scheduled_jobs_tool,
    "delete_scheduled_job": delete_scheduled_job_tool,
    "pause_scheduled_job": pause_scheduled_job_tool,
    "resume_scheduled_job": resume_scheduled_job_tool,
    "create_task_chain": create_task_chain_tool,
}
