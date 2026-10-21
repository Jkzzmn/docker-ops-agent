import json
import subprocess

import pytest

from backend import docker_tools as tools


def test_probe_restricts_services_and_declared_container_ports():
    with pytest.raises(ValueError): tools.test_tcp_connection("api", "host.docker.internal", 5432)
    with pytest.raises(ValueError): tools.test_tcp_connection("api", "db", 22)
    with pytest.raises(ValueError): tools.test_container_dns("api; id", "db")


def test_json_parser_does_not_truncate_structured_data(monkeypatch):
    payload = {"large": "x" * 20000}
    monkeypatch.setattr(tools, "_capture", lambda args: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""))
    assert tools._json(["docker", "compose", "config"]) == payload


def test_live_inspect_exposes_only_safe_fields(monkeypatch):
    monkeypatch.setattr(tools, "MODE", "live")
    monkeypatch.setattr(tools, "_inspect", lambda service: {"State": {"OOMKilled": True, "ExitCode": 137}, "Config": {"Env": ["PASSWORD=do-not-expose"]}, "NetworkSettings": {"Networks": {"demo": {}}}})
    result = tools.inspect_container("api")
    assert result["oom_killed"] is True and result["exit_code"] == 137
    assert "PASSWORD" not in json.dumps(result) and "Config" not in result


def test_stopped_stats_are_unknown_instead_of_zero(monkeypatch):
    monkeypatch.setattr(tools, "MODE", "live")
    monkeypatch.setattr(tools, "_inspect", lambda service: {"State": {"Status": "exited"}})
    result = tools.get_container_stats("api")
    assert result["ok"] is False
    assert "memory_percent" not in result
