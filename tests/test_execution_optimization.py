import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import pytest
from pydantic import BaseModel
from fastapi.testclient import TestClient
from backend import observability, usage, server
from backend.config import Settings
from backend.execution_policy import RequestBudget, CURRENT_BUDGET, BudgetExceeded, execute
from backend.llm import ModelGateway
from backend.model_policy import select_model
from backend.mcp_client import DockerMCPClient


class Decision(BaseModel):
    route: str


def test_router_has_own_model_and_schema_failure_escalates_once():
    class Gateway(ModelGateway):
        async def _ask_impl(self, *args, **kwargs):
            model, role = select_model(self.s, kwargs.get("quality", False))
            self.seen.append((model, role))
            return '{}' if role == "router" else '{"route":"chat"}'
    gateway = Gateway(Settings(router_model="tiny", fast_model="fast", quality_model="strong")); gateway.seen = []
    usage.begin()
    value = asyncio.run(gateway.route_json("question", schema=Decision, system="safe"))
    assert value == {"route": "chat"} and gateway.seen == [("tiny", "router"), ("fast", "fast")]
    assert usage.CURRENT_USAGE.get()["router_escalations"] == 1
    assert select_model(gateway.s) == ("fast", "fast") and select_model(gateway.s, True) == ("strong", "quality")


def test_valid_ambiguous_router_result_is_not_escalated():
    class Gateway(ModelGateway):
        async def _ask_impl(self, *args, **kwargs):
            self.seen.append(select_model(self.s)[0]); return '{"route":"clarify"}'
    gateway = Gateway(Settings(router_model="tiny")); gateway.seen = []
    assert asyncio.run(gateway.route_json("unclear", schema=Decision, system="safe"))["route"] == "clarify"
    assert gateway.seen == ["tiny"]


def test_router_project_provenance_failure_escalates_to_fast_without_rule_route():
    from backend.routing import classify_request
    class Gateway(ModelGateway):
        async def _ask_impl(self, *args, **kwargs):
            _, role = select_model(self.s, kwargs.get("quality", False))
            self.seen.append(role)
            if "route" in kwargs["schema"].get("properties", {}):
                return json.dumps({"route":"lookup", "reason":"service state", "needs_context":False, "clarification":None,
                                  "inventory_topics":["status"], "declarations_only":False, "operation":None, "action_quote":None})
            return json.dumps({
                "project_reference":"explicit" if role == "router" else "current", "requested_project":"bizxray" if role == "router" else None,
                "target_scope":"service", "service":"ai"})
    gateway = Gateway(Settings(router_model="tiny")); gateway.seen = []
    value = asyncio.run(classify_request(gateway, "ai 상태", [], allowed_services=("ai",), selected_target={"project":"bizxray"}))
    assert value["route"] == "status" and gateway.seen == ["quality", "router", "fast"]


def test_default_fast_router_still_uses_existing_project_safety_guard():
    from backend.routing import classify_request
    class Gateway(ModelGateway):
        async def _ask_impl(self, *args, **kwargs):
            if kwargs.get("quality") and "route" not in kwargs["schema"].get("properties", {}): return '{"project_reference":"explicit","requested_project":"ai"}'
            if "route" in kwargs["schema"].get("properties", {}):
                return json.dumps({"route":"lookup", "reason":"explicit other project", "needs_context":False, "clarification":None,
                                  "inventory_topics":["status"], "declarations_only":False, "operation":None, "action_quote":None})
            return json.dumps({
                "project_reference":"explicit", "requested_project":"ai", "target_scope":"service", "service":"ai"})
    gateway = Gateway(Settings(router_model=""))
    value = asyncio.run(classify_request(gateway, "bizxray 말고 ai 프로젝트 상태", [], allowed_services=("ai",), selected_target={"project":"bizxray"}))
    assert value["route"] == "target_selection"


def test_model_admission_and_concurrency_are_bounded():
    class Gateway(ModelGateway):
        running = peak = 0
        async def _ask_impl(self, *args, **kwargs):
            self.running += 1; self.peak = max(self.peak, self.running)
            await asyncio.sleep(.01); self.running -= 1; return "ok"
    async def scenario():
        gateway = Gateway(Settings(model_concurrency=1))
        budget = RequestBudget(model_limit=2); token = CURRENT_BUDGET.set(budget)
        try:
            results = await asyncio.gather(*(gateway.text("q") for _ in range(3)), return_exceptions=True)
            assert gateway.peak == 1 and budget.model_calls == 2
            assert sum(isinstance(r, BudgetExceeded) for r in results) == 1
        finally: CURRENT_BUDGET.reset(token)
    asyncio.run(scenario())


