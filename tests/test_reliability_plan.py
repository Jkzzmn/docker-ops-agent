"""Contract/failure injection tests; these are not live model quality scores."""
import asyncio
import json
import pytest
from backend.approvals import ApprovalStore
from backend.graph import Dependencies, build_graph
from backend.routing import classify_request
from backend.config_validation import validate_review
from backend.knowledge import retrieve_lessons, teaching_guide
from backend.observations import status_answer, health_status


@pytest.mark.parametrize("value,expected", [({}, "unknown"), ({"health": ""}, "unknown"), ({"health_configured": False}, "not_configured"), ({"health_configured": True}, "unknown"), ({"health": "healthy"}, "healthy"), ({"health": "starting"}, "starting")])
def test_empty_health_is_not_configuration_evidence(value, expected):
    assert health_status(value) == expected


def test_exited_container_empty_health_is_unknown_in_answer():
    result = status_answer({"id": "E1", "tool": "check_service_health", "args": {"service": "api"}, "observation": {"ok": True, "subject_status": "present", "state": "exited", "health": ""}})
    assert result["health"] == "unknown" and "not_configured" not in result["final"]
    assert result["lifecycle"] == "exited" and result["answer_status"] == "validated"
from tests.model_responses import ScriptedModel


class Decision:
    def __init__(self, **fields): self.fields = fields
    async def json(self, *args, **kwargs):
        if args[0].startswith("Verify live-file intent"): return {"inspect_current_files": False, "wants_edits": False, "request_quote": None}
        return {"route": "status", "reason": "모델의 의미 분류", "target_scope": "service", "service": "backend", **self.fields}


@pytest.mark.parametrize("project,reference", [("skala-miniproject", "explicit"), ("buttercast", "explicit"), ("unknown", "explicit"), (None, "ambiguous")])
@pytest.mark.parametrize("route", ["status", "incident", "configuration", "change"])
def test_foreign_project_stops_before_tools(project, reference, route):
    class NeverTools:
        async def tool(self, *args): raise AssertionError("wrong scope tool call")
    store = ApprovalStore()
    deps = Dependencies(Decision(route=route, project_reference=reference, requested_project=project), NeverTools(), store, allowed_services=("backend",), selected_target={"project": "bizxray"})
    try:
        state = asyncio.run(build_graph(deps).ainvoke({"query": f"bizxray 말고 {project} 프로젝트 확인", "history": [], "events": [], "thread_id": "scope"}))
        assert state["route"] == "target_selection" and state["approval"] is None
        assert [e["node"] for e in state["events"] if e["type"] == "NodeStarted"] == ["route", "target_selection"]
        assert not state.get("evidence") and not state.get("clarification")
    finally: store.close()


def test_missing_project_schema_fails_closed():
    result = asyncio.run(classify_request(Decision(), "backend 상태", [], allowed_services=("backend",), selected_target={"project": "bizxray"}))
    assert result["route"] == "unavailable"


def test_education_misroute_is_rechecked_for_actual_file_intent_by_model_not_rules():
    class Model(Decision):
        async def json(self, prompt, **kwargs):
            if prompt.startswith("Verify live-file intent"):
                assert "lesson_catalog" not in prompt and "K1" not in kwargs["system"]
                return {"inspect_current_files": True, "wants_edits": False, "request_quote": "빌드 파일을 읽고"}
            return await super().json(prompt, **kwargs)
    result = asyncio.run(classify_request(Model(route="education", project_reference="current"), "현재 프로젝트의 빌드 파일을 읽고 위험을 설명해주세요", [], allowed_services=("backend",), selected_target={"project": "bizxray"}))
    assert result["route"] == "configuration" and result["route_file_intent_check"]["inspect_current_files"]
    assert "semantic_lesson_ids" not in result


def test_generic_education_cannot_be_overridden_into_file_reads_by_a_model():
    class Model(Decision):
        async def json(self, prompt, **kwargs):
            assert not prompt.startswith("Verify live-file intent")
            return await super().json(prompt, **kwargs)
    result = asyncio.run(classify_request(Model(route="education", lesson_ids=["K1", "K2"]), "새 인스턴스 교체 후 사진이 안 보입니다. 예방 원리를 배우고 싶습니다.", []))
    assert result["route"] == "education" and not result.get("route_file_intent_check")


