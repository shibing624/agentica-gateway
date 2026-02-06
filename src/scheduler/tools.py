"""Agent tools for creating and managing scheduled jobs.

These tools are designed to be used by an AI agent to handle natural language
requests for scheduled tasks. The LLM should parse natural language and fill
in the structured parameters directly.

Note: agentica tool functions MUST return str, not dict.
"""
import json
from datetime import datetime
from typing import Optional, List

from .models import JobCreate
from .types import (
    CronSchedule,
    EverySchedule,
    AtSchedule,
    AgentTurnPayload,
)
from .schedule import schedule_to_human
from .service import SchedulerService


def _to_json(data: dict) -> str:
    """Convert dict to JSON string for tool return value."""
    return json.dumps(data, ensure_ascii=False, indent=2)


# Singleton instances (initialized by gateway)
_scheduler_service: SchedulerService | None = None


def init_scheduler_tools(
    scheduler_service: SchedulerService,
) -> None:
    """Initialize the scheduler tools with service instances.

    Call this during gateway startup.
    """
    global _scheduler_service
    _scheduler_service = scheduler_service


# ============== Tool Definitions ==============
# These follow a common tool schema pattern compatible with OpenAI/Anthropic

CREATE_SCHEDULED_JOB_TOOL = {
    "name": "create_scheduled_job",
    "description": """创建定时任务。根据用户的自然语言描述，解析并填入对应参数。

调度类型（三选一）：
1. cron_expression: Cron表达式，支持5段或6段格式
   - 5段(分钟精度): "分 时 日 月 周" 如 "30 7 * * *"（每天7:30）
   - 6段(秒级精度): "秒 分 时 日 月 周" 如 "0 30 7 * * *"（每天7:30:00）
   - 常用示例：
     * "30 7 * * *" = 每天7:30
     * "0 30 7 * * *" = 每天7:30:00（秒级）
     * "0 9 * * 1-5" = 工作日9:00
     * "*/30 * * * *" = 每30分钟
     * "*/10 * * * * *" = 每10秒

2. interval_seconds: 间隔执行（秒数）
   - 30 = 每30秒
   - 300 = 每5分钟
   - 3600 = 每小时

3. run_at_iso: 一次性执行时间（ISO格式）
   - "2024-01-15T09:30:00" = 某天9:30执行一次
   - "2024-01-15T09:30:45" = 某天9:30:45执行一次（秒级）""",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "任务名称（简短描述）"
            },
            "prompt": {
                "type": "string",
                "description": "任务执行时要做的事情（给Agent的指令）"
            },
            "user_id": {
                "type": "string",
                "description": "用户ID"
            },
            "cron_expression": {
                "type": "string",
                "description": "Cron表达式。5段(分钟)或6段(秒级)格式。如 '30 7 * * *'(每天7:30) 或 '0 30 7 * * *'(每天7:30:00秒级)"
            },
            "interval_seconds": {
                "type": "integer",
                "description": "间隔秒数。如 30(每30秒), 300(每5分钟), 3600(每小时)"
            },
            "run_at_iso": {
                "type": "string",
                "description": "一次性执行时间，ISO格式。如 '2024-01-15T09:30:45'"
            },
            "timezone": {
                "type": "string",
                "description": "时区，默认 Asia/Shanghai",
                "default": "Asia/Shanghai"
            },
            "notify_channel": {
                "type": "string",
                "description": "通知渠道",
                "enum": ["feishu", "telegram", "discord", "slack", "webhook", "gradio"],
                "default": "feishu"
            },
            "notify_chat_id": {
                "type": "string",
                "description": "通知的目标chat/channel ID"
            }
        },
        "required": ["name", "prompt", "user_id"]
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
# Note: All tool functions MUST return str (not dict) for agentica compatibility.

