import json
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from backend import server
from tests.model_responses import ScriptedModel


class RememberingModel(ScriptedModel):
    async def json(self, prompt, **kwargs):
        if prompt.startswith("Classify intent"):
            data = json.loads(prompt.split("Input=", 1)[1])
            if data["query"] == "그 서비스 상태 다시 알려줘":
                assert any("db" in item["content"] for item in data["history"])
                return {"route": "status", "reason": "이전 사용자 대상 db", "project_reference": "current", "needs_context": False,
                        "target_scope": "service", "target_cardinality": "one", "service": "db"}
        return await super().json(prompt, **kwargs)


def state(client, thread="remember"):
    bundle = server.app.state.bundle
    config = {"configurable": {"thread_id": bundle["history_scope"] + ":" + thread}}
    return client.portal.call(bundle["graph"].aget_state, config)


def test_real_sqlite_checkpointer_persists_and_followup_remembers_after_restart(monkeypatch):
    monkeypatch.setattr(server, "ModelGateway", lambda settings: RememberingModel())
    with TestClient(server.app) as client:
        assert isinstance(server.app.state.bundle["graph"].checkpointer, AsyncSqliteSaver)
        first = client.post("/chat", json={"thread_id": "remember", "message": "db 상태만 알려줘"}).json()
        assert first["evidence"][0]["args"] == {"service": "db"}
        checkpoint_id = state(client).config["configurable"]["checkpoint_id"]
    with TestClient(server.app) as client:
        assert state(client).config["configurable"]["checkpoint_id"] == checkpoint_id
        result = client.post("/chat", json={"thread_id": "remember", "message": "그 서비스 상태 다시 알려줘"}).json()
        assert result["evidence"][0]["args"] == {"service": "db"}
        assert result["metadata"]["route_context_used"]
        assert result["metadata"]["tool_calls"] == 1 and result["metadata"]["evidence_count"] == 1
        assert [e["node"] for e in result["events"] if e["type"] == "NodeStarted"] == ["route", "status_observe"]
        assert not state(client, "other").values


def test_previous_command_evidence_and_approval_do_not_leak_into_new_turn():
    with TestClient(server.app) as client:
        client.post("/chat", json={"thread_id": "remember", "message": "/diagnose-network api db"}).raise_for_status()
        blocked = client.post("/chat", json={"thread_id": "remember", "message": "docker volume prune 자동 실행해줘"}).json()
        assert blocked["metadata"]["route"] == "blocked" and blocked["metadata"]["tool_calls"] == 0
        assert blocked["evidence"] == [] and blocked["approval"] is None and blocked["proposal"] is None
        assert state(client).values["command"] is None
        assert [e["node"] for e in blocked["events"] if e["type"] == "NodeStarted"] == ["route", "policy_block"]


def test_sse_checkpoint_and_subsequent_json_do_not_reuse_old_nodes():
    with TestClient(server.app) as client:
        response = client.get("/chat/stream", params={"thread_id": "remember", "message": "db 상태만 알려줘"})
        assert '"type": "FinalAnswer"' in response.text and state(client).values["routing_target"]["service"] == "db"
        result = client.post("/chat", json={"thread_id": "remember", "message": "안녕하세요"}).json()
        assert result["metadata"]["route"] == "chat" and not result["evidence"]
        assert [e["node"] for e in result["events"] if e["type"] == "NodeStarted"] == ["route", "chat_answer"]


def test_concurrent_same_thread_is_rejected_without_adding_another_message():
    with TestClient(server.app) as client:
        bundle = server.app.state.bundle
        key = bundle["history_scope"] + ":busy"
        bundle["running_threads"] = {key}
        response = client.post("/chat", json={"thread_id": "busy", "message": "db 상태"})
        assert response.status_code == 409 and bundle["store"].history(key) == []


def test_service_order_changes_do_not_change_conversation_scope(monkeypatch):
    monkeypatch.setenv("DOCKER_ALLOWED_SERVICES", "api,db,web")
    with TestClient(server.app) as client:
        first_scope = server.app.state.bundle["history_scope"]
        client.post("/chat", json={"thread_id": "remember", "message": "db 상태만 알려줘"}).raise_for_status()
        checkpoint_id = state(client).config["configurable"]["checkpoint_id"]
    monkeypatch.setenv("DOCKER_ALLOWED_SERVICES", "web,api,db")
    with TestClient(server.app) as client:
        assert server.app.state.bundle["history_scope"] == first_scope
        assert state(client).config["configurable"]["checkpoint_id"] == checkpoint_id


def test_legacy_service_order_message_keys_are_read_without_cross_project_leak():
    with TestClient(server.app) as client:
        bundle = server.app.state.bundle
        old = next(prefix for prefix in bundle["legacy_history_scopes"] if prefix != bundle["history_scope"])
        bundle["store"].add(old + ":remember", "user", "이전 서비스 순서의 db 대화")
        bundle["store"].add("unrelated-scope:remember", "user", "different-project-private")
        client.post("/chat", json={"thread_id": "remember", "message": "안녕"}).raise_for_status()
        history = state(client).values["history"]
        assert any("이전 서비스 순서" in item["content"] for item in history)
        assert not any("different-project-private" in item["content"] for item in history)
