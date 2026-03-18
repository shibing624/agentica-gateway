"""Agent 服务 - 封装 agentica SDK

提供功能完善的 Agent 服务，包括：
- Workspace 配置层（静态配置 + 持久记忆）
- 会话历史管理（按 session_id 隔离）
- 多用户 Agent 实例隔离（按 session_id 独立 Agent，避免 WorkingMemory 污染）
- 工具调用显示
- 调度器工具集成（定时任务）
"""
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, List, Any, Dict

from loguru import logger
from agentica import Agent
from agentica.run_response import AgentCancelledError
from agentica.run_config import RunConfig
from agentica.workspace import Workspace
from agentica.agent.config import WorkspaceMemoryConfig, ToolConfig, PromptConfig

from ..config import settings

logger = logger.bind(module="agent_service")


@dataclass
class ChatResult:
    """聊天结果"""
    content: str
    tool_calls: int = 0
    session_id: str = ""
    user_id: str = ""
    tools_used: List[str] = field(default_factory=list)
    reasoning: str = ""
    metrics: Optional[Dict[str, Any]] = None


class AgentService:
    """Agent 服务

    封装 agentica SDK，提供统一的 Agent 调用接口：
    - Workspace 配置层（AGENT.md, PERSONA.md, MEMORY.md 等）
    - 会话历史管理（数据库存储，按 session_id 隔离）
    - 多用户支持（按 user_id 隔离 Workspace 记忆）
    - 调度器工具集成
    """

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        model_name: Optional[str] = None,
        model_provider: Optional[str] = None,
        extra_tools: Optional[List[Any]] = None,
        extra_instructions: Optional[List[str]] = None,
    ):
        self.workspace_path = Path(workspace_path or settings.workspace_path).expanduser()
        self.model_name = model_name or settings.model_name
        self.model_provider = model_provider or settings.model_provider
        self.extra_tools = extra_tools or []
        self.extra_instructions = extra_instructions or []

        self._agents: Dict[str, Agent] = {}
        self._workspace: Optional[Workspace] = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self):
        """确保已初始化（异步，Lock 保护防止并发重入）"""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._do_initialize)

    def _do_initialize(self):
        """初始化 Workspace（同步，在 to_thread 中执行）

        Agent 实例按 session_id 延迟创建，此处只初始化共享的 Workspace。
        """
        try:
            self._workspace = Workspace(self.workspace_path)
            if not self._workspace.exists():
                self._workspace.initialize()
                logger.info(f"Workspace initialized at {self.workspace_path}")

            self._initialized = True
            logger.info("AgentService initialized successfully")
            logger.info(f"Model: {self.model_provider}/{self.model_name}")
            logger.info(f"Workspace: {self.workspace_path}")

        except Exception as e:
            logger.error(f"AgentService init error: {e}")
            logger.warning("Running in mock mode")
            self._initialized = True

    def _build_agent(self) -> Agent:
        """构建一个新的 Agent 实例"""
        model = self._create_model()

        all_tools = list(self.extra_tools) if self.extra_tools else []
        scheduler_tools = self._get_scheduler_tools()
        all_tools.extend(scheduler_tools)

        instructions = list(self.extra_instructions) if self.extra_instructions else []
        if scheduler_tools:
            instructions.append(self._get_scheduler_instructions())

        agent = Agent(
            model=model,
            workspace=self._workspace,
            tools=all_tools if all_tools else None,
            instructions=instructions if instructions else None,
            add_history_to_messages=True,
            history_window=14,
            debug=settings.debug,
            long_term_memory_config=WorkspaceMemoryConfig(
                load_workspace_context=True,
                load_workspace_memory=True,
                memory_days=7,
            ),
            tool_config=ToolConfig(
                tool_call_limit=40,
                auto_load_mcp=True,
                add_builtin_tools=True,
            ),
            prompt_config=PromptConfig(
                add_datetime_to_instructions=True,
            ),
        )

        if all_tools:
            logger.info(f"Tools loaded: {len(all_tools)}")
        return agent

    async def _get_agent(self, session_id: str) -> Optional[Agent]:
        """获取 session 对应的 Agent 实例（不存在则创建）"""
        if session_id not in self._agents:
            try:
                agent = await asyncio.to_thread(self._build_agent)
                self._agents[session_id] = agent
                logger.info(f"Agent created for session: {session_id}")
            except Exception as e:
                logger.error(f"Failed to create agent for session {session_id}: {e}")
                return None
        return self._agents[session_id]

    def _create_model(self) -> Any:
        """创建模型实例"""
        _ANTHROPIC_PROVIDERS = {"kimi", "anthropic", "claude"}

        params: dict[str, Any] = {"id": self.model_name, "timeout": 300}

        if settings.model_thinking and settings.model_thinking in ("enabled", "disabled", "auto"):
            if self.model_provider in _ANTHROPIC_PROVIDERS:
                params["thinking"] = {"type": settings.model_thinking, "budget_tokens": 8000}
            else:
                params["extra_body"] = {"thinking": {"type": settings.model_thinking}}
            logger.info(f"Model thinking mode: {settings.model_thinking} (provider={self.model_provider})")

        if self.model_provider == "zhipuai":
            from agentica import ZhipuAI
            return ZhipuAI(**params)
        elif self.model_provider == "openai":
            from agentica import OpenAIChat
            return OpenAIChat(**params)
        elif self.model_provider == "deepseek":
            from agentica import DeepSeek
            return DeepSeek(**params)
        elif self.model_provider == "moonshot":
            from agentica import Moonshot
            return Moonshot(**params)
        elif self.model_provider == "yi":
            from agentica import Yi
            return Yi(**params)
        elif self.model_provider == 'doubao':
            from agentica import Doubao
            return Doubao(**params)
        elif self.model_provider == "kimi":
            from agentica import KimiChat
            return KimiChat(**params)
        elif self.model_provider in ("anthropic", "claude"):
            from agentica import Claude
            return Claude(**params)
        elif self.model_provider == "azure":
            from agentica import AzureOpenAIChat
            return AzureOpenAIChat(**params)
        else:
            from agentica import OpenAIChat
            return OpenAIChat(**params)

    def _get_scheduler_tools(self) -> List[Any]:
        """获取调度器工具"""
        try:
            from ..scheduler import (
                create_scheduled_job_tool,
                list_scheduled_jobs_tool,
                delete_scheduled_job_tool,
                pause_scheduled_job_tool,
                resume_scheduled_job_tool,
                create_task_chain_tool,
            )

            tools = [
                create_scheduled_job_tool,
                list_scheduled_jobs_tool,
                delete_scheduled_job_tool,
                pause_scheduled_job_tool,
                resume_scheduled_job_tool,
                create_task_chain_tool,
            ]

            logger.debug(f"Loaded {len(tools)} scheduler tools")
            return tools

        except Exception as e:
            logger.warning(f"Failed to load scheduler tools: {e}")
            return []

    def _get_scheduler_instructions(self) -> str:
        """获取调度器相关的指令"""
        return """
# 定时任务功能

你可以帮助用户创建和管理定时任务。当用户想要设置提醒、定时执行某些操作时，使用以下工具：

## 可用工具
- `create_scheduled_job`: 创建新的定时任务（支持自然语言描述）
- `list_scheduled_jobs`: 列出用户的定时任务
- `delete_scheduled_job`: 删除定时任务
- `pause_scheduled_job`: 暂停定时任务
- `resume_scheduled_job`: 恢复暂停的任务
- `create_task_chain`: 创建任务链（任务A完成后自动触发任务B）

## 使用场景
- "每天早上9点提醒我看新闻" → 使用 create_scheduled_job
- "帮我取消那个每日新闻提醒" → 先 list_scheduled_jobs 找到任务，再 delete_scheduled_job
- "暂停一下那个任务" → 使用 pause_scheduled_job
- "当数据备份完成后，自动开始数据分析" → 使用 create_task_chain

## 注意事项
- 创建任务时需要提供 user_id（从会话上下文中获取）
- 支持的时间格式：cron表达式、间隔执行、一次性执行
- 任务执行结果可以通过通知渠道发送给用户
"""

    @staticmethod
    def _extract_metrics(agent: Optional[Agent]) -> Optional[Dict[str, Any]]:
        """从 agent.run_response 提取本次 run 的 metrics"""
        if not agent:
            return None
        if agent.run_response and agent.run_response.metrics:
            return agent.run_response.metrics
        return None

    @staticmethod
    def _format_tool_call_args(tool_name: str, tool_args: dict) -> dict:
        """格式化工具调用参数用于前端展示

        对文件操作工具计算行数 diff 元数据，其他工具截断过长参数值。
        """
        display_args: dict = {}

        if tool_name == 'edit_file':
            old_s = tool_args.get('old_string', '')
            new_s = tool_args.get('new_string', '')
            display_args['_diff_add'] = new_s.count('\n') + (1 if new_s else 0)
            display_args['_diff_del'] = old_s.count('\n') + (1 if old_s else 0)
            fp = tool_args.get('file_path', '') or tool_args.get('file', '') or tool_args.get('path', '')
            if fp:
                display_args['file_path'] = fp

        elif tool_name == 'multi_edit_file':
            edits = tool_args.get('edits', [])
            total_add = total_del = 0
            for ed in (edits if isinstance(edits, list) else []):
                old_s = ed.get('old_string', '')
                new_s = ed.get('new_string', '')
                total_del += old_s.count('\n') + (1 if old_s else 0)
                total_add += new_s.count('\n') + (1 if new_s else 0)
            display_args['_diff_add'] = total_add
            display_args['_diff_del'] = total_del
            display_args['_edit_count'] = len(edits) if isinstance(edits, list) else 0
            fp = tool_args.get('file_path', '') or tool_args.get('file', '') or tool_args.get('path', '')
            if fp:
                display_args['file_path'] = fp

        elif tool_name == 'write_file':
            content = tool_args.get('content', '')
            display_args['_lines'] = content.count('\n') + (1 if content else 0)
            fp = tool_args.get('file_path', '') or tool_args.get('file', '') or tool_args.get('path', '')
            if fp:
                display_args['file_path'] = fp

        else:
            for k, v in tool_args.items():
                if isinstance(v, str) and len(v) > 100:
                    display_args[k] = v[:100] + "..."
                else:
                    display_args[k] = v

        return display_args

    @staticmethod
    def _format_tool_result(tool_info: dict) -> tuple[str, str, bool]:
        """格式化工具调用结果

        Returns:
            (tool_name, result_str, is_task_meta) - is_task_meta 表示是否为 task 工具的元数据
        """
        t_name = tool_info.get("tool_name") or tool_info.get("name", "unknown")
        t_content = tool_info.get("content", "")
        is_error = tool_info.get("tool_call_error", False)

        # task 工具特殊处理：解析 JSON 提取子代理执行信息
        if t_name == "task" and t_content:
            try:
                task_data = json.loads(str(t_content))
                task_meta = {
                    "_task_meta": True,
                    "success": task_data.get("success", False),
                    "tool_calls_summary": task_data.get("tool_calls_summary", []),
                    "execution_time": task_data.get("execution_time"),
                    "tool_count": task_data.get("tool_count", 0),
                }
                if not task_data.get("success"):
                    task_meta["error"] = task_data.get("error", "Unknown error")
                return t_name, json.dumps(task_meta, ensure_ascii=False), True
            except (ValueError, TypeError):
                pass

        if t_content:
            result_str = str(t_content)[:500] + ("..." if len(str(t_content)) > 500 else "")
        else:
            result_str = "(no output)"
        if is_error:
            result_str = "Error: " + result_str

        return t_name, result_str, False

    async def chat(
        self,
        message: str,
        session_id: str,
        user_id: str = "default",
    ) -> ChatResult:
        """处理聊天消息（非流式）

        Args:
            message: 用户消息
            session_id: 会话ID
            user_id: 用户ID

        Returns:
            聊天结果
        """
        await self._ensure_initialized()

        agent = await self._get_agent(session_id)
        if not agent:
            return ChatResult(
                content=f"[Mock] Received: {message}",
                tool_calls=0,
                session_id=session_id,
                user_id=user_id,
            )

        try:
            if self._workspace:
                await asyncio.to_thread(self._workspace.set_user, user_id)

            response = await agent.run(message)

            content = (response.content or "").strip()
            tools_used = []
            tool_calls = 0

            if response.tools:
                tool_calls = len(response.tools)
                for tool in response.tools:
                    if isinstance(tool, dict):
                        tools_used.append(tool.get("tool_name", tool.get("name", "unknown")))
                    else:
                        tools_used.append(str(tool))

            return ChatResult(
                content=content,
                tool_calls=tool_calls,
                session_id=session_id,
                user_id=user_id,
                tools_used=tools_used,
                metrics=self._extract_metrics(agent),
            )

        except Exception as e:
            logger.error(f"AgentService chat error: {e}")
            return ChatResult(
                content=f"Error: {e}",
                tool_calls=0,
                session_id=session_id,
                user_id=user_id,
            )

    async def chat_stream(
        self,
        message: str,
        session_id: str,
        user_id: str = "default",
        on_content: Optional[Callable[[str], Any]] = None,
        on_tool_call: Optional[Callable[[str, dict], Any]] = None,
        on_tool_result: Optional[Callable[[str, str], Any]] = None,
        on_thinking: Optional[Callable[[str], Any]] = None,
    ) -> ChatResult:
        """流式聊天

        Args:
            message: 用户消息
            session_id: 会话ID
            user_id: 用户ID
            on_content: 内容回调
            on_tool_call: 工具调用回调 (name, args)
            on_tool_result: 工具结果回调 (name, result)
            on_thinking: 思考过程回调

        Returns:
            聊天结果
        """
        await self._ensure_initialized()

        agent = await self._get_agent(session_id)
        if not agent:
            content = f"[Mock] Received: {message}"
            if on_content:
                await on_content(content)
            return ChatResult(
                content=content,
                tool_calls=0,
                session_id=session_id,
                user_id=user_id,
            )

        try:
            if self._workspace:
                await asyncio.to_thread(self._workspace.set_user, user_id)

            full_content = ""
            reasoning_content = ""
            tools_used = []
            tool_calls = 0

            async for chunk in agent.run_stream(message, config=RunConfig(stream_intermediate_steps=True)):
                if chunk is None:
                    continue

                # 工具调用开始
                if chunk.event == "ToolCallStarted":
                    tool_info = chunk.tools[-1] if chunk.tools else None
                    if tool_info:
                        tool_name = tool_info.get("tool_name") or tool_info.get("name", "unknown")
                        tool_args = tool_info.get("tool_args") or tool_info.get("arguments", {})
                        display_args = self._format_tool_call_args(tool_name, tool_args)
                        tools_used.append(tool_name)
                        tool_calls += 1
                        if on_tool_call:
                            await on_tool_call(tool_name, display_args)
                    continue

                # 工具调用完成
                elif chunk.event == "ToolCallCompleted":
                    if chunk.tools and on_tool_result:
                        for ti in reversed(chunk.tools):
                            if "content" in ti:
                                t_name, result_str, _ = self._format_tool_result(ti)
                                await on_tool_result(t_name, result_str)
                                break
                    continue

                # 跳过其他中间事件
                if chunk.event in ("RunStarted", "RunCompleted", "UpdatingMemory",
                                   "MultiRoundTurn", "MultiRoundToolCall",
                                   "MultiRoundToolResult", "MultiRoundCompleted"):
                    continue

                # 处理响应内容
                if chunk.event == "RunResponse":
                    if hasattr(chunk, 'reasoning_content') and chunk.reasoning_content:
                        reasoning_content += chunk.reasoning_content
                        if on_thinking:
                            await on_thinking(chunk.reasoning_content)

                    if chunk.content:
                        full_content += chunk.content
                        if on_content:
                            await on_content(chunk.content)

            return ChatResult(
                content=full_content.strip(),
                tool_calls=tool_calls,
                session_id=session_id,
                user_id=user_id,
                tools_used=tools_used,
                reasoning=reasoning_content,
                metrics=self._extract_metrics(agent),
            )

        except (asyncio.CancelledError, AgentCancelledError, KeyboardInterrupt):
            logger.info(f"AgentService stream cancelled, session={session_id}")
            raise

        except Exception as e:
            logger.error(f"AgentService stream error: {e}")
            return ChatResult(
                content=f"Error: {e}",
                tool_calls=0,
                session_id=session_id,
                user_id=user_id,
            )

    def list_sessions(self) -> List[str]:
        """列出所有会话"""
        return []

    def delete_session(self, session_id: str) -> bool:
        """删除会话（清空对话后调用）"""
        return True

    def clear_session(self, session_id: str) -> bool:
        """清除会话历史"""
        return self.delete_session(session_id)

    async def save_memory(self, content: str, user_id: str = "default", long_term: bool = False):
        """保存记忆到 Workspace"""
        await self._ensure_initialized()

        if self._workspace and self._workspace.exists():
            await asyncio.to_thread(self._workspace.set_user, user_id)
            if long_term:
                await asyncio.to_thread(self._workspace.write_memory, content)
            else:
                await asyncio.to_thread(self._workspace.write_memory, content, True)
            logger.debug(f"Memory saved for user {user_id}: {content[:50]}...")

    async def get_memory(self, user_id: str = "default", days: int = 7) -> str:
        """获取记忆"""
        await self._ensure_initialized()

        if self._workspace and self._workspace.exists():
            await asyncio.to_thread(self._workspace.set_user, user_id)
            return await asyncio.to_thread(self._workspace.get_memory_prompt, days=days) or ""
        return ""

    async def get_workspace_context(self, user_id: str = "default") -> str:
        """获取工作空间上下文"""
        await self._ensure_initialized()

        if self._workspace and self._workspace.exists():
            await asyncio.to_thread(self._workspace.set_user, user_id)
            return await asyncio.to_thread(self._workspace.get_context_prompt) or ""
        return ""

    async def list_users(self) -> List[str]:
        """列出所有用户"""
        await self._ensure_initialized()

        if self._workspace:
            return await asyncio.to_thread(self._workspace.list_users)
        return []

    async def get_user_info(self, user_id: str) -> dict:
        """获取用户信息"""
        await self._ensure_initialized()

        if self._workspace:
            return await asyncio.to_thread(self._workspace.get_user_info, user_id=user_id)
        return {"user_id": user_id}

    def update_work_dir(self, new_dir: str) -> None:
        """运行时更新 work_dir，需要重建所有 Agent 实例中的内置工具

        新 Agent API 中 work_dir 在 BuiltinFileTool 等工具初始化时传入，
        运行时切换需要清除所有已有 Agent 实例，下次请求时按需重建。
        """
        self._agents.clear()
        logger.info(f"work_dir updated to: {new_dir}, all agent instances cleared")

    async def reload_model(self, model_provider: str, model_name: str) -> None:
        """运行时切换模型（Lock 保护，防止并发竞态）"""
        async with self._init_lock:
            self.model_provider = model_provider
            self.model_name = model_name
            self._initialized = False
            self._agents.clear()
            logger.info(f"Model reloaded: {model_provider}/{model_name}")

    async def add_tool(self, tool: Any) -> None:
        """动态添加工具"""
        async with self._init_lock:
            self.extra_tools.append(tool)
            self._initialized = False
            self._agents.clear()

    def add_instruction(self, instruction: str) -> None:
        """动态添加指令"""
        self.extra_instructions.append(instruction)
        for agent in self._agents.values():
            agent.add_instruction(instruction)

    @property
    def workspace(self) -> Optional[Workspace]:
        """获取工作空间实例（同步属性，调用前需确保已初始化）"""
        return self._workspace

    @property
    def agent(self) -> Optional[Agent]:
        """获取默认 Agent 实例（兼容旧接口，返回任意一个已创建的 Agent）"""
        if self._agents:
            return next(iter(self._agents.values()))
        return None
