"""Scheduler demo showing SessionTarget modes and second-level precision.

This example demonstrates:
- Second-level precision with 6-part cron: "0 30 7 * * *" (7:30:00)
- CronSchedule.at_time() helper method
- EverySchedule with second-level intervals
- SessionTarget: main (inject to main session) vs isolated (independent execution)
- Dependency injection callbacks
- JSON export for human-readable task viewing
"""
import asyncio
from pathlib import Path
from typing import Any

from loguru import logger
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scheduler import (
    SchedulerService,
    ALL_SCHEDULER_TOOLS,
    TOOL_IMPLEMENTATIONS,
    schedule_to_human,
)
from src.scheduler.executor import JobExecutor
from src.scheduler.models import JobCreate
from src.scheduler.types import (
    SessionTarget,
    SessionTargetKind,
    CronSchedule,
    EverySchedule,
    AgentTurnPayload,
    SystemEventPayload,
    WebhookPayload,
)


# ============== Agent Runner (使用 gpt-4o) ==============

class OpenAIAgentRunner:
    """Agent runner using OpenAI gpt-4o."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")

    async def run(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Run agent with prompt using gpt-4o."""
        if not self.api_key:
            # Mock response when no API key
            return f"[Mock] Executed: {prompt}"

        import openai
        client = openai.AsyncOpenAI(api_key=self.api_key)

        system_msg = "You are a helpful assistant executing scheduled tasks."
        if context:
            system_msg += f"\nContext: {context}"

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content or ""


# ============== Dependency Injection Callbacks ==============

async def on_system_event(user_id: str, event_data: dict[str, Any]) -> None:
    """Inject system event into user's main session."""
    logger.info(f"[Main Mode] Injecting event for user {user_id}: {event_data.get('job_name')}")


async def run_heartbeat(user_id: str) -> None:
    """Trigger heartbeat in user's main session."""
    logger.info(f"[Main Mode] Triggering heartbeat for user {user_id}")


async def report_to_main(user_id: str, job_id: str, result: str) -> None:
    """Report isolated execution result to main session."""
    logger.info(f"[Isolated Mode] Reporting result for user {user_id}, job {job_id}")
    logger.info(f"  Result: {result[:100]}...")


# ============== Main Demo ==============

