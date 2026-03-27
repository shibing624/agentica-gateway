"""Integration tests for SchedulerService: start/add/run/stop/chain.

Uses a real SQLite + YAML store in a temp directory.
Does NOT require a real LLM — uses a mock AgentRunner.
"""
import asyncio
import pytest
import tempfile
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scheduler import (
    SchedulerService,
    JobExecutor,
    JobCreate,
    CronSchedule,
    EverySchedule,
    AtSchedule,
    AgentTurnPayload,
    JobStatus,
    RunStatus,
)


class MockAgentRunner:
    """Fake AgentRunner that records calls."""

    def __init__(self, response: str = "done"):
        self.calls: list[dict] = []
        self.response = response

    async def run(self, prompt: str, context: dict | None = None) -> str:
        self.calls.append({"prompt": prompt, "context": context})
        return self.response


@pytest.fixture
def tmp_data_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
async def scheduler(tmp_data_dir):
    runner = MockAgentRunner(response="job result")
    executor = JobExecutor(agent_runner=runner)
    svc = SchedulerService(data_dir=tmp_data_dir, executor=executor)
    await svc.start()
    yield svc, runner
    await svc.stop()


# ============== Lifecycle ==============

@pytest.mark.asyncio
async def test_start_stop(tmp_data_dir):
    runner = MockAgentRunner()
    executor = JobExecutor(agent_runner=runner)
    svc = SchedulerService(data_dir=tmp_data_dir, executor=executor)

    await svc.start()
    assert svc.state.running is True

    await svc.stop()
    assert svc.state.running is False


@pytest.mark.asyncio
async def test_start_idempotent(tmp_data_dir):
    runner = MockAgentRunner()
    executor = JobExecutor(agent_runner=runner)
    svc = SchedulerService(data_dir=tmp_data_dir, executor=executor)

    await svc.start()
    await svc.start()  # should not raise or double-start
    assert svc.state.running is True
    await svc.stop()


# ============== Job CRUD ==============

@pytest.mark.asyncio
async def test_add_and_get_job(scheduler):
    svc, _ = scheduler
    job = await svc.add(JobCreate(
        user_id="user1",
        name="test job",
        schedule=CronSchedule(expression="0 9 * * *"),
        payload=AgentTurnPayload(prompt="say hello"),
    ))

    assert job.id
    assert job.name == "test job"
    assert job.status == JobStatus.ACTIVE

    fetched = await svc.get(job.id)
    assert fetched is not None
    assert fetched.id == job.id


@pytest.mark.asyncio
async def test_list_jobs(scheduler):
    svc, _ = scheduler
    await svc.add(JobCreate(
        user_id="user1",
        name="job A",
        schedule=EverySchedule(interval_ms=60000),
        payload=AgentTurnPayload(prompt="task A"),
    ))
    await svc.add(JobCreate(
        user_id="user2",
        name="job B",
        schedule=EverySchedule(interval_ms=120000),
        payload=AgentTurnPayload(prompt="task B"),
    ))

    all_jobs = await svc.list()
    assert len(all_jobs) == 2

    user1_jobs = await svc.list(user_id="user1")
    assert len(user1_jobs) == 1
    assert user1_jobs[0].name == "job A"


@pytest.mark.asyncio
async def test_remove_job(scheduler):
    svc, _ = scheduler
    job = await svc.add(JobCreate(
        user_id="user1",
        name="to delete",
        schedule=EverySchedule(interval_ms=60000),
        payload=AgentTurnPayload(prompt="will be deleted"),
    ))

    result = await svc.remove(job.id)
    assert result.removed is True
    assert await svc.get(job.id) is None


@pytest.mark.asyncio
async def test_remove_nonexistent_job(scheduler):
    svc, _ = scheduler
    result = await svc.remove("nonexistent-id")
    assert result.removed is False


# ============== Pause / Resume ==============

@pytest.mark.asyncio
async def test_pause_and_resume(scheduler):
    svc, _ = scheduler
    job = await svc.add(JobCreate(
        user_id="user1",
        name="pausable",
        schedule=EverySchedule(interval_ms=60000),
        payload=AgentTurnPayload(prompt="tick"),
    ))
    assert job.status == JobStatus.ACTIVE

    paused = await svc.pause(job.id)
    assert paused.status == JobStatus.PAUSED

    resumed = await svc.resume(job.id)
    assert resumed.status == JobStatus.ACTIVE


