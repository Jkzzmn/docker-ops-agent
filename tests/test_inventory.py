import asyncio
import copy
import json

import pytest

from backend import docker_tools as docker
from backend.approvals import ApprovalStore
from backend.execution_policy import align_answer
from backend.graph import Dependencies, build_graph
from backend.inventory import inspect_project_configuration, inventory_answer
from backend.llm import ModelGateway
from backend.config import Settings
from backend.routing import classify_request, TargetDecision
from backend.state import fresh_state


@pytest.fixture
def inventory(monkeypatch):
    monkeypatch.setattr(docker, "MODE", "live")
    monkeypatch.setattr(docker, "ALLOWED", {"api", "worker"})
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "test-project")
    calls = []
    container = {"Id": "a"*64, "Name": "/test-api-1", "State": {"Status": "exited"}, "Image": "sha256:actual",
        "Config": {"Image": "old-api:v1", "Env": ["PASSWORD=never-expose"], "Cmd": ["secret-command"], "Labels": {"com.docker.compose.service": "api", "com.docker.compose.project": "test-project"}, "ExposedPorts": {"8000/tcp": {}}},
        "HostConfig": {"Memory": 0, "PortBindings": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}, "RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [{"Type": "bind", "Source": "/srv/app/data", "Destination": "/data", "RW": True, "unneeded": "never-copy"}],
        "NetworkSettings": {"Ports": {}, "Networks": {"test_default": {"IPAddress": "10.0.0.2"}}}}
    def read(args):
        calls.append(args)
        if args[:3] == ["docker", "compose", "ps"]: return [{"Service": "api", "ID": "a"*12}, {"Service": "outside", "ID": "b"*12}]
        assert args == ["docker", "inspect", "a"*12]
        return [copy.deepcopy(container)]
    monkeypatch.setattr(docker, "_json", read)
    monkeypatch.setattr(docker, "validate_compose_config", lambda: {"ok": True, "service_config": {"api": {"image": "new-api:v2", "volumes": [{"type": "volume", "source": "declared", "target": "/new"}]}, "worker": {}}})
    return container, calls


def answer(value, topics=("images", "mounts"), source="both"):
    return inventory_answer(value, list(topics), source, "test-project")


def test_inventory_keeps_declared_and_actual_configuration_separate_without_secret_fields(inventory):
    value = inspect_project_configuration(["api", "worker"])
    assert value["runtime"]["containers"][0]["images"] == {"reference": "old-api:v1", "image_id": "sha256:actual"}
    assert value["compose"]["services"]["api"]["images"]["reference"] == "new-api:v2"
    assert value["runtime"]["services_without_containers"] == ["worker"]
    text = json.dumps(value)
    assert all(secret not in text for secret in ("never-expose", "secret-command", "unneeded", "10.0.0.2", "outside"))
    result = answer(value)
    assert result["answer_status"] == "validated" and result["approval"] is None
    assert "old-api:v1" in result["final"] and "new-api:v2" in result["final"]
    assert "/srv/app/data → /data" in result["final"] and "worker: 대응 컨테이너 없음" in result["final"]
    assert "healthcheck" not in result["final"] and "비효율" not in result["final"]
    assert inventory[1] == [["docker", "compose", "ps", "--all", "--format", "json"], ["docker", "inspect", "a"*12]]


@pytest.mark.parametrize("services", [[], ["outside"], ["api", "api"], ["api; whoami"]])
def test_inventory_rejects_unscoped_or_duplicate_services_before_commands(inventory, services):
    with pytest.raises(ValueError): inspect_project_configuration(services)
    assert inventory[1] == []


def test_compose_only_does_not_inspect_or_infer_actual_configuration(inventory):
    value = inspect_project_configuration(["api"], "compose")
    assert "runtime" not in value and inventory[1] == []
    text = answer(value, source="compose")["final"]
    assert "실제 컨테이너 설정" not in text and "old-api:v1" not in text


