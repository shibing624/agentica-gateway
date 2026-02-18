"""FastAPI 主入口"""
import asyncio
import json as json_mod
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict, Any, List

import shutil

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from loguru import logger
from uuid import uuid4

from .config import settings
from .services.agent_service import AgentService
from .services.channel_manager import ChannelManager
from .services.router import MessageRouter

try:
    from agentica.run_response import AgentCancelledError
except ImportError:
    AgentCancelledError = None
from .scheduler import (
    SchedulerService,
    JobExecutor,
    init_scheduler_tools,
)

# 全局服务实例
agent_service: Optional[AgentService] = None
channel_manager: Optional[ChannelManager] = None
message_router: Optional[MessageRouter] = None
scheduler: Optional[SchedulerService] = None


# ============== Agent Runner for Scheduler ==============

class GatewayAgentRunner:
    """Agent runner that uses the gateway's AgentService for scheduled jobs."""

    def __init__(self, agent_svc: AgentService):
        self.agent_service = agent_svc

    async def run(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Run agent with a prompt and return the result.

        Args:
            prompt: The prompt to execute
            context: Context including job_id, user_id, etc.

        Returns:
            Agent response content
        """
        context = context or {}
        job_id = context.get('job_id', str(uuid4()))
        user_id = context.get('user_id', settings.default_user_id)
        session_id = f"scheduled_{job_id}"

        result = await self.agent_service.chat(
            message=prompt,
            session_id=session_id,
            user_id=user_id,
        )
        return result.content


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """应用生命周期管理"""
    global agent_service, channel_manager, message_router, scheduler

    logger.info("=" * 50)
    logger.info("  Agentica Gateway")
    logger.info(f"  Workspace: {settings.workspace_path}")
    logger.info(f"  Data dir: {settings.data_dir}")
    logger.info(f"  Model: {settings.model_provider}/{settings.model_name}")
    logger.info("=" * 50)

    # 初始化服务
    agent_service = AgentService(
        workspace_path=str(settings.workspace_path),
        model_name=settings.model_name,
        model_provider=settings.model_provider,
    )

    channel_manager = ChannelManager()
    message_router = MessageRouter(default_agent="main")

    # 初始化调度器（YAML 配置 + SQLite 状态）
    agent_runner = GatewayAgentRunner(agent_service)
    executor = JobExecutor(agent_runner=agent_runner)

    scheduler = SchedulerService(
        data_dir=str(settings.data_dir),
        executor=executor,
    )

    # 初始化调度器工具
    init_scheduler_tools(scheduler)

    # 注册渠道
    await setup_channels()

    # 启动调度器
    try:
        await scheduler.start()
        logger.info("Scheduler service started")
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")

    logger.info("Gateway started")
    logger.info(f"FastAPI docs: http://{settings.host}:{settings.port}/docs")
    logger.info(f"Web UI: http://{settings.host}:{settings.port}/chat")
    logger.info(f"WebSocket: ws://{settings.host}:{settings.port}/ws")

    yield

    # 清理
    logger.info("Shutting down...")
    await channel_manager.disconnect_all()
    await scheduler.stop()
    logger.info("Goodbye!")


app = FastAPI(
    title="Agentica Gateway",
    description="Python OpenClaw - AI Agent Gateway",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件目录（CSS/JS）
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ============== Pydantic Models ==============

class ChatRequest(BaseModel):
    """聊天请求"""
    message: str
    session_id: str = "default"
    user_id: str = "default"
    agent_id: str = "main"


class ChatResponse(BaseModel):
    """聊天响应"""
    content: str
    session_id: str
    user_id: str = "default"
    tool_calls: int = 0


class MemoryRequest(BaseModel):
    """记忆保存请求"""
    content: str
    user_id: str = "default"
    long_term: bool = False


class SendRequest(BaseModel):
    """发送消息请求"""
    channel: str
    channel_id: str
    message: str


class JobCreateRequest(BaseModel):
    """创建任务请求"""
    name: str
    prompt: str
    user_id: str
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    run_at_iso: Optional[str] = None
    timezone: str = "Asia/Shanghai"


class JobResponse(BaseModel):
    """任务响应"""
    id: str
    name: str
    schedule: str
    status: str
    next_run_at_ms: Optional[int] = None


class BatchJobsRequest(BaseModel):
    """批量任务操作请求"""
    job_ids: List[str]


class CloneJobRequest(BaseModel):
    """克隆任务请求"""
    new_name: Optional[str] = None


# ============== REST API ==============

@app.get("/")
async def root():
    """根路径"""
    return {
        "name": "Agentica Gateway",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/chat", response_class=HTMLResponse)
async def web_chat():
    """Web Chat 页面"""
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/health")
@app.get("/api/health")
async def health():
    """健康检查"""
    scheduler_status = {}
    if scheduler:
        status = await scheduler.status()
        scheduler_status = status.to_dict()

    return {
        "status": "ok",
        "version": "0.1.1",
        "channels": channel_manager.get_status() if channel_manager else {},
        "scheduler": scheduler_status,
    }


@app.get("/api/status")
async def status():
    """系统状态"""
    scheduler_status = {}
    if scheduler:
        status = await scheduler.status()
        scheduler_status = status.to_dict()

    return {
        "workspace": str(settings.workspace_path),
        "base_dir": str(settings.base_dir),
        "model": f"{agent_service.model_provider}/{agent_service.model_name}" if agent_service else f"{settings.model_provider}/{settings.model_name}",
        "model_provider": agent_service.model_provider if agent_service else settings.model_provider,
        "model_name": agent_service.model_name if agent_service else settings.model_name,
        "version": "0.1.1",
        "channels": channel_manager.get_status() if channel_manager else {},
        "scheduler": scheduler_status,
    }


SUPPORTED_MODELS = {
    "zhipuai": ["glm-4.7-flash", "glm-4-plus", "glm-4-long", "glm-4-flashx", "glm-4-flash", "glm-4-air", "glm-4-airx", "glm-4"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1", "o1-mini", "o3-mini"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "yi": ["yi-lightning", "yi-large", "yi-medium", "yi-spark"],
    "doubao": ["doubao-1.5-pro-32k", "doubao-pro-32k", "doubao-lite-32k"],
    "azure": ["gpt-4o", "gpt-4-turbo", "gpt-35-turbo"],
}


@app.get("/api/models")
async def list_models():
    """列出支持的模型"""
    current_provider = agent_service.model_provider if agent_service else settings.model_provider
    current_name = agent_service.model_name if agent_service else settings.model_name
    return {
        "current_provider": current_provider,
        "current_name": current_name,
        "current": f"{current_provider}/{current_name}",
        "providers": SUPPORTED_MODELS,
    }


class ModelSwitchRequest(BaseModel):
    """模型切换请求"""
    model_provider: str
    model_name: str


@app.post("/api/model")
async def switch_model(request: ModelSwitchRequest):
    """切换模型"""
    if not agent_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    agent_service.reload_model(request.model_provider, request.model_name)
    settings.model_provider = request.model_provider
    settings.model_name = request.model_name

    return {
        "status": "ok",
        "model": f"{request.model_provider}/{request.model_name}",
    }


class BaseDirRequest(BaseModel):
    """Working directory 修改请求"""
    base_dir: str


@app.post("/api/config/base_dir")
async def set_base_dir(request: BaseDirRequest):
    """修改 working directory"""
    p = Path(request.base_dir).expanduser()
    created = False
    if not p.exists():
        # Parent exists → auto-create the last-level dir; otherwise reject
        if p.parent.exists():
            p.mkdir(parents=False, exist_ok=True)
            created = True
        else:
            raise HTTPException(status_code=400, detail="文件夹路径不存在，需要写一个存在的路径")
    elif not p.is_dir():
        raise HTTPException(status_code=400, detail="该路径不是文件夹")
    settings.base_dir = p
    if agent_service:
        agent_service.update_work_dir(str(p))
    _add_dir_history(str(p))
    return {"status": "ok", "base_dir": str(p), "created": created}


# ---- Dir history management ----
_DIR_HISTORY_MAX = 20

def _dir_history_file() -> Path:
    return settings.data_dir / "dir_history.json"

def _load_dir_history() -> list[str]:
    f = _dir_history_file()
    if f.exists():
        try:
            return json_mod.loads(f.read_text())
        except Exception:
            pass
    return []

def _save_dir_history(history: list[str]):
    f = _dir_history_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json_mod.dumps(history, ensure_ascii=False))

def _add_dir_history(path: str):
    history = _load_dir_history()
    if path in history:
        history.remove(path)
    history.insert(0, path)
    history = history[:_DIR_HISTORY_MAX]
    _save_dir_history(history)


@app.get("/api/config/dir_history")
async def get_dir_history():
    """获取历史路径列表"""
    history = _load_dir_history()
    # Ensure current base_dir is in history
    current = str(settings.base_dir)
    if current not in history:
        history.insert(0, current)
        _save_dir_history(history)
    return {"history": history}


@app.delete("/api/config/dir_history")
async def clear_dir_history():
    """清空历史路径"""
    _save_dir_history([str(settings.base_dir)])
    return {"status": "ok"}


class OpenRequest(BaseModel):
    """打开路径请求"""
    path: str
    app: str = "finder"  # "finder" or "terminal"


@app.post("/api/open")
async def open_path(request: OpenRequest):
    """在 Finder 或 Terminal 中打开指定路径"""
    import subprocess
    import sys

    p = Path(request.path).expanduser()
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    try:
        if sys.platform == "darwin":
            if request.app == "terminal":
                subprocess.Popen(["open", "-a", "Terminal", str(p)])
            else:
                subprocess.Popen(["open", str(p)])
        elif sys.platform == "linux":
            if request.app == "terminal":
                for term in ["gnome-terminal", "xterm", "konsole"]:
                    if shutil.which(term):
                        subprocess.Popen([term, f"--working-directory={str(p)}"])
                        break
            else:
                subprocess.Popen(["xdg-open", str(p)])
        else:
            subprocess.Popen(["explorer", str(p)])

        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """发送消息到 Agent"""
    if not agent_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    result = await agent_service.chat(
        message=request.message,
        session_id=request.session_id,
        user_id=request.user_id,
    )

    return ChatResponse(
        content=result.content,
        session_id=result.session_id,
        user_id=result.user_id,
        tool_calls=result.tool_calls,
    )


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    """SSE 流式聊天"""
    if not agent_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    async def event_generator():
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def on_content(delta: str):
            await queue.put({"event": "content", "data": delta})

        async def on_tool_call(name: str, args: dict):
            await queue.put({"event": "tool_call", "data": {"name": name, "args": args}})

        async def on_tool_result(name: str, result: str):
            await queue.put({"event": "tool_result", "data": {"name": name, "result": result}})

        async def on_thinking(delta: str):
            await queue.put({"event": "thinking", "data": delta})

        async def run_agent():
            try:
                result = await agent_service.chat_stream(
                    message=request.message,
                    session_id=request.session_id,
                    user_id=request.user_id,
                    on_content=on_content,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    on_thinking=on_thinking,
                )
                # 发送完成事件（包含 token 使用量等元信息）
                # metrics 来自 agentica，格式为 {key: [values...]}, 需要 sum
                raw_metrics = result.metrics or {}
                def _sum_metric(key):
                    v = raw_metrics.get(key, 0)
                    if isinstance(v, list):
                        return sum(x for x in v if isinstance(x, (int, float)))
                    return v if isinstance(v, (int, float)) else 0

                await queue.put({"event": "done", "data": {
                    "session_id": result.session_id,
                    "tool_calls": result.tool_calls,
                    "tools_used": result.tools_used,
                    "input_tokens": _sum_metric("input_tokens"),
                    "output_tokens": _sum_metric("output_tokens"),
                    "total_tokens": _sum_metric("total_tokens"),
                }})
            except asyncio.CancelledError:
                # 用户中止 — 不发 error 事件，直接结束
                pass
            except Exception as e:
                # AgentCancelledError 也视为正常中止
                if AgentCancelledError and isinstance(e, AgentCancelledError):
                    pass
                else:
                    await queue.put({"event": "error", "data": str(e)})
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.create_task(run_agent())

        try:
            while True:
                item = await queue.get()
                if item is None:
                    yield "data: [DONE]\n\n"
                    break
                yield f"data: {json_mod.dumps(item, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # 通知 agentica agent 立即停止当前运行
            if agent_service and agent_service._agent:
                agent_service._agent.cancel()
            task.cancel()
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/memory")
async def save_memory(request: MemoryRequest):
    """保存记忆"""
    if not agent_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    agent_service.save_memory(request.content, user_id=request.user_id, long_term=request.long_term)
    return {"status": "saved", "user_id": request.user_id}


@app.get("/api/sessions")
async def list_sessions():
    """列出会话"""
    if not agent_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    return {"sessions": agent_service.list_sessions()}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    if not agent_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    success = agent_service.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"status": "deleted"}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    target_dir: str = Form(""),
):
    """上传文件到 working directory"""
    base = Path(target_dir) if target_dir else settings.workspace_path
    base.mkdir(parents=True, exist_ok=True)
    dest = base / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"status": "ok", "path": str(dest), "filename": file.filename, "size": dest.stat().st_size}


@app.get("/api/channels")
async def list_channels():
    """列出渠道"""
    if not channel_manager:
        raise HTTPException(status_code=503, detail="Service not ready")

    return {
        "channels": channel_manager.list_channels(),
        "status": channel_manager.get_status(),
    }


@app.post("/api/send")
async def send_message(request: SendRequest):
    """发送消息到渠道"""
    if not channel_manager:
        raise HTTPException(status_code=503, detail="Service not ready")

    success = await channel_manager.send(
        request.channel,
        request.channel_id,
        request.message,
    )

    if not success:
        raise HTTPException(status_code=400, detail="Failed to send message")

    return {"status": "sent"}


# ============== Scheduler API ==============

@app.get("/api/scheduler/jobs")
async def list_jobs(
    user_id: Optional[str] = None,
    include_disabled: bool = False,
    limit: int = 100,
):
    """列出定时任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    jobs = await scheduler.list(
        user_id=user_id,
        include_disabled=include_disabled,
        limit=limit,
    )

    from .scheduler import schedule_to_human

    return {
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "description": job.description,
                "user_id": job.user_id,
                "schedule": schedule_to_human(job.schedule),
                "status": job.status.value,
                "enabled": job.enabled,
                "next_run_at_ms": job.state.next_run_at_ms,
                "last_run_at_ms": job.state.last_run_at_ms,
                "run_count": job.state.run_count,
            }
            for job in jobs
        ],
        "total": len(jobs),
    }


