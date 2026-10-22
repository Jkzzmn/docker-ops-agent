"""Demo contracts: requested measurements/targets and mutation prerequisites."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from backend import docker_tools as docker
from backend.config_agent import compose_safety_facts
from backend.config_validation import grounded_suggestions, validate_review
from backend.execution_policy import facts_only
from backend.inventory import _usage, _image_details, inventory_answer
from backend.port_owners import inspect_port_owners, requested_ports, observe_port_owners
from backend.routing import classify_request
from backend.runtime_actions import propose_runtime_action


def test_stopped_usage_is_unavailable_not_zero_and_not_limits(monkeypatch):
    monkeypatch.setattr(docker, "_json", lambda args: pytest.fail("stopped containers have no live stats"))
    rows = [{"service": "api", "container_id": "a"*64, "name": "api-1", "state": "exited", "resources": {"Memory": 0}}]
    _usage(rows)
    result = inventory_answer({"runtime": {"ok": True, "containers": rows}}, ["usage"], "runtime", "test")
    assert "사용량을 0으로 추정하지 않습니다" in result["final"] and "Memory" not in result["final"]
    assert result["answer_status"] == "partial" and not result["validation_ok"]


def test_running_usage_binds_measurement_to_container_identity(monkeypatch):
    rows = [{"container_id": "a"*64, "state": "running"}, {"container_id": "b"*64, "state": "running"}]
    monkeypatch.setattr(docker, "_json", lambda args: [{"ID": "a"*12, "CPUPerc": "12.5%", "MemPerc": "2%", "MemUsage": "20MiB / 1GiB"}, {"ID": "c"*12, "CPUPerc": "99%", "MemPerc": "99%"}])
    _usage(rows)
    assert rows[0]["usage"]["cpu_percent"] == 12.5 and rows[0]["usage"]["historical_available"] is False
    assert rows[1]["usage"]["available"] is False


def test_image_details_use_immutable_image_not_current_tag_and_no_env(monkeypatch):
    identity = "sha256:" + "a"*64
    calls = []
    def read(args):
        calls.append(args)
        return [{"Id": identity, "Size": 1048576, "Architecture": "arm64", "Os": "linux", "Config": {"Env": ["SECRET=hidden"], "Labels": {"other": "hidden"}}}]
    monkeypatch.setattr(docker, "_json", read)
    rows = [{"service": "api", "images": {"reference": "mutable:latest", "image_id": identity}}]
    _image_details(rows, {})
    assert calls == [["docker", "image", "inspect", identity]]
    assert rows[0]["image_details"]["size_bytes"] == 1048576 and rows[0]["image_details"]["base_metadata"] == {}
    assert "hidden" not in json.dumps(rows)


class TargetModel:
    async def structured_json(self, prompt, **kwargs):
        return {"route": "incident", "reason": "장애", "needs_context": False, "inventory_topics": [], "declarations_only": False, "operation": None, "action_quote": None, "clarification": None}
    async def route_json(self, prompt, **kwargs):
        return {"project_reference": "current", "requested_project": None, "target_scope": "project", "service": None, "requested_services": ["Backend", "DB"]}


def test_explicit_unknown_names_cannot_be_erased_by_project_scope():
    result = asyncio.run(classify_request(TargetModel(), "Backend에서 DB 연결이 안 돼", [], allowed_services=("api", "worker"), selected_target={"project": "test"}))
    assert result["route"] == "clarify" and "Backend, DB" in result["clarification"]["question"]
    assert "routing_target" not in result


def test_even_valid_model_json_cannot_silently_drop_literal_unknown_container_name():
    class Drops(TargetModel):
        async def route_json(self, *args, **kwargs):
            return {"project_reference": "current", "requested_project": None, "target_scope": "project", "service": None, "requested_services": []}
    value = asyncio.run(classify_request(Drops(), "현재 backend 컨테이너 이미지 확인", [], allowed_services=("api", "worker")))
    assert value["route"] == "clarify" and "backend" in value["clarification"]["question"]


def test_casefold_known_target_does_not_invent_alias():
    class API(TargetModel):
        async def route_json(self, *args, **kwargs):
            return {"project_reference": "current", "requested_project": None, "target_scope": "service", "service": "API", "requested_services": ["API"]}
    result = asyncio.run(classify_request(API(), "API가 죽었어 OOM 확인", [], allowed_services=("api", "worker")))
    assert result["route"] == "incident" and result["routing_target"]["service"] == "api"


def test_model_selected_port_dependency_precedes_runtime_and_missing_demo_context():
    class Ports(TargetModel):
        async def structured_json(self, prompt, **kwargs):
            return {"route": "runtime", "reason": "포트 점유 조회 후 시작", "needs_context": True, "inventory_topics": ["port_owners", "status"], "declarations_only": False, "operation": "stop", "action_quote": "종료하고", "clarification": None}
        async def route_json(self, *args, **kwargs): pytest.fail("Engine-wide discovery must not guess a service")
    value = asyncio.run(classify_request(Ports(), "8000번 포트 좀비를 찾아서 종료하고 데모 띄워줘", [], allowed_services=("api", "worker")))
    assert value["route"] == "inventory" and "port_owners" in value["inventory_topics"]


def test_hallucinated_target_arguments_escalate_once_not_to_guessed_default():
    class Retry(TargetModel):
        async def route_json(self, *args, **kwargs):
            return {"project_reference": "current", "requested_project": None, "target_scope": "project", "service": None, "requested_services": ["api"]}
        async def structured_json(self, prompt, **kwargs):
            if prompt.startswith("Resolve target"):
                return {"project_reference": "current", "requested_project": None, "target_scope": "project", "service": None, "requested_services": []}
            return await super().structured_json(prompt, **kwargs)
    value = asyncio.run(classify_request(Retry(), "현재 프로젝트 장애를 진단해줘", [], allowed_services=("api", "worker")))
    assert value["route"] == "incident" and value["routing_target"]["scope"] == "project"


@pytest.mark.parametrize("query,expected", [("현재 8000번 포트 충돌", [8000]), ("host port 8000 is busy", [8000]), ("8000 포트와 9000 포트", [8000, 9000]), ("80000번 포트", []), ("api 재시작", [])])
def test_port_operand(query, expected): assert requested_ports(query) == expected


def test_port_owner_requires_running_active_host_binding_not_exposed_or_stopped(monkeypatch):
    monkeypatch.setattr(docker, "MODE", "live"); monkeypatch.setattr(docker, "ALLOWED", {"api"}); monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ours")
    template = {"Id": "a"*64, "Name": "/foreign-api", "State": {"Status": "running"}, "Config": {"Env": ["PASSWORD=hidden"], "Labels": {"com.docker.compose.project": "foreign", "com.docker.compose.service": "api"}}, "HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": "8000"}]}}, "NetworkSettings": {"Ports": {"80/tcp": [{"HostPort": "8000"}]}}}
    stopped = copy.deepcopy(template); stopped["Id"] = "b"*64; stopped["State"]["Status"] = "exited"
    def read(args):
        return [{"ID": "a"*12}, {"ID": "b"*12}] if args[1] == "ps" else [template, stopped]
    monkeypatch.setattr(docker, "_json", read)
    value = inspect_port_owners(8000)
    assert len(value["active_owners"]) == 1 and len(value["configured_bindings"]) == 2
    assert not value["active_owners"][0]["within_selected_project"] and "hidden" not in json.dumps(value)


def test_runtime_port_request_never_previews_all_service_restart():
    class MCP:
        async def tool(self, tool, args):
            assert tool == "inspect_port_owners" and args == {"port": 8000}
            return {"ok": True, "active_owners": [], "configured_bindings": [], "note": "host process not checked"}, 0
    deps = SimpleNamespace(mcp=MCP())
    value = asyncio.run(propose_runtime_action(deps, {"query": "8000번 포트 충돌 좀비 종료 후 데모 띄워줘", "runtime_operation": "restart", "routing_target": {"scope": "project"}}))
    assert value["approval"] is None and "아직 중지" in value["final"] and value["result_kind"] == "runtime_action"


def test_compose_security_limit_absence_is_conditional_not_root_or_unlimited():
    value = compose_safety_facts([{"id": "E2", "tool": "validate_compose_config", "observation": {"ok": True, "service_config": {"api": {}, "db": {"deploy": {"resources": {"limits": {"cpus": "1", "memory": "1G"}}}, "read_only": True}}}}])
    text = "\n".join(value)
    assert "CPU 제한=명시 없음" in text and "CPU 제한=1" in text and "메모리 제한=1G" in text
    assert "이미지 USER=root를 단정하지 않습니다" in text and "호스트/상위 제한까지 없다는 뜻은 아닙니다" in text
    assert "no-new-privileges" in text and "cap_drop" in text


def test_copy_chown_only_span_cannot_prove_recursive_layer_overhead_even_if_judge_agrees():
    snippet = "COPY --chown=app:app app /app\nCOPY --chown=app:app data /data"
    evidence = [{"id": "E1", "tool": "read_dockerfile", "args": {"path": "Dockerfile"}, "observation": {"ok": True, "found": True, "sha256": "hash", "content": snippet}}]
    review = {"findings": [{"text": "반복된 COPY --chown이 추가 소유권 레이어를 만들어 크기와 빌드 비용이 커집니다", "snippet": snippet, "rule": "model_review", "claim_kind": "static_risk", "path": "Dockerfile", "evidence_ids": ["E1"]}]}
    checked = validate_review(evidence, review)
    assert grounded_suggestions(checked, review) == []


def test_incident_fallback_keeps_exit_oom_logs_and_does_not_invent_historical_memory():
    value = facts_only({"query": "API OOM 당시 메모리", "evidence": [
        {"id": "E1", "tool": "inspect_container", "args": {"service": "api"}, "observation": {"ok": True, "state": "exited", "exit_code": 137, "oom_killed": False, "restarts": 2}},
        {"id": "E2", "tool": "get_container_logs", "args": {"service": "api"}, "observation": {"ok": True, "stdout": "connection refused"}}]}, "unknown")
    assert "ExitCode=137" in value["final"] and "OOMKilled=False" in value["final"] and "connection refused" in value["final"]
    assert "당시 메모리 사용량을 복원할 수 없습니다" in value["final"] and value["approval"] is None
