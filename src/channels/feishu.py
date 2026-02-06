"""飞书渠道实现"""
import json
import asyncio
import threading
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from .base import Channel, ChannelType, Message
from ..config import settings

# 飞书 SDK 相关（延迟导入）
lark = None
CreateMessageRequest = None
CreateMessageRequestBody = None
_lark_executor = None


def _get_lark_executor():
    """获取飞书专用线程池（用于隔离事件循环）"""
    global _lark_executor
    if _lark_executor is None:
        _lark_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lark-ws")
    return _lark_executor


def _init_lark_in_thread():
    """在独立线程中导入并初始化 lark_oapi（避免事件循环污染主线程）"""
    global lark, CreateMessageRequest, CreateMessageRequestBody

    # 创建该线程专属的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    import lark_oapi as _lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest as _CreateMessageRequest,
        CreateMessageRequestBody as _CreateMessageRequestBody,
    )
    lark = _lark
    CreateMessageRequest = _CreateMessageRequest
    CreateMessageRequestBody = _CreateMessageRequestBody


def _ensure_lark_sdk():
    """确保飞书 SDK 已导入"""
    global lark
    if lark is None:
        # 在独立线程中初始化，避免事件循环冲突
        executor = _get_lark_executor()
        future = executor.submit(_init_lark_in_thread)
        future.result()  # 等待完成


class FeishuChannel(Channel):
    """飞书渠道

    使用 WebSocket 长连接模式接收消息
    """

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        allowed_users: Optional[List[str]] = None,
        allowed_groups: Optional[List[str]] = None,
    ):
        super().__init__()
        self.app_id = app_id or settings.feishu_app_id
        self.app_secret = app_secret or settings.feishu_app_secret
        self.allowed_users = allowed_users or settings.feishu_allowed_users or []
        self.allowed_groups = allowed_groups or settings.feishu_allowed_groups or []
        self._client = None
        self._ws_client = None
        self._ws_thread = None
        self._main_loop = None

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.FEISHU

    async def connect(self) -> bool:
        """建立连接"""
        if not self.app_id or not self.app_secret:
            logger.warning("Feishu: Missing credentials, skipped")
            return False

        try:
            _ensure_lark_sdk()

            self._client = (
                lark.Client.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .build()
            )

            # 保存主事件循环引用
            self._main_loop = asyncio.get_running_loop()

            # 事件处理器
            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message)
                .register_p1_customized_event("im.message.message_read_v1", lambda _: None)
                .build()
            )

            # WebSocket 长连接
            self._ws_client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.WARNING,
            )

            # 在独立线程启动 WebSocket（避免事件循环冲突）
            self._ws_thread = threading.Thread(
                target=self._run_ws_client,
                daemon=True,
            )
            self._ws_thread.start()

            self._connected = True
            logger.info("Feishu: Connected")
            return True

        except ImportError as e:
            logger.error(f"Feishu: SDK not installed: {e}")
            return False
        except Exception as e:
            logger.error(f"Feishu: Connect failed: {e}")
            return False

    def _run_ws_client(self):
        """在独立线程运行 WebSocket 客户端"""
        try:
            # lark_oapi 内部会使用模块级事件循环，这里不需要再创建
            self._ws_client.start()
        except Exception as e:
            logger.error(f"Feishu: WebSocket error: {e}")
            self._connected = False

    async def disconnect(self):
        """断开连接"""
        self._connected = False
        logger.info("Feishu: Disconnected")

    async def send(self, channel_id: str, content: str, **kwargs) -> bool:  # noqa: ARG002
        """发送消息"""
        if not self._client:
            logger.warning("Feishu: Not connected")
            return False

        try:
            _ensure_lark_sdk()

            # 分片长消息
            for chunk in self._split_text(content, 4000):
                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(channel_id)
                        .msg_type("text")
                        .content(json.dumps({"text": chunk}))
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.message.create(request)

                if not response.success():
                    logger.error(f"Feishu: Send failed: {response.code} {response.msg}")
                    return False

            return True

        except Exception as e:
            logger.error(f"Feishu: Send error: {e}")
            return False

    def _on_message(self, data) -> None:
        """处理飞书消息（同步回调，在 ws 线程中执行）"""
        try:
            msg = data.event.message
            sender = data.event.sender

            # 只处理文本消息
            if msg.message_type != "text":
                return

            content = json.loads(msg.content)
            text = content.get("text", "").strip()
            if not text:
                return

            user_id = sender.sender_id.user_id if sender.sender_id else "feishu_user"
            open_id = sender.sender_id.open_id if sender.sender_id else ""

            # 记录用户消息日志
            logger.debug(f"[feishu] {user_id}: {text[:100]}")

            # 白名单检查
            if self.allowed_users and user_id not in self.allowed_users:
                logger.debug(f"Feishu: User {user_id} not in allowlist")
                return

            # 构造统一消息
            message = Message(
                channel=ChannelType.FEISHU,
                channel_id=msg.chat_id,
                sender_id=user_id,
                sender_name=open_id,
                content=text,
                message_id=msg.message_id,
                metadata={
                    "chat_type": msg.chat_type,
                    "open_id": open_id,
                }
            )

            # 使用 call_soon_threadsafe 将任务提交到主事件循环
            if self._message_handler and self._main_loop:
                self._main_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._emit_message(message))
                )

        except Exception as e:
            logger.error(f"Feishu: Message error: {e}")

    @staticmethod
    def _split_text(text: str, max_len: int) -> List[str]:
        """分片长文本"""
        if not text:
            return [""]
        return [text[i:i + max_len] for i in range(0, len(text), max_len)]