@pytest.mark.parametrize("text,expected", [("Compose에서", True), ("Dockerfile의", True), ("dockerfiles를", True), ("decompose", False), ("사진 파일이 사라졌어요", False)])
def test_file_reference_grounding_handles_korean_particles_without_classifying_intent(text, expected):
    from backend.routing import _mentions_inspectable_file
    assert _mentions_inspectable_file(text) is expected


def test_model_cannot_invent_a_quoted_file_inspection_request():
    class Model(Decision):
        async def json(self, prompt, **kwargs):
            if prompt.startswith("Verify live-file intent"):
                return {"inspect_current_files": True, "wants_edits": False, "request_quote": "실제 Compose 파일을 열어주세요"}
            return await super().json(prompt, **kwargs)
    result = asyncio.run(classify_request(Model(route="education", lesson_ids=["K5"]), "Compose 준비 상태의 개념을 배우고 싶습니다.", []))
    assert result["route"] == "education" and result["route_file_intent_check"]["file_reference_grounded"] is False


def test_filesystem_scope_does_not_casefold_distinct_posix_paths():
    result = asyncio.run(classify_request(Decision(project_reference="explicit", requested_project="/srv/app"), "/srv/app 프로젝트 backend 상태", [], allowed_services=("backend",), selected_target={"project": "bizxray", "project_dir": "/srv/App"}))
    assert result["route"] == "target_selection"


def test_unidentified_single_service_cannot_become_project_status():
    result = asyncio.run(classify_request(Decision(project_reference="current", target_scope="project", target_cardinality="one", service=None), "서비스 하나 이름 미정", [], allowed_services=("backend",), selected_target={"project": "bizxray"}))
    assert result["route"] == "clarify" and result["clarification"]


@pytest.mark.parametrize("query,resolved,expected", [
    ("buttercast api 상태, bizxray는 제외", {"project_reference": "explicit", "requested_project": "buttercast"}, "target_selection"),
    ("buttercast는 제외하고 현재 bizxray backend 상태", {"project_reference": "current", "requested_project": None}, "status"),
])
def test_conflicting_known_project_is_semantically_re_resolved_not_keyword_routed(query, resolved, expected):
    class Model(Decision):
        calls = 0
        async def json(self, prompt, **kwargs):
            self.calls += 1
            if prompt.startswith("Classify intent"):
                return {"route": "status", "reason": "wrong initial project extraction", "project_reference": "current", "requested_project": None, "target_scope": "service", "service": "backend"}
            assert "known_projects" in prompt
            return resolved
    model = Model()
    result = asyncio.run(classify_request(model, query, [], allowed_services=("backend",), selected_target={"project": "bizxray", "known_projects": ["bizxray", "buttercast"]}))
    assert result["route"] == expected and model.calls == 2


def test_service_confused_with_project_gets_one_semantic_correction():
    class Model(Decision):
        async def json(self, prompt, **kwargs):
            if prompt.startswith("Classify intent"):
                return {"route": "status", "reason": "status", "project_reference": "current", "requested_project": "backend", "target_scope": "service", "service": "backend"}
            return {"project_reference": "current", "requested_project": None}
    result = asyncio.run(classify_request(Model(), "backend 상태만", [], allowed_services=("backend",), selected_target={"project": "bizxray"}))
    assert result["route"] == "status" and result["routing_target"]["service"] == "backend"


def test_project_reference_schema_forbids_current_plus_service_name():
    from backend.routing import ProjectReference
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ProjectReference.model_validate({"project_reference": "current", "requested_project": "ai"})
    assert ProjectReference.model_validate({"project_reference": "current", "requested_project": None}).model_dump()["requested_project"] is None


def test_project_named_like_a_service_is_not_silently_read_in_current_scope():
    class Model(Decision):
        async def json(self, prompt, **kwargs):
            if prompt.startswith("Classify intent"):
                return {"route": "status", "reason": "ambiguous fast extraction", "project_reference": "current", "requested_project": "ai", "target_scope": "service", "service": "frontend"}
            assert kwargs["quality"] is True
            return {"project_reference": "explicit", "requested_project": "ai"}
    result = asyncio.run(classify_request(Model(), "ai 프로젝트의 frontend 상태, 현재 프로젝트 제외", [], allowed_services=("ai", "frontend"), selected_target={"project": "bizxray", "known_projects": ["bizxray"]}))
    assert result["route"] == "target_selection" and not result.get("clarification")


