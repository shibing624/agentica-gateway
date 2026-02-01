"""Example daemon integration showing how to wire everything together."""
import asyncio
import logging
from pathlib import Path
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.scheduler import (
    SchedulerService,
    TaskParser,
    init_scheduler_tools,
    ALL_SCHEDULER_TOOLS,
    TOOL_IMPLEMENTATIONS,
)
from src.scheduler.executor import TaskExecutor, SimpleAgentRunner, MultiChannelNotificationSender

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """Example daemon startup."""

    # 1. Initialize components
    logger.info("Initializing scheduler components...")

    # Task parser (with optional LLM client for complex parsing)
    # llm_client = openai.AsyncOpenAI()  # Uncomment with your client
    task_parser = TaskParser(llm_client=None)

    # Task executor (with your agent runner and notification sender)
    agent_runner = SimpleAgentRunner(llm_client=None)
    notification_sender = MultiChannelNotificationSender(
        telegram_bot=None,  # Your telegram bot instance
        discord_client=None,
        slack_client=None,
    )
    executor = TaskExecutor(
        agent_runner=agent_runner,
        notification_sender=notification_sender,
    )

    # Scheduler service
    scheduler_service = SchedulerService(
        db_path=Path("~/.agentica/scheduler.db"),
        executor=executor.execute,
    )

    # 2. Initialize tools for agent framework
    init_scheduler_tools(scheduler_service, task_parser)

    # 3. Start scheduler
    await scheduler_service.start()
    logger.info("Scheduler service started!")

    # 4. Example: Create a task programmatically
    from scheduler.tools import create_scheduled_task_tool

    result = await create_scheduled_task_tool(
        task_description="每天早上9点提醒我查看新闻",
        user_id="user_123",
        notify_channel="telegram",
        notify_chat_id="12345678",
    )
    logger.info(f"Created task: {result}")

    # 5. List tasks
    from scheduler.tools import list_scheduled_tasks_tool

    tasks = await list_scheduled_tasks_tool(user_id="user_123")
    logger.info(f"User tasks: {tasks}")

    # 6. Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await scheduler_service.stop()


def register_tools_with_agent(agent_framework):
    """Example: Register scheduler tools with your agent framework.

    This shows how to integrate with different agent frameworks.
    """

    # For OpenAI function calling style
    tools = [
        {"type": "function", "function": tool}
        for tool in ALL_SCHEDULER_TOOLS
    ]

    # For frameworks that use tool handlers
    async def handle_tool_call(tool_name: str, arguments: dict):
        if tool_name in TOOL_IMPLEMENTATIONS:
            return await TOOL_IMPLEMENTATIONS[tool_name](**arguments)
        raise ValueError(f"Unknown tool: {tool_name}")

    return tools, handle_tool_call


if __name__ == "__main__":
    asyncio.run(main())
