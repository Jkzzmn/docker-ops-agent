"""Read-only host-published-port preflight. Discovery never authorizes a stop."""
import os
import re
import time

from . import docker_tools as docker
from .sanitize import redact
from .events import emit


def requested_ports(query):
    # Extract an operand only; the LLM chooses the intent, not this parser.
    matches = re.findall(r"(?<![\w])([0-9]{1,5})\s*(?:번\s*)?(?:포트|port\b)|\bport\s+([0-9]{1,5})\b", query, re.I)
    return sorted({int(value) for pair in matches for value in pair if value and 0 < int(value) <= 65535})


def inspect_port_owners(port: int) -> dict:
    """현재 Engine 전체에서 지정 호스트 포트를 게시한 컨테이너만 조회합니다. 변경 없음."""
    if type(port) is not int or not 1 <= port <= 65535: raise ValueError("invalid host port")
    if docker.MODE != "live": raise ValueError("port ownership requires live Docker observations")
    rows = docker._json(["docker", "ps", "--all", "--format", "{{json .}}"])
    rows = [rows] if isinstance(rows, dict) else rows
    ids = [r.get("ID", "") for r in rows]
    if len(ids) > 200 or any(not re.fullmatch(r"[a-f0-9]{12,64}", cid) for cid in ids): raise ValueError("narrow the Engine scope; invalid or more than 200 containers")
    actual = docker._json(["docker", "inspect", *ids]) if ids else []
    if not isinstance(actual, list) or len(actual) != len(ids): raise ValueError("incomplete port ownership inspect")
    owners, declarations = [], []
    for identity, item in zip(ids, actual):
        if not item.get("Id", "").startswith(identity) or not all(isinstance(item.get(k), dict) for k in ("Config", "State", "HostConfig", "NetworkSettings")):
            raise ValueError("incomplete/changed container identity")
        labels = item["Config"].get("Labels") or {}
        scope = {"container_id": item["Id"], "name": item.get("Name", "").lstrip("/"), "state": item["State"].get("Status"),
                 "project": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service")}
        scope["within_selected_project"] = bool(os.getenv("COMPOSE_PROJECT_NAME") and scope["project"] == os.getenv("COMPOSE_PROJECT_NAME") and scope["service"] in docker.ALLOWED)
        for stage, target in (("HostConfig", declarations), ("NetworkSettings", owners)):
            if stage == "NetworkSettings" and scope["state"] != "running": continue
            for container_port, bindings in (item[stage].get("PortBindings" if stage == "HostConfig" else "Ports") or {}).items():
                for binding in bindings or []:
                    if str(binding.get("HostPort")) == str(port):
                        target.append(scope | {"host_ip": binding.get("HostIp"), "host_port": port, "container_port": container_port})
    return {"ok": True, "port": port, "scope": "current Docker Engine; published host ports only", "active_owners": owners, "configured_bindings": declarations,
            "note": "Docker 게시 포트만 조회했습니다. 호스트 프로세스·host-network 점유는 확인하지 않았습니다. 중지 컨테이너의 선언만으로 현재 충돌·좀비를 확정하지 않습니다."}


async def observe_port_owners(deps, state):
    ports = requested_ports(state["query"])
    events = list(state.get("events", []))
    base = {"events": events, "approval": None, "proposal": None, "result_kind": "configuration_inventory", "answer_status": "unavailable", "validation_ok": False}
    if len(ports) != 1:
        return base | {"final": "점유를 확인할 호스트 포트 번호 한 개를 알려주세요. 점유 대상이 확인되지 않아 중지·전체 재시작 승인을 만들지 않았습니다."}
    started = time.perf_counter()
    try:
        value, retries = await deps.mcp.tool("inspect_port_owners", {"port": ports[0]})
        if value.get("ok") is not True: raise ValueError(value.get("error", "조회 미완료"))
    except Exception as exc:
        emit(events, {"type": "TaskFailed", "target": "inspect_port_owners", "node": "port_diagnose", "agent": "runtime"})
        return base | {"final": f"{ports[0]}번 포트 점유 조회를 완료하지 못했습니다: {redact(str(exc), 500)}. 충돌 대상으로 추측해 중지하지 않았습니다."}
    import json
    owners = value.get("active_owners", [])
    text = f"호스트 {ports[0]}번 포트의 Docker Engine 게시 포트를 조회했습니다. [E1]\n\n"
    text += ("현재 게시 컨테이너:\n" + "\n".join("- " + json.dumps(row, ensure_ascii=False) + " [E1]" for row in owners)) if owners else "실행 중인 Docker 게시 포트 점유 컨테이너가 관측되지 않았습니다. 포트가 비었다는 증명은 아닙니다. [E1]"
    if value.get("configured_bindings"):
        text += "\n\n생성 시 바인딩 선언 (현재 점유와 별개):\n" + "\n".join("- " + json.dumps(row, ensure_ascii=False) + " [E1]" for row in value["configured_bindings"])
    text += "\n\n" + value["note"] + "\n아직 중지·재시작·데모 배포를 수행하지 않았습니다. 정확한 프로젝트·서비스와 데모 시작 대상을 확인해야 합니다. 현재는 선택 프로젝트의 기존 컨테이너 start/stop/restart만 각각 1회 승인하며, 포트 점유자 자동 종료와 연쇄 배포는 지원하지 않습니다."
    item = {"id": "E1", "tool": "inspect_port_owners", "args": {"port": ports[0]}, "observation": value}
    emit(events, {"type": "TaskCompleted", "id": "E1", "target": "inspect_port_owners", "node": "port_diagnose", "agent": "runtime", "retries": retries})
    return base | {"final": text, "answer_status": "partial", "evidence": [item], "tool_time_sum": time.perf_counter()-started, "retry_count": retries}
