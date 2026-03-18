"""Tests for the scheduler module."""
import pytest
from datetime import datetime
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scheduler.models import ScheduledJob, JobState, JobCreate
from src.scheduler.types import (
    JobStatus,
    RunStatus,
    CronSchedule,
    EverySchedule,
    AtSchedule,
    AgentTurnPayload,
)
from src.scheduler.schedule import (
    cron_to_human,
    interval_to_human,
    schedule_to_human,
    compute_next_run_at_ms,
    validate_cron_expression,
)


class TestScheduledJob:
    """Tests for ScheduledJob model."""

    def test_job_creation(self):
        """Test basic job creation."""
        job = ScheduledJob(
            user_id="user_123",
            name="test job",
            description="test description",
        )

        assert job.id is not None
        assert job.status == JobStatus.PENDING
        assert job.user_id == "user_123"
        assert job.enabled is True

    def test_job_serialization(self):
        """Test job to_dict/from_dict roundtrip."""
        job = ScheduledJob(
            user_id="user_123",
            name="test job",
            schedule=CronSchedule(expression="0 9 * * *"),
            payload=AgentTurnPayload(prompt="test prompt"),
        )

        data = job.to_dict()
        restored = ScheduledJob.from_dict(data)

        assert restored.id == job.id
        assert restored.user_id == job.user_id
        assert restored.status == job.status

    def test_job_with_at_schedule(self):
        """Test one-time job with AtSchedule."""
        at_ms = int(datetime.now().timestamp() * 1000) + 86400000
        job = ScheduledJob(
            user_id="user_123",
            name="one-time job",
            schedule=AtSchedule(at_ms=at_ms),
        )

        data = job.to_dict()
        restored = ScheduledJob.from_dict(data)

        assert isinstance(restored.schedule, AtSchedule)
        assert restored.schedule.at_ms == at_ms


class TestCronToHuman:
    """Tests for cron expression to human-readable conversion."""

    def test_daily_cron(self):
        assert cron_to_human("0 9 * * *") == "每天 9:00"
        assert cron_to_human("30 14 * * *") == "每天 14:30"

    def test_weekly_cron(self):
        result = cron_to_human("0 9 * * 1")
        assert "周一" in result

        result = cron_to_human("0 9 * * 1-5")
        assert "工作日" in result

    def test_interval_cron(self):
        result = cron_to_human("*/30 * * * *")
        assert "30" in result
        assert "分钟" in result


class TestScheduleComputation:
    """Tests for schedule computation."""

    def test_at_schedule_future(self):
        """Test AtSchedule with future time."""
        future_ms = int(datetime.now().timestamp() * 1000) + 86400000
        schedule = AtSchedule(at_ms=future_ms)
        result = compute_next_run_at_ms(schedule)
        assert result == future_ms

    def test_at_schedule_past(self):
        """Test AtSchedule with past time returns None."""
        past_ms = int(datetime.now().timestamp() * 1000) - 86400000
        schedule = AtSchedule(at_ms=past_ms)
        result = compute_next_run_at_ms(schedule)
        assert result is None

    def test_every_schedule(self):
        """Test EverySchedule interval computation."""
        schedule = EverySchedule(interval_ms=60000)
        now = int(datetime.now().timestamp() * 1000)
        result = compute_next_run_at_ms(schedule, current_ms=now)
        assert result is not None
        assert result > now

    def test_validate_cron_expression(self):
        """Test cron expression validation."""
        assert validate_cron_expression("0 9 * * *") is True
        assert validate_cron_expression("*/30 * * * *") is True
        assert validate_cron_expression("invalid") is False
        assert validate_cron_expression("") is False


class TestIntervalToHuman:
    """Tests for interval to human-readable conversion."""

    def test_seconds(self):
        assert interval_to_human(30000) == "每隔 30 秒"

    def test_minutes(self):
        assert interval_to_human(1800000) == "每隔 30 分钟"

    def test_hours(self):
        assert interval_to_human(7200000) == "每隔 2 小时"


class TestScheduleToHuman:
    """Tests for schedule_to_human."""

    def test_cron_schedule(self):
        schedule = CronSchedule(expression="0 9 * * *")
        result = schedule_to_human(schedule)
        assert "每天" in result

    def test_every_schedule(self):
        schedule = EverySchedule(interval_ms=1800000)
        result = schedule_to_human(schedule)
        assert "30 分钟" in result

    def test_at_schedule(self):
        at_ms = int(datetime.now().timestamp() * 1000) + 86400000
        schedule = AtSchedule(at_ms=at_ms)
        result = schedule_to_human(schedule)
        assert "执行一次" in result