async def main():
    """Demonstrate scheduler with second-level precision and SessionTarget."""
    
    logger.info("=" * 60)
    logger.info("Scheduler Demo: Second-Level Precision & LLM-Friendly Tools")
    logger.info("=" * 60)

    # ============== Demo 1: CronSchedule.at_time() Helper ==============
    logger.info("\n--- CronSchedule.at_time() 便捷方法 ---")
    
    cron_examples = [
        ("每天7:30", CronSchedule.at_time(7, 30)),
        ("每天7:30:45 (秒级)", CronSchedule.at_time(7, 30, 45)),
        ("工作日9:00", CronSchedule.at_time(9, 0, 0, "1-5")),
        ("周末10点", CronSchedule.at_time(10, 0, 0, "0,6")),
    ]
    for desc, cron in cron_examples:
        logger.info(f'  {desc} -> expression="{cron.expression}" ({schedule_to_human(cron)})')

    # ============== Demo 2: Direct Cron Expressions ==============
    logger.info("\n--- 直接使用 Cron 表达式 ---")
    
    cron_direct = [
        ("5段格式 - 每天7:30", "30 7 * * *"),
        ("6段格式 - 每天7:30:45", "45 30 7 * * *"),
        ("每30分钟", "*/30 * * * *"),
        ("每10秒", "*/10 * * * * *"),
    ]
    for desc, expr in cron_direct:
        cron = CronSchedule(expression=expr)
        logger.info(f'  {desc} -> "{expr}" ({schedule_to_human(cron)})')

    # ============== Demo 3: Interval with Seconds ==============
    logger.info("\n--- EverySchedule 秒级间隔 ---")
    
    every_examples = [
        ("每30秒", EverySchedule(interval_ms=30000)),
        ("每5分钟", EverySchedule.from_seconds(300)),
        ("每小时", EverySchedule.from_seconds(3600)),
    ]
    for desc, every in every_examples:
        logger.info(f'  {desc} -> interval_ms={every.interval_ms} ({schedule_to_human(every)})')

    # ============== Demo 4: Full Scheduler Integration ==============
    logger.info("\n--- 完整调度器示例 ---")

    # Initialize executor with gpt-4o agent runner
    agent_runner = OpenAIAgentRunner()
    
    executor = JobExecutor(
        agent_runner=agent_runner,
        on_system_event=on_system_event,
        run_heartbeat=run_heartbeat,
        report_to_main=report_to_main,
    )

    # Initialize scheduler service
    db_path = Path("/tmp/demo_scheduler.db").expanduser()
    json_path = Path("/tmp/demo_tasks.json").expanduser()
    
    scheduler = SchedulerService(
        db_path=db_path,
        json_path=json_path,
        executor=executor.execute,
        on_system_event=on_system_event,
        run_heartbeat=run_heartbeat,
        report_to_main=report_to_main,
        auto_export_json=True,
    )

    await scheduler.start()
    logger.info("Scheduler started!")

    # Create jobs using various methods
    
    # Job 1: CronSchedule.at_time() - 每天7:30:45 (秒级精度)
    job1 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="每日晨报",
        description="每天早上7:30:45精确推送新闻",
        schedule=CronSchedule.at_time(7, 30, 45),
        payload=AgentTurnPayload(
            prompt="请为我总结今天的科技新闻头条",
            agent_id="news_agent",
        ),
        target=SessionTarget(
            kind=SessionTargetKind.MAIN,
            trigger_heartbeat=True,
        ),
    ))
    logger.info(f"Created job: {job1.name} (schedule: {schedule_to_human(job1.schedule)})")

    # Job 2: EverySchedule - 每30秒
    job2 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="秒级心跳检测",
        description="每30秒执行健康检查",
        schedule=EverySchedule(interval_ms=30000),
        payload=WebhookPayload(
            url="https://api.example.com/health",
            method="GET",
            timeout_seconds=5,
        ),
        target=SessionTarget(kind=SessionTargetKind.ISOLATED),
    ))
    logger.info(f"Created job: {job2.name} (schedule: {schedule_to_human(job2.schedule)})")

    # Job 3: CronSchedule 6-part format - 每10秒
    job3 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="高频数据采集",
        description="每10秒采集数据",
        schedule=CronSchedule(expression="*/10 * * * * *"),
        payload=AgentTurnPayload(
            prompt="采集最新传感器数据",
            agent_id="data_agent",
        ),
    ))
    logger.info(f"Created job: {job3.name} (schedule: {schedule_to_human(job3.schedule)})")

    # Job 4: 工作日9点
    job4 = await scheduler.add(JobCreate(
        user_id="demo_user",
        name="工作日晨会提醒",
        description="工作日早上9点提醒",
        schedule=CronSchedule.at_time(9, 0, 0, "1-5"),
        payload=SystemEventPayload(
            message="该参加晨会了！",
            channel="telegram",
        ),
    ))
    logger.info(f"Created job: {job4.name} (schedule: {schedule_to_human(job4.schedule)})")

    # List all jobs
    jobs = await scheduler.list(user_id="demo_user")
    logger.info(f"\nTotal jobs: {len(jobs)}")
    for job in jobs:
        target_mode = job.target.kind.value if job.target else "isolated"
        human_schedule = schedule_to_human(job.schedule)
        logger.info(f"  - {job.name} [{target_mode}]: {human_schedule}")

    # Cleanup
    logger.info("\nCleaning up demo jobs...")
    for job in jobs:
        await scheduler.remove(job.id)
    
    await scheduler.stop()
    logger.info("Done!")


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