@app.get("/api/scheduler/jobs/{job_id}")
async def get_job(job_id: str):
    """获取任务详情"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    job = await scheduler.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from .scheduler import schedule_to_human

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


@app.post("/api/scheduler/jobs")
async def create_job(request: JobCreateRequest):
    """创建定时任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    from .scheduler import create_scheduled_job_tool
    import json

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
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Failed to create job")
        )

    return result


@app.delete("/api/scheduler/jobs/{job_id}")
async def delete_job(job_id: str, user_id: str):
    """删除定时任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    # 获取任务检查权限
    job = await scheduler.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Permission denied")

    result = await scheduler.remove(job_id)

    if not result.removed:
        raise HTTPException(status_code=400, detail=result.reason or "Delete failed")

    return {"status": "deleted", "job_id": job_id}


@app.post("/api/scheduler/jobs/{job_id}/run")
async def run_job(job_id: str, mode: str = "force"):
    """手动执行任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    job = await scheduler.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = await scheduler.run(job_id, mode=mode)

    return {
        "job_id": result.job_id,
        "status": result.status.value,
        "started_at_ms": result.started_at_ms,
        "finished_at_ms": result.finished_at_ms,
        "result": str(result.result)[:500] if result.result else None,
        "error": result.error,
    }


@app.post("/api/scheduler/jobs/{job_id}/pause")
async def pause_job(job_id: str, user_id: str):
    """暂停任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    job = await scheduler.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Permission denied")

    updated_job = await scheduler.pause(job_id)
    if not updated_job:
        raise HTTPException(status_code=400, detail="Pause failed")

    return {"status": "paused", "job_id": job_id}


@app.post("/api/scheduler/jobs/{job_id}/resume")
async def resume_job(job_id: str, user_id: str):
    """恢复任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    job = await scheduler.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Permission denied")

    updated_job = await scheduler.resume(job_id)
    if not updated_job:
        raise HTTPException(status_code=400, detail="Resume failed")

    return {
        "status": "resumed",
        "job_id": job_id,
        "next_run_at_ms": updated_job.state.next_run_at_ms,
    }


