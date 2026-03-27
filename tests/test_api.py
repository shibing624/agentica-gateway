"""API route tests: authentication, chat endpoints, scheduler API.

Uses FastAPI TestClient (sync) and overrides dependencies.
Does NOT require a real LLM or database.
"""
import json
import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Patch agentica imports before loading the app modules
import unittest.mock as mock
sys.modules.setdefault("agentica", mock.MagicMock())
sys.modules.setdefault("agentica.run_response", mock.MagicMock())
sys.modules.setdefault("agentica.run_config", mock.MagicMock())
sys.modules.setdefault("agentica.workspace", mock.MagicMock())
sys.modules.setdefault("agentica.agent.config", mock.MagicMock())

from fastapi.testclient import TestClient

import src.deps as deps
from src.services.agent_service import AgentService, ChatResult


# ============== Test App fixture ==============

def _make_mock_agent_service() -> AgentService:
    """Return an AgentService with all async methods stubbed."""
    svc = MagicMock(spec=AgentService)
    svc._initialized = True
    svc._cache = MagicMock()
    svc._cache.keys.return_value = ["session-1"]
    svc.model_provider = "openai"
    svc.model_name = "gpt-4o"

    svc.chat = AsyncMock(return_value=ChatResult(
        content="Hello from mock agent",
        session_id="session-1",
        user_id="user1",
        tool_calls=0,
    ))
    svc.chat_stream = AsyncMock(return_value=ChatResult(
        content="Streamed response",
        session_id="session-1",
        user_id="user1",
        tool_calls=1,
        tools_used=["web_search"],
    ))
    svc.list_sessions = MagicMock(return_value=["session-1", "session-2"])
    svc.delete_session = MagicMock(return_value=True)
    svc.save_memory = AsyncMock()
    svc.reload_model = AsyncMock()
    return svc


@pytest.fixture
def client_no_auth():
    """TestClient with no gateway token configured."""
    from src.main import app

    mock_svc = _make_mock_agent_service()
    mock_cm = MagicMock()
    mock_cm.get_status.return_value = {}
    mock_cm.list_channels.return_value = []

    # Override dependencies using FastAPI's dependency_override
    from src.deps import get_agent_service, get_channel_manager, get_scheduler
    app.dependency_overrides[get_agent_service] = lambda: mock_svc
    app.dependency_overrides[get_channel_manager] = lambda: mock_cm
    app.dependency_overrides[get_scheduler] = lambda: None

    # Also set global deps for direct access in routes
    deps.agent_service = mock_svc
    deps.channel_manager = mock_cm
    deps.scheduler = None

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, mock_svc

    # Cleanup
    app.dependency_overrides.clear()


@pytest.fixture
def client_with_auth(monkeypatch):
    """TestClient with gateway_token = 'test-secret'."""
    from src.main import app
    from src import config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "gateway_token", "test-secret")

    mock_svc = _make_mock_agent_service()
    mock_cm = MagicMock()
    mock_cm.get_status.return_value = {}
    mock_cm.list_channels.return_value = []

    # Override dependencies using FastAPI's dependency_override
    from src.deps import get_agent_service, get_channel_manager, get_scheduler
    app.dependency_overrides[get_agent_service] = lambda: mock_svc
    app.dependency_overrides[get_channel_manager] = lambda: mock_cm
    app.dependency_overrides[get_scheduler] = lambda: None

    # Also set global deps for direct access in routes
    deps.agent_service = mock_svc
    deps.channel_manager = mock_cm
    deps.scheduler = None

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, mock_svc

    # Cleanup
    app.dependency_overrides.clear()


# ============== Authentication tests ==============

class TestAuthentication:

    def test_no_token_configured_allows_all(self, client_no_auth):
        client, _ = client_no_auth
        r = client.get("/api/status")
        assert r.status_code == 200

    def test_with_token_rejects_missing_auth(self, client_with_auth):
        client, _ = client_with_auth
        r = client.get("/api/status")
        assert r.status_code == 401

    def test_with_token_accepts_correct_bearer(self, client_with_auth):
        client, _ = client_with_auth
        r = client.get("/api/status", headers={"Authorization": "Bearer test-secret"})
        assert r.status_code == 200

    def test_with_token_accepts_correct_query_param(self, client_with_auth):
        client, _ = client_with_auth
        r = client.get("/api/status?token=test-secret")
        assert r.status_code == 200

    def test_with_token_rejects_wrong_token(self, client_with_auth):
        client, _ = client_with_auth
        r = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_health_exempt_from_auth(self, client_with_auth):
        client, _ = client_with_auth
        r = client.get("/health")
        assert r.status_code == 200

    def test_root_exempt_from_auth(self, client_with_auth):
        client, _ = client_with_auth
        r = client.get("/")
        assert r.status_code == 200


