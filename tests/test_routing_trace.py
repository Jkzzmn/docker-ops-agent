import asyncio
import json

import pytest

from backend.routing import classify_request
from backend.tracing import snapshot
from tests.model_responses import ScriptedModel
from tests.test_diagnostics import FixtureMCP, run_graph


def test_semantic_classifier_can_choose_a_route_not_matching_keywords():
    class Semantic(ScriptedModel):
        async def json(self, prompt, **kwargs):
            if prompt.startswith("Classify intent"):
                if not json.loads(prompt.split("Input=", 1)[1])["history"]:
                    return {"route": "education", "reason": "후속 질문의 대상이 필요함", "needs_context": True}
                assert "이전의 볼륨 이야기" in prompt
                return {"route": "education", "reason": "이전 대화를 이어 영속성을 학습하는 질문"}
            return await super().json(prompt, **kwargs)
    result = asyncio.run(classify_request(Semantic(), "그럼 없어지지 않게 하려면요?", [{"role":"user","content":"이전의 볼륨 이야기"}]))
    assert result["route"] == "education" and result["route_method"] == "llm" and not result["route_fallback"]


def test_bad_classifier_schema_stops_without_rule_fallback():
    class Invalid(ScriptedModel):
        async def json(self, *args, **kwargs):
            return {"route": ["incident"], "reason": "invalid"}
    result = asyncio.run(classify_request(Invalid(), "안녕하세요", []))
    assert result["route"] == "unavailable" and result["route_method"] == "llm-unavailable" and not result["route_fallback"]


def test_current_request_overrides_old_assistant_rag_and_is_not_duplicated():
    query = "현재 도커 컴포즈 파일과 도커파일들을 확인해서 오류가 있는지, 혹은 비효율적인 부분이 있는지 확인해주세요"
    class Capture(ScriptedModel):
        async def json(self, prompt, **kwargs):
            data = json.loads(prompt.split("Input=", 1)[1])
            assert list(data)[-1] == "query" and data["query"] == query
            assert data["history"] == [{"role": "user", "content": "볼륨이 뭐야?"}]
            assert len(data["previous_answer"]) <= 800
            return {"route": "configuration", "reason": "실제 설정 파일 오류·효율 검토"}
    history = [{"role": "user", "content": "볼륨이 뭐야?"},
               {"role": "assistant", "content": "교육 안내입니다. K1 컨테이너 쓰기 계층 " * 3000}, {"role": "user", "content": query}]
    result = asyncio.run(classify_request(Capture(), query, history))
    assert result["route"] == "configuration" and result["route_method"] == "llm"


def test_classifier_context_is_bounded_but_followups_keep_recent_user_topics():
    class Capture:
        async def json(self, prompt, **kwargs):
            data = json.loads(prompt.split("Input=", 1)[1])
            if not data["history"]:
                return {"route": "change", "reason": "후속 질문", "needs_context": True}
            assert len(data["history"]) == 3
            assert all(len(item["content"]) <= 500 for item in data["history"])
            assert data["history"][-1]["content"].startswith("Dockerfile 검토")
            return {"route": "change", "reason": "직전 Dockerfile 검토의 수정 요청"}
    history = [{"role": "user", "content": str(i) * 900} for i in range(10)] + [{"role": "user", "content": "Dockerfile 검토" * 200}]
    assert asyncio.run(classify_request(Capture(), "그럼 그걸 고쳐줘", history))["route"] == "change"


def test_model_requested_context_is_explicit_in_metadata():
    class Contextual:
        calls = 0
        async def json(self, prompt, **kwargs):
            self.calls += 1
            return {"route": "change", "reason": "후속 변경 요청", "needs_context": self.calls == 1}
    model = Contextual()
    result = asyncio.run(classify_request(model, "그걸 고쳐줘", [{"role": "user", "content": "Dockerfile 점검"}]))
    assert result["route_context_used"] and model.calls == 1


