import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.clarifications import ClarificationStore, question_schema
from backend import server
from backend.routing import classify_request
from tests.model_responses import ScriptedModel


QUESTION = {"question": "어느 서비스의 상태를 확인할까요?", "reason": "여러 서비스 중 점검 대상이 지정되지 않았습니다.",
            "options": ["api", "db", "web"]}


class QuestionModel(ScriptedModel):
    async def json(self, prompt, **kwargs):
        if prompt.startswith("Classify intent"):
            query = json.loads(prompt.split("Input=", 1)[1])["query"]
            if query.startswith("모호한서비스"):
                return {"route": "clarify", "reason": QUESTION["reason"], "project_reference": "current", "needs_context": False, "clarification": QUESTION}
            if "사용자의 원래 요청" in query:
                from backend.diagnostics import target_service
                return {"route": "status", "reason": "사용자가 상태 조회 대상을 확정했습니다", "project_reference": "current", "needs_context": False, "target_scope": "service", "service": target_service(query, ("api", "db", "web"))}
        return await super().json(prompt, **kwargs)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "ModelGateway", lambda settings: QuestionModel())
    with TestClient(server.app) as value: yield value


def ask(client, thread="question-test"):
    response = client.post("/chat", json={"message": "모호한서비스의 상태를 확인하고 싶어요", "thread_id": thread})
    assert response.status_code == 200
    return response.json()


def test_graph_asks_only_then_stops_without_observations(client):
    result = ask(client)
    assert result["metadata"]["waiting_for_user"] and result["metadata"]["tool_calls"] == 0
    assert result["evidence"] == [] and result["knowledge"] == [] and result["approval"] is None
    assert result["clarification"]["status"] == "pending"
    assert [e["node"] for e in result["events"] if e["type"] == "NodeStarted"] == ["route", "human_question"]
    pending = client.get("/clarifications/pending", params={"thread_id": "question-test"}).json()
    assert pending["clarification"]["id"] == result["clarification"]["id"]


@pytest.mark.parametrize("reply,service", [({"choice": 2}, "db"), ({"answer": "1번"}, "api"), ({"answer": "web 서비스 상태만 알려주세요"}, "web")])
def test_number_and_free_text_continue_original_request(client, reply, service):
    question = ask(client)["clarification"]
    result = client.post(f"/clarifications/{question['id']}/reply", json={"thread_id": "question-test", **reply})
    assert result.status_code == 200
    body = result.json()
    assert body["metadata"]["route"] == "status" and not body["metadata"]["waiting_for_user"]
    assert body["metadata"]["clarification_rounds"] == 1
    assert body["evidence"][0]["args"] == {"service": service}
    route_input = next(e["input"] for e in body["events"] if e["type"] == "NodeStarted" and e["node"] == "route")
    assert "모호한서비스" in route_input["query"] and QUESTION["question"] in route_input["query"]
    assert client.post(f"/clarifications/{question['id']}/reply", json={"thread_id": "question-test", "choice": 1}).status_code == 409


def test_reply_stream_delivers_real_node_events(client):
    question = ask(client)["clarification"]
    result = client.post(f"/clarifications/{question['id']}/reply?stream=true", json={"thread_id": "question-test", "choice": 1})
    assert result.status_code == 200 and result.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(x[6:]) for x in result.text.splitlines() if x.startswith("data: ") and x != "data: [DONE]"]
    assert events[0]["type"] == "NodeStarted"
    assert any(e["type"] == "FinalAnswer" and e["metadata"]["route"] == "status" for e in events)
    assert result.text.endswith("data: [DONE]\n\n")


def test_reply_cannot_bypass_mutation_safety_or_approve_restart(client):
    question = ask(client)["clarification"]
    body = client.post(f"/clarifications/{question['id']}/reply", json={"thread_id": "question-test", "answer": "docker volume prune 자동 실행해주세요"}).json()
    assert body["metadata"]["route"] == "blocked" and body["metadata"]["mutation_calls"] == 0
    assert body["metadata"]["tool_calls"] == 0 and body["approval"] is None