def test_read_tools_share_four_slots_and_budget_counts_admitted_calls():
    async def scenario():
        class Session:
            running = peak = 0
            async def call_tool(self, name, args):
                self.running += 1; self.peak = max(self.peak, self.running)
                await asyncio.sleep(.01); self.running -= 1
                return SimpleNamespace(content=[SimpleNamespace(text='{"ok":true}')], structuredContent=None, isError=False)
        client = DockerMCPClient({"READ_TOOL_CONCURRENCY": "4"}); session = Session(); client.sessions["ops"] = session
        budget = RequestBudget(tool_limit=6); token = CURRENT_BUDGET.set(budget)
        try:
            values = await asyncio.gather(*(client.tool("list_containers", {}) for _ in range(8)), return_exceptions=True)
            assert session.peak == 4 and budget.tool_calls == 6
            assert sum(isinstance(v, BudgetExceeded) for v in values) == 2
        finally: CURRENT_BUDGET.reset(token)
    asyncio.run(scenario())


class SlowGraph:
    async def astream(self, initial, **kwargs):
        yield (), "values", initial
        yield (), "custom", {"type":"NodeStarted", "run_id":"real-start", "event_id":"start", "node":"slow", "scope":"supervisor", "agent":"runtime", "timestamp":0}
        await asyncio.sleep(1)


def test_deadline_returns_facts_and_closes_only_real_started_nodes():
    async def scenario():
        usage.begin()
        initial = {"query":"q", "events":[], "evidence":[], "approval":{"id":"must-clear"}}
        values = [x async for x in execute(SlowGraph(), initial, {"configurable":{"thread_id":"t"}}, Settings(request_seconds=.02))]
        final = values[-1][2]
        assert final["answer_status"] == "partial" and final["approval"] is None
        assert [e["run_id"] for e in final["events"] if e["type"] == "NodeFailed"] == ["real-start"]
        assert usage.CURRENT_USAGE.get()["execution_budget"]["stop_reason"] == "request_timeout"
        assert CURRENT_BUDGET.get() is None
    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
def test_api_deadline_is_a_partial_final_not_an_unfinished_stream(stream):
    with TestClient(server.app) as client:
        bundle = server.app.state.bundle; bundle["settings"] = replace(bundle["settings"], request_seconds=.02)
        bundle["graph"] = SlowGraph()
        response = client.get("/chat/stream", params={"message":"q", "thread_id":"timeout"}) if stream else client.post("/chat", json={"message":"q", "thread_id":"timeout"})
        assert response.status_code == 200
        body = next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {"type": "FinalAnswer"')) if stream else response.json()
        assert body["metadata"]["answer_status"] == "partial" and body["approval"] is None
        assert body["metadata"]["execution_budget"]["stop_reason"] == "request_timeout"
        assert not bundle["running_threads"]


def test_langsmith_off_without_key_and_never_exports_raw_data(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False); monkeypatch.setenv("LANGSMITH_TRACING", "true")
    usage.begin()
    with observability.request_trace("sensitive-thread", "ollama"): assert observability.CURRENT_TRACE.get() is None
    calls = []
    class Run:
        id = "test-run"
        def __init__(self, **kwargs): calls.append(kwargs)
        def post(self): pass
        def patch(self): pass
        def create_child(self, **kwargs): return Run(**kwargs)
        def end(self, **kwargs): calls.append(kwargs)
    monkeypatch.setenv("LANGSMITH_API_KEY", "fake-test-only")
    monkeypatch.setattr(observability.ls, "Client", lambda **kwargs: object())
    monkeypatch.setattr(observability, "RunTree", Run)
    with observability.request_trace("sensitive-thread", "ollama"):
        usage.record("json", "tiny", "private-system", "password=secret", "private-output", 10, 3, 15, False, "router")
    exported = json.dumps(calls, default=str)
    assert all(secret not in exported for secret in ("sensitive-thread", "password", "private-system", "private-output", "secret"))
    assert '"total_tokens": 13' in exported and usage.CURRENT_USAGE.get()["langsmith_run_id"] == "test-run"
