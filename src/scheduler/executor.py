"""Task executor for running scheduled agent tasks.

Refactored to work with the new ScheduledJob model and support:
- Session target modes (main/isolated)
- Dependency injection callbacks (on_system_event, run_heartbeat)
- Task chains
"""
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol, cast

from loguru import logger

from .models import ScheduledJob
from .types import (
    AgentTurnPayload,
    SystemEventPayload,
    WebhookPayload,
    PayloadKind,
    SessionTarget,
    SessionTargetKind,
)

logger = logger.bind(module="scheduler.executor")


# ============== Protocol Definitions ==============

class AgentRunner(Protocol):
    """Protocol for agent execution (isolated mode)."""

    async def run(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Run agent with a prompt and return the result."""
        ...


class NotificationSender(Protocol):
    """Protocol for sending notifications."""

    async def send(
        self,
        channel: str,
        chat_id: str,
        message: str,
    ) -> bool:
        """Send a notification and return success status."""
        ...


# Type aliases for dependency injection callbacks
OnSystemEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
RunHeartbeatCallback = Callable[[str], Awaitable[None]]
ReportToMainCallback = Callable[[str, str, str], Awaitable[None]]


class JobExecutor:
    """Executes scheduled jobs by running agents or sending notifications.

    Supports two execution modes:
    - main: Inject systemEvent into user's main session, trigger heartbeat
    - isolated: Run in independent agent session, report result back

    This is the bridge between the scheduler and your agent system.
    """

    def __init__(
        self,
        agent_runner: AgentRunner | None = None,
        notification_sender: NotificationSender | None = None,
        # Dependency injection callbacks for main mode
        on_system_event: OnSystemEventCallback | None = None,
        run_heartbeat: RunHeartbeatCallback | None = None,
        report_to_main: ReportToMainCallback | None = None,
    ):
        """Initialize executor with runners and callbacks.

        Args:
            agent_runner: Implementation for running agent tasks (isolated mode)
            notification_sender: Implementation for sending notifications
            on_system_event: Callback to inject system event into main session
            run_heartbeat: Callback to trigger heartbeat in main session
            report_to_main: Callback to report isolated execution result to main session
        """
        self.agent_runner = agent_runner
        self.notification_sender = notification_sender
        self.on_system_event = on_system_event
        self.run_heartbeat = run_heartbeat
        self.report_to_main = report_to_main

    async def execute(
        self,
        job: ScheduledJob,
        target: SessionTarget | None = None,
    ) -> str:
        """Execute a scheduled job.

        Args:
            job: The job to execute
            target: Session target (main/isolated), defaults to isolated

        Returns:
            Execution result message
        """
        logger.info(f"Executing job {job.id}: {job.name}")
        target = target or SessionTarget()

        try:
            result = ""

            # Dispatch based on target mode
            if target.kind == SessionTargetKind.MAIN:
                result = await self._execute_main_mode(job, target)
            else:
                result = await self._execute_isolated_mode(job, target)

            return result

        except Exception as e:
            error_msg = f"Job execution failed: {e}"
            logger.error(error_msg)

            # Try to notify about failure
            if isinstance(job.payload, AgentTurnPayload) and job.payload.notify_chat_id:
                await self._send_error_notification(job, job.payload, str(e))

            raise

    async def _execute_main_mode(
        self,
        job: ScheduledJob,
        target: SessionTarget,
    ) -> str:
        """Execute job in main session mode.
        
        Injects systemEvent into user's active main session and triggers heartbeat.
        """
        if not self.on_system_event:
            raise RuntimeError("on_system_event callback not configured for main mode")

        # Build system event payload
        event_data = {
            "type": "scheduled_task",
            "job_id": job.id,
            "job_name": job.name,
            "payload": job.payload.to_dict(),
            "timestamp_ms": int(datetime.now().timestamp() * 1000),
        }

        # Inject system event into main session
        await self.on_system_event(job.user_id, event_data)
        logger.info(f"Injected system event for job {job.id} to user {job.user_id}")

        # Trigger heartbeat if configured
        if target.trigger_heartbeat and self.run_heartbeat:
            await self.run_heartbeat(job.user_id)
            logger.info(f"Triggered heartbeat for user {job.user_id}")

        return "Injected to main session"

    async def _execute_isolated_mode(
        self,
        job: ScheduledJob,
        target: SessionTarget,
    ) -> str:
        """Execute job in isolated agent session mode."""
        result = ""

        # Dispatch based on payload type
        payload = job.payload
        payload_kind = payload.kind if hasattr(payload, "kind") else "agent_turn"

        if payload_kind == PayloadKind.AGENT_TURN.value or isinstance(payload, AgentTurnPayload):
            result = await self._execute_agent_task(job, cast(AgentTurnPayload, payload))
        elif payload_kind == PayloadKind.SYSTEM_EVENT.value or isinstance(payload, SystemEventPayload):
            result = await self._execute_notification(job, cast(SystemEventPayload, payload))
        elif payload_kind == PayloadKind.WEBHOOK.value or isinstance(payload, WebhookPayload):
            result = await self._execute_webhook(job, cast(WebhookPayload, payload))
        else:
            result = f"Unknown payload type: {payload_kind}"

        # Report result to main session if configured
        if target.report_to_main and self.report_to_main:
            await self.report_to_main(job.user_id, job.id, result)
            logger.info(f"Reported result to main session for user {job.user_id}")

        # Send result notification if configured
        if isinstance(payload, AgentTurnPayload) and payload.notify_chat_id:
            await self._send_result_notification(job, payload, result)

        return result

    async def _execute_agent_task(
        self,
        job: ScheduledJob,
        payload: AgentTurnPayload,
    ) -> str:
        """Execute an agent-run task."""
        if not self.agent_runner:
            raise RuntimeError("No agent runner configured")

        # Build context for agent
        context = {
            "job_id": job.id,
            "scheduled": True,
            "original_prompt": job.description,
            **(payload.context or {}),
        }

        # Run the agent with the prompt
        result = await self.agent_runner.run(
            prompt=payload.prompt,
            context=context,
        )

        return result

    async def _execute_notification(
        self,
        job: ScheduledJob,  # noqa: ARG002
        payload: SystemEventPayload,
    ) -> str:
        """Execute a simple notification task."""
        if not self.notification_sender:
            raise RuntimeError("No notification sender configured")

        message = payload.message
        success = await self.notification_sender.send(
            channel=payload.channel,
            chat_id=payload.chat_id,
            message=f"⏰ 提醒：{message}",
        )

        return "Notification sent" if success else "Notification failed"

    async def _execute_webhook(
        self,
        job: ScheduledJob,
        payload: WebhookPayload,
    ) -> str:
        """Execute a webhook task."""
        import aiohttp

        if not payload.url:
            raise ValueError("No webhook URL configured")

        request_payload = {
            "job_id": job.id,
            "name": job.name,
            "timestamp": datetime.now().isoformat(),
            **(payload.body or {}),
        }

        timeout = aiohttp.ClientTimeout(total=payload.timeout_seconds)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            method = payload.method.upper()
            headers = payload.headers or {}

            if method == "GET":
                async with session.get(payload.url, headers=headers) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"Webhook failed with status {resp.status}")
                    return f"Webhook GET: {resp.status}"
            elif method == "POST":
                async with session.post(payload.url, json=request_payload, headers=headers) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"Webhook failed with status {resp.status}")
                    return f"Webhook POST: {resp.status}"
            elif method == "PUT":
                async with session.put(payload.url, json=request_payload, headers=headers) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"Webhook failed with status {resp.status}")
                    return f"Webhook PUT: {resp.status}"
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

    async def _send_result_notification(
        self,
        job: ScheduledJob,
        payload: AgentTurnPayload,
        result: str,
    ) -> None:
        """Send task result notification."""
        if not self.notification_sender:
            return

        message = f"✅ 定时任务完成\n任务：{job.name}\n结果：{result[:500]}"

        try:
            await self.notification_sender.send(
                channel=payload.notify_channel,
                chat_id=payload.notify_chat_id,
                message=message,
            )
        except Exception as e:
            logger.error(f"Failed to send result notification: {e}")

    async def _send_error_notification(
        self,
        job: ScheduledJob,
        payload: AgentTurnPayload,
        error: str,
    ) -> None:
        """Send task error notification."""
        if not self.notification_sender:
            return

        message = f"❌ 定时任务失败\n任务：{job.name}\n错误：{error[:200]}"

        try:
            await self.notification_sender.send(
                channel=payload.notify_channel,
                chat_id=payload.notify_chat_id,
                message=message,
            )
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")


# ============== Example Implementations ==============

class SimpleAgentRunner:
    """Simple agent runner implementation for testing."""

    def __init__(self, llm_client: Any = None):
        self.llm_client = llm_client

    async def run(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> str:
        """Run a simple prompt through LLM."""
        if not self.llm_client:
            # Mock response for testing
            return f"[Mock] Executed: {prompt}"

        # Real LLM call
        response = await self.llm_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""


class MultiChannelNotificationSender:
    """Notification sender supporting multiple channels."""

    def __init__(
        self,
        telegram_bot: Any = None,
        discord_client: Any = None,
        slack_client: Any = None,
    ):
        self.telegram_bot = telegram_bot
        self.discord_client = discord_client
        self.slack_client = slack_client

    async def send(
        self,
        channel: str,
        chat_id: str,
        message: str,
    ) -> bool:
        """Send notification to specified channel."""
        try:
            if channel == "telegram" and self.telegram_bot:
                await self.telegram_bot.send_message(
                    chat_id=chat_id,
                    text=message,
                )
                return True

            elif channel == "discord" and self.discord_client:
                discord_channel = await self.discord_client.fetch_channel(int(chat_id))
                await discord_channel.send(message)
                return True

            elif channel == "slack" and self.slack_client:
                await self.slack_client.chat_postMessage(
                    channel=chat_id,
                    text=message,
                )
                return True

            else:
                logger.warning(f"Unknown channel or not configured: {channel}")
                return False

        except Exception as e:
            logger.error(f"Failed to send to {channel}: {e}")
            return False


# ============== Legacy Compatibility ==============
# Keep TaskExecutor as alias

TaskExecutor = JobExecutor
