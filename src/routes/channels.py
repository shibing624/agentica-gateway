"""Channel routes: /api/channels, /api/send, /webhook/*"""
from fastapi import APIRouter, Depends, HTTPException

from .. import deps
from ..models import SendRequest
from ..services.channel_manager import ChannelManager

router = APIRouter()


@router.get("/api/channels")
async def list_channels(cm: ChannelManager = Depends(deps.get_channel_manager)):
    return {
        "channels": cm.list_channels(),
        "status": cm.get_status(),
    }


@router.post("/api/send")
async def send_message(
    request: SendRequest,
    cm: ChannelManager = Depends(deps.get_channel_manager),
):
    success = await cm.send(request.channel, request.channel_id, request.message)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to send message")
    return {"status": "sent"}


@router.post("/webhook/feishu")
async def feishu_webhook(request: dict):
    """Feishu webhook endpoint (URL verification + event delivery)."""
    if "challenge" in request:
        return {"challenge": request["challenge"]}
    return {"status": "ok"}
