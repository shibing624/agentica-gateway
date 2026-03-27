"""Pydantic request/response models shared across routes."""
from typing import Optional, List
from pydantic import BaseModel


class ChatRequest(BaseModel):
    """Chat request payload."""
    message: str
    session_id: str = "default"
    user_id: str = "default"
    agent_id: str = "main"
    work_dir: Optional[str] = None


class ChatResponse(BaseModel):
    """Chat response payload."""
    content: str
    session_id: str
    user_id: str = "default"
    tool_calls: int = 0


class MemoryRequest(BaseModel):
    content: str
    user_id: str = "default"
    long_term: bool = False


class SendRequest(BaseModel):
    channel: str
    channel_id: str
    message: str


class JobCreateRequest(BaseModel):
    name: str
    prompt: str
    user_id: str
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    run_at_iso: Optional[str] = None
    timezone: str = "Asia/Shanghai"


class JobResponse(BaseModel):
    id: str
    name: str
    schedule: str
    status: str
    next_run_at_ms: Optional[int] = None


class BatchJobsRequest(BaseModel):
    job_ids: List[str]


class CloneJobRequest(BaseModel):
    new_name: Optional[str] = None


class ModelSwitchRequest(BaseModel):
    model_provider: str
    model_name: str


class ThinkingToggleRequest(BaseModel):
    enabled: bool


class BaseDirRequest(BaseModel):
    base_dir: str


class OpenRequest(BaseModel):
    path: str
    app: str = "finder"  # "finder" or "terminal"
