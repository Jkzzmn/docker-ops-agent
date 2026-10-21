import asyncio

import pytest

from backend.agents import build_observation_agent
from backend.planning import normalize_tasks
from tests.routing_double import route_request
from tests.model_responses import ScriptedModel
from tests.test_diagnostics import FixtureMCP, run_graph


@pytest.mark.parametrize("query,expected", [
    ("안녕하세요", "chat"), ("Docker가 무엇인가요?", "chat"), ("OOM이 뭔가요?", "chat"),
    ("Docker exec 개념을 설명해주세요", "chat"), ("api 상태만 알려주세요", "status"),
    ("Docker 컨테이너 목록만 보여주세요", "status"), ("api가 계속 재시작됩니다", "incident"),
    ("현재 api 장애 원인을 설명해주세요", "incident"), ("web에서 db 연결이 안 됩니다", "incident"),
    ("docker system prune 자동 실행", "blocked"),
    ("goodbye", "chat"), ("api status only", "status"),
])
def test_scripted_route_contract(query, expected):
    assert route_request(query)[0] == expected


def test_simple_concept_never_plans_or_calls_mcp():
    mcp, model = FixtureMCP(), ScriptedModel()
    state = run_graph("Docker가 뭔가요?", mcp, model)
    assert state["route"] == "chat" and state["final"]
    assert mcp.calls == []
    assert [call[0] for call in model.calls] == ["json", "stream"]
    assert not any(e["type"] == "AgentStarted" for e in state["events"])


def test_status_only_executes_one_tool_and_skips_analysis_and_planning_model():
    mcp, model = FixtureMCP(), ScriptedModel()
    state = run_graph("api 상태만 알려주세요", mcp, model)
    assert state["route"] == "status" and state["approval"] is None
    assert mcp.calls == [("check_service_health", {"service": "api"})]
    assert not state.get("analysis_used")
    assert not any(prompt.startswith(("Create", "Refine", "Review")) for _, prompt, _ in model.calls)
    assert any(e["type"] == "AnalysisSkipped" for e in state["events"])


@pytest.mark.parametrize("policy,review_next,analysis_used", [("skip", "analysis", False), ("required", "write", True), ("auto", "write", False), ("auto", "analysis", False)])
def test_planning_and_supervisor_decisions_control_optional_analysis(policy, review_next, analysis_used):
    model = ScriptedModel()
    model.analysis_policy, model.review_next = policy, review_next
    state = run_graph("api 컨테이너 재시작 장애", llm=model)
    assert state.get("analysis_used", False) is analysis_used
    assert any(e["type"] == "AgentStarted" and e["agent"] == "analysis" for e in state["events"]) is analysis_used


def test_missing_evidence_returns_facts_without_model_analysis_or_writer():
    model = ScriptedModel(); model.analysis_policy = "skip"
    state = run_graph("api 장애 원인", FixtureMCP(fail={"get_container_stats"}), model)
    assert not state.get("analysis_used")
    assert state["answer_status"] == "partial" and "확인된 관측만" in state["final"]
    assert not any(kind in {"text", "stream"} for kind, _, _ in model.calls)
    assert state["approval"] is None
    # Transport retries belong to MCP, not a second graph collection round.
    assert state["replan_rounds"] == 1


def test_supervisor_delegates_to_independent_worker_subgraphs(monkeypatch):
    monkeypatch.setenv("WORKER_REFINEMENT", "1")
    from backend import docker_tools
    monkeypatch.setattr(docker_tools, "SCENARIO", "network")
    model = ScriptedModel()
    state = run_graph("web에서 db 네트워크 연결 장애", llm=model)
    agents = {e["agent"] for e in state["events"] if e["type"] == "AgentStarted"}
    assert {"runtime", "configuration", "network", "writer", "validator"} <= agents
    assert {r["agent"] for r in state["worker_reports"]} == {"runtime", "configuration", "network"}
    systems = {system for _, _, system in model.calls}
    assert any("supervisor agent" in s for s in systems)
    for role in ("runtime", "configuration", "network"):
        assert any(f"{role} diagnostics sub-agent" in s for s in systems)
    assert len({e["id"] for e in state["evidence"]}) == len(state["evidence"])


def test_worker_cannot_expand_its_capabilities_or_target_scope():
    class OutOfScope(ScriptedModel):
        async def json(self, *args, **kwargs):
            return {"tasks": [{"tool": "restart_service", "args": {"service": "api"}},
                              {"tool": "test_container_dns", "args": {"source": "api", "target": "db"}},
                              {"tool": "check_service_health", "args": {"service": "db"}}]}
    mcp = FixtureMCP()
    worker = build_observation_agent("runtime", OutOfScope(), mcp, ("api", "db", "web"))
    assert set(worker.get_graph().nodes) == {"__start__", "plan", "observe", "report", "__end__"}
    result = asyncio.run(worker.ainvoke({"query": "api 상태", "assigned_tasks": [{"tool": "check_service_health", "args": {"service": "api"}}], "known_tasks": []}))
    assert mcp.calls == [("check_service_health", {"service": "api"})]
    assert result["report"]["agent"] == "runtime"


def test_no_legacy_model_backend():
    from backend.config import Settings
    with pytest.raises(ValueError):
        Settings(backend="unsupported")


def test_malformed_tool_name_is_discarded():
    assert normalize_tasks([{"tool": ["restart_service"], "args": {}}], ("api",), "api") == []
