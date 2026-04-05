"""Agent service - wraps the agentica SDK.

Key design decisions:
- LRU cache for Agent instances (bounded by settings.agent_max_sessions)
- Per-session work_dir stored separately from global settings
- Fail fast on initialization errors (no silent mock mode)
- cancel_session(session_id) for precise stream cancellation
- Agent build timeout to guard against SDK hangs
- Uses DeepAgent (batteries-included) instead of manual Agent + builtin tools
"""
import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, List, Any, Dict

from loguru import logger
from agentica import DeepAgent
from agentica.run_response import AgentCancelledError
from agentica.run_config import RunConfig
from agentica.workspace import Workspace

from ..config import settings

logger = logger.bind(module="agent_service")

# Timeout in seconds for building a new Agent instance (guards against SDK hangs)
_AGENT_BUILD_TIMEOUT_S = 30


@dataclass
class ChatResult:
    """Chat response from the agent."""
    content: str
    tool_calls: int = 0
    session_id: str = ""
    user_id: str = ""
    tools_used: List[str] = field(default_factory=list)
    reasoning: str = ""
    metrics: Optional[Dict[str, Any]] = None


class LRUAgentCache:
    """Thread-unsafe but asyncio-safe LRU cache for DeepAgent instances."""

    def __init__(self, max_size: int = 50):
        self._cache: OrderedDict[str, DeepAgent] = OrderedDict()
        self.max_size = max_size

    def get(self, session_id: str) -> Optional[DeepAgent]:
        if session_id not in self._cache:
            return None
        self._cache.move_to_end(session_id)
        return self._cache[session_id]

    def put(self, session_id: str, agent: DeepAgent) -> None:
        if session_id in self._cache:
            self._cache.move_to_end(session_id)
            self._cache[session_id] = agent
            return
        self._cache[session_id] = agent
        if len(self._cache) > self.max_size:
            evicted_id, _ = self._cache.popitem(last=False)
            logger.debug(f"LRU evicted agent for session: {evicted_id}")

    def delete(self, session_id: str) -> bool:
        if session_id in self._cache:
            del self._cache[session_id]
            return True
        return False

    def clear(self) -> None:
        self._cache.clear()

    def keys(self) -> List[str]:
        return list(self._cache.keys())

    def __len__(self) -> int:
        return len(self._cache)


