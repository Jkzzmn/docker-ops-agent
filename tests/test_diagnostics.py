import asyncio

import pytest

from backend import docker_tools as tools
from backend.approvals import ApprovalStore
from backend.diagnostics import task_key
from backend.graph import Dependencies, build_graph
from tests.model_responses import ScriptedModel


class FixtureMCP:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    async def prompt(self, query):
        return query

    async def named_prompt(self, name, arguments):
        return f"{name}:{arguments}"

    async def resource(self, uri):
        return "Only supplied live observations prove a cause."

    async def tool(self, name, args):
        self.calls.append((name, args))
        if name in self.fail:
            raise TimeoutError("injected timeout")
        return getattr(tools, name)(**args), 0


def run_graph(query, mcp=None, llm=None):
    mcp = mcp or FixtureMCP()
    approvals = ApprovalStore()
    graph = build_graph(Dependencies(llm or ScriptedModel(), mcp, approvals))
    try:
        return asyncio.run(graph.ainvoke({"query": query, "thread_id": "test", "events": [], "evidence": [], "replan_rounds": 0, "validation_attempts": 0}, {"configurable": {"thread_id": "run"}}))
    finally:
        approvals.close()


@pytest.mark.parametrize("scenario,query,cause", [
    ("oom", "api 컨테이너 재시작", "memory"),
    ("readiness", "api에서 db 연결 거부, 준비 상태 점검", "readiness"),
    ("network", "web에서 db DNS 연결 불가", "network"),
    ("healthy", "api 서비스 상태 점검", "healthy"),
    ("exit137", "api exit 137 장애", "unknown"),
])
def test_scenarios_use_observations_not_query_or_static_oom_response(monkeypatch, scenario, query, cause):
    monkeypatch.setattr(tools, "SCENARIO", scenario)
    monkeypatch.setattr(tools, "RECOVERED", set())
    state = run_graph(query)
    assert state["diagnosis"]["cause"] == cause
    assert state["evidence_coverage"] == (None if cause == "unknown" else 1)
    assert state["validation_ok"] is (cause != "unknown")
    assert bool(state["approval"]) == (cause == "memory")
    assert state["replan_rounds"] <= 2


def test_partial_failure_is_not_counted_as_coverage_or_eligible_for_mutation(monkeypatch):
    monkeypatch.setattr(tools, "RECOVERED", set())
    mcp = FixtureMCP(fail={"get_container_stats"})
    state = run_graph("api 컨테이너 재시작", mcp=mcp)
    assert state["evidence_coverage"] is None and state["answer_status"] == "partial"
    assert state["evidence_sufficient"] is False
    assert state["approval"] is None
    assert any(e["type"] == "TaskFailed" for e in state["events"])
    assert any(e.get("exhausted") for e in state["events"])


def test_failed_validator_never_creates_an_approval():
    class RejectingModel(ScriptedModel):
        async def json(self, prompt, *, quality=False, system=""):
            if prompt.startswith("Validate"):
                return {"ok": False, "problems": ["injected rejection"]}
            return await super().json(prompt, quality=quality, system=system)
    state = run_graph("api 재시작 장애", llm=RejectingModel())
    assert state["validation_attempts"] == 1
    assert state["answer_status"] == "partial" and "확인된 관측만" in state["final"]
    assert state["validation_ok"] is False
    assert state["approval"] is None
    assert state["final"].startswith("[검증 미통과")
    assert not any(e["type"] == "AnswerToken" for e in state["events"])


def test_dangerous_query_never_reaches_mcp():
    mcp = FixtureMCP()
    state = run_graph("docker system prune 자동 실행", mcp=mcp)
    assert mcp.calls == []
    assert state["approval"] is None
    assert any(e["type"] == "PolicyBlocked" for e in state["events"])


def test_task_identity_ignores_explanation_and_default_tail():
    assert task_key({"tool": "get_container_logs", "args": {"service": "api", "tail": 100}, "why": "one"}) == task_key({"tool": "get_container_logs", "args": {"service": "api"}, "why": "two"})


def test_gate_discards_write_unknown_service_and_duplicate_tasks():
    class BadPlanner(ScriptedModel):
        async def json(self, prompt, *, quality=False, system=""):
            if "Create a minimal" in prompt:
                return {"tasks": [{"tool": "restart_service", "args": {"service": "api"}},
                                  {"tool": "inspect_container", "args": {"service": "outside"}},
                                  {"tool": "list_containers", "args": {}, "why": "one"},
                                  {"tool": "list_containers", "args": {}, "why": "two"}]}
            return await super().json(prompt, quality=quality, system=system)
    mcp = FixtureMCP()
    state = run_graph("api 컨테이너 상태", mcp=mcp, llm=BadPlanner())
    assert all(name != "restart_service" and args.get("service") != "outside" for name, args in mcp.calls)
    assert state["tasks"] == [{"tool": "list_containers", "args": {}, "why": "one"}]


def test_oom_event_corroborates_cause_when_current_stats_are_unavailable():
    state = run_graph("api 컨테이너 장애", mcp=FixtureMCP(fail={"get_container_stats"}))
    assert state["diagnosis"]["cause"] == "memory"
    assert state["approval"] is None
    ids = set(state["diagnosis"]["evidence_ids"])
    assert any(e["id"] in ids and e["tool"] == "get_container_events" for e in state["evidence"])


def test_evicted_oom_events_still_report_authoritative_exit_but_never_approve():
    class EvictedEvents(FixtureMCP):
        async def tool(self, name, args):
            if name == "get_container_events": return {"ok": True, "events": []}, 0
            if name == "get_container_logs": return {"ok": True, "logs": "allocated 100MiB"}, 0
            return await super().tool(name, args)
    state = run_graph("api 컨테이너 장애", mcp=EvictedEvents(fail={"get_container_stats"}))
    assert state["diagnosis"]["cause"] == "memory"
    assert state["diagnosis"]["corroborated"] is False
    assert state["approval"] is None
