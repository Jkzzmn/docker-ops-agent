import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import server, target_catalog
from backend.approvals import ApprovalStore
from backend.clarifications import ClarificationStore
from backend.target import TargetSelectionRequired


def test_inventory_includes_stopped_and_non_compose_but_no_other_labels(monkeypatch):
    def query(env, args, **kwargs):
        if args[0] == "compose": return [{"Name": "a", "Status": "exited(1)", "ConfigFiles": "/project/compose.yml"}]
        return [{"ID": "one", "Names": "api", "State": "exited", "Status": "Exited", "Labels": "com.docker.compose.project=a,com.docker.compose.service=api,private=secret"},
                {"ID": "two", "Names": "standalone", "State": "running", "Labels": ""}]
    monkeypatch.setattr(target_catalog, "docker_json", query)
    result = target_catalog.inventory({"DOCKER_MODE": "live"})
    assert len(result["containers"]) == 2 and result["containers"][0]["project"] == "a"
    assert result["containers"][1]["project"] is None and "secret" not in json.dumps(result)


def test_manual_selection_clears_previous_project_environment(monkeypatch, tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}")
    seen = []
    def resolve(env):
        seen.append(env)
        return env | {"DOCKER_ALLOWED_SERVICES": "gateway"}, {"project_dir": str(tmp_path), "services": ["gateway"], "project": "folder"}
    monkeypatch.setattr(target_catalog, "resolve_target", resolve)
    monkeypatch.setattr(target_catalog, "docker_json", lambda *a, **kw: {"name": "actual-compose-name", "services": {"gateway": {"build": {"context": str(tmp_path), "dockerfile": "Dockerfile"}}}})
    env, result = target_catalog.resolve_selection({"DOCKER_MODE": "live", "DOCKER_CONTEXT": "pinned", "COMPOSE_FILE": "/old/compose.yml", "COMPOSE_PROJECT_NAME": "old", "DOCKER_ALLOWED_SERVICES": "old", "COMPOSE_ENV_FILES": "/old/.env", "COMPOSE_PROFILES": "old"}, project_dir=str(tmp_path))
    assert seen[0]["DOCKER_CONTEXT"] == "pinned" and seen[0]["DOCKER_PROJECT_DIR"] == str(tmp_path)
    assert not set(seen[0]) & {"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "DOCKER_ALLOWED_SERVICES", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES"}
    assert env["COMPOSE_PROJECT_NAME"] == result["project"] == "actual-compose-name"
    assert env["COMPOSE_FILE"] == str(compose) and not result["build_sources"][0]["readable"]


@pytest.mark.parametrize("path", ["relative/path", "/", "/missing-project-for-test"])
def test_invalid_manual_path_is_rejected_before_docker(path):
    with pytest.raises(ValueError): target_catalog.resolve_selection({"DOCKER_MODE": "live"}, project_dir=path)


def install_choice(monkeypatch, tmp_path):
    old = server.app.state.bundle
    root = tmp_path / "second"
    root.mkdir(); (root / "compose.yml").write_text("services: {}")
    def resolve(base, *, project_name=None, project_dir=None):
        if project_name == "broken": raise ValueError("no Compose config")
        if project_name == "original": return old["env"], old["target"]
        return old["env"] | {"DOCKER_PROJECT_DIR": str(root), "COMPOSE_PROJECT_NAME": "second", "COMPOSE_FILE": str(root / "compose.yml")}, old["target"] | {"project": "second", "project_dir": str(root), "source": "explicit", "services": ["api", "db", "web"]}
    monkeypatch.setattr(server, "resolve_selection", resolve)
    return old


