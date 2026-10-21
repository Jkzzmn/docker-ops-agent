"""Regression tests for the review findings: masking, command detection, stopped-container OOM, port/binding causes,
config proposals, slash commands, stateless graph, MCP surface, failure handling and token accounting."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import docker_tools as tools
from backend import usage
from backend.config import Settings
from backend.diagnostics import contains_dangerous
from backend.graph import Dependencies, build_graph
from backend.llm import ModelGateway
from backend.planning import unsafe_execution_request
from backend.routing import parse_command
from backend.sanitize import redact
from backend.server import app
from tests.model_responses import ScriptedModel
from tests.test_diagnostics import FixtureMCP, run_graph


# --- masking -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,secret", [
    ('{"password": "hunter2"}', "hunter2"),
    ("DATABASE_URL=postgres://user:s3cret@db:5432/app", "s3cret"),
    ("Authorization: Bearer abc.def.ghi1234", "abc.def.ghi1234"),
    ("MY_SECRET_KEY=abc123xyz", "abc123xyz"),
    ("AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ('api_key: "abcd1234"', "abcd1234"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
])
def test_redaction_covers_json_urls_bearer_and_prefixed_keys(text, secret):
    assert secret not in redact(text)


def test_redaction_is_idempotent_and_keeps_diagnostic_text():
    once = redact("password=hunter2 connection refused db:5432 allocated 92 MiB")
    assert redact(once) == once == "password=[REDACTED] connection refused db:5432 allocated 92 MiB"


# --- dangerous commands vs ordinary words -------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["api 컨테이너 시작 시 powershell 스크립트 에러 로그 점검해줘", "db shell 접속이 안 돼요 연결 점검", "Docker exec 개념을 설명해주세요"])
def test_plain_words_are_not_execution_requests(query):
    assert unsafe_execution_request(query) is False


@pytest.mark.parametrize("query", ["docker system prune 자동 실행", "docker exec -it api sh 실행해줘", "docker compose down -v 실행", "임의 셸 명령 실행해줘", "rm -rf / 자동 실행"])
def test_command_shaped_requests_are_blocked(query):
    assert unsafe_execution_request(query) is True


def test_draft_check_flags_commands_not_the_word_shell():
    assert not contains_dangerous("컨테이너 내부 shell 환경 변수는 확인하지 않았습니다.")
    assert contains_dangerous("docker exec -it api sh 로 접속하세요")
    assert contains_dangerous("docker system prune 을 실행하세요")


# --- stopped OOM container: reachable approval -----------------------------------------------------------------
def test_stopped_oom_container_reaches_approval_without_current_stats(monkeypatch):
    monkeypatch.setattr(tools, "SCENARIO", "oom-exited")
    monkeypatch.setattr(tools, "RECOVERED", set())
    state = run_graph("api 컨테이너 장애 원인")
    assert state["diagnosis"]["cause"] == "memory" and state["diagnosis"]["corroborated"]
    assert state["evidence_sufficient"] is True
    stats = [e for e in state["evidence"] if e["tool"] == "get_container_stats"]
    assert all(e["observation"]["ok"] is False for e in stats)  # unavailable, and still not demanded
    assert state["approval"]["tool"] == "restart_service" and state["approval"]["status"] == "pending"


def test_stopped_container_still_needs_independent_oom_evidence(monkeypatch):
    monkeypatch.setattr(tools, "SCENARIO", "oom-exited")
    class NoEvents(FixtureMCP):
        async def tool(self, name, args):
            if name == "get_container_events": return {"ok": True, "events": []}, 0
            if name == "get_container_logs": return {"ok": True, "logs": "allocated 100MiB"}, 0
            return await super().tool(name, args)
    state = run_graph("api 컨테이너 장애 원인", mcp=NoEvents())
    assert state["diagnosis"]["cause"] == "memory" and state["diagnosis"]["corroborated"] is False
    assert state["approval"] is None


def test_recovery_after_restart_resets_oom_state_and_polls_until_healthy(monkeypatch):
    monkeypatch.setenv("DOCKER_FIXTURE_SCENARIO", "oom-exited")
    monkeypatch.setenv("RECOVERY_WAIT_SECONDS", "5")
    with TestClient(app) as client:
        body = client.post("/chat", json={"message": "api 컨테이너 장애 원인", "thread_id": "stopped"}).json()
        assert body["approval"]["status"] == "pending"
        mcp, original, seen = app.state.bundle["mcp"], app.state.bundle["mcp"].tool, {"after": 0, "restarted": False}
        async def starting_first(name, args, **kwargs):
            value, retries = await original(name, args, **kwargs)
            if name == "restart_service": seen["restarted"] = True
            if name == "check_service_health" and seen["restarted"]:
                seen["after"] += 1
                if seen["after"] < 3: value = value | {"health": "starting"}
            return value, retries
        mcp.tool = starting_first
        verified = client.post(f"/actions/{body['approval']['id']}/approve").json()
    assert seen["after"] >= 3 and verified["recovery_ok"] is True
    assert verified["inspect_before"]["oom_killed"] is True and verified["inspect_before"]["state"] == "exited"
    assert verified["inspect_after"] == {"state": "running", "health": "healthy", "exit_code": 0, "oom_killed": False}


# --- host port / loopback binding ---------------------------------------------------------------------------------
def test_host_published_port_used_between_containers_is_diagnosed(monkeypatch):
    monkeypatch.setattr(tools, "SCENARIO", "host-port")
    state = run_graph("api에서 db 연결이 안 됩니다")
    assert state["diagnosis"]["cause"] == "network" and "15432" in state["diagnosis"]["reason"] and "5432" in state["diagnosis"]["reason"]
    assert state["approval"] is None
    assert state["proposal"]["kind"] == "connection-setting" and state["proposal"]["applied"] is False


class FullPlanner(ScriptedModel):
    """Plans the baseline like a real planner would, so the three dependency tiers fit into two replans."""
    async def json(self, prompt, *, quality=False, system=""):
        if prompt.startswith("Create a minimal"):
            names = ("inspect_container", "get_container_logs", "get_container_events", "check_service_health")
            return {"tasks": [{"tool": "get_docker_engine_status", "args": {}}, {"tool": "list_containers", "args": {}}, {"tool": "validate_compose_config", "args": {}},
                              *[{"tool": n, "args": {"service": "api"}} for n in names]], "analysis_policy": "auto"}
        return await super().json(prompt, quality=quality, system=system)


def test_loopback_only_listener_is_diagnosed_after_asking_the_target(monkeypatch):
    monkeypatch.setattr(tools, "SCENARIO", "loopback")
    mcp = FixtureMCP()
    state = run_graph("api에서 db 연결이 안 됩니다", mcp=mcp, llm=FullPlanner())
    assert state["diagnosis"]["cause"] == "network" and "loopback" in state["diagnosis"]["reason"]
    assert ("inspect_listening_ports", {"service": "db"}) in mcp.calls
    assert state["evidence_sufficient"] is True and state["approval"] is None
    assert state["proposal"]["kind"] == "service-setting"


def test_listening_ports_are_not_requested_when_dependency_is_simply_not_ready(monkeypatch):
    monkeypatch.setattr(tools, "SCENARIO", "readiness")
    mcp = FixtureMCP()
    run_graph("api에서 db 연결 거부, 준비 상태 점검", mcp=mcp)
    assert not any(name == "inspect_listening_ports" for name, _ in mcp.calls)


# --- reviewed proposals (never applied) ---------------------------------------------------------------------------
def test_readiness_proposal_is_a_reviewable_diff_that_is_never_applied(monkeypatch):
    monkeypatch.setattr(tools, "SCENARIO", "readiness")
    state = run_graph("api에서 db 연결 거부, 준비 상태 점검")
    proposal = state["proposal"]
    assert proposal["applied"] is False and proposal["kind"] == "compose-override"
    assert "healthcheck" in proposal["yaml"] and "condition: service_healthy" in proposal["yaml"] and "pg_isready" in proposal["yaml"]
    assert state["approval"] is None
    assert any(e["type"] == "ProposalCreated" and e["applied"] is False for e in state["events"])


def test_network_separation_proposal_joins_the_target_network(monkeypatch):
    monkeypatch.setattr(tools, "SCENARIO", "network")
    state = run_graph("web에서 db DNS 연결 불가")
    assert state["proposal"]["kind"] == "compose-override" and "aliases: [db]" in state["proposal"]["yaml"]


# --- MCP prompts as slash commands --------------------------------------------------------------------------------
def test_slash_command_parsing_is_strict():
    allowed = ("api", "db", "web")
    assert parse_command("/diagnose-oom api", allowed)["prompt"] == "diagnose_oom"
    command = parse_command("/diagnose-network api db", allowed)
    assert command["args"] == {"service_a": "api", "service_b": "db"} and command["query"].startswith("api에서 db")
    assert parse_command("/diagnose-oom", allowed) is None  # malformed: normal classification instead
    assert parse_command("/diagnose-oom a b", allowed) is None
    assert "허용 서비스" in parse_command("/diagnose-oom redis", allowed)["error"]
    assert parse_command("안녕하세요", allowed) is None


def test_slash_command_uses_the_named_mcp_prompt_and_skips_llm_routing(monkeypatch):
    mcp, model = FixtureMCP(), ScriptedModel()
    state = run_graph("/diagnose-oom api", mcp, model)
    assert state["route"] == "incident" and state["route_method"] == "slash-command"
    assert state["prompt_used"].startswith("diagnose_oom:") and "'service': 'api'" in state["prompt_used"]
    assert not any(prompt.startswith("Classify") for _, prompt, _ in model.calls)


def test_slash_command_for_unknown_service_is_blocked_before_any_tool():
    mcp = FixtureMCP()
    state = run_graph("/diagnose-oom redis", mcp)
    assert state["route"] == "blocked" and mcp.calls == []


# --- checklist node names, gate analysis check, stateless graph ---------------------------------------------------
def test_checklist_node_names_and_gate_validates_analysis_policy():
    mcp, model = FixtureMCP(), ScriptedModel()
    model.analysis_policy = "bogus"
    state = run_graph("api 컨테이너 장애", mcp, model)
    nodes = [e["node"] for e in state["events"] if e["type"] == "NodeStarted" and e["scope"] == "supervisor"]
    assert nodes[:6] == ["route", "plan", "gate", "research", "replan_check", "gate"]
    assert {"write", "validate", "finalize"} <= set(nodes)
    assert next(e for e in state["events"] if e["type"] == "GateApplied")["analysis_policy"] == "auto"


def test_graph_is_stateless_no_checkpointer():
    from backend.approvals import ApprovalStore
    store = ApprovalStore()
    graph = build_graph(Dependencies(ScriptedModel(), FixtureMCP(), store))
    store.close()
    assert graph.checkpointer is None


# --- MCP surface over real stdio -----------------------------------------------------------------------------------
def test_mcp_exposes_prompts_runbook_aliases_and_compose_resource():
    with TestClient(app) as client:
        mcp = app.state.bundle["mcp"]
        listed = client.portal.call(mcp.session.list_prompts)
        assert {p.name for p in listed.prompts} >= {"docker_incident_prompt", "diagnose_network", "diagnose_oom"}
        assert "OOMKilled" in client.portal.call(mcp.resource, "docker://runbooks/oom-killed")
        assert client.portal.call(mcp.resource, "docker://runbooks/oom") == client.portal.call(mcp.resource, "docker://runbooks/oom-killed")
        compose = client.portal.call(mcp.resource, "docker://projects/demo-app/compose")
        assert "depends_on" in compose
        assert "diagnose_oom" not in compose
        text = client.portal.call(mcp.named_prompt, "diagnose_network", {"service_a": "api", "service_b": "db"})
        assert "inspect_listening_ports" in text
        tool_names = {t.name for t in client.portal.call(mcp.session.list_tools).tools}
        assert "inspect_listening_ports" in tool_names and not {"run_shell_command", "recreate_service", "apply_compose_patch"} & tool_names


def test_compose_resource_refuses_other_projects():
    with pytest.raises(ValueError):
        tools.read_compose_text("someone-elses-project")


# --- failure handling -----------------------------------------------------------------------------------------------
def test_unavailable_mcp_prompt_does_not_stop_the_graph():
    class NoPrompt(FixtureMCP):
        async def prompt(self, query):
            raise ConnectionError("prompt provider down")
    state = run_graph("api 컨테이너 장애", mcp=NoPrompt())
    assert state["final"] and any(e["type"] == "PromptUnavailable" for e in state["events"])


def test_chat_model_outage_returns_an_honest_message():
    class Down(ScriptedModel):
        async def stream_text(self, prompt, *, quality=True, system=""):
            raise httpx.ConnectError("ollama down")
            yield ""
    state = run_graph("안녕하세요", llm=Down())
    assert "모델 응답을 받지 못했습니다" in state["final"] and "수행하지 않았습니다" in state["final"]
    assert any(e["type"] == "ModelError" for e in state["events"])


def test_writer_outage_falls_back_to_cited_rule_summary():
    class WriterDown(ScriptedModel):
        async def stream_text(self, prompt, *, quality=True, system=""):
            raise httpx.ReadTimeout("slow")
            yield ""
    state = run_graph("api 컨테이너 장애", llm=WriterDown())
    assert state["draft"].startswith("모델 응답을 받지 못해 규칙 기반 요약만 제공합니다")
    assert "[E" in state["draft"] and any(e["type"] == "ModelError" and e["agent"] == "writer" for e in state["events"])


def test_analysis_outage_is_reported_not_fatal():
    class AnalysisDown(ScriptedModel):
        async def text(self, prompt, *, quality=True, system=""):
            raise httpx.ConnectError("down")
    model = AnalysisDown(); model.analysis_policy = "required"
    state = run_graph("api 컨테이너 장애", llm=model)
    assert state["analysis_used"] and "분석 모델 호출에 실패했습니다" in state["analysis"]


def test_chat_endpoint_reports_graph_failure_as_502_without_mutation(monkeypatch):
    with TestClient(app) as client:
        async def boom(*args, **kwargs):
            raise RuntimeError("password=hunter2 graph exploded")
            yield
        monkeypatch.setattr(app.state.bundle["graph"], "astream", boom)
        response = client.post("/chat", json={"message": "api 장애", "thread_id": "boom"})
    assert response.status_code == 502 and "hunter2" not in response.text and "변경 작업은 수행하지 않았습니다" in response.text


# --- token accounting and metrics -----------------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_gateway_records_tokens_for_json_and_stream_calls(monkeypatch, backend):
    def handle(request):
        payload = json.loads(request.content)
        if payload.get("stream"):
            if backend == "ollama":
                text = "\n".join(json.dumps(x) for x in ({"message": {"content": "hi"}}, {"message": {"content": ""}, "done": True, "prompt_eval_count": 11, "eval_count": 3}))
            else:
                text = "\n".join("data: " + json.dumps(x) for x in ({"choices": [{"delta": {"content": "hi"}}]}, {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3}})) + "\ndata: [DONE]\n"
            return httpx.Response(200, text=text)
        if backend == "ollama":
            return httpx.Response(200, json={"message": {"content": '{"ok":true}'}, "prompt_eval_count": 20, "eval_count": 5})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 20, "completion_tokens": 5}})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client_type(*a, **kw, transport=httpx.MockTransport(handle)))
    monkeypatch.setenv("OPENAI_API_KEY", "transport-test-only")
    gateway = ModelGateway(Settings(backend=backend))
    async def run():
        ledger = usage.begin(capture_io=True)
        await gateway.json("plan", quality=True)
        _ = [c async for c in gateway.stream_text("write", quality=False)]
        return ledger
    ledger = asyncio.run(run())
    summary = usage.summarize(ledger)
    assert summary["model_calls"] == 2 and summary["quality_calls"] == 1 and summary["fast_calls"] == 1
    assert summary["prompt_tokens"] == 31 and summary["completion_tokens"] == 8 and summary["total_tokens"] == 39
    assert ledger["calls"][1]["response"] == "hi" and ledger["calls"][0]["prompt"] == "plan"


def test_metrics_count_blocked_requests_and_replans():
    with TestClient(app) as client:
        client.post("/chat", json={"message": "docker system prune 자동 실행", "thread_id": "m1"})
        client.post("/chat", json={"message": "api 컨테이너 장애", "thread_id": "m2"})
        counters = client.get("/metrics").json()["counters"]
    assert counters["policy_blocked_requests"] == 1 and counters["requests"] == 2
    assert counters["replanned_requests"] == 1 and counters["replan_resolved_requests"] == 1
    assert counters["approvals_created"] == 1


def test_parallel_workers_record_tool_time_for_speedup_evidence():
    state = run_graph("api 컨테이너 장애")
    assert state["tool_time_sum"] > 0 and state["parallel_elapsed"] > 0
    assert all("elapsed_ms" in e for e in state["evidence"] if e["tool"] != "runbook_resource")


# --- writer prompt: failed observations cannot be cited ---------------------------------------------------------------
def test_writer_sees_failed_observations_only_as_unavailable_without_ids():
    model = ScriptedModel()
    state = run_graph("api 컨테이너 장애", mcp=FixtureMCP(fail={"get_container_stats"}), llm=model)
    failed_id = next(e["id"] for e in state["evidence"] if e["tool"] == "get_container_stats")
    assert not any(kind == "stream" for kind, _, _ in model.calls)
    from backend.agents import build_writer_agent
    asyncio.run(build_writer_agent(model).ainvoke({"query": state["query"], "route": "incident", "evidence": state["evidence"], "diagnosis": state["diagnosis"], "attempt": 1, "events": []}))
    prompt = next(p for kind, p, _ in model.calls if kind == "stream" and "Diagnosis=" in p)
    evidence_part = prompt.split("Evidence=", 1)[1].split("UnavailableObservations", 1)[0]
    unavailable_part = prompt.split("UnavailableObservations", 1)[1].split("Runbook", 1)[0]
    assert f'"{failed_id}"' not in evidence_part and "get_container_stats" in unavailable_part and failed_id not in unavailable_part
    assert "runbook_resource" not in evidence_part


# --- semantic decisions are no longer overwritten by keyword routing ------------------------------------------------
@pytest.mark.parametrize("query", [
    "api 컨테이너 상태 진단", "api에서 db port 연결 점검", "api db Docker network 대역 확인",
    "api depends_on healthcheck 상태 점검", "api 상태만 알려주세요", "db 연결 상태만 알려주세요", "Docker 컨테이너 목록만 보여주세요",
])
def test_keywords_do_not_override_the_model_route(query):
    from backend.routing import classify_request
    class AlwaysStatus(ScriptedModel):
        async def json(self, prompt, **kwargs):
            return {"route": "status", "reason": "상태 조회로 분류"}
    result = asyncio.run(classify_request(AlwaysStatus(), query, []))
    assert (result["route"], result["route_method"]) == ("status", "llm")