@pytest.mark.parametrize("corruption", ["project", "service", "identity", "incomplete"])
def test_runtime_scope_and_completeness_failures_are_not_absence(inventory, corruption):
    item = inventory[0]
    if corruption == "project": item["Config"]["Labels"]["com.docker.compose.project"] = "foreign"
    elif corruption == "service": item["Config"]["Labels"]["com.docker.compose.service"] = "foreign"
    elif corruption == "identity": item["Id"] = "b"*64
    else: item.pop("Mounts")
    value = inspect_project_configuration(["api"])
    assert value["runtime"]["ok"] is False and value["compose"]["ok"] is True
    result = answer(value)
    assert result["answer_status"] == "partial" and "조회 실패" in result["final"]
    assert "마운트 없음" not in result["final"] and "대응 컨테이너 없음" not in result["final"]


def test_ports_and_resources_do_not_claim_live_reachability_or_usage(inventory):
    value = inspect_project_configuration(["api"])
    text = answer(value, ("ports", "resources"))["final"]
    assert "8080" in text and "active_bindings" in text and "연결 테스트는 하지 않았습니다" in text
    assert '"Memory": 0' in text and "사용량 측정이 아닙니다" in text


def test_fully_failed_lookup_never_reports_empty_mounts(inventory, monkeypatch):
    monkeypatch.setattr(docker, "_json", lambda args: (_ for _ in ()).throw(ConnectionError("engine unavailable")))
    value = inspect_project_configuration(["api"], "runtime")
    result = answer(value, ("mounts",), "runtime")
    assert result["answer_status"] == "unavailable" and result["validation_ok"] is False
    assert "마운트 없음" not in result["final"] and "engine unavailable" in result["final"]


class InventoryModel:
    def __init__(self, **fields): self.fields = fields
    async def json(self, prompt, **kwargs):
        assert prompt.startswith("Classify intent")
        return {"route": "inventory", "reason": "현재 구성 사실 조회", "needs_context": False,
                "project_reference": "current", "requested_project": None, "target_scope": "project", "target_cardinality": "all", "service": None,
                "inventory_topics": ["mounts"], "inventory_source": "both", **self.fields}


def test_inventory_graph_runs_two_nodes_one_read_and_no_writer_or_review(inventory):
    class MCP:
        async def tool(self, name, args):
            assert name == "inspect_project_configuration"
            return inspect_project_configuration(**args), 0
    approvals = ApprovalStore()
    try:
        graph = build_graph(Dependencies(InventoryModel(), MCP(), approvals, allowed_services=("api", "worker"), selected_target={"project": "test-project"}))
        value = asyncio.run(graph.ainvoke(fresh_state(query="현재 도커 볼륨 마운트는 어떻게 되어있나요?", thread_id="inventory")))
    finally: approvals.close()
    assert value["route"] == "inventory" and value["result_kind"] == "configuration_inventory"
    assert [e["node"] for e in value["events"] if e["type"] == "NodeStarted"] == ["route", "inventory_observe"]
    assert value["approval"] is None and len(value["evidence"]) == 1


@pytest.mark.parametrize("topic", ["images", "mounts", "ports", "networks", "resources", "restart", "healthcheck", "dependencies", "build"])
def test_inventory_topics_and_scope_are_model_fields_not_keyword_fallback(topic):
    value = asyncio.run(classify_request(InventoryModel(inventory_topics=[topic]), "질문 원문", [], allowed_services=("api",), selected_target={"project": "test-project"}))
    assert value["route"] == "inventory" and value["inventory_topics"] == [topic]
    assert value["routing_target"] == {"scope": "project", "service": None}


@pytest.mark.parametrize("topic", ["build", "dependencies"])
def test_declaration_only_fields_compile_to_a_compose_capable_read(topic):
    value = asyncio.run(classify_request(InventoryModel(inventory_topics=[topic], inventory_source="runtime"), "구성 질문", [], allowed_services=("api",)))
    assert value["inventory_source"] == "both"