# ============== Scheduler Monitoring API ==============

@app.get("/api/scheduler/stats")
async def get_scheduler_stats():
    """获取调度器全局统计"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    stats = await scheduler.get_stats()
    return stats.to_dict()


@app.get("/api/scheduler/jobs/{job_id}/stats")
async def get_job_stats(job_id: str):
    """获取单个任务统计"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    stats = await scheduler.get_job_stats(job_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Job not found")

    return stats.to_dict()


@app.get("/api/scheduler/jobs/{job_id}/runs")
async def get_job_runs(
    job_id: str,
    limit: int = 20,
    offset: int = 0,
):
    """获取任务执行历史"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    # 先检查任务是否存在
    job = await scheduler.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    runs, total = await scheduler.get_job_runs(job_id, limit=limit, offset=offset)

    return {
        "job_id": job_id,
        "runs": [run.to_dict() for run in runs],
        "total": total,
        "has_more": offset + len(runs) < total,
    }


@app.get("/api/scheduler/runs/recent")
async def get_recent_runs(
    limit: int = 20,
    since_ms: Optional[int] = None,
):
    """获取最近执行记录"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    runs = await scheduler.get_recent_runs(limit=limit, since_ms=since_ms)

    return {
        "runs": [run.to_dict() for run in runs],
        "total": len(runs),
    }


