import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import asyncio

import pytest
import yaml

from backend.approvals import sign_approval
from backend.config_control import ConfigControl, USED
from backend.project_files import ProjectFiles, sha, MAX_BYTES


@pytest.fixture
def control(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "compose.yaml").write_text("services:\n  api:\n    build: .\n    restart: always\n  db:\n    image: postgres:16\n")
    (project / "Dockerfile").write_text("FROM python:3.11-slim\nCOPY . /app\n")
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    monkeypatch.setenv("MCP_APPROVAL_SECRET", "test-config-secret")
    USED.clear()
    calls = []
    def normalized():
        value = yaml.safe_load((project / "compose.yaml").read_text())
        if "restart: [" in (project / "compose.yaml").read_text(): raise ValueError("invalid YAML")
        value["name"] = "test-project"
        value["services"]["api"]["build"] = {"context": str(project), "dockerfile": "Dockerfile"}
        return value
    def capture(args, timeout=15):
        return SimpleNamespace(returncode=0, stdout="test-context\n", stderr="")
    def _json(args):
        if args[1] == "info": return {"ID": "engine-test"}
        return normalized()
    def run(args, timeout=15):
        calls.append(args)
        try: normalized()
        except Exception: return {"ok": False, "stderr": "invalid Compose"}
        return {"ok": True, "stderr": "", "stdout": ""}
    states = {s: {"Id": s + "-old", "Image": "sha256:old-" + s, "State": {"Status": "exited"},
                  "Config": {"Healthcheck": {"Test": ["CMD", "true"]}}} for s in ("api", "db")}
    docker = SimpleNamespace(PROJECT=project, MODE="live", ALLOWED={"api", "db"}, _json=_json, _capture=capture, _run=run,
                             _inspect=lambda s: states[s], calls=calls, states=states)
    return ConfigControl(docker, tmp_path / "backups")


def edits(control, find="restart: always", replacement="restart: unless-stopped", path="compose.yaml"):
    return [{"path": path, "base_hash": sha(control.files.raw(path)), "find": find, "replace": replacement}]


def token(tool, plan, action="test", expires=None):
    return sign_approval("test-config-secret", action, tool, {"plan": plan}, expires or time.time() + 60)


@pytest.mark.parametrize("path", ["../compose.yaml", "/tmp/Dockerfile", ".env", "backend/.env", "backend/../Dockerfile", ".git/Dockerfile", "foo.py", "Dockerfile\\x"])
def test_rejects_outside_or_non_config_paths(control, path):
    with pytest.raises(ValueError): control.files.raw(path)


def test_no_symlink_or_hardlink_or_large_files(control, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("secret")
    link = control.files.root / "Dockerfile.link"
    link.symlink_to(outside)
    with pytest.raises(OSError): control.files.raw("Dockerfile.link")
    link.unlink()
    os.link(outside, link)
    with pytest.raises(ValueError): control.files.raw("Dockerfile.link")
    (control.files.root / "Dockerfile.large").write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ValueError): control.files.raw("Dockerfile.large")


def test_parent_symlink_rejected_and_inventory_bounded(control, tmp_path):
    (control.files.root / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError): control.files.raw("linked/Dockerfile")
    assert not any(f["path"].startswith("linked/") for f in control.files.inventory()["files"])


def test_active_override_read_and_secret_masking(control):
    override = control.files.root / "compose.override.yaml"
    override.write_text("services:\n  api:\n    environment:\n      PASSWORD: hide-me\n")
    result = control.files.compose()
    assert result["active_files"] == ["compose.yaml", "compose.override.yaml"]
    assert "hide-me" not in json.dumps(result)
    assert "[REDACTED]" in json.dumps(result)


def test_explicit_outside_or_missing_file_refused(control, monkeypatch):
    monkeypatch.setenv("COMPOSE_FILE", "/tmp/compose.yaml")
    with pytest.raises(ValueError): control.files.compose()
    monkeypatch.setenv("COMPOSE_FILE", "compose.missing.yaml")
    with pytest.raises(ValueError): control.files.compose()


