"""Existing-container operations: exact preview, scoped one-use approval, no build/up/pull."""
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from . import docker_tools as docker
from .approvals import verify_approval
from .config_control import ConfigControl, LOCK
from .events import emit

OPERATIONS = {"start": "시작", "stop": "중지", "restart": "재시작"}


def preview_runtime_action(operation: Literal["start", "stop", "restart"], services: list[str]) -> dict:
    """현재 대상의 기존 컨테이너만 시작/중지/재시작할 계획을 조회합니다. 실행하지 않습니다."""
    if operation not in OPERATIONS or not services or len(services) > 12 or len(set(services)) != len(services):
        raise ValueError("a supported operation and 1-12 unique services are required")
    if set(services) - docker.ALLOWED: raise ValueError("service outside allowlist")
    control = ConfigControl(docker, Path("unused"))
    scope = control.scope()
    before = {}
    for service in sorted(services):
        try: row = docker._inspect(service)
        except ValueError:
            raise ValueError(f"{service}: 기존 컨테이너가 정확히 하나 있어야 합니다. 새 생성·빌드·이미지 다운로드는 이 작업에서 지원하지 않습니다.")
        state = row.get("State", {}).get("Status")
        if state not in {"running", "exited", "created"}: raise ValueError(f"{service}: 현재 상태 {state}에서는 지원하지 않습니다.")
        before[service] = {"container_id": row["Id"], "image_id": row["Image"], "state": state}
    command = ["docker", "compose", operation, *sorted(services)]
    return {"ok": True, "plan": {"stage": "service_operation", "operation": operation, "services": sorted(services),
        "scope": scope, "before": before, "commands": [command],
        "risk": "중지·재시작은 서비스 중단을 일으킵니다. 시작은 기존 설정의 프로세스를 실행합니다. 파일·컨테이너·볼륨을 삭제하거나 빌드·새 생성하지 않습니다.",
        "note": "기존 이미지·컨테이너 설정을 그대로 사용하며 Dockerfile 변경을 빌드·반영하지 않습니다. 실행 상태만 사후 확인하고, healthcheck가 없으면 애플리케이션 정상·준비 완료를 확정하지 않습니다."}}


def execute_runtime_action(plan: dict, approval_token: str) -> dict:
    """정확한 대상·명령·컨테이너 identity의 승인을 한 번만 사용합니다."""
    payload = verify_approval(os.getenv("MCP_APPROVAL_SECRET", ""), approval_token, "execute_runtime_action", {"plan": plan})
    with LOCK, docker.APPROVAL_LOCK:
        for identity, expiry in list(docker.USED_APPROVALS.items()):
            if expiry <= time.time(): del docker.USED_APPROVALS[identity]
        if payload["id"] in docker.USED_APPROVALS: raise PermissionError("approval already consumed")
        current = preview_runtime_action(plan.get("operation"), plan.get("services", []))["plan"]
        if current != plan: raise ValueError("대상·설정·컨테이너 상태가 바뀌었습니다. 새 미리보기와 승인이 필요합니다.")
        docker.USED_APPROVALS[payload["id"]] = payload["expires_at"]
        # Consume before running: a timeout must never trigger an automatic second mutation.
        result = docker._run(plan["commands"][0], timeout=60)
        observations = []
        for service in plan["services"]:
            try: value = docker.check_service_health(service)
            except Exception: value = {"ok": False, "service": service, "state": "unknown"}
            observations.append(value)
        wanted = {"exited", "created"} if plan["operation"] == "stop" else {"running"}
        verified = all(v.get("ok") and v.get("state") in wanted for v in observations)
        return {"ok": bool(result.get("ok")) and verified, "mutation_started": True, "operation": plan["operation"],
                "commands": plan["commands"], "command_result": result, "lifecycle_verified": verified,
                "health_verified": all(v.get("health") == "healthy" for v in observations) if plan["operation"] != "stop" else None,
                "observations": observations, "note": plan["note"]}


async def propose_runtime_action(deps, state):
    from .port_owners import requested_ports, observe_port_owners
    # A port-constrained operation needs ownership evidence, even if the model
    # accidentally chooses runtime instead of the read-only preflight intent.
    if requested_ports(state["query"]):
        return await observe_port_owners(deps, state) | {"result_kind": "runtime_action"}
    operation, target = state.get("runtime_operation"), state.get("routing_target", {})
    services = list(deps.allowed_services) if target.get("scope") == "project" else [target.get("service")]
    events = list(state.get("events", []))
    base = {"events": events, "approval": None, "proposal": None, "result_kind": "runtime_action", "answer_status": "unavailable"}
    if operation not in OPERATIONS or not services or any(s not in deps.allowed_services for s in services):
        return base | {"final": "실행할 작업과 서비스를 확정하지 못했습니다. 시작·중지·재시작 중 원하는 작업과 대상을 알려주세요. 변경하지 않았습니다."}
    started = time.perf_counter()
    try:
        value, retries = await deps.mcp.tool("preview_runtime_action", {"operation": operation, "services": services})
        if not value.get("ok"): raise ValueError(value.get("error", "실행 미리보기를 완료하지 못했습니다."))
        plan = value["plan"]
        if plan.get("operation") != operation or plan.get("services") != sorted(services): raise ValueError("실행 미리보기 대상 불일치")
    except Exception as exc:
        emit(events, {"type": "TaskFailed", "target": "preview_runtime_action", "agent": "supervisor", "node": "runtime_action"})
        from .sanitize import redact
        return base | {"final": f"{', '.join(services)} {OPERATIONS[operation]} 계획을 만들지 못했습니다. {redact(str(exc), 1200)}\n변경은 수행하지 않았습니다. 설정 파일 검토로 대신 처리하지 않습니다."}
    item = {"id": "E1", "tool": "preview_runtime_action", "args": {"operation": operation, "services": services}, "observation": value}
    emit(events, {"type": "TaskCompleted", "id": "E1", "target": "preview_runtime_action", "retries": retries, "agent": "supervisor", "node": "runtime_action"})
    wanted = {"running"} if operation == "start" else {"exited", "created"} if operation == "stop" else set()
    common = base | {"evidence": [item], "validation_ok": True, "evidence_coverage": None, "retry_count": retries,
                     "tool_time_sum": time.perf_counter()-started, "answer_status": "validated"}
    if wanted and all(row["state"] in wanted for row in plan["before"].values()):
        return common | {"final": f"{', '.join(services)}: 이미 요청한 실행 상태입니다. [{item['id']}]\n추가 변경을 수행하지 않았습니다. 애플리케이션의 정상·준비 완료를 뜻하지는 않습니다."}
    action = deps.approvals.create(state["thread_id"], "execute_runtime_action", {"plan": plan},
        f"{plan['scope']['project']}의 {', '.join(services)}만 {OPERATIONS[operation]}합니다. 표시된 명령과 기존 컨테이너에 대한 1회 승인입니다.")
    emit(events, {"type": "ApprovalRequired", "approval": asdict(action), "agent": "supervisor", "node": "runtime_action"})
    return common | {"approval": asdict(action), "execution_status": "waiting_for_user",
        "final": f"{plan['scope']['project']}의 {', '.join(services)} {OPERATIONS[operation]}을 준비했습니다. [E1]\n\n실행 예정: `{' '.join(plan['commands'][0])}`\n\n아직 실행하지 않았습니다. 아래 승인 버튼을 누르면 실행하고 상태를 다시 확인합니다. {plan['note']}"}
