"""Agent 服务 - 封装 agentica SDK

提供功能完善的 DeepAgent 服务，包括：
- Workspace 配置层（静态配置 + 持久记忆）
- 会话历史管理（按 session_id 隔离）
- 工具调用显示
- 调度器工具集成（定时任务）
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, List, Any

from loguru import logger

from agentica import DeepAgent
from agentica.db import SqliteDb
from agentica.workspace import Workspace

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


class AgentService:
    """Agent 服务

    封装 agentica SDK，提供统一的 Agent 调用接口：
    - Workspace 配置层（AGENT.md, PERSONA.md, MEMORY.md 等）
    - 会话历史管理（数据库存储，按 session_id 隔离）
    - 多用户支持（按 user_id 隔离 Workspace 记忆）
    - 调度器工具集成
    
    架构设计：
    ┌─────────────────────────────────────────────────────┐
    │                    Agent                            │
    ├─────────────────────────────────────────────────────┤
    │  Workspace (配置层 - 按 user_id 隔离)                │
    │  ├── AGENT.md, PERSONA.md, TOOLS.md → 全局共享      │
    │  └── users/{user_id}/                              │
    │      ├── USER.md                    → 用户配置      │
    │      ├── MEMORY.md                  → 长期记忆      │
    │      └── memory/                    → 日记忆        │
    ├─────────────────────────────────────────────────────┤
    │  SqliteDb (运行时层 - 按 session_id 隔离)            │
    │  └── 会话历史 (messages, runs)                      │
    └─────────────────────────────────────────────────────┘
    """

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        model_name: Optional[str] = None,
        model_provider: Optional[str] = None,
        extra_tools: Optional[List[Any]] = None,
        extra_instructions: Optional[List[str]] = None,
    ):
        """初始化 Agent 服务

        Args:
            workspace_path: 工作空间路径
            model_name: 模型名称
            model_provider: 模型提供商
            extra_tools: 额外的工具列表
            extra_instructions: 额外的指令列表
        """
        self.workspace_path = Path(workspace_path or settings.workspace_path).expanduser()
        self.model_name = model_name or settings.model_name
        self.model_provider = model_provider or settings.model_provider
        self.extra_tools = extra_tools or []
        self.extra_instructions = extra_instructions or []

        # 延迟初始化
        self._agent: Optional[DeepAgent] = None
        self._workspace: Optional[Workspace] = None
        self._db: Optional[SqliteDb] = None
        self._initialized = False

    def _ensure_initialized(self):
        """确保已初始化"""
        if self._initialized:
            return

        try:
            # 初始化工作空间（配置层，user_id 在调用时动态设置）
            self._workspace = Workspace(self.workspace_path)
            if not self._workspace.exists():
                self._workspace.initialize()
                logger.info(f"Workspace initialized at {self.workspace_path}")

            # 初始化数据库（会话历史存储）
            db_path = self.workspace_path.parent / "agent_sessions.db"
            self._db = SqliteDb(db_file=str(db_path))
            logger.info(f"Session database: {db_path}")

            # 创建模型
            model = self._create_model()

            # 收集所有工具（包括调度器工具）
            all_tools = list(self.extra_tools) if self.extra_tools else []
            scheduler_tools = self._get_scheduler_tools()
            all_tools.extend(scheduler_tools)

            # 构建指令
            instructions = list(self.extra_instructions) if self.extra_instructions else []

            # 添加调度器工具说明
            if scheduler_tools:
                instructions.append(self._get_scheduler_instructions())

            # 创建 DeepAgent
            self._agent = DeepAgent(
                model=model,
                # 数据库配置（会话历史存储）
                db=self._db,
                # 工作空间配置（配置层）
                workspace=self._workspace,
                load_workspace_context=True,
                load_workspace_memory=True,
                memory_days=7,
                # 历史记录配置
                add_history_to_messages=True,
                num_history_responses=4,
                # 工具配置
                tools=all_tools if all_tools else None,
                show_tool_calls=True,
                tool_call_limit=40,
                # 指令
                instructions=instructions if instructions else None,
                add_datetime_to_instructions=True,
                auto_load_mcp=True,
                run_timeout=600,
                # 调试
                debug_mode=settings.debug,
            )

            self._initialized = True
            logger.info("AgentService initialized successfully")
            logger.info(f"Model: {self.model_provider}/{self.model_name}")
            logger.info(f"Workspace: {self.workspace_path}")
            if all_tools:
                logger.info(f"Tools loaded: {len(all_tools)}")

        except Exception as e:
            logger.error(f"AgentService init error: {e}")
            logger.warning("Running in mock mode")
            self._initialized = True

    def _create_model(self) -> Any:
        """创建模型实例"""
        params = {"id": self.model_name, "timeout": 300}

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
        elif self.model_provider == "azure":
            from agentica import AzureOpenAIChat
            return AzureOpenAIChat(**params)
        else:
            # 默认使用 OpenAI 兼容接口
            from agentica import OpenAIChat
            return OpenAIChat(**params)

    def _get_scheduler_tools(self) -> List[Any]:
        """获取调度器工具

        agentica 支持直接传递函数作为工具，会自动从 docstring 解析参数。
        """
        try:
            from ..scheduler import (
                create_scheduled_job_tool,
                list_scheduled_jobs_tool,
                delete_scheduled_job_tool,
                pause_scheduled_job_tool,
                resume_scheduled_job_tool,
                create_task_chain_tool,
            )

            # 直接返回工具函数列表
            # agentica 会自动从函数签名和 docstring 解析工具定义
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

    async def chat(
        self,
        message: str,
        session_id: str,
        user_id: str = "default",
    ) -> ChatResult:
        """处理聊天消息

        Args:
            message: 用户消息
            session_id: 会话ID（每个 channel 唯一，清空对话后生成新 uuid4）
            user_id: 用户ID（用于 Workspace 记忆隔离）

        Returns:
            聊天结果
        """
        self._ensure_initialized()

        if not self._agent:
            # Mock 模式
            return ChatResult(
                content=f"[Mock] Received: {message}",
                tool_calls=0,
                session_id=session_id,
                user_id=user_id,
            )

        try:
            # 设置 user_id 和 session_id（动态切换）
            self._agent.user_id = user_id
            self._agent.session_id = session_id
            # 同步到 Workspace
            if self._workspace:
                self._workspace.set_user(user_id)

            # 运行 Agent
            response = await self._agent.arun(message)

            # 提取结果
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
        on_thinking: Optional[Callable[[str], Any]] = None,
    ) -> ChatResult:
        """流式聊天

        Args:
            message: 用户消息
            session_id: 会话ID（每个 channel 唯一，uuid4）
            user_id: 用户ID（用于 Workspace 记忆隔离）
            on_content: 内容回调
            on_tool_call: 工具调用回调
            on_thinking: 思考过程回调

        Returns:
            聊天结果
        """
        self._ensure_initialized()

        if not self._agent:
            # Mock 模式
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
            # 设置 user_id 和 session_id（动态切换）
            self._agent.user_id = user_id
            self._agent.session_id = session_id
            # 同步到 Workspace
            if self._workspace:
                self._workspace.set_user(user_id)

            # 流式运行
            full_content = ""
            reasoning_content = ""
            tools_used = []
            tool_calls = 0
            shown_tool_count = 0

            async for chunk in self._agent.arun_stream(message):
                if chunk is None:
                    continue

                # 处理工具调用事件
                if chunk.event == "ToolCallStarted":
                    if chunk.tools and len(chunk.tools) > shown_tool_count:
                        new_tools = chunk.tools[shown_tool_count:]
                        for tool_info in new_tools:
                            tool_name = tool_info.get("tool_name") or tool_info.get("name", "unknown")
                            tool_args = tool_info.get("tool_args") or tool_info.get("arguments", {})
                            tools_used.append(tool_name)
                            tool_calls += 1
                            if on_tool_call:
                                await on_tool_call(tool_name, tool_args)
                        shown_tool_count = len(chunk.tools)
                    continue

                elif chunk.event == "ToolCallCompleted":
                    continue

                # 跳过其他中间事件
                if chunk.event in ("RunStarted", "RunCompleted", "UpdatingMemory",
                                   "MultiRoundTurn", "MultiRoundToolCall",
                                   "MultiRoundToolResult", "MultiRoundCompleted"):
                    continue

                # 处理响应内容
                if chunk.event == "RunResponse":
                    # 处理思考内容
                    if hasattr(chunk, 'reasoning_content') and chunk.reasoning_content:
                        reasoning_content += chunk.reasoning_content
                        if on_thinking:
                            await on_thinking(chunk.reasoning_content)

                    # 处理实际内容
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
            )

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
        self._ensure_initialized()

        if self._db:
            return self._db.get_all_session_ids()
        return []

    def delete_session(self, session_id: str) -> bool:
        """删除会话（清空对话后调用）"""
        self._ensure_initialized()

        if self._db:
            self._db.delete_session(session_id=session_id)
            return True
        return False

    def clear_session(self, session_id: str) -> bool:
        """清除会话历史

        Args:
            session_id: 会话ID

        Returns:
            是否成功
        """
        return self.delete_session(session_id)

    def save_memory(self, content: str, user_id: str = "default", long_term: bool = False):
        """保存记忆到 Workspace

        Args:
            content: 要保存的内容
            user_id: 用户ID
            long_term: 是否保存为长期记忆（MEMORY.md）
        """
        self._ensure_initialized()

        if self._workspace and self._workspace.exists():
            self._workspace.set_user(user_id)
            if long_term:
                self._workspace.write_memory(content)
            else:
                self._workspace.write_memory(content, to_daily=True)
            logger.debug(f"Memory saved for user {user_id}: {content[:50]}...")

    def get_memory(self, user_id: str = "default", days: int = 7) -> str:
        """获取记忆

        Args:
            user_id: 用户ID
            days: 获取最近多少天的记忆

        Returns:
            记忆内容
        """
        self._ensure_initialized()

        if self._workspace and self._workspace.exists():
            self._workspace.set_user(user_id)
            return self._workspace.get_memory_prompt(days=days) or ""
        return ""

    def get_workspace_context(self, user_id: str = "default") -> str:
        """获取工作空间上下文

        Args:
            user_id: 用户ID

        Returns:
            工作空间上下文内容（AGENT.md, PERSONA.md, USER.md 等）
        """
        self._ensure_initialized()

        if self._workspace and self._workspace.exists():
            self._workspace.set_user(user_id)
            return self._workspace.get_context_prompt() or ""
        return ""

    def list_users(self) -> List[str]:
        """列出所有用户"""
        self._ensure_initialized()

        if self._workspace:
            return self._workspace.list_users()
        return []

    def get_user_info(self, user_id: str) -> dict:
        """获取用户信息

        Args:
            user_id: 用户ID

        Returns:
            用户信息字典
        """
        self._ensure_initialized()

        if self._workspace:
            return self._workspace.get_user_info(user_id=user_id)
        return {"user_id": user_id}

    def add_tool(self, tool: Any) -> None:
        """动态添加工具

        Args:
            tool: 工具实例
        """
        self.extra_tools.append(tool)
        # 重新初始化以应用新工具
        self._initialized = False
        self._agent = None

    def add_instruction(self, instruction: str) -> None:
        """动态添加指令

        Args:
            instruction: 指令内容
        """
        self.extra_instructions.append(instruction)
        # 如果 agent 已初始化，直接添加
        if self._agent:
            self._agent.add_instruction(instruction)

    @property
    def workspace(self) -> Optional[Workspace]:
        """获取工作空间实例"""
        self._ensure_initialized()
        return self._workspace

    @property
    def db(self) -> Optional[SqliteDb]:
        """获取数据库实例"""
        self._ensure_initialized()
        return self._db

    @property
    def agent(self) -> Optional[DeepAgent]:
        """获取 Agent 实例"""
        self._ensure_initialized()
        return self._agent