@app.get("/api/scheduler/runs/failed")
async def get_failed_runs(
    limit: int = 20,
    since_ms: Optional[int] = None,
):
    """获取失败的执行记录"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    runs = await scheduler.get_failed_runs(limit=limit, since_ms=since_ms)

    return {
        "runs": [run.to_dict() for run in runs],
        "total": len(runs),
    }


@app.get("/api/scheduler/jobs/upcoming")
async def get_upcoming_jobs(
    within_minutes: int = 30,
    limit: int = 20,
):
    """获取即将执行的任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    from .scheduler import schedule_to_human

    jobs = await scheduler.get_upcoming_jobs(within_minutes=within_minutes, limit=limit)

    return {
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "next_run_at_ms": job.state.next_run_at_ms,
                "schedule": schedule_to_human(job.schedule),
            }
            for job in jobs
        ],
        "within_minutes": within_minutes,
    }


# ============== Scheduler Management API ==============

@app.post("/api/scheduler/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    """重试失败的任务（强制立即执行）"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    job = await scheduler.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = await scheduler.retry_job(job_id)

    return {
        "job_id": result.job_id,
        "status": result.status.value,
        "started_at_ms": result.started_at_ms,
        "finished_at_ms": result.finished_at_ms,
        "result": str(result.result)[:500] if result.result else None,
        "error": result.error,
    }


@app.post("/api/scheduler/jobs/{job_id}/clone")
async def clone_job(job_id: str, request: CloneJobRequest):
    """克隆任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    new_job = await scheduler.clone_job(job_id, new_name=request.new_name)
    if not new_job:
        raise HTTPException(status_code=404, detail="Source job not found")

    from .scheduler import schedule_to_human

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


@app.post("/api/scheduler/jobs/batch/pause")
async def batch_pause_jobs(request: BatchJobsRequest):
    """批量暂停任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    result = await scheduler.batch_pause(request.job_ids)

    return {
        "success": result.success,
        "paused": result.processed,
        "failed": result.failed_ids,
        "errors": result.errors,
    }


@app.post("/api/scheduler/jobs/batch/resume")
async def batch_resume_jobs(request: BatchJobsRequest):
    """批量恢复任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    result = await scheduler.batch_resume(request.job_ids)

    return {
        "success": result.success,
        "resumed": result.processed,
        "failed": result.failed_ids,
        "errors": result.errors,
    }


@app.post("/api/scheduler/jobs/batch/delete")
async def batch_delete_jobs(request: BatchJobsRequest):
    """批量删除任务"""
    if not scheduler:
        raise HTTPException(status_code=503, detail="Service not ready")

    result = await scheduler.batch_delete(request.job_ids)

    return {
        "success": result.success,
        "deleted": result.processed,
        "failed": result.failed_ids,
        "errors": result.errors,
    }


# ============== WebSocket Gateway ==============

class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        """接受连接"""
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.debug(f"WebSocket connected: {client_id}")

    def disconnect(self, client_id: str):
        """断开连接"""
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.debug(f"WebSocket disconnected: {client_id}")

    async def send_event(self, client_id: str, event: str, payload: dict):
        """发送事件到指定客户端"""
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_json({
                "type": "event",
                "event": event,
                "payload": payload,
            })

    async def broadcast(self, event: str, payload: dict):
        """广播事件到所有客户端"""
        for ws in self.active_connections.values():
            try:
                await ws.send_json({
                    "type": "event",
                    "event": event,
                    "payload": payload,
                })
            except Exception:
                pass

    def count(self) -> int:
        """连接数"""
        return len(self.active_connections)


ws_manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket Gateway 端点"""
    client_id = None

    try:
        # 等待连接请求
        data = await websocket.receive_json()

        if data.get("method") != "connect":
            await websocket.close(code=4000, reason="Must connect first")
            return

        # 验证 token
        params = data.get("params", {})
        auth_token = params.get("auth", {}).get("token")

        if settings.gateway_token and auth_token != settings.gateway_token:
            await websocket.close(code=4001, reason="Invalid token")
            return

        # 接受连接
        client_id = params.get("client", {}).get("id", "unknown")
        await ws_manager.connect(websocket, client_id)

        # 发送 hello-ok
        await websocket.send_json({
            "type": "res",
            "id": data.get("id"),
            "ok": True,
            "payload": {
                "type": "hello-ok",
                "protocol": 1,
                "policy": {
                    "tickIntervalMs": 15000,
                },
            },
        })

        # 消息循环
        while True:
            message = await websocket.receive_json()
            await handle_ws_message(websocket, client_id, message)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if client_id:
            ws_manager.disconnect(client_id)


async def handle_ws_message(ws: WebSocket, client_id: str, message: dict):
    """处理 WebSocket 消息"""
    msg_type = message.get("type")
    req_id = message.get("id", "")

    if msg_type != "req":
        return

    method = message.get("method")
    params = message.get("params", {})

    try:
        result: Dict[str, Any] = {}

        if method == "health":
            result = {"status": "ok", "connections": ws_manager.count()}

        elif method == "status":
            scheduler_status = {}
            if scheduler:
                status = await scheduler.status()
                scheduler_status = status.to_dict()

            result = {
                "channels": channel_manager.get_status() if channel_manager else {},
                "scheduler": scheduler_status,
            }

        elif method == "agent":
            # 流式 Agent
            text = params.get("message", "")
            session_id = params.get("sessionId", "default")
            user_id = params.get("userId", settings.default_user_id)

            async def on_content(delta: str):
                await ws_manager.send_event(client_id, "agent.content", {
                    "delta": delta,
                    "sessionId": session_id,
                    "userId": user_id,
                })

            if agent_service:
                chat_result = await agent_service.chat_stream(
                    message=text,
                    session_id=session_id,
                    user_id=user_id,
                    on_content=on_content,
                )
                result = {
                    "content": chat_result.content,
                    "toolCalls": chat_result.tool_calls,
                    "sessionId": chat_result.session_id,
                    "userId": chat_result.user_id,
                }
            else:
                result = {"error": "Agent service not ready"}

        elif method == "send":
            # 发送消息到渠道
            channel = params.get("channel")
            target = params.get("target")
            content = params.get("message")

            if channel_manager:
                success = await channel_manager.send(channel, target, content)
                result = {"status": "sent" if success else "failed"}
            else:
                result = {"status": "failed", "error": "Channel manager not ready"}

        else:
            raise ValueError(f"Unknown method: {method}")

        await ws.send_json({
            "type": "res",
            "id": req_id,
            "ok": True,
            "payload": result,
        })

    except Exception as e:
        await ws.send_json({
            "type": "res",
            "id": req_id,
            "ok": False,
            "error": {"code": "ERROR", "message": str(e)},
        })


# ============== Webhooks ==============

@app.post("/webhook/feishu")
async def feishu_webhook(request: dict):
    """飞书 Webhook（用于 URL 验证）"""
    # URL 验证
    if "challenge" in request:
        return {"challenge": request["challenge"]}

    return {"status": "ok"}


# ============== 辅助函数 ==============

async def setup_channels():
    """设置渠道"""
    from .channels.feishu import FeishuChannel
    from .channels.telegram import TelegramChannel
    from .channels.discord import DiscordChannel

    # 飞书
    if settings.feishu_app_id and settings.feishu_app_secret:
        try:
            feishu = FeishuChannel(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                allowed_users=settings.feishu_allowed_users,
                allowed_groups=settings.feishu_allowed_groups,
            )
            channel_manager.register(feishu)
        except Exception as e:
            logger.error(f"Failed to create Feishu channel: {e}")

    # Telegram
    if settings.telegram_bot_token:
        try:
            telegram = TelegramChannel(
                bot_token=settings.telegram_bot_token,
                allowed_users=settings.telegram_allowed_users,
            )
            channel_manager.register(telegram)
        except Exception as e:
            logger.error(f"Failed to create Telegram channel: {e}")

    # Discord
    if settings.discord_bot_token:
        try:
            discord = DiscordChannel(
                bot_token=settings.discord_bot_token,
                allowed_users=settings.discord_allowed_users,
                allowed_guilds=settings.discord_allowed_guilds,
            )
            channel_manager.register(discord)
        except Exception as e:
            logger.error(f"Failed to create Discord channel: {e}")

    # 设置消息处理器
    channel_manager.set_handler(handle_channel_message)

    # 连接所有渠道
    await channel_manager.connect_all()


async def handle_channel_message(message):
    """处理渠道消息"""
    logger.info(f"[{message.channel.value}] {message.sender_id}: {message.content[:500]}")

    if not agent_service:
        logger.error("Agent service not ready")
        return

    # 路由到 Agent
    agent_id = message_router.route(message)
    session_id = message_router.get_session_id(message, agent_id)
    # 使用 sender_id 作为 user_id（渠道用户标识）
    user_id = message.sender_id or settings.default_user_id

    try:
        result = await agent_service.chat(
            message=message.content,
            session_id=session_id,
            user_id=user_id,
        )

        # 回复
        if result.content:
            await channel_manager.send(
                message.channel,
                message.channel_id,
                result.content,
            )

        # 广播事件到 WebSocket 客户端
        await ws_manager.broadcast("channel.message", {
            "channel": message.channel.value,
            "sender": message.sender_id,
            "userId": user_id,
            "content": message.content[:100],
            "response": result.content[:100] if result.content else "",
        })

    except Exception as e:
        logger.error(f"Handle message error: {e}")
        await channel_manager.send(
            message.channel,
            message.channel_id,
            f"处理失败: {e}",
        )


# ============== 启动入口 ==============

def main():
    """启动 FastAPI 服务"""
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