def test_preview_no_mutation_exact_hash_and_stage_one_file_only(control):
    before = control.files.raw("compose.yaml")
    plan = control.preview(edits(control), ["api"])["plan"]
    assert control.files.raw("compose.yaml") == before
    assert "-    restart: always" in plan["diff"] and "+    restart: unless-stopped" in plan["diff"]
    result = control.apply(plan, token("apply_project_config_patch", plan))
    assert result["ok"] and result["runtime_plan"]["stage"] == "runtime"
    assert "unless-stopped" in control.files.raw("compose.yaml").decode()
    assert all("build" not in c and "up" not in c for c in control.d.calls)
    _, originals = control.originals(result["transaction"])
    assert originals["compose.yaml"] == before
    assert (control.backup_root / result["transaction"] / "0").stat().st_mode & 0o777 == 0o600
    with pytest.raises(PermissionError): control.apply(plan, token("apply_project_config_patch", plan))


@pytest.mark.parametrize("drift", ["file", "engine", "config", "services"])
def test_scope_drift_refuses_without_writes(control, drift):
    plan = control.preview(edits(control), ["api"])["plan"]
    if drift == "file": (control.files.root / "Dockerfile").write_text("FROM changed\n")
    elif drift == "engine": plan["scope"]["engine_id"] = "other-engine"
    elif drift == "config": plan["scope"]["config_sha256"] = "changed-env-expansion"
    else: plan["scope"]["allowed_services"] = ["other"]
    before = control.files.raw("compose.yaml")
    with pytest.raises(ValueError): control.apply(plan, token("apply_project_config_patch", plan))
    assert control.files.raw("compose.yaml") == before


def test_expired_mismatched_and_unsigned_approvals_refused(control):
    plan = control.preview(edits(control), ["api"])["plan"]
    for invalid in ("", token("deploy_project_config", plan), token("apply_project_config_patch", plan, expires=time.time()-1)):
        with pytest.raises(PermissionError): control.apply(plan, invalid)


@pytest.mark.parametrize("find,replacement", [("restart: always", "restart: ["), ("restart: always", "restart: always\n    privileged: true"),
                                               ("image: postgres:16", "image: postgres:17")])
def test_bad_or_out_of_scope_compose_edits_refused(control, find, replacement):
    with pytest.raises((ValueError, yaml.YAMLError)):
        control.preview(edits(control, find, replacement), ["api"])


def test_secret_snippet_rejected(control):
    with pytest.raises(ValueError): control.preview(edits(control, replacement="PASSWORD: do-not-leak"), ["api"])


def test_compose_validation_failure_restores_original(control):
    plan = control.preview(edits(control), ["api"])["plan"]
    before = control.files.raw("compose.yaml")
    control.d._run = lambda *a, **kw: {"ok": False, "stderr": "bad config"}
    result = control.apply(plan, token("apply_project_config_patch", plan))
    assert not result["ok"] and result["rollback"]["ok"]
    assert control.files.raw("compose.yaml") == before


def test_restore_never_clobbers_external_edit(control):
    original = control.files.raw("compose.yaml")
    (control.files.root / "compose.yaml").write_text("external edit")
    result = control.restore({"compose.yaml": original}, {"compose.yaml": sha(original)})
    assert not result["ok"] and control.files.raw("compose.yaml") == b"external edit"


def test_create_missing_dockerignore_with_approval(control):
    edit = {"path": ".dockerignore", "base_hash": None, "find": "", "replace": ".git\n.env\nnode_modules\n"}
    plan = control.preview([edit], ["api"])["plan"]
    assert not (control.files.root / ".dockerignore").exists()
    result = control.apply(plan, token("apply_project_config_patch", plan))
    assert result["ok"] and (control.files.root / ".dockerignore").exists()


