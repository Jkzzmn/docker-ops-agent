import json
import os
import subprocess

import pytest

from backend.target import resolve_target


def docker_runner(projects, services="gateway\ncache", calls=None):
    def run(args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        command = args[1:]
        if command[:2] == ["context", "show"]: output = "other-workstation"
        elif command[0] == "info": output = json.dumps({"ID":"other-engine", "Name":"workstation-b", "ServerVersion":"29"})
        elif command[:2] == ["compose", "ls"]: output = json.dumps(projects)
        elif command[:2] == ["compose", "config"]: output = services
        else: raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, "")
    return run


def test_other_machine_auto_detection_has_no_api_db_web_assumption(tmp_path):
    config = tmp_path / "custom-stack.yaml"
    config.write_text("services: {}")
    calls = []
    env, target = resolve_target({}, docker_runner([{"Name":"another-project", "ConfigFiles":str(config)}], calls=calls))
    assert env["DOCKER_PROJECT_DIR"] == str(tmp_path)
    assert env["DOCKER_ALLOWED_SERVICES"] == "gateway,cache"
    assert env["DOCKER_CONTEXT"] == "other-workstation"
    assert env["COMPOSE_FILE"] == str(config)
    assert target["project"] == "another-project" and target["engine_id"] == "other-engine"
    assert not any("restart" in args for args, _ in calls)


def test_multiple_projects_require_explicit_choice():
    with pytest.raises(RuntimeError, match="여러 개"):
        resolve_target({}, docker_runner([{"Name":"a"}, {"Name":"b"}]))


def test_no_project_does_not_start_or_modify_any_container():
    env, target = resolve_target({}, docker_runner([]))
    assert target["source"] == "demo-not-running" and target["warning"]


def test_remote_context_requires_readable_local_compose_files():
    with pytest.raises(RuntimeError, match="로컬 사본"):
        resolve_target({"DOCKER_CONTEXT":"remote"}, docker_runner([{"Name":"remote", "ConfigFiles":"/not-on-this-computer/compose.yml"}]))


def test_explicit_project_derives_services_and_respects_allowlist(tmp_path):
    env, target = resolve_target({"DOCKER_PROJECT_DIR":str(tmp_path), "DOCKER_ALLOWED_SERVICES":"cache"}, docker_runner([]))
    assert env["DOCKER_ALLOWED_SERVICES"] == "cache" and target["source"] == "explicit"
    with pytest.raises(RuntimeError, match="선택한 Compose"):
        resolve_target({"DOCKER_PROJECT_DIR":str(tmp_path), "DOCKER_ALLOWED_SERVICES":"api"}, docker_runner([]))


def test_fixture_never_discovers_the_hosts_docker():
    def forbidden(*args, **kwargs): raise AssertionError("must not call Docker")
    _, target = resolve_target({"DOCKER_MODE":"fixture"}, forbidden)
    assert target["mode"] == "fixture"