def test_wrong_thread_invalid_body_and_cancel(client):
    question = ask(client)["clarification"]
    path = f"/clarifications/{question['id']}"
    assert client.post(path + "/reply", json={"thread_id": "other", "choice": 1}).status_code == 404
    assert client.post(path + "/reply", json={"thread_id": "question-test", "choice": 1, "answer": "api"}).status_code == 422
    assert client.post(path + "/reply", json={"thread_id": "question-test", "choice": True}).status_code == 422
    assert client.post(path + "/cancel", json={"thread_id": "question-test"}).json()["status"] == "cancelled"
    assert client.post(path + "/reply", json={"thread_id": "question-test", "choice": 1}).status_code == 409
    assert client.get("/clarifications/pending", params={"thread_id": "question-test"}).json()["clarification"] is None


def test_new_chat_supersedes_previous_pending_question(client):
    question = ask(client)["clarification"]
    assert client.post("/chat", json={"thread_id": "question-test", "message": "안녕하세요"}).status_code == 200
    assert client.post(f"/clarifications/{question['id']}/reply", json={"thread_id": "question-test", "choice": 1}).status_code == 409


def test_store_survives_restart_and_binds_scope(tmp_path):
    path = tmp_path / "questions.db"
    store = ClarificationStore(path, "target-a")
    question = store.create("thread", "original request", QUESTION, 0)
    store.close()
    store = ClarificationStore(path, "target-a")
    assert store.pending("thread")["id"] == question["id"]
    other = ClarificationStore(path, "target-b")
    assert other.pending("thread") is None
    with pytest.raises(KeyError): other.finish(question["id"], "thread", choice=1)
    other.create("thread", "other request", QUESTION, 0)
    assert store.pending("thread")["id"] == question["id"]
    store.close(); other.close()


def test_expiration_invalid_choice_and_no_replay(tmp_path):
    store = ClarificationStore(tmp_path / "questions.db", "target")
    expired = store.create("t", "query", QUESTION, 0, ttl=-1)
    assert store.pending("t") is None
    with pytest.raises(ValueError): store.finish(expired["id"], "t", choice=1)
    question = store.create("t", "query", QUESTION | {"options": ["api", "db"]}, 0)
    with pytest.raises(ValueError): store.finish(question["id"], "t", choice=3)
    assert store.pending("t")["id"] == question["id"]
    assert store.finish(question["id"], "t", answer="2")["answer"] == "db"
    with pytest.raises(ValueError): store.finish(question["id"], "t", choice=1)
    store.close()


@pytest.mark.parametrize("value", [QUESTION | {"options": ["x"] * 4}, QUESTION | {"options": ["x", "x"]}, QUESTION | {"question": "API 키를 입력해주세요"}, QUESTION | {"options": ["docker system prune 실행"]}])
def test_invalid_or_unsafe_questions_are_rejected(value):
    with pytest.raises(ValueError): question_schema(value)


def test_clear_requests_do_not_create_questions(client):
    body = client.post("/chat", json={"thread_id": "clear", "message": "api 상태만 알려주세요"}).json()
    assert body["clarification"] is None and body["metadata"]["route"] == "status"
    assert not any(e["node"] == "human_question" for e in body["events"])


def test_repeated_ambiguity_is_bounded(client, monkeypatch):
    class AlwaysAsk(QuestionModel):
        async def json(self, *args, **kwargs): return {"route": "clarify", "reason": "대상 모름", "clarification": QUESTION}
    from backend.graph import Dependencies, build_graph
    bundle = server.app.state.bundle
    bundle["graph"] = build_graph(Dependencies(llm=AlwaysAsk(), mcp=bundle["mcp"], approvals=bundle["approvals"]))
    body = ask(client)
    for _ in range(2):
        body = client.post(f"/clarifications/{body['clarification']['id']}/reply", json={"thread_id": "question-test", "answer": "아직 잘 모르겠어요"}).json()
    assert body["clarification"] is None and not body["metadata"]["waiting_for_user"]
    assert body["metadata"]["tool_calls"] == 0 and "두 차례" in body["reply"]


def test_model_extra_options_are_capped_but_free_text_remains_available():
    class ExtraOptions:
        async def json(self, *args, **kwargs):
            return {"route": "clarify", "reason": "대상 미지정", "clarification": QUESTION | {"options": ["api", "db", "web", "other"]}}
    result = asyncio.run(classify_request(ExtraOptions(), "그 서비스만", [], allowed_services=("api", "db", "web", "other")))
    assert result["route"] == "clarify" and result["clarification"]["options"] == ["api", "db", "web"]


