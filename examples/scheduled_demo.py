"""Scheduler demo showing SessionTarget modes and dependency injection callbacks.

This example demonstrates:
- SessionTarget: main (inject to main session) vs isolated (independent execution)
- Dependency injection callbacks: on_system_event, run_heartbeat, report_to_main
- JSON export for human-readable task viewing
- Various payload types: agent_turn, system_event, webhook
"""
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
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
from src.scheduler.executor import JobExecutor, SimpleAgentRunner, MultiChannelNotificationSender
from src.scheduler.models import JobCreate
from src.scheduler.types import (
    SessionTarget,
    SessionTargetKind,
    CronSchedule,
    AtSchedule,
    EverySchedule,
    AgentTurnPayload,
    SystemEventPayload,
    WebhookPayload,
)


# ============== Dependency Injection Callbacks ==============
# These callbacks integrate with your main application's session system

async def on_system_event(user_id: str, event_data: dict[str, Any]) -> None:
    """Inject system event into user's main session.
    
    In a real application, this would:
    - Find user's active session
    - Add event to session's systemEvent queue
    - Notify session of new event
    """
    logger.info(f"[Main Mode] Injecting event for user {user_id}: {event_data.get('job_name')}")
    # Real implementation: session_manager.inject_event(user_id, event_data)


async def run_heartbeat(user_id: str) -> None:
    """Trigger heartbeat in user's main session.
    
    This causes the agent to wake up and process the injected event.
    """
    logger.info(f"[Main Mode] Triggering heartbeat for user {user_id}")
    # Real implementation: session_manager.trigger_heartbeat(user_id)


async def report_to_main(user_id: str, job_id: str, result: str) -> None:
    """Report isolated execution result to main session.
    
    This allows the main session to know about background task completion.
    """
    logger.info(f"[Isolated Mode] Reporting result for user {user_id}, job {job_id}")
    logger.info(f"  Result: {result[:100]}...")
    # Real implementation: session_manager.add_notification(user_id, job_id, result)


# ============== Main Demo ==============

async def main():
    """Demonstrate scheduler with SessionTarget and callbacks."""
    
    logger.info("=" * 60)
    logger.info("Scheduler Demo: SessionTarget & Dependency Injection")
    logger.info("=" * 60)

    # 1. Initialize executor with both modes supported
    agent_runner = SimpleAgentRunner(llm_client=None)  # Mock for demo
    notification_sender = MultiChannelNotificationSender(
        telegram_bot=None,
        discord_client=None,
        slack_client=None,
    )
    
    executor = JobExecutor(
        agent_runner=agent_runner,
        notification_sender=notification_sender,
        # Main mode callbacks
        on_system_event=on_system_event,
        run_heartbeat=run_heartbeat,
        report_to_main=report_to_main,
    )

    # 2. Initialize scheduler service with JSON export
    db_path = Path("~/.agentica/demo_scheduler.db").expanduser()
    json_path = Path("~/.agentica/demo_tasks.json").expanduser()
    
    scheduler = SchedulerService(
        db_path=db_path,
        json_path=json_path,
        executor=executor.execute,
        notification_sender=notification_sender,
        # Main mode callbacks (also passed to service for reference)
        on_system_event=on_system_event,
        run_heartbeat=run_heartbeat,
        report_to_main=report_to_main,
        # Auto-export to JSON for human viewing
        auto_export_json=True,
    )

    # 3. Start scheduler
    await scheduler.start()
    logger.info("Scheduler started!")

    # 4. Demo: Create jobs with different SessionTarget modes
    
    # Job 1: Main mode - Inject daily morning briefing into main session
    job1 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="每日晨报",
        description="每天早上9点在主会话中推送新闻摘要",
        schedule=CronSchedule(expression="0 9 * * *", timezone="Asia/Shanghai"),
        payload=AgentTurnPayload(
            prompt="请为我总结今天的科技新闻头条",
            agent_id="news_agent",
        ),
        target=SessionTarget(
            kind=SessionTargetKind.MAIN,
            trigger_heartbeat=True,  # Wake up agent to process
        ),
    ))
    logger.info(f"Created main-mode job: {job1.name} (ID: {job1.id})")

    # Job 2: Isolated mode - Background research task
    job2 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="后台研究任务",
        description="每小时独立执行的数据分析任务",
        schedule=EverySchedule.from_seconds(3600),  # Every hour
        payload=AgentTurnPayload(
            prompt="分析最近的股票市场趋势",
            agent_id="research_agent",
            timeout_seconds=600,
        ),
        target=SessionTarget(
            kind=SessionTargetKind.ISOLATED,
            report_to_main=True,  # Report result back
        ),
    ))
    logger.info(f"Created isolated-mode job: {job2.name} (ID: {job2.id})")

    # Job 3: Simple notification (system_event)
    job3 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="喝水提醒",
        description="每2小时提醒喝水",
        schedule=EverySchedule.from_seconds(7200),
        payload=SystemEventPayload(
            message="该喝水了，保持健康！",
            channel="telegram",
            chat_id="12345678",
        ),
    ))
    logger.info(f"Created notification job: {job3.name} (ID: {job3.id})")

    # Job 4: One-time webhook task
    future_time = datetime.now() + timedelta(minutes=5)
    job4 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="API数据同步",
        description="5分钟后执行一次性数据同步",
        schedule=AtSchedule.from_datetime(future_time),
        payload=WebhookPayload(
            url="https://api.example.com/sync",
            method="POST",
            headers={"Authorization": "Bearer xxx"},
            body={"source": "scheduler"},
            timeout_seconds=30,
        ),
    ))
    logger.info(f"Created one-time webhook job: {job4.name} (ID: {job4.id})")

    # 5. Show JSON export path
    json_path = await scheduler.get_json_path()
    logger.info(f"Tasks exported to JSON: {json_path}")

    # 6. List all jobs
    jobs = await scheduler.list(user_id="demo_user")
    logger.info(f"Total jobs for demo_user: {len(jobs)}")
    for job in jobs:
        target_mode = job.target.kind.value if job.target else "isolated"
        logger.info(f"  - {job.name} [{target_mode}] (next: {job.state.next_run_at_ms})")

    # 7. Get scheduler stats
    stats = await scheduler.get_stats()
    logger.info(f"Scheduler stats: {stats.to_dict()}")

    # 8. Demo: Force run a job
    logger.info("\n--- Force executing job2 (isolated mode) ---")
    run_result = await scheduler.run(job2.id, mode="force")
    logger.info(f"Run result: {run_result.to_dict()}")

    # 9. Keep running briefly then cleanup
    logger.info("\nScheduler running... (Ctrl+C to stop)")
    try:
        await asyncio.sleep(10)  # Run for 10 seconds in demo
    except KeyboardInterrupt:
        pass
    
    # Cleanup
    logger.info("\nCleaning up demo jobs...")
    for job in jobs:
        await scheduler.remove(job.id)
    
    await scheduler.stop()
    logger.info("Scheduler stopped!")


def register_tools_with_agent(agent_framework):
    """Register scheduler tools with your agent framework.

    Example for OpenAI function calling style.
    """
    tools = [
        {"type": "function", "function": tool}
        for tool in ALL_SCHEDULER_TOOLS
    ]

    async def handle_tool_call(tool_name: str, arguments: dict):
        if tool_name in TOOL_IMPLEMENTATIONS:
            return await TOOL_IMPLEMENTATIONS[tool_name](**arguments)
        raise ValueError(f"Unknown tool: {tool_name}")

    return tools, handle_tool_call


if __name__ == "__main__":
    asyncio.run(main())