def test_runtime_needs_separate_token_and_healthcheck(control):
    plan = control.preview(edits(control), ["api"])["plan"]
    runtime = control.apply(plan, token("apply_project_config_patch", plan))["runtime_plan"]
    with pytest.raises(PermissionError): control.deploy(runtime, token("apply_project_config_patch", plan))
    control.d.states["api"]["Config"] = {}
    result = control.deploy(runtime, token("deploy_project_config", runtime, "runtime"))
    assert not result["ok"] and not result["mutation_started"]
    assert not any("build" in c for c in control.d.calls)


def test_runtime_failure_restores_files_old_image_and_stopped_state(control):
    before = control.files.raw("compose.yaml")
    plan = control.preview(edits(control), ["api"])["plan"]
    runtime = control.apply(plan, token("apply_project_config_patch", plan))["runtime_plan"]
    calls = control.d.calls
    def run(args, timeout=15):
        calls.append(args)
        return {"ok": "build" not in args, "stderr": "build failed" if "build" in args else ""}
    control.d._run = run
    control.state = lambda services: {s: {"state": "exited", "health": ""} for s in services}
    result = control.deploy(runtime, token("deploy_project_config", runtime, "runtime"))
    assert not result["ok"] and result["rollback"]["ok"]
    assert control.files.raw("compose.yaml") == before
    assert any("--no-start" in c and "--no-build" in c for c in calls)
    assert not any("down" in c or "rm" in c or "prune" in c for c in calls)


def test_runtime_success_and_replay_refused(control):
    plan = control.preview(edits(control), ["api"])["plan"]
    runtime = control.apply(plan, token("apply_project_config_patch", plan))["runtime_plan"]
    control.verify = lambda services: {"ok": True, "containers": {}}
    result = control.deploy(runtime, token("deploy_project_config", runtime, "runtime"))
    assert result["ok"]
    assert control.d.calls[1][:3] == ["docker", "image", "tag"]
    assert any("--no-deps" in c and "--force-recreate" in c for c in control.d.calls)
    with pytest.raises(PermissionError): control.deploy(runtime, token("deploy_project_config", runtime, "runtime"))


def test_runtime_rejects_container_identity_drift(control):
    plan = control.preview(edits(control), ["api"])["plan"]
    runtime = control.apply(plan, token("apply_project_config_patch", plan))["runtime_plan"]
    control.d.states["api"]["Id"] = "recreated-elsewhere"
    with pytest.raises(ValueError): control.deploy(runtime, token("deploy_project_config", runtime, "runtime"))


def test_failed_dns_is_not_success_even_when_tool_ok(control):
    path = control.files.root / "compose.yaml"
    path.write_text(path.read_text().replace("restart: always", "restart: always\n    depends_on:\n      db:\n        condition: service_started"))
    plan = control.preview(edits(control), ["api"])["plan"]
    runtime = control.apply(plan, token("apply_project_config_patch", plan))["runtime_plan"]
    control.verify = lambda services: {"ok": True, "containers": {}}
    control.state = lambda services: {s: {"state": "exited"} for s in services}
    control.d.inspect_network = lambda *a: {"ok": True, "shared_networks": ["net"]}
    control.d.test_container_dns = lambda *a: {"ok": True, "resolved": False}
    result = control.deploy(runtime, token("deploy_project_config", runtime, "runtime"))
    assert not result["ok"] and not result["verification"]["ok"] and result["rollback"]["ok"]


def test_scripted_change_response_keeps_approval_boundary():
    from tests.routing_double import route_request
    assert route_request("현재 .dockerignore가 없다면 추가하는 diff를 제안해주세요. 승인 전 실행하지 마세요")[0] == "change"