def test_unresolved_followup_without_context_does_not_guess_a_target():
    class Ambiguous:
        async def json(self, *args, **kwargs): return {"route": "incident", "reason": "대상 모름", "needs_context": True}
    result = asyncio.run(classify_request(Ambiguous(), "그걸 해줘", []))
    assert result["route"] == "clarify" and result["clarification"] and not result["route_fallback"]


def test_api_classifier_failure_returns_no_education_or_docker(monkeypatch):
    from fastapi.testclient import TestClient
    from backend import server
    class Failed(ScriptedModel):
        async def json(self, *args, **kwargs): raise ConnectionError("Ollama unavailable")
    monkeypatch.setattr(server, "ModelGateway", lambda settings: Failed())
    with TestClient(server.app) as client:
        body = client.post("/chat", json={"thread_id": "route-failed", "message": "현재 도커 컴포즈와 도커파일을 확인해주세요"}).json()
        assert body["metadata"]["route"] == "unavailable" and body["metadata"]["tool_calls"] == 0
        assert body["knowledge"] == [] and body["evidence"] == [] and body["approval"] is None
        assert "교육 안내입니다" not in body["reply"]


@pytest.mark.parametrize("error", [TimeoutError, ConnectionError])
def test_model_failure_never_runs_education_or_docker(error):
    class Failed(ScriptedModel):
        async def json(self, *args, **kwargs): raise error("classifier unavailable")
    mcp = FixtureMCP()
    result = run_graph("현재 도커 컴포즈와 도커파일 오류를 확인해주세요", mcp=mcp, llm=Failed())
    assert result["route"] == "unavailable" and not result["unsafe_request"]
    assert mcp.calls == [] and not result.get("knowledge") and result["approval"] is None
    assert [e["node"] for e in result["events"] if e["type"] == "NodeStarted"] == ["route", "routing_unavailable"]
    assert "교육 안내입니다" not in result["final"]


def test_safety_policy_cannot_be_bypassed_by_model():
    model = ScriptedModel()
    result = asyncio.run(classify_request(model, "docker volume prune 자동 실행해줘", []))
    assert result["route"] == "blocked" and result["route_method"] == "safety-policy"
    assert model.calls == []


def test_trace_contains_only_executed_nodes_and_actual_io():
    state = run_graph("컨테이너 데이터 사라졌어요")
    starts = [e for e in state["events"] if e["type"] == "NodeStarted"]
    ends = {e["run_id"]: e for e in state["events"] if e["type"] == "NodeCompleted"}
    roots = [e for e in starts if e["scope"] == "supervisor"]
    assert {e["node"] for e in starts if e["scope"] != "supervisor"} == {"retrieve", "compose", "code_check"}
    assert len(starts) == 8  # JSON retains the same real child lifecycles as custom SSE.
    assert [e["node"] for e in roots] == ["route", "education_retrieve", "write", "validate", "finalize"]
    assert all(e["run_id"] in ends and e["timestamp"] <= ends[e["run_id"]]["timestamp"] for e in starts)
    assert all(ends[e["run_id"]]["elapsed_ms"] >= 0 for e in starts)
    assert all(e["parent_run_id"] in {r["run_id"] for r in roots} for e in starts if e["scope"] != "supervisor")
    route = roots[0]
    assert route["input"]["query"] == "컨테이너 데이터 사라졌어요"
    assert ends[route["run_id"]]["output"]["route_method"] == "llm"
    assert not any(e["node"] in {"analysis", "research", "gate", "judge"} for e in starts)
    assert len({e["event_id"] for e in state["events"]}) == len(state["events"])


def test_snapshot_redacts_nested_secrets_without_invalidating_json():
    result = snapshot({"api_key":"sensitive", "nested":[{"log":"password=unsafe-value next"}], "events":["not included"]})
    assert result == {"api_key":"[REDACTED]", "nested":[{"log":"password=[REDACTED] next"}]}
    large = snapshot({"text":"x" * 25000})
    assert large["truncated"] and len(large["preview"]) == 24000


def test_chat_path_has_two_nodes_not_the_education_topology():
    state = run_graph("안녕하세요")
    assert [e["node"] for e in state["events"] if e["type"] == "NodeStarted"] == ["route", "chat_answer"]