async def create_scheduled_job_tool(
    name: str,
    prompt: str,
    user_id: str,
    cron_expression: Optional[str] = None,
    interval_seconds: Optional[int] = None,
    run_at_iso: Optional[str] = None,
    timezone: Optional[str] = None,
    notify_channel: Optional[str] = None,
    notify_chat_id: Optional[str] = None,
) -> str:
    """创建定时任务。LLM 根据用户自然语言解析后直接填参数。

    调度类型三选一：
    - cron_expression: Cron表达式，5段或6段格式
    - interval_seconds: 间隔秒数
    - run_at_iso: 一次性执行时间（ISO格式）

    Args:
        name: 任务名称
        prompt: Agent执行的指令
        user_id: 用户ID
        cron_expression: Cron表达式，如 "30 7 * * *"(每天7:30) 或 "0 30 7 * * *"(每天7:30:00秒级)
        interval_seconds: 间隔秒数，如 30(每30秒), 3600(每小时)
        run_at_iso: 一次性执行时间，ISO格式如 "2024-01-15T09:30:45"
        timezone: 时区，默认 Asia/Shanghai
        notify_channel: 通知渠道
        notify_chat_id: 通知目标ID

    Returns:
        JSON string with job creation result
    """
    timezone = timezone or "Asia/Shanghai"
    notify_channel = notify_channel or "feishu"
    notify_chat_id = notify_chat_id or ""
    
    if not _scheduler_service:
        return _to_json({
            "success": False,
            "error": "Scheduler service not initialized"
        })

    try:
        # Determine schedule type (priority: cron > interval > run_at)
        schedule = None
        
        if cron_expression:
            schedule = CronSchedule(
                expression=cron_expression,
                timezone=timezone,
            )
        elif interval_seconds and interval_seconds > 0:
            schedule = EverySchedule(interval_ms=interval_seconds * 1000)
        elif run_at_iso:
            try:
                # Parse ISO format datetime
                run_at = datetime.fromisoformat(run_at_iso.replace("Z", "+00:00"))
                schedule = AtSchedule.from_datetime(run_at)
            except ValueError as e:
                return _to_json({
                    "success": False,
                    "error": f"无效的时间格式: {run_at_iso}，请使用ISO格式如 2024-01-15T09:30:45"
                })
        
        if not schedule:
            return _to_json({
                "success": False,
                "error": "请指定调度方式：cron_expression、interval_seconds 或 run_at_iso"
            })

        # Create payload
        payload = AgentTurnPayload(
            prompt=prompt,
            notify_channel=notify_channel,
            notify_chat_id=notify_chat_id,
        )

        # Create job
        job_create = JobCreate(
            user_id=user_id,
            name=name[:50],
            description=prompt,
            schedule=schedule,
            payload=payload,
        )

        # Add to scheduler
        job = await _scheduler_service.add(job_create)

        return _to_json({
            "success": True,
            "job": {
                "id": job.id,
                "name": job.name,
                "schedule": schedule_to_human(job.schedule),
                "next_run_at_ms": job.state.next_run_at_ms,
                "status": job.status.value,
            },
            "message": f"已创建定时任务：{job.name}，{schedule_to_human(job.schedule)}"
        })

    except Exception as e:
        return _to_json({
            "success": False,
            "error": str(e)
        })


async def list_scheduled_jobs_tool(
    user_id: str,
    include_disabled: Optional[bool] = None,
    limit: Optional[int] = None,
) -> str:
    """列出用户的所有定时任务。

    Args:
        user_id: 用户ID
        include_disabled: 是否包含已暂停的任务，默认 False
        limit: 返回数量限制，默认 20

    Returns:
        JSON string with jobs list
    """
    # Handle None values with defaults
    include_disabled = include_disabled if include_disabled is not None else False
    limit = limit if limit is not None else 20
    
    if not _scheduler_service:
        return _to_json({
            "success": False,
            "error": "Scheduler service not initialized"
        })

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

        return _to_json({
            "success": True,
            "jobs": job_list,
            "total": len(job_list),
        })

    except Exception as e:
        return _to_json({
            "success": False,
            "error": str(e)
        })


async def delete_scheduled_job_tool(
    job_id: str,
    user_id: str,
) -> str:
    """删除一个定时任务。

    Args:
        job_id: 要删除的任务ID
        user_id: 用户ID（用于权限验证）

    Returns:
        JSON string with deletion result
    """
    if not _scheduler_service:
        return _to_json({
            "success": False,
            "error": "Scheduler service not initialized"
        })

    try:
        # Get job first to check ownership
        job = await _scheduler_service.get(job_id)

        if not job:
            return _to_json({
                "success": False,
                "error": f"找不到任务 {job_id}"
            })

        if job.user_id != user_id:
            return _to_json({
                "success": False,
                "error": "无权删除此任务"
            })

        # Delete the job
        result = await _scheduler_service.remove(job_id)

        if result.removed:
            return _to_json({
                "success": True,
                "message": f"已删除任务：{job.name}"
            })
        else:
            return _to_json({
                "success": False,
                "error": result.reason or "删除失败"
            })

    except Exception as e:
        return _to_json({
            "success": False,
            "error": str(e)
        })


