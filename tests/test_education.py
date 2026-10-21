import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.agents import build_validator_agent
from backend.knowledge import retrieve_lessons, teaching_guide
from tests.routing_double import route_request
from backend.server import app
from tests.model_responses import ScriptedModel
from tests.test_diagnostics import FixtureMCP, run_graph


@pytest.mark.parametrize("query,route", [
    ("도커 컨테이너에 저장된 내용이 모두 사라졌어요", "education"),
    ("컨테이너 재시작 후 데이터가 사라졌어요", "education"),
    ("볼륨과 바인드 마운트 차이 설명", "education"),
    ("컨테이너 볼륨은 어떻게 쓰나요", "education"),
    ("컨테이너에서 localhost 연결이 왜 안 되는지 개념 설명", "education"),
    ("현재 api 볼륨 설정을 점검해주세요", "incident"),
    ("데이터가 사라졌어요. 실제로 확인해주세요", "incident"),
    ("docker volume rm 자동 실행", "blocked"),
    ("docker volume prune 자동 실행", "blocked"),
    ("docker compose down -v 실행", "blocked"),
    ("Docker 양자 스토리지 예제로 가르쳐주세요", "education"),
])
def test_scripted_education_route(query, route):
    assert route_request(query)[0] == route


def test_local_retrieval_has_reviewed_sources_and_no_forced_irrelevant_results():
    results = retrieve_lessons("컨테이너 데이터 저장 내용이 사라졌어요")
    assert {item["id"] for item in results} >= {"K1", "K2"}
    assert all(item["source_url"].startswith("https://docs.docker.com/") and item["revision"] and item["reviewed_at"] for item in results)
    assert retrieve_lessons("양자 얽힘") == []
    assert retrieve_lessons("important export report") == []


def test_educational_retrieval_is_separate_from_live_observation():
    mcp, model = FixtureMCP(), ScriptedModel()
    state = run_graph("도커 컨테이너에 저장된 내용이 모두 사라졌어요", mcp, model)
    assert state["route"] == "education" and state["validation_ok"]
    assert mcp.calls == [] and not state["evidence"] and not state.get("diagnosis")
    assert state["approval"] is None and not state.get("analysis_used")
    assert {e["agent"] for e in state["events"] if e["type"] == "AgentStarted"} == {"knowledge", "writer", "validator"}
    assert "볼륨" in state["final"] and "재시작" in state["final"]
    assert "https://docs.docker.com/engine/storage/" in state["final"]
    assert "[K1]" in state["final"] and "[E1]" not in state["final"]
    assert not any(prompt.startswith(("Create", "Refine", "Review")) for _, prompt, _ in model.calls)


def test_no_matching_lesson_abstains_without_model_or_docker():
    mcp, model = FixtureMCP(), ScriptedModel()
    state = run_graph("Docker 양자 스토리지 예제로 가르쳐주세요", mcp, model)
    assert state["route"] == "education" and state["knowledge"] == []
    assert "근거를 찾지 못했습니다" in state["final"]
    assert mcp.calls == [] and len(model.calls) == 1 and model.calls[0][1].startswith("Classify intent") and state["approval"] is None


@pytest.mark.parametrize("draft", [
    "컨테이너를 검사했습니다. 볼륨이 없습니다. [E1]" * 3,
    "모든 데이터가 볼륨에 남습니다. [K999]" * 4,
    "볼륨을 설명합니다. [K1] https://invented.example/ " * 3,
    "docker volume rm app_data [K1] " * 3,
    "docker compose down -v [K1] " * 3,
])
def test_education_code_gate_rejects_fake_citations_links_and_destructive_commands(draft):
    model = ScriptedModel()
    state = asyncio.run(build_validator_agent(model).ainvoke({"route": "education", "draft": draft, "evidence": [], "knowledge": retrieve_lessons("데이터 사라짐")}))
    assert not state["ok"] and not model.calls


def test_education_nonverbatim_failure_suppresses_unsafe_final_answer(monkeypatch):
    from backend import graph
    class UntrustedWriter:
        async def ainvoke(self, state):
            return {"draft": "잘못된 학습 설명입니다. 재시작하면 데이터가 반드시 사라집니다. [K1]", "events": []}
    monkeypatch.setattr(graph, "build_writer_agent", lambda llm: UntrustedWriter())
    state = run_graph("컨테이너 데이터 사라졌어요")
    assert state["validation_attempts"] == 1 and not state["validation_ok"]
    assert state["final"].startswith("[교육 답변 검증 미통과]")
    assert "학습 설명입니다" not in state["final"] and state["approval"] is None


def test_model_cannot_inject_factual_text_or_drop_storage_safety_lessons():
    class UntrustedModel(ScriptedModel):
        async def json(self, prompt, **kwargs):
            if not prompt.startswith("Select lessons"):
                return await super().json(prompt, **kwargs)
            return {"lesson_ids": ["K2"], "answer": "볼륨을 나중에 연결하면 반드시 복구됩니다."}
    state = run_graph("컨테이너 데이터가 사라졌어요", llm=UntrustedModel())
    assert state["final"] == teaching_guide(state["knowledge"])
    assert "반드시 복구" not in state["final"] and "[K1]" in state["final"]


def test_extractive_validator_rejects_false_statement_with_valid_citation():
    knowledge = retrieve_lessons("데이터 사라졌어요")
    model = ScriptedModel()
    draft = teaching_guide(knowledge).replace("사라지지는 않습니다", "사라집니다")
    result = asyncio.run(build_validator_agent(model).ainvoke({"route": "education", "draft": draft, "evidence": [], "knowledge": knowledge}))
    assert not result["ok"] and model.calls == []


def test_education_api_and_sse_expose_knowledge_separately():
    with TestClient(app) as client:
        query = "도커 컨테이너에 저장된 내용이 모두 사라졌어요"
        body = client.post("/chat", json={"message": query, "thread_id": "learn"}).json()
        assert body["metadata"]["route"] == "education"
        assert body["metadata"]["tool_calls"] == 0 and body["metadata"]["mutation_calls"] == 0
        assert body["knowledge"] and body["evidence"] == [] and body["diagnosis"] is None
        stream = client.get("/chat/stream", params={"message": query, "thread_id": "learn-sse"}).text
        events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        final = next(e for e in events if e["type"] == "FinalAnswer")
        assert any(e["type"] == "KnowledgeRetrieved" and e["namespace"] for e in events)
        assert final["knowledge"] and final["metadata"]["validation_ok"]
        assert "".join(e["delta"] for e in events if e["type"] == "AnswerToken") == final["reply"]