class AgentService:
    """Agent service wrapping the agentica SDK.

    Features:
    - Workspace config layer (AGENT.md, PERSONA.md, MEMORY.md, etc.)
    - Session history management (per session_id)
    - LRU-bounded Agent instance cache (evicts on overflow)
    - Per-session working directory
    - Scheduler tool integration
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

        self._cache = LRUAgentCache(max_size=settings.agent_max_sessions)
        # Per-session work_dir overrides; falls back to settings.base_dir
        self._session_work_dirs: Dict[str, str] = {}
        self._workspace: Optional[Workspace] = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    # ============== Initialization ==============

    async def _ensure_initialized(self) -> None:
        """Ensure the workspace is initialized (idempotent, Lock-protected)."""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._do_initialize)

    def _do_initialize(self) -> None:
        """Initialize the shared Workspace (sync, runs in thread).

        Raises RuntimeError on failure — callers must handle this explicitly.
        No silent mock mode.
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
            logger.error(
                f"AgentService initialization failed: {e}\n"
                f"Check your API key, model provider, and agentica version."
            )
            raise RuntimeError(f"AgentService init failed: {e}") from e

    def _build_agent(self) -> DeepAgent:
        """Build a new DeepAgent instance (sync, runs in thread).

        DeepAgent auto-includes: builtin tools, skills, agentic prompt,
        compression, workspace memory, conversation archive.
        Only extra tools and scheduler need manual addition.
        """
        model = self._create_model()
        work_dir = str(settings.base_dir)

        # Extra tools: user-provided + scheduler
        extra = list(self.extra_tools)
        scheduler_tools = self._get_scheduler_tools()
        extra.extend(scheduler_tools)

        instructions = list(self.extra_instructions) if self.extra_instructions else None
        if scheduler_tools:
            if instructions is None:
                instructions = []
            instructions.append(self._get_scheduler_instructions())

        agent = DeepAgent(
            model=model,
            tools=extra if extra else None,
            workspace=self._workspace,
            work_dir=work_dir,
            history_window=14,
            instructions=instructions,
            debug=settings.debug,
        )

        tool_count = len(agent.tools) if agent.tools else 0
        logger.info(f"DeepAgent built: {tool_count} tools (extra={len(extra)}, scheduler={len(scheduler_tools)})")
        return agent

    async def _get_agent(self, session_id: str) -> DeepAgent:
        """Return the cached Agent for a session, creating one if absent.

        Raises RuntimeError if the agent cannot be built (e.g. SDK error).
        Times out after _AGENT_BUILD_TIMEOUT_S seconds.
        """
        agent = self._cache.get(session_id)
        if agent is not None:
            return agent

        try:
            agent = await asyncio.wait_for(
                asyncio.to_thread(self._build_agent),
                timeout=_AGENT_BUILD_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Agent build timed out after {_AGENT_BUILD_TIMEOUT_S}s "
                f"for session {session_id}. Check MCP server connectivity."
            )

        self._cache.put(session_id, agent)
        logger.info(f"Agent created for session: {session_id} (cache size: {len(self._cache)})")
        return agent

    # ============== Model factory ==============

    def _create_model(self) -> Any:
        """Instantiate the configured LLM model."""
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
        elif self.model_provider == "doubao":
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

    # ============== Scheduler tools ==============

    def _get_scheduler_tools(self) -> List[Any]:
        """Load scheduler tools (returns empty list on failure)."""
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

    # ============== Metrics helpers ==============

    @staticmethod
    def _extract_metrics(agent: Optional[DeepAgent]) -> Optional[Dict[str, Any]]:
        """Extract metrics from the agent's last run_response."""
        if not agent:
            return None
        if agent.run_response and agent.run_response.metrics:
            return agent.run_response.metrics
        return None

    @staticmethod
    def _format_tool_call_args(tool_name: str, tool_args: dict) -> dict:
        """Format tool call arguments for frontend display.

        Computes diff metadata for file-editing tools; truncates others.
        """
        display_args: dict = {}

        if tool_name == "edit_file":
            old_s = tool_args.get("old_string", "")
            new_s = tool_args.get("new_string", "")
            display_args["_diff_add"] = new_s.count("\n") + (1 if new_s else 0)
            display_args["_diff_del"] = old_s.count("\n") + (1 if old_s else 0)
            fp = tool_args.get("file_path") or tool_args.get("file") or tool_args.get("path", "")
            if fp:
                display_args["file_path"] = fp

        elif tool_name == "multi_edit_file":
            edits = tool_args.get("edits", [])
            total_add = total_del = 0
            for ed in (edits if isinstance(edits, list) else []):
                old_s = ed.get("old_string", "")
                new_s = ed.get("new_string", "")
                total_del += old_s.count("\n") + (1 if old_s else 0)
                total_add += new_s.count("\n") + (1 if new_s else 0)
            display_args["_diff_add"] = total_add
            display_args["_diff_del"] = total_del
            display_args["_edit_count"] = len(edits) if isinstance(edits, list) else 0
            fp = tool_args.get("file_path") or tool_args.get("file") or tool_args.get("path", "")
            if fp:
                display_args["file_path"] = fp

        elif tool_name == "write_file":
            content = tool_args.get("content", "")
            display_args["_lines"] = content.count("\n") + (1 if content else 0)
            fp = tool_args.get("file_path") or tool_args.get("file") or tool_args.get("path", "")
            if fp:
                display_args["file_path"] = fp

        else:
            for k, v in tool_args.items():
                if isinstance(v, str) and len(v) > 100:
                    display_args[k] = v[:100] + "..."
                else:
                    display_args[k] = v

        return display_args

    @staticmethod
    def _format_tool_result(tool_info: dict) -> tuple[str, str, bool]:
        """Format tool result for frontend display.

        Returns:
            (tool_name, result_str, is_task_meta)
        """
        t_name = tool_info.get("tool_name") or tool_info.get("name", "unknown")
        t_content = tool_info.get("content", "")
        is_error = tool_info.get("tool_call_error", False)

        # task tool: parse subagent JSON and produce structured metadata
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

    # ============== Public API ==============

    async def chat(
        self,
        message: str,
        session_id: str,
        user_id: str = "default",
    ) -> ChatResult:
        """Send a message and return the full response (non-streaming).

        Args:
            message: User message
            session_id: Session identifier
            user_id: User identifier (for workspace memory isolation)

        Returns:
            ChatResult with content, tool_calls, metrics
        """
        await self._ensure_initialized()
        agent = await self._get_agent(session_id)

        try:
            if self._workspace:
                await asyncio.to_thread(self._workspace.set_user, user_id)

            response = await agent.run(message)

            content = (response.content or "").strip()
            tools_used: List[str] = []
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
            logger.error(f"AgentService.chat error (session={session_id}): {e}")
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
        """Send a message and stream the response via callbacks.

        Args:
            message: User message
            session_id: Session identifier
            user_id: User identifier
            on_content: Called with each content delta
            on_tool_call: Called when a tool call starts (name, args)
            on_tool_result: Called when a tool call completes (name, result)
            on_thinking: Called with each reasoning delta

        Returns:
            ChatResult with accumulated content + metrics
        """
        await self._ensure_initialized()
        agent = await self._get_agent(session_id)

        try:
            if self._workspace:
                await asyncio.to_thread(self._workspace.set_user, user_id)

            full_content = ""
            reasoning_content = ""
            tools_used: List[str] = []
            tool_calls = 0

            async for chunk in agent.run_stream(message, config=RunConfig(stream_intermediate_steps=True)):
                if chunk is None:
                    continue

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

                elif chunk.event == "ToolCallCompleted":
                    if chunk.tools and on_tool_result:
                        for ti in reversed(chunk.tools):
                            if "content" in ti:
                                t_name, result_str, _ = self._format_tool_result(ti)
                                await on_tool_result(t_name, result_str)
                                break
                    continue

                if chunk.event in (
                    "RunStarted", "RunCompleted", "UpdatingMemory",
                    "MultiRoundTurn", "MultiRoundToolCall",
                    "MultiRoundToolResult", "MultiRoundCompleted",
                ):
                    continue

                if chunk.event == "RunResponse":
                    if hasattr(chunk, "reasoning_content") and chunk.reasoning_content:
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
            logger.info(f"AgentService stream cancelled (session={session_id})")
            raise

        except Exception as e:
            logger.error(f"AgentService.chat_stream error (session={session_id}): {e}")
            return ChatResult(
                content=f"Error: {e}",
                tool_calls=0,
                session_id=session_id,
                user_id=user_id,
            )

    # ============== Session management ==============

    def list_sessions(self) -> List[str]:
        """List all active session IDs."""
        return self._cache.keys()

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its cached Agent instance.

        Returns True if the session existed, False otherwise.
        """
        removed = self._cache.delete(session_id)
        self._session_work_dirs.pop(session_id, None)
        if removed:
            logger.debug(f"Session deleted: {session_id}")
        return removed

    def clear_session(self, session_id: str) -> bool:
        """Alias for delete_session (for compatibility)."""
        return self.delete_session(session_id)

    def cancel_session(self, session_id: str) -> bool:
        """Cancel the in-flight run for a specific session.

        Returns True if the session has an agent to cancel, False otherwise.
        """
        agent = self._cache.get(session_id)
        if agent is None:
            return False
        try:
            agent.cancel()
            logger.debug(f"Cancelled agent for session: {session_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to cancel session {session_id}: {e}")
            return False

    # ============== Work directory ==============

    def set_session_work_dir(self, session_id: str, work_dir: str) -> None:
        """Set the working directory for a specific session.

        Per-session work_dirs override the global settings.base_dir.
        Does NOT clear other sessions' agents.
        """
        self._session_work_dirs[session_id] = work_dir

    def get_session_work_dir(self, session_id: str) -> str:
        """Get the working directory for a session (falls back to global base_dir)."""
        return self._session_work_dirs.get(session_id, str(settings.base_dir))

    def update_work_dir(self, new_dir: str) -> None:
        """Update the global work_dir and clear ALL cached agents.

        Called when the user changes the global working directory via the UI.
        All agents must be rebuilt to pick up the new directory.
        """
        self._cache.clear()
        self._session_work_dirs.clear()
        logger.info(f"Global work_dir updated to: {new_dir}, all agent instances cleared")

    # ============== Memory ==============

    async def save_memory(self, content: str, user_id: str = "default", long_term: bool = False) -> None:
        """Persist content to Workspace memory."""
        await self._ensure_initialized()
        if self._workspace and self._workspace.exists():
            await asyncio.to_thread(self._workspace.set_user, user_id)
            await self._workspace.write_memory(content)
            logger.debug(f"Memory saved for user {user_id}: {content[:50]}...")

    async def get_memory(self, user_id: str = "default", query: str = "", limit: int = 5) -> str:
        """Retrieve memory for a user via search_memory (keyword/bigram matching).

        Args:
            user_id: User identifier
            query: Search query (empty returns recent entries)
            limit: Maximum number of entries
        """
        await self._ensure_initialized()
        if self._workspace and self._workspace.exists():
            await asyncio.to_thread(self._workspace.set_user, user_id)
            results = self._workspace.search_memory(query=query, limit=limit)
            if results:
                return "\n\n".join(
                    f"**{r.get('title', 'Memory')}**: {r.get('content', '')}"
                    for r in results
                )
        return ""

    async def get_workspace_context(self, user_id: str = "default") -> str:
        """Retrieve workspace context prompt for a user."""
        await self._ensure_initialized()
        if self._workspace and self._workspace.exists():
            await asyncio.to_thread(self._workspace.set_user, user_id)
            return await self._workspace.get_context_prompt() or ""
        return ""

    async def list_users(self) -> List[str]:
        """List all known users from Workspace."""
        await self._ensure_initialized()
        if self._workspace:
            return await asyncio.to_thread(self._workspace.list_users)
        return []

    async def get_user_info(self, user_id: str) -> dict:
        """Get workspace user info."""
        await self._ensure_initialized()
        if self._workspace:
            return await asyncio.to_thread(self._workspace.get_user_info, user_id=user_id)
        return {"user_id": user_id}

    # ============== Hot reload ==============

    async def reload_model(self, model_provider: str, model_name: str) -> None:
        """Switch model at runtime; clears all cached agents."""
        async with self._init_lock:
            self.model_provider = model_provider
            self.model_name = model_name
            self._initialized = False
            self._cache.clear()
            logger.info(f"Model reloaded: {model_provider}/{model_name}")

    async def add_tool(self, tool: Any) -> None:
        """Dynamically add a tool; clears agent cache to force rebuild."""
        async with self._init_lock:
            self.extra_tools.append(tool)
            self._initialized = False
            self._cache.clear()

    def add_instruction(self, instruction: str) -> None:
        """Append an instruction to all existing agents."""
        self.extra_instructions.append(instruction)
        for session_id in self._cache.keys():
            agent = self._cache.get(session_id)
            if agent:
                agent.add_instruction(instruction)

    # ============== Properties ==============

    @property
    def workspace(self) -> Optional[Workspace]:
        """Shared Workspace instance (synchronous; call after initialization)."""
        return self._workspace

    @property
    def agent(self) -> Optional[DeepAgent]:
        """Deprecated: returns an arbitrary cached DeepAgent.

        Prefer cancel_session(session_id) for targeted cancellation.
        """
        sessions = self._cache.keys()
        if sessions:
            return self._cache.get(sessions[0])
        return None