def test_model_canonical_service_overrides_an_earlier_mention():
    class Resolved:
        async def json(self, *args, **kwargs): return {"route": "status", "reason": "사용자가 db 선택", "service": "db"}
    result = asyncio.run(classify_request(Resolved(), "원래 api인지 db인지 모른다고 했지만 db 상태만 확인해주세요", [], allowed_services=("api", "db")))
    from backend.diagnostics import target_service
    assert target_service(result["query"], ("api", "db")) == "db"


def test_only_one_concurrent_reply_can_claim_question(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = ClarificationStore(tmp_path / "questions.db", "target")
    question = store.create("t", "query", QUESTION, 0)
    def reply(_):
        try: return store.finish(question["id"], "t", choice=1)["status"]
        except ValueError: return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(reply, range(2)))
    assert sorted(results) == ["answered", "rejected"]
    store.close()


def test_resolved_second_pass_route_is_not_overridden_by_a_context_flag():
    class Resolved:
        async def json(self, prompt, **kwargs):
            data = json.loads(prompt.split("Input=", 1)[1])
            return {"route": "change", "reason": "문맥의 Dockerfile을 수정하는 요청", "needs_context": True}
    result = asyncio.run(classify_request(Resolved(), "그걸 고쳐주세요", [{"role": "user", "content": "Dockerfile 검토"}]))
    assert result["route"] == "change" and "clarification" not in result


def test_missing_required_service_does_not_fall_back_to_first_service():
    class MissingTarget:
        async def json(self, *args, **kwargs): return {"route": "status", "reason": "상태 요청", "target_scope": "unspecified", "service": None}
    result = asyncio.run(classify_request(MissingTarget(), "그 서비스 상태만", [], allowed_services=("ai", "backend")))
    assert result["route"] == "clarify" and result["clarification"]["options"] == ["ai", "backend"]


def test_explicit_project_scope_is_not_forced_to_choose_a_service():
    class Project:
        async def json(self, *args, **kwargs): return {"route": "status", "reason": "전체 상태", "target_scope": "project", "service": None}
    result = asyncio.run(classify_request(Project(), "현재 프로젝트 상태", [], allowed_services=("ai", "backend")))
    assert result["route"] == "status" and "clarification" not in result and "전체" in result["query"]


def test_model_cannot_guess_an_unmentioned_service():
    class Guess:
        async def json(self, *args, **kwargs): return {"route": "incident", "reason": "추측", "target_scope": "service", "service": "backend"}
    result = asyncio.run(classify_request(Guess(), "어느 서비스인지 모릅니다. 그 서비스만 진단", [], allowed_services=("ai", "backend")))
    assert result["route"] == "clarify"
    result = asyncio.run(classify_request(Guess(), "원래 backend도 후보였습니다", [], allowed_services=("ai", "backend"), confirmed_answer="ai 상태만 확인"))
    assert result["route"] == "clarify"


def test_invented_operational_choices_are_not_clickable():
    class Invented:
        async def json(self, *args, **kwargs):
            return {"route": "clarify", "reason": "작업 모름", "clarification": QUESTION | {"options": ["api 실행", "db 복구", "web 재시작"]}}
    result = asyncio.run(classify_request(Invented(), "그거 처리", [], allowed_services=("api", "db", "web")))
    assert result["route"] == "clarify" and result["clarification"]["options"] == []


def test_missing_service_keeps_known_status_intent_even_if_model_requests_history():
    class Missing:
        async def json(self, *args, **kwargs):
            return {"route": "status", "reason": "상태만 조회", "needs_context": True, "target_scope": "unspecified"}
    result = asyncio.run(classify_request(Missing(), "제가 고를 서비스 하나만 상태 조회", [], allowed_services=("ai", "postgres")))
    assert result["route"] == "clarify" and result["clarification"]["options"] == ["ai", "postgres"]


def test_confirmed_scope_is_not_reinterpreted_from_question_wording():
    from backend.planning import status_tasks, review_evidence
    query = "이전 질문: 목록에 없으면 직접 입력. 답변: db만 상태 확인"
    target = {"scope": "service", "service": "db"}
    tasks = status_tasks(query, ("api", "db"), target)
    assert tasks[0]["tool"] == "check_service_health" and tasks[0]["args"] == {"service": "db"}
    assert review_evidence(query, "status", [], ("api", "db"), target)["missing"] == tasks
    with pytest.raises(ValueError): status_tasks(query, ("api", "db"), {"scope": "service", "service": "unknown"})