def test_explicitly_misreading_excluded_current_project_is_rechecked():
    class Model(Decision):
        async def json(self, prompt, **kwargs):
            if prompt.startswith("Classify intent"):
                return {"route": "status", "reason": "bad negation extraction", "project_reference": "explicit", "requested_project": "bizxray", "target_scope": "service", "service": "backend"}
            return {"project_reference": "explicit", "requested_project": "skala-miniproject"}
    result = asyncio.run(classify_request(Model(), "bizxray 말고 skala-miniproject backend", [], allowed_services=("backend",), selected_target={"project": "bizxray", "known_projects": ["bizxray", "skala-miniproject"]}))
    assert result["route"] == "target_selection"


@pytest.mark.parametrize("stream", [False, True])
def test_project_guard_applies_to_json_and_get_sse(monkeypatch, stream):
    from fastapi.testclient import TestClient
    from backend import server
    monkeypatch.setattr(server, "ModelGateway", lambda settings: Decision(project_reference="explicit", requested_project="foreign"))
    with TestClient(server.app) as client:
        if stream:
            response = client.get("/chat/stream", params={"message": "foreign backend 상태", "thread_id": "scope-sse"})
            body = next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and '"type": "FinalAnswer"' in line)
        else:
            body = client.post("/chat", json={"message": "foreign backend 상태", "thread_id": "scope-json"}).json()
        assert body["metadata"]["target_selection_required"]
        assert body["metadata"]["execution_status"] == "waiting_for_user"
        assert body["metadata"]["tool_calls"] == 0 and not body["evidence"]


def test_project_guard_applies_to_post_question_reply_sse(monkeypatch):
    from fastapi.testclient import TestClient
    from backend import server
    class Model(Decision):
        async def json(self, prompt, **kwargs):
            if "사용자의 원래 요청" in json.loads(prompt.split("Input=", 1)[1])["query"]:
                return await super().json(prompt, **kwargs)
            return {"route": "clarify", "reason": "대상 필요", "project_reference": "current", "clarification": {"question": "대상은?", "reason": "대상 필요", "options": []}}
    monkeypatch.setattr(server, "ModelGateway", lambda settings: Model(project_reference="explicit", requested_project="foreign"))
    with TestClient(server.app) as client:
        q = client.post("/chat", json={"message": "대상을 고르겠습니다", "thread_id": "scope-post"}).json()["clarification"]
        response = client.post(f"/clarifications/{q['id']}/reply?stream=true", json={"thread_id": "scope-post", "answer": "foreign 프로젝트 backend"})
        body = next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and '"type": "FinalAnswer"' in line)
        assert body["metadata"]["route"] == "target_selection" and body["metadata"]["tool_calls"] == 0


def test_metadata_does_not_fabricate_measurements_for_education():
    from fastapi.testclient import TestClient
    from backend.server import app
    with TestClient(app) as client:
        body = client.post("/chat", json={"message": "데이터 유실 원리를 가르쳐주세요", "thread_id": "meta-null"}).json()
        m = body["metadata"]
        assert m["evidence_coverage"] is None and m["evidence_sufficient"] is None and m["tool_time_sum"] is None
        assert m["answer_status"] == "validated" and m["execution_status"] == "completed"


@pytest.mark.parametrize("value,subject,valid", [
    ({"ok": True, "found": False, "observation_status": "complete", "subject_status": "absent"}, "absent", True),
    ({"ok": True, "count": 2, "observation_status": "complete", "subject_status": "ambiguous"}, "ambiguous", True),
    ({"ok": False, "error": "transport failure"}, "unknown", False),
    ({"ok": True, "state": "exited", "health": ""}, "present", True),
])
def test_status_ends_after_one_observation_without_writer(value, subject, valid):
    class MCP:
        calls = 0
        async def tool(self, *args): self.calls += 1; return value, 0
    mcp, llm, store = MCP(), ScriptedModel(), ApprovalStore()
    try:
        state = asyncio.run(build_graph(Dependencies(llm, mcp, store)).ainvoke({"query": "api 상태만", "history": [], "events": []}))
        assert state["subject_status"] == subject and state["validation_ok"] == valid
        assert mcp.calls == 1 and len(llm.calls) == 1 and state["validation_attempts"] == 0
        assert state["approval"] is None
        assert [e["node"] for e in state["events"] if e["type"] == "NodeStarted"] == ["route", "status_observe"]
    finally: store.close()


