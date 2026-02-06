"""Gradio Channel - Gradio Web 交互渠道"""
import asyncio
import threading
import time
import uuid

from loguru import logger

from .base import Channel, ChannelType


class GradioChannel(Channel):
    """Gradio Web 交互渠道（流式输出）"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 7860,
        share: bool = False,
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.share = share
        self._app = None
        self._thread = None
        self._agent_service = None

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.GRADIO

    def set_agent_service(self, agent_service):
        """设置 Agent 服务（支持流式输出）"""
        self._agent_service = agent_service

    async def connect(self) -> bool:
        """初始化并启动 Gradio"""
        try:
            import gradio  # noqa: F401

            self._create_app()

            self._thread = threading.Thread(
                target=self._launch_in_thread,
                daemon=True,
            )
            self._thread.start()

            self._connected = True
            logger.info(f"Gradio: Started on http://{self.host}:{self.port}")
            return True

        except ImportError as e:
            logger.warning(f"Gradio: Not installed, skipped: {e}")
            return False
        except Exception as e:
            logger.error(f"Gradio: Connect failed: {e}")
            return False

    def _launch_in_thread(self):
        """在独立线程启动 Gradio"""
        import warnings
        import os
        
        # 抑制 Gradio 启动时的输出
        os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
        warnings.filterwarnings("ignore", category=UserWarning)
        
        try:
            self._app.launch(
                server_name=self.host,
                server_port=self.port,
                share=self.share,
                show_error=True,
                prevent_thread_lock=True,
                quiet=True,  # 静默模式
            )
        except Exception as e:
            logger.error(f"Gradio: Launch error: {e}")

    async def disconnect(self):
        """断开 Gradio"""
        if self._app:
            try:
                self._app.close()
            except Exception:
                pass
        self._connected = False
        logger.info("Gradio: Disconnected")

    async def send(self, channel_id: str, content: str, **kwargs) -> bool:  # noqa: ARG002
        """发送响应（Gradio 通过流式直接返回，此方法备用）"""
        return True

    def _create_app(self):
        """创建 Gradio 应用"""
        import gradio as gr
        from ..config import settings

        # 保存 self 引用，供闭包使用
        channel = self

        # 使用全局配置的默认 user_id
        default_user_id = settings.default_user_id

        # 构建界面
        with gr.Blocks(
            title="Agentica",
            theme=gr.themes.Soft(),
        ) as app:
            # 使用 State 存储当前 session_id
            session_state = gr.State(value=f"gradio:{uuid.uuid4().hex[:12]}")

            gr.HTML("""
            <div style="text-align: center; margin-bottom: 20px;">
                <h1>🤖 Agentica</h1>
                <p style="color: #666;">AI Agent Chat</p>
            </div>
            """)

            with gr.Row():
                with gr.Column(scale=4):
                    chatbot = gr.Chatbot(
                        label="Chat",
                        show_copy_button=True,
                        render_markdown=True,
                        type="messages",
                        height=500,
                    )

                    with gr.Row():
                        msg = gr.Textbox(
                            placeholder="Type your message...",
                            show_label=False,
                            scale=9,
                            autofocus=True,
                        )
                        submit_btn = gr.Button("Send", variant="primary", scale=1)

                    with gr.Row():
                        clear_btn = gr.Button("Clear", size="sm")

                with gr.Column(scale=1):
                    gr.Markdown(f"""
### Configuration
- **Provider**: `{settings.model_provider}`
- **Model**: `{settings.model_name}`
- **Workspace**: `{settings.workspace_path}`

""")

            # 流式响应
            def user_message(message, history):
                """用户消息处理"""
                if not message.strip():
                    return history, ""
                history = history + [{"role": "user", "content": message}]
                return history, ""

            def bot_response(history, session_id):
                """Bot 流式响应"""
                if not history:
                    return history

                user_msg = history[-1]["content"]
                history = history + [{"role": "assistant", "content": ""}]

                # 记录用户输入日志
                logger.info(f"[gradio] {session_id}: {user_msg}")

                # 动态获取 agent_service
                agent_service = channel._agent_service
                if not agent_service:
                    history[-1]["content"] = "Agent service not ready. Please wait..."
                    yield history
                    return

                # 流式调用
                content_buffer = ""
                finished = False
                error_msg = None

                async def run_stream():
                    nonlocal content_buffer, finished, error_msg
                    try:
                        async def on_content(delta: str):
                            nonlocal content_buffer
                            content_buffer += delta

                        await agent_service.chat_stream(
                            message=user_msg,
                            session_id=session_id,
                            user_id=default_user_id,
                            on_content=on_content,
                        )
                    except Exception as e:
                        error_msg = str(e)
                    finally:
                        finished = True

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                thread = threading.Thread(
                    target=lambda: loop.run_until_complete(run_stream()),
                    daemon=True,
                )
                thread.start()

                last_len = 0
                while not finished or len(content_buffer) > last_len:
                    if len(content_buffer) > last_len:
                        history[-1]["content"] = content_buffer
                        last_len = len(content_buffer)
                        yield history
                    time.sleep(0.05)

                thread.join(timeout=60)
                
                # 清理事件循环
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()
                except Exception:
                    pass

                # 最终更新
                if error_msg:
                    history[-1]["content"] = f"Error: {error_msg}"
                elif content_buffer:
                    history[-1]["content"] = content_buffer
                else:
                    history[-1]["content"] = "No response received."
                yield history

            # 事件绑定
            msg.submit(
                user_message, [msg, chatbot], [chatbot, msg]
            ).then(
                bot_response, [chatbot, session_state], [chatbot]
            )

            submit_btn.click(
                user_message, [msg, chatbot], [chatbot, msg]
            ).then(
                bot_response, [chatbot, session_state], [chatbot]
            )

            def clear_and_new_session():
                """清空对话并创建新 session"""
                new_session_id = f"gradio:{uuid.uuid4().hex[:12]}"
                logger.info(f"[gradio] New session: {new_session_id}")
                return [], new_session_id

            clear_btn.click(clear_and_new_session, outputs=[chatbot, session_state])

        self._app = app
