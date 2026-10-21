import os
import json

import pytest

os.environ["LLM_BACKEND"] = "ollama"
os.environ["DOCKER_MODE"] = "fixture"

from fastapi.testclient import TestClient
from backend.server import app


def test_incident_replans_requires_approval_and_verifies_recovery():
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["mcp_connected"] is True
        response = client.post("/chat", json={"message": "api docker container restart", "thread_id": "integration"})
        assert response.status_code == 200
        body = response.json()
        assert body["metadata"]["route"] == "incident"
        assert body["metadata"]["replan_rounds"] == 1
        assert body["metadata"]["evidence_count"] >= 5
        assert any(e["type"] == "ReplanCheck" and not e["is_sufficient"] for e in body["events"])
        assert body["approval"]["status"] == "pending"
        verified = client.post(f"/actions/{body['approval']['id']}/approve").json()
        assert verified["before"]["health"] == "unhealthy"
        assert verified["after"]["health"] == "healthy"
        assert verified["recovery_ok"] is True
        assert client.post(f"/actions/{body['approval']['id']}/approve").status_code == 409


def test_sse_contract_ends_with_done():
    with TestClient(app) as client:
        text = client.get("/chat/stream", params={"message": "안녕하세요", "thread_id": "sse"}).text
        assert '"type": "RouteDecided"' in text
        assert '"type": "FinalAnswer"' in text
        assert text.endswith("data: [DONE]\n\n")


@pytest.mark.parametrize("scenario,cause,query", [
    ("readiness", "readiness", "api에서 db 연결 준비 상태 점검"),
    ("network", "network", "web에서 db 연결이 안 됩니다"),
    ("healthy", "healthy", "api Docker 상태 점검"),
    ("exit137", "unknown", "api OOM 여부를 진단"),
])
def test_real_stdio_mcp_scenarios(monkeypatch, scenario, cause, query):
    monkeypatch.setenv("DOCKER_FIXTURE_SCENARIO", scenario)
    with TestClient(app) as client:
        response = client.post("/chat", json={"message": query, "thread_id": scenario})
        assert response.status_code == 200
        body = response.json()
        assert body["diagnosis"]["cause"] == cause
        assert body["metadata"]["evidence_coverage"] == (None if cause == "unknown" else 1)
        assert body["approval"] is None


def test_rejection_preserves_state_and_unsafe_request_has_no_tool_calls():
    with TestClient(app) as client:
        body = client.post("/chat", json={"message": "api 컨테이너 재시작 장애", "thread_id": "reject"}).json()
        action_id = body["approval"]["id"]
        assert client.post(f"/actions/{action_id}/reject").json()["status"] == "rejected"
        assert client.post(f"/actions/{action_id}/approve").status_code == 409
        again = client.post("/chat", json={"message": "api 컨테이너 점검", "thread_id": "reject"}).json()
        assert again["approval"] is not None
        blocked = client.post("/chat", json={"message": "docker system prune 실행", "thread_id": "unsafe"}).json()
        assert blocked["approval"] is None
        assert blocked["metadata"]["policy_blocked"] is True
        assert blocked["metadata"]["tool_calls"] == 0


def test_incident_sse_contains_structured_evidence():
    with TestClient(app) as client:
        text = client.get("/chat/stream", params={"message": "api 컨테이너 점검", "thread_id": "sse-incident"}).text
        payloads = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        final = next(p for p in payloads if p["type"] == "FinalAnswer")
        assert final["diagnosis"]["cause"] == "memory"
        assert final["metadata"]["validation_ok"] is True
        assert len(final["evidence"]) >= 7
        assert {p["agent"] for p in payloads if p["type"] == "AgentStarted"} >= {"runtime", "configuration", "writer", "validator"}
        assert any(p["type"] == "SupervisorDecision" and p["next"] == "write" for p in payloads)
        assert any(p["type"] == "AnalysisSkipped" for p in payloads)
        assert any(p["type"] == "AgentStarted" and p["namespace"] for p in payloads)
        assert "".join(p["delta"] for p in payloads if p["type"] == "AnswerToken") == final["reply"]


def test_successful_command_without_healthy_service_is_not_recovery():
    with TestClient(app) as client:
        body = client.post("/chat", json={"message": "api 컨테이너 장애", "thread_id": "not-recovered"}).json()
        mcp = app.state.bundle["mcp"]
        original = mcp.tool
        async def unhealthy(name, args, **kwargs):
            value, retries = await original(name, args, **kwargs)
            if name == "check_service_health": value = value | {"health": "unhealthy"}
            return value, retries
        mcp.tool = unhealthy
        response = client.post(f"/actions/{body['approval']['id']}/approve")
        assert response.status_code == 200
        assert response.json()["status"] == "executed"
        assert response.json()["recovery_ok"] is False
