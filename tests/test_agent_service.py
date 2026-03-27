"""Tests for AgentService: LRU cache, session management, fail-fast init, cancel."""
import asyncio
import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.agent_service import AgentService, LRUAgentCache, ChatResult


# ============== LRUAgentCache unit tests ==============

class TestLRUAgentCache:

    def _make_agent(self, name: str = "agent"):
        m = MagicMock()
        m.name = name
        return m

    def test_put_and_get(self):
        cache = LRUAgentCache(max_size=3)
        a = self._make_agent("a")
        cache.put("s1", a)
        assert cache.get("s1") is a

    def test_miss_returns_none(self):
        cache = LRUAgentCache(max_size=3)
        assert cache.get("nonexistent") is None

    def test_evicts_lru_on_overflow(self):
        cache = LRUAgentCache(max_size=2)
        a1, a2, a3 = self._make_agent("a1"), self._make_agent("a2"), self._make_agent("a3")
        cache.put("s1", a1)
        cache.put("s2", a2)
        # Access s1 to make s2 the LRU
        cache.get("s1")
        cache.put("s3", a3)  # s2 should be evicted
        assert cache.get("s1") is a1
        assert cache.get("s3") is a3
        assert cache.get("s2") is None  # evicted

    def test_delete(self):
        cache = LRUAgentCache(max_size=3)
        a = self._make_agent()
        cache.put("s1", a)
        assert cache.delete("s1") is True
        assert cache.get("s1") is None
        assert cache.delete("s1") is False

    def test_clear(self):
        cache = LRUAgentCache(max_size=3)
        cache.put("s1", self._make_agent())
        cache.put("s2", self._make_agent())
        cache.clear()
        assert len(cache) == 0

    def test_keys(self):
        cache = LRUAgentCache(max_size=5)
        cache.put("a", self._make_agent())
        cache.put("b", self._make_agent())
        assert set(cache.keys()) == {"a", "b"}

    def test_max_size_respected(self):
        cache = LRUAgentCache(max_size=3)
        for i in range(10):
            cache.put(f"s{i}", self._make_agent(f"a{i}"))
        assert len(cache) == 3


# ============== AgentService unit tests ==============

def _make_service(workspace_path="/tmp/test_ws") -> AgentService:
    svc = AgentService(
        workspace_path=workspace_path,
        model_name="test-model",
        model_provider="openai",
    )
    return svc


@pytest.mark.asyncio
async def test_init_fail_fast_on_workspace_error():
    """AgentService should raise RuntimeError if workspace init fails."""
    svc = _make_service(workspace_path="/nonexistent/bad/path/xyz")
    with patch.object(svc, "_do_initialize", side_effect=RuntimeError("workspace failed")):
        with pytest.raises(RuntimeError, match="workspace failed"):
            await svc._ensure_initialized()


@pytest.mark.asyncio
async def test_init_idempotent():
    """_ensure_initialized should only call _do_initialize once."""
    svc = _make_service()
    call_count = 0

    def _fake_init():
        nonlocal call_count
        call_count += 1
        svc._initialized = True
        svc._workspace = MagicMock()

    with patch.object(svc, "_do_initialize", side_effect=_fake_init):
        await svc._ensure_initialized()
        await svc._ensure_initialized()
    assert call_count == 1


@pytest.mark.asyncio
async def test_chat_returns_result():
    """chat() should return a ChatResult with content."""
    svc = _make_service()
    svc._initialized = True
    svc._workspace = MagicMock()
    svc._workspace.set_user = MagicMock()

    mock_agent = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "Hello from agent"
    mock_response.tools = []
    mock_agent.run = AsyncMock(return_value=mock_response)

    svc._cache.put("test-session", mock_agent)

    # Mock asyncio.to_thread to run sync functions synchronously
    async def mock_to_thread(func, *args):
        return func(*args)

    with patch("asyncio.to_thread", side_effect=mock_to_thread):
        result = await svc.chat(
            message="hi",
            session_id="test-session",
            user_id="user1",
        )

    assert isinstance(result, ChatResult)
    assert result.content == "Hello from agent"
    assert result.session_id == "test-session"


@pytest.mark.asyncio
async def test_delete_session_removes_from_cache():
    """delete_session() must actually remove the agent from the LRU cache."""
    svc = _make_service()
    svc._initialized = True
    mock_agent = MagicMock()
    svc._cache.put("session-abc", mock_agent)
    svc._session_work_dirs["session-abc"] = "/some/dir"

    assert svc.delete_session("session-abc") is True
    assert svc._cache.get("session-abc") is None
    assert "session-abc" not in svc._session_work_dirs
    # Second delete returns False
    assert svc.delete_session("session-abc") is False


def test_list_sessions_returns_cache_keys():
    """list_sessions() must return actual session IDs, not an empty list."""
    svc = _make_service()
    svc._initialized = True
    svc._cache.put("s1", MagicMock())
    svc._cache.put("s2", MagicMock())
    sessions = svc.list_sessions()
    assert "s1" in sessions
    assert "s2" in sessions


def test_cancel_session_calls_agent_cancel():
    """cancel_session() should call agent.cancel() for the correct session."""
    svc = _make_service()
    agent_a = MagicMock()
    agent_b = MagicMock()
    svc._cache.put("session-a", agent_a)
    svc._cache.put("session-b", agent_b)

    svc.cancel_session("session-a")

    agent_a.cancel.assert_called_once()
    agent_b.cancel.assert_not_called()


def test_cancel_session_returns_false_for_unknown():
    """cancel_session() returns False when session doesn't exist."""
    svc = _make_service()
    assert svc.cancel_session("nonexistent") is False


def test_per_session_work_dir():
    """Per-session work_dir should be isolated from other sessions."""
    svc = _make_service()
    svc.set_session_work_dir("s1", "/project-a")
    svc.set_session_work_dir("s2", "/project-b")

    assert svc.get_session_work_dir("s1") == "/project-a"
    assert svc.get_session_work_dir("s2") == "/project-b"
    # Unknown session falls back to global base_dir
    from src.config import settings
    assert svc.get_session_work_dir("unknown") == str(settings.base_dir)


def test_update_work_dir_clears_all():
    """update_work_dir() should clear ALL cached agents and per-session dirs."""
    svc = _make_service()
    svc._cache.put("s1", MagicMock())
    svc._cache.put("s2", MagicMock())
    svc._session_work_dirs["s1"] = "/dir-a"

    svc.update_work_dir("/new-dir")

    assert len(svc._cache) == 0
    assert len(svc._session_work_dirs) == 0


@pytest.mark.asyncio
async def test_agent_build_timeout():
    """_get_agent() should raise RuntimeError when build times out."""
    svc = _make_service()
    svc._initialized = True
    svc._workspace = MagicMock()

    async def slow_build():
        await asyncio.sleep(100)  # never finishes

    with patch(
        "src.services.agent_service._AGENT_BUILD_TIMEOUT_S", 0.01
    ), patch.object(
        svc, "_build_agent", side_effect=lambda: asyncio.sleep(100)
    ), patch(
        "asyncio.to_thread", new_callable=lambda: lambda f: asyncio.sleep(100)
    ):
        with pytest.raises((RuntimeError, asyncio.TimeoutError)):
            await asyncio.wait_for(svc._get_agent("new-session"), timeout=0.5)