async def pause_scheduled_job_tool(
    job_id: str,
    user_id: str,
) -> str:
    """暂停一个定时任务。暂停后任务不会执行，但可以恢复。

    Args:
        job_id: 要暂停的任务ID
        user_id: 用户ID（用于权限验证）

    Returns:
        JSON string with pause result
    """
    if not _scheduler_service:
        return _to_json({
            "success": False,
            "error": "Scheduler service not initialized"
        })

    try:
        # Get job first to check ownership
        job = await _scheduler_service.get(job_id)

        if not job:
            return _to_json({
                "success": False,
                "error": f"找不到任务 {job_id}"
            })

        if job.user_id != user_id:
            return _to_json({
                "success": False,
                "error": "无权暂停此任务"
            })

        # Pause the job
        updated_job = await _scheduler_service.pause(job_id)

        if updated_job:
            return _to_json({
                "success": True,
                "message": f"已暂停任务：{job.name}"
            })
        else:
            return _to_json({
                "success": False,
                "error": "暂停失败"
            })

    except Exception as e:
        return _to_json({
            "success": False,
            "error": str(e)
        })


async def resume_scheduled_job_tool(
    job_id: str,
    user_id: str,
) -> str:
    """恢复一个已暂停的定时任务。

    Args:
        job_id: 要恢复的任务ID
        user_id: 用户ID（用于权限验证）

    Returns:
        JSON string with resume result
    """
    if not _scheduler_service:
        return _to_json({
            "success": False,
            "error": "Scheduler service not initialized"
        })

    try:
        # Get job first to check ownership
        job = await _scheduler_service.get(job_id)

        if not job:
            return _to_json({
                "success": False,
                "error": f"找不到任务 {job_id}"
            })

        if job.user_id != user_id:
            return _to_json({
                "success": False,
                "error": "无权恢复此任务"
            })

        # Resume the job
        updated_job = await _scheduler_service.resume(job_id)

        if updated_job:
            return _to_json({
                "success": True,
                "message": f"已恢复任务：{job.name}",
                "next_run_at_ms": updated_job.state.next_run_at_ms,
            })
        else:
            return _to_json({
                "success": False,
                "error": "恢复失败"
            })

    except Exception as e:
        return _to_json({
            "success": False,
            "error": str(e)
        })


async def create_task_chain_tool(
    source_job_id: str,
    target_job_id: str,
    user_id: str,
    on_status: Optional[List[str]] = None,
) -> str:
    """创建任务链，让一个任务完成后自动触发另一个任务。

    Args:
        source_job_id: 源任务ID（完成后触发下一个任务）
        target_job_id: 目标任务ID（被触发的任务）
        user_id: 用户ID（用于权限验证）
        on_status: 触发条件，任务在什么状态下触发（默认成功时触发）

    Returns:
        JSON string with chain creation result
    """
    if not _scheduler_service:
        return _to_json({
            "success": False,
            "error": "Scheduler service not initialized"
        })

    try:
        # Verify source job ownership
        source_job = await _scheduler_service.get(source_job_id)
        if not source_job:
            return _to_json({
                "success": False,
                "error": f"找不到源任务 {source_job_id}"
            })

        if source_job.user_id != user_id:
            return _to_json({
                "success": False,
                "error": "无权修改此任务"
            })

        # Verify target job exists and belongs to same user
        target_job = await _scheduler_service.get(target_job_id)
        if not target_job:
            return _to_json({
                "success": False,
                "error": f"找不到目标任务 {target_job_id}"
            })

        if target_job.user_id != user_id:
            return _to_json({
                "success": False,
                "error": "目标任务不属于当前用户"
            })

        # Create the chain
        updated_job = await _scheduler_service.chain(
            source_job_id,
            target_job_id,
            on_status or ["ok"],
        )

        if updated_job:
            return _to_json({
                "success": True,
                "message": f"已创建任务链：{source_job.name} -> {target_job.name}",
            })
        else:
            return _to_json({
                "success": False,
                "error": "创建任务链失败"
            })

    except Exception as e:
        return _to_json({
            "success": False,
            "error": str(e)
        })


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