def test_inventory_invalid_fields_and_invented_service_fail_closed():
    for fields in ({"inventory_topics": []}, {"inventory_topics": ["env_secrets"]}, {"inventory_source": "guessed"}):
        assert asyncio.run(classify_request(InventoryModel(**fields), "구성?", []))["route"] == "unavailable"
    out = asyncio.run(classify_request(InventoryModel(service="worker", target_scope="service", target_cardinality="one"), "이름 미정 서비스", [], allowed_services=("api", "worker")))
    assert out["route"] == "clarify" and not out.get("routing_target")


def test_clarify_route_without_model_question_uses_safe_target_question():
    value = asyncio.run(classify_request(InventoryModel(route="clarify", target_scope="unspecified", clarification=None), "이름 모르는 서비스 하나", [], allowed_services=("api", "worker")))
    assert value["route"] == "clarify" and value["clarification"]["options"] == ["api", "worker"]
    assert "어느 서비스" in value["clarification"]["question"]


def test_inventory_foreign_project_never_falls_back_to_current():
    value = asyncio.run(classify_request(InventoryModel(project_reference="explicit", requested_project="other"), "other 프로젝트 마운트", [], allowed_services=("api",), selected_target={"project": "test-project"}))
    assert value["route"] == "target_selection"


def test_named_project_reference_is_normalized_then_source_validated():
    raw = {"project_reference": "current", "requested_project": "bizxray", "target_scope": "project", "service": None}
    assert TargetDecision.model_validate(raw, context={"routing_input": {"query": "buttercast 말고 bizxray 프로젝트", "history": []}}).project_reference == "explicit"
    with pytest.raises(ValueError):
        TargetDecision.model_validate(raw, context={"routing_input": {"query": "현재 프로젝트만", "history": []}})


def test_inventory_cannot_publish_configuration_review_result():
    out = align_answer({"route": "inventory", "final": "unrelated healthcheck review", "result_kind": "configuration_review", "approval": {"id": "bad"}})
    assert out["answer_status"] == "unavailable" and out["approval"] is None and "unrelated" not in out["final"]


def test_inventory_per_turn_fields_are_reset():
    assert fresh_state()["inventory_topics"] is None and fresh_state()["inventory_source"] is None


def test_healthcheck_durations_are_seconds_and_command_is_not_exposed(inventory):
    inventory[0]["Config"]["Healthcheck"] = {"Test": ["CMD-SHELL", "secret-command"], "Interval": 30_000_000_000, "Timeout": 1_000_000_000, "Retries": 3}
    value = inspect_project_configuration(["api"], "runtime")
    health = value["runtime"]["containers"][0]["healthcheck"]
    assert health["timing"] == {"Interval_seconds": 30.0, "Timeout_seconds": 1.0, "Retries": 3}
    assert "secret-command" not in json.dumps(value)


def test_production_router_separates_purpose_scope_and_lessons_in_small_schemas():
    class Gateway(ModelGateway):
        async def _ask_impl(self, prompt, **kwargs):
            props = kwargs["schema"]["properties"]
            self.schemas.append(set(props))
            if "route" in props:
                assert kwargs["quality"] is True and "project_reference" not in props and "lesson_ids" not in props
                return json.dumps({"route": "lookup", "reason": "현재 이미지 조회", "needs_context": False, "inventory_topics": ["images"], "declarations_only": False, "operation": None, "action_quote": None, "clarification": None})
            assert "route" not in props and "target_cardinality" not in props and "lesson_ids" not in props
            return json.dumps({"project_reference": "current", "requested_project": None, "target_scope": "project", "service": None})
    gateway = Gateway(Settings()); gateway.schemas = []
    value = asyncio.run(classify_request(gateway, "현재 도커 이미지 이름?", [], allowed_services=("api", "worker"), selected_target={"project": "test-project"}))
    assert value["route"] == "inventory" and value["routing_target"]["scope"] == "project"
    assert value["inventory_source"] == "both"
    assert len(gateway.schemas) == 2