class ConfigMCP:
    approval_secret = "test-config-secret"
    def __init__(self, control): self.control, self.calls = control, []
    async def tool(self, name, args):
        self.calls.append(name)
        c = self.control
        if name == "read_compose_files": value = c.files.compose()
        elif name == "validate_compose_config": value = {"ok": True, "service_config": c.normalized()["services"]}
        elif name == "list_project_config_files": value = c.files.inventory()
        elif name == "read_dockerfile": value = c.files.read(args["path"])
        elif name == "preview_project_config_patch": value = c.preview(**args)
        elif name == "apply_project_config_patch": value = c.apply(**args)
        elif name == "deploy_project_config": value = c.deploy(**args)
        else: value = {"ok": True, "state": "exited"}
        return value, 0


@pytest.mark.parametrize("route", ["configuration", "change"])
def test_real_config_graph_branch_and_io_no_mutation(control, route):
    from backend.approvals import ApprovalStore
    from backend.graph import Dependencies, build_graph
    class Model:
        async def json(self, prompt, **kwargs):
            if "classify" in kwargs.get("system", "").lower(): return {"route": route, "reason": "설정 검토"}
            return {"summary": "활성 Compose 파일을 읽었습니다. [E1]", "findings": [{"text": "restart 정책 검토", "evidence_ids": ["E1"]}],
                "services": ["api"], "edits": [{"path": ".dockerignore", "find": "", "replace": ".git\n.venv\n"}]}
    store = ApprovalStore()
    mcp = ConfigMCP(control)
    deps = Dependencies(llm=Model(), mcp=mcp, approvals=store, allowed_services=("api", "db"))
    before = control.files.raw("compose.yaml")
    result = asyncio.run(build_graph(deps).ainvoke({"query": "현재 Dockerfile과 Compose 설정을 검토/개선해주세요", "route": route,
                                                   "thread_id": "config-graph", "history": [], "events": []}))
    assert result["route"] == route and result["evidence"]
    nodes = [e["node"] for e in result["events"] if e["type"] == "NodeStarted"]
    assert nodes == ["route", "configuration", "config_collect", "config_review", "config_validate", "config_propose"]
    assert all("input" in e for e in result["events"] if e["type"] == "NodeStarted")
    assert bool(result["approval"]) == (route == "change")
    assert control.files.raw("compose.yaml") == before
    assert not any(name in mcp.calls for name in ("apply_project_config_patch", "deploy_project_config"))
    store.close()


def test_api_file_approval_then_separate_runtime_rejection(control, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.approvals import ApprovalStore
    from backend.server import app
    from collections import Counter
    store, mcp = ApprovalStore(), ConfigMCP(control)
    plan = control.preview(edits(control), ["api"])["plan"]
    action = store.create("test", "apply_project_config_patch", {"plan": plan}, "file-only")
    bundle = {"approvals": store, "mcp": mcp, "settings": SimpleNamespace(recovery_wait_seconds=0), "metrics": Counter(), "selected": True, "instance_id": "test-target"}
    monkeypatch.setattr(app.state, "bundle", bundle, raising=False)
    monkeypatch.setattr(app.state, "switching", False, raising=False)
    monkeypatch.setattr(app.state, "active_runs", 0, raising=False)
    client = TestClient(app)
    response = client.post(f"/actions/{action.id}/approve")
    assert response.status_code == 200
    result = response.json()
    assert result["stage"] == "files" and result["next_approval"]["status"] == "pending"
    assert result["next_approval"]["tool"] == "deploy_project_config"
    assert not any("build" in c or "up" in c for c in control.d.calls)
    assert client.post(f"/actions/{action.id}/approve").status_code == 409
    runtime_id = result["next_approval"]["id"]
    assert client.post(f"/actions/{runtime_id}/reject").status_code == 200
    assert client.post(f"/actions/{runtime_id}/approve").status_code == 409
    assert not any("build" in c or "up" in c for c in control.d.calls)
    assert b"unless-stopped" in control.files.raw("compose.yaml")
    store.close()