@pytest.mark.asyncio
async def test_pause_nonexistent_returns_none(scheduler):
    svc, _ = scheduler
    result = await svc.pause("nonexistent")
    assert result is None


# ============== Job execution ==============

@pytest.mark.asyncio
async def test_run_job_force(scheduler):
    svc, runner = scheduler
    job = await svc.add(JobCreate(
        user_id="user1",
        name="runnable",
        schedule=CronSchedule(expression="0 0 1 1 *"),  # far future
        payload=AgentTurnPayload(prompt="execute me"),
    ))

    result = await svc.run(job.id, mode="force")

    assert result.status == RunStatus.OK
    assert len(runner.calls) == 1
    assert runner.calls[0]["prompt"] == "execute me"


@pytest.mark.asyncio
async def test_run_job_records_history(scheduler):
    svc, runner = scheduler
    job = await svc.add(JobCreate(
        user_id="user1",
        name="history test",
        schedule=CronSchedule(expression="0 0 1 1 *"),
        payload=AgentTurnPayload(prompt="record me"),
    ))

    await svc.run(job.id, mode="force")
    runs, total = await svc.get_job_runs(job.id)

    assert total == 1
    assert runs[0].status == RunStatus.OK


# ============== Task chain ==============

@pytest.mark.asyncio
async def test_chain_jobs(scheduler):
    svc, _ = scheduler
    job_a = await svc.add(JobCreate(
        user_id="user1",
        name="chain source",
        schedule=CronSchedule(expression="0 9 * * *"),
        payload=AgentTurnPayload(prompt="step 1"),
    ))
    job_b = await svc.add(JobCreate(
        user_id="user1",
        name="chain target",
        schedule=CronSchedule(expression="0 10 * * *"),
        payload=AgentTurnPayload(prompt="step 2"),
    ))

    updated_a = await svc.chain(job_a.id, job_b.id, on_status=["ok"])
    assert updated_a is not None
    assert len(updated_a.on_complete) > 0


# ============== Clone ==============

@pytest.mark.asyncio
async def test_clone_job(scheduler):
    svc, _ = scheduler
    original = await svc.add(JobCreate(
        user_id="user1",
        name="original",
        schedule=EverySchedule(interval_ms=3600000),
        payload=AgentTurnPayload(prompt="original task"),
    ))

    clone = await svc.clone_job(original.id, new_name="clone")
    assert clone is not None
    assert clone.id != original.id
    assert clone.name == "clone"


# ============== Batch ==============

@pytest.mark.asyncio
async def test_batch_pause(scheduler):
    svc, _ = scheduler
    j1 = await svc.add(JobCreate(user_id="u", name="j1", schedule=EverySchedule(interval_ms=1000), payload=AgentTurnPayload(prompt="a")))
    j2 = await svc.add(JobCreate(user_id="u", name="j2", schedule=EverySchedule(interval_ms=1000), payload=AgentTurnPayload(prompt="b")))

    result = await svc.batch_pause([j1.id, j2.id])
    assert result.success is True
    assert result.processed == 2

    jobs = await svc.list(include_disabled=True)
    assert all(j.status == JobStatus.PAUSED for j in jobs)


# ============== Statistics ==============

@pytest.mark.asyncio
async def test_get_stats(scheduler):
    svc, _ = scheduler
    await svc.add(JobCreate(
        user_id="u1", name="s1",
        schedule=EverySchedule(interval_ms=1000),
        payload=AgentTurnPayload(prompt="x"),
    ))
    stats = await svc.get_stats()
    assert stats.total_jobs >= 1
    assert stats.running is True


# ============== YAML persistence ==============

@pytest.mark.asyncio
async def test_yaml_persistence(tmp_data_dir):
    """Jobs added in one run should survive a restart via YAML."""
    runner = MockAgentRunner()
    executor = JobExecutor(agent_runner=runner)
    svc = SchedulerService(data_dir=tmp_data_dir, executor=executor)
    await svc.start()

    job = await svc.add(JobCreate(
        user_id="u1",
        name="persistent job",
        schedule=CronSchedule(expression="0 9 * * *"),
        payload=AgentTurnPayload(prompt="persist me"),
    ))
    await svc.stop()

    # Restart
    svc2 = SchedulerService(data_dir=tmp_data_dir, executor=JobExecutor(agent_runner=runner))
    await svc2.start()
    fetched = await svc2.get(job.id)
    assert fetched is not None
    assert fetched.name == "persistent job"
    await svc2.stop()