def file_evidence(content, path="Dockerfile", **extra):
    return {"id": "E1", "tool": "read_dockerfile", "args": {"path": path}, "observation": {"ok": True, "found": True, "path": path, "content": content, "sha256": "snapshot-hash", **extra}}


def test_claim_validation_rejects_already_applied_recommendations_and_runtime_fiction():
    evidence = [file_evidence("FROM debian\nRUN apt-get update \\\n && apt-get install -y curl\nRUN npm ci\n")]
    review = {"findings": [{"rule": rule, "path": "Dockerfile", "text": "arbitrary unsupported text", "evidence_ids": ["E1"]} for rule in ("combine_apt", "use_npm_ci", "health_success")], "edits": []}
    result = validate_review(evidence, review)
    assert [x["verdict"] for x in result["candidates"]] == ["contradicted", "contradicted", "insufficient"]
    assert result["claims"] == [] and result["edits"] == []


@pytest.mark.parametrize("content,rule", [
    ("FROM debian\nRUN apt-get update\nRUN apt-get install -y curl", "combine_apt"),
    ("FROM python:3.12\nCOPY app ./app\nRUN pip install .", "source_before_pip"),
])
def test_supported_static_risks_have_exact_file_refs(content, rule):
    result = validate_review([file_evidence(content)], {"findings": [], "edits": []})
    assert [x["rule"] for x in result["claims"]] == [rule]
    assert result["claims"][0]["sha256"] == "snapshot-hash" and result["claims"][0]["snippet"] in content


@pytest.mark.parametrize("content,extra", [("FROM x\nRUN <<EOF\napt-get update\nEOF", {}), ("FROM x\nCOPY app ./app\nRUN pip install .", {"truncated": True}), ("FROM x\nCOPY app ./app\nRUN pip install .", {"redacted": True})])
def test_unsupported_or_incomplete_sources_are_not_static_proof(content, extra):
    assert validate_review([file_evidence(content, **extra)], {"findings": [], "edits": []})["claims"] == []


def test_missing_ignore_supports_only_observed_missing_file_edit():
    evidence = [file_evidence("", ".dockerignore", ok=False, found=False, sha256=None)]
    result = validate_review(evidence, {"findings": [], "edits": [{"path": ".dockerignore", "find": "", "replace": ".git\n"}, {"path": "Dockerfile", "find": "", "replace": "FROM invented"}]})
    assert len(result["edits"]) == 1 and result["edits"][0]["claim_id"] == "C1"


def test_failed_file_read_is_not_verified_absence_or_a_basis_for_edit():
    evidence = [file_evidence("", ".dockerignore", ok=False, found=False, error="permission denied")]
    result = validate_review(evidence, {"findings": [], "edits": [{"path": ".dockerignore", "find": "", "replace": ".git\n"}]})
    assert result["claims"] == [] and result["edits"] == []


def test_malformed_observation_is_not_citable_live_evidence():
    from backend.diagnostics import completed_observation, succeeded
    assert not succeeded({"observation": {"raw": "not structured"}})
    assert not completed_observation({"observation": {"ok": False, "observation_status": "complete"}})


def test_semantic_retrieval_handles_paraphrase_and_empty_negative_without_keywords():
    assert {x["id"] for x in retrieve_lessons("인스턴스 교체 후 사진 안 보임", ["K1", "K2"])} == {"K1", "K2"}
    assert retrieve_lessons("도커와 무관한 사진 데이터 손상", []) == []


@pytest.mark.parametrize("lifecycle", ["stopped", "deleted", "recreated"])
def test_education_preserves_safety_but_does_not_repeat_known_lifecycle(lifecycle):
    text = teaching_guide(retrieve_lessons("유실"), {"lifecycle": lifecycle})
    assert "컨테이너를 중지만 했나요" not in text
    assert "앱은 어느 경로" in text and "되살릴 수는 없습니다" in text


def test_education_routing_validates_catalog_and_fact_quote():
    result = asyncio.run(classify_request(Decision(route="education", lesson_ids=["K1", "K2"], known_lifecycle="deleted", lifecycle_quote="없는 문구"), "컨테이너가 안 보여요", []))
    assert result["semantic_lesson_ids"] == ["K1", "K2"] and result["education_context"]["lifecycle"] == "unknown"
    bad = asyncio.run(classify_request(Decision(route="education", lesson_ids=["K999"]), "설명", []))
    assert bad["route"] == "unavailable"
