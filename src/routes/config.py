"""Config routes: /api/config/*, /api/models, /api/model, /api/open, /api/status."""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from .. import deps
from ..config import settings
from ..__init__ import __version__
from ..models import ModelSwitchRequest, ThinkingToggleRequest, BaseDirRequest, OpenRequest
from ..services.agent_service import AgentService

router = APIRouter()

# Supported model list (provider → models)
SUPPORTED_MODELS = {
    "zhipuai": ["glm-4.7-flash", "glm-4-plus", "glm-4-long", "glm-4-flashx", "glm-4-flash", "glm-4-air", "glm-4-airx", "glm-4"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1", "o1-mini", "o3-mini"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "yi": ["yi-lightning", "yi-large", "yi-medium", "yi-spark"],
    "doubao": ["doubao-1.5-pro-32k", "doubao-pro-32k", "doubao-lite-32k"],
    "kimi": ["k2p5", "k1.5-long", "k1.5-short"],
    "anthropic": ["claude-sonnet-4-20250514", "claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
    "azure": ["gpt-4o", "gpt-4-turbo", "gpt-35-turbo"],
}

_DIR_HISTORY_MAX = 20


# ============== Root + Status ==============

@router.get("/")
async def root():
    return {"name": "Agentica Gateway", "version": __version__, "status": "running"}


@router.get("/health")
@router.get("/api/health")
async def health():
    scheduler_status = {}
    if deps.scheduler:
        status = await deps.scheduler.status()
        scheduler_status = status.to_dict()
    return {
        "status": "ok",
        "version": __version__,
        "channels": deps.channel_manager.get_status() if deps.channel_manager else {},
        "scheduler": scheduler_status,
    }


@router.get("/api/status")
async def status():
    scheduler_status = {}
    if deps.scheduler:
        st = await deps.scheduler.status()
        scheduler_status = st.to_dict()

    context_window = 128000
    svc = deps.agent_service
    if svc:
        first_session = next(iter(svc._cache.keys()), None)
        if first_session:
            agent = svc._cache.get(first_session)
            if agent and agent.model:
                context_window = getattr(agent.model, "context_window", 128000)

    return {
        "workspace": str(settings.workspace_path),
        "base_dir": str(settings.base_dir),
        "model": f"{svc.model_provider}/{svc.model_name}" if svc else f"{settings.model_provider}/{settings.model_name}",
        "model_provider": svc.model_provider if svc else settings.model_provider,
        "model_name": svc.model_name if svc else settings.model_name,
        "model_thinking": settings.model_thinking or "",
        "context_window": context_window,
        "version": __version__,
        "channels": deps.channel_manager.get_status() if deps.channel_manager else {},
        "scheduler": scheduler_status,
    }


# ============== Models ==============

@router.get("/api/models")
async def list_models():
    svc = deps.agent_service
    current_provider = svc.model_provider if svc else settings.model_provider
    current_name = svc.model_name if svc else settings.model_name
    return {
        "current_provider": current_provider,
        "current_name": current_name,
        "current": f"{current_provider}/{current_name}",
        "providers": SUPPORTED_MODELS,
    }


@router.post("/api/model")
async def switch_model(
    request: ModelSwitchRequest,
    svc: AgentService = Depends(deps.get_agent_service),
):
    await svc.reload_model(request.model_provider, request.model_name)
    settings.model_provider = request.model_provider
    settings.model_name = request.model_name
    return {"status": "ok", "model": f"{request.model_provider}/{request.model_name}"}


# ============== Thinking ==============

@router.post("/api/config/thinking")
async def toggle_thinking(request: ThinkingToggleRequest):
    new_val = "enabled" if request.enabled else ""
    settings.model_thinking = new_val
    svc = deps.agent_service
    if svc:
        await svc.reload_model(settings.model_provider, settings.model_name)
    return {"status": "ok", "thinking": new_val}


@router.get("/api/config/thinking")
async def get_thinking():
    return {"thinking": settings.model_thinking or ""}


# ============== Working directory ==============

@router.post("/api/config/base_dir")
async def set_base_dir(request: BaseDirRequest):
    raw = request.base_dir.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="路径不能为空")
    p = Path(raw).expanduser().resolve()
    created = False
    if not p.exists():
        if p.parent.exists():
            p.mkdir(parents=False, exist_ok=True)
            created = True
        else:
            raise HTTPException(status_code=400, detail=f"路径不存在且无法自动创建: {p}")
    elif not p.is_dir():
        raise HTTPException(status_code=400, detail=f"该路径不是文件夹: {p}")

    settings.base_dir = p
    svc = deps.agent_service
    if svc:
        svc.update_work_dir(str(p))
    await _add_dir_history(str(p))
    return {"status": "ok", "base_dir": str(p), "created": created}


@router.get("/api/config/dir_history")
async def get_dir_history():
    history = await _load_dir_history()
    current = str(settings.base_dir)
    if current not in history:
        history.insert(0, current)
        await _save_dir_history(history)
    return {"history": history}


@router.delete("/api/config/dir_history")
async def clear_dir_history():
    await _save_dir_history([str(settings.base_dir)])
    return {"status": "ok"}


def _dir_history_file() -> Path:
    return settings.data_dir / "dir_history.json"


async def _load_dir_history() -> list[str]:
    f = _dir_history_file()
    if f.exists():
        try:
            text = await asyncio.to_thread(f.read_text)
            return json.loads(text)
        except Exception:
            pass
    return []


async def _save_dir_history(history: list[str]) -> None:
    f = _dir_history_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(history, ensure_ascii=False)
    await asyncio.to_thread(f.write_text, data)


async def _add_dir_history(path: str) -> None:
    history = await _load_dir_history()
    if path in history:
        history.remove(path)
    history.insert(0, path)
    await _save_dir_history(history[:_DIR_HISTORY_MAX])


# ============== Open in Finder / Terminal ==============

@router.post("/api/open")
async def open_path(request: OpenRequest):
    """Open a path in Finder or Terminal (local deployments only)."""
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