# ============== Root / health ==============

class TestBasicEndpoints:

    def test_root(self, client_no_auth):
        client, _ = client_no_auth
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "running"
        assert "version" in data

    def test_health(self, client_no_auth):
        client, _ = client_no_auth
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_api_status(self, client_no_auth):
        client, _ = client_no_auth
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert "model" in data
        assert "version" in data


# ============== Chat ==============

class TestChatEndpoints:

    def test_post_chat(self, client_no_auth):
        client, svc = client_no_auth
        r = client.post("/api/chat", json={
            "message": "hello",
            "session_id": "test",
            "user_id": "user1",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["content"] == "Hello from mock agent"
        assert data["session_id"] == "session-1"
        svc.chat.assert_awaited_once()

    def test_post_chat_missing_message_fails(self, client_no_auth):
        client, _ = client_no_auth
        r = client.post("/api/chat", json={"session_id": "test"})
        assert r.status_code == 422  # Pydantic validation error

    def test_list_sessions(self, client_no_auth):
        client, svc = client_no_auth
        r = client.get("/api/sessions")
        assert r.status_code == 200
        data = r.json()
        assert "sessions" in data
        assert "session-1" in data["sessions"]

    def test_delete_session(self, client_no_auth):
        client, svc = client_no_auth
        r = client.delete("/api/sessions/session-1")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"
        svc.delete_session.assert_called_once_with("session-1")

    def test_delete_nonexistent_session(self, client_no_auth):
        client, svc = client_no_auth
        svc.delete_session = MagicMock(return_value=False)
        r = client.delete("/api/sessions/ghost")
        assert r.status_code == 404

    def test_save_memory(self, client_no_auth):
        client, svc = client_no_auth
        r = client.post("/api/memory", json={
            "content": "remember this",
            "user_id": "user1",
        })
        assert r.status_code == 200
        svc.save_memory.assert_awaited_once()


# ============== Models ==============

class TestModelEndpoints:

    def test_list_models(self, client_no_auth):
        client, _ = client_no_auth
        r = client.get("/api/models")
        assert r.status_code == 200
        data = r.json()
        assert "providers" in data
        assert "current_provider" in data

    def test_switch_model(self, client_no_auth):
        client, svc = client_no_auth
        r = client.post("/api/model", json={
            "model_provider": "deepseek",
            "model_name": "deepseek-chat",
        })
        assert r.status_code == 200
        svc.reload_model.assert_awaited_once_with("deepseek", "deepseek-chat")


# ============== Channels ==============

class TestChannelEndpoints:

    def test_list_channels(self, client_no_auth):
        client, _ = client_no_auth
        r = client.get("/api/channels")
        assert r.status_code == 200
        assert "channels" in r.json()

    def test_feishu_webhook_challenge(self, client_no_auth):
        client, _ = client_no_auth
        r = client.post("/webhook/feishu", json={"challenge": "abc123"})
        assert r.status_code == 200
        assert r.json()["challenge"] == "abc123"


# ============== Upload ==============

class TestUpload:

    def test_upload_allowed_extension(self, client_no_auth, tmp_path):
        client, _ = client_no_auth
        f = tmp_path / "test.txt"
        f.write_text("hello")
        with open(f, "rb") as fh:
            r = client.post(
                "/api/upload",
                files={"file": ("test.txt", fh, "text/plain")},
                data={"target_dir": str(tmp_path)},
            )
        assert r.status_code == 200
        assert r.json()["filename"] == "test.txt"

    def test_upload_rejected_extension(self, client_no_auth, tmp_path, monkeypatch):
        from src import config as cfg_mod
        monkeypatch.setattr(cfg_mod.settings, "upload_allowed_extensions", ".txt,.md")
        client, _ = client_no_auth
        r = client.post(
            "/api/upload",
            files={"file": ("malware.exe", b"\x00\x01", "application/octet-stream")},
            data={"target_dir": str(tmp_path)},
        )
        assert r.status_code == 400
        assert "not allowed" in r.json()["detail"]

    def test_upload_file_too_large(self, client_no_auth, tmp_path, monkeypatch):
        from src import config as cfg_mod
        monkeypatch.setattr(cfg_mod.settings, "upload_max_size_mb", 0)  # 0 MB = reject everything
        client, _ = client_no_auth
        r = client.post(
            "/api/upload",
            files={"file": ("test.txt", b"x" * 100, "text/plain")},
            data={"target_dir": str(tmp_path)},
        )
        assert r.status_code == 413