def test_switch_rebuilds_mcp_invalidates_questions_approvals_and_separates_history(monkeypatch, tmp_path):
    with TestClient(server.app) as client:
        old = install_choice(monkeypatch, tmp_path)
        action = old["approvals"].create("t", "restart_service", {"service": "api"}, "old target")
        question = old["clarifications"].create("t", "old query", {"question": "대상?", "reason": "필수 대상 없음", "options": ["api"]}, 0)
        old["store"].add(old["history_scope"] + ":t", "user", "old-project-only")
        response = client.post("/targets/select", json={"project_name": "second", "expected_instance_id": old["instance_id"]})
        assert response.status_code == 200
        new = server.app.state.bundle
        assert old["owner_task"].done() and new["mcp"] is not old["mcp"]
        assert new["target"]["project"] == "second" and new["store"].history(new["history_scope"] + ":t") == []
        assert client.post("/chat", headers={"X-Docker-Target": old["instance_id"]}, json={"message": "안녕", "thread_id": "t"}).status_code == 409
        assert client.get("/chat/stream", params={"message": "안녕", "thread_id": "t", "target_id": old["instance_id"]}).status_code == 409
        assert client.post(f"/actions/{action.id}/approve").status_code == 409
        assert client.post(f"/clarifications/{question['id']}/reply", json={"thread_id": "t", "choice": 1}).status_code == 404
        back = client.post("/targets/select", json={"project_name": "original", "expected_instance_id": new["instance_id"]})
        assert back.status_code == 200
        assert client.post(f"/actions/{action.id}/approve").status_code == 409
        assert client.post(f"/clarifications/{question['id']}/reply", json={"thread_id": "t", "choice": 1}).status_code == 409
        assert client.get("/clarifications/pending", params={"thread_id": "t"}).json()["clarification"] is None


def test_selection_failure_stale_preview_and_busy_leave_old_target(monkeypatch, tmp_path):
    with TestClient(server.app) as client:
        old = install_choice(monkeypatch, tmp_path)
        assert client.post("/targets/select", json={"project_name": "second", "expected_instance_id": "stale"}).status_code == 409
        assert client.post("/targets/select", json={"project_name": "broken", "expected_instance_id": old["instance_id"]}).status_code == 400
        server.app.state.active_runs = 1
        assert client.post("/targets/select", json={"project_name": "second", "expected_instance_id": old["instance_id"]}).status_code == 409
        server.app.state.active_runs = 0
        async def fail(*args): raise RuntimeError("MCP failed")
        monkeypatch.setattr(server, "_launch_bundle", fail)
        assert client.post("/targets/select", json={"project_name": "second", "expected_instance_id": old["instance_id"]}).status_code == 502
        assert server.app.state.bundle is old and not old["owner_task"].done()
        assert client.get("/health").json()["instance_id"] == old["instance_id"]


def test_multiple_project_startup_opens_chooser_not_default_diagnostics(monkeypatch):
    def choose(env):
        raise TargetSelectionRequired(env, {"source": "selection-required", "project": None, "context": "test", "services": []})
    monkeypatch.setattr(server, "resolve_target", choose)
    with TestClient(server.app) as client:
        assert client.get("/health").json()["target_selected"] is False
        assert client.post("/chat", json={"message": "현재 상태", "thread_id": "t"}).status_code == 409
        assert server.app.state.bundle["mcp"] is None


def test_no_project_startup_does_not_select_the_demo(monkeypatch):
    monkeypatch.setattr(server, "resolve_target", lambda env: (env, {"source": "demo-not-running", "project": "demo", "services": ["api"]}))
    with TestClient(server.app) as client:
        health = client.get("/health").json()
        assert not health["target_selected"] and health["docker_target"]["project"] is None
        assert server.app.state.bundle["mcp"] is None


def test_invalid_bundle_settings_fail_instead_of_hanging():
    async def exercise():
        with pytest.raises(ValueError):
            await asyncio.wait_for(server._launch_bundle({"DOCKER_MODE": "invalid"}, {}), timeout=1)
    asyncio.run(exercise())


def test_lease_stays_held_for_whole_stream_and_releases_after_failure():
    async def exercise():
        state = SimpleNamespace(active_runs=0, switching=False, bundle={"selected": True, "instance_id": "a"})
        entered, release = asyncio.Event(), asyncio.Event()
        async def stream(scope, receive, send):
            entered.set(); await release.wait(); raise RuntimeError("disconnect")
        guard = server.TargetGuard(stream)
        scope = {"type": "http", "path": "/chat/stream", "method": "GET", "headers": [], "app": SimpleNamespace(state=state)}
        async def receive(): return {"type": "http.request"}
        sent = []
        async def send(message): sent.append(message)
        task = asyncio.create_task(guard(scope, receive, send)); await entered.wait()
        assert state.active_runs == 1
        await guard(scope | {"path": "/targets/select", "method": "POST"}, receive, send)
        assert sent[0]["status"] == 409 and not state.switching
        release.set()
        with pytest.raises(RuntimeError): await task
        assert state.active_runs == 0
    asyncio.run(exercise())
