"""Read-only configuration facts, not optimization review or educational prose."""
import json
import os
import re
import time
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal

from . import docker_tools as docker
from .events import emit
from .sanitize import redact


TOPICS = ("status", "usage", "image_details", "images", "mounts", "ports", "networks", "resources", "restart", "healthcheck", "dependencies", "build")
TITLES = dict(zip(TOPICS, ("실행 상태", "현재 CPU/메모리 사용량", "이미지 세부 정보", "이미지", "볼륨·마운트", "포트", "네트워크", "리소스 제한", "재시작 정책", "healthcheck 설정", "의존 서비스", "빌드 설정")))


def _usage(containers):
    running = [r for r in containers if r["state"] == "running"]
    try:
        raw = docker._json(["docker", "stats", "--no-stream", "--format", "{{json .}}", *[r["container_id"] for r in running]]) if running else []
        rows = [raw] if isinstance(raw, dict) else raw
        error = None
    except Exception as exc: rows, error = [], redact(str(exc))
    for item in containers:
        value = {"available": False, "reason": "현재 중지 상태여서 실시간 사용량을 수집할 수 없습니다.", "historical_available": False}
        if item["state"] == "running":
            matches = [r for r in rows if isinstance(r.get("ID"), str) and len(r["ID"]) >= 12 and item["container_id"].startswith(r["ID"])]
            try:
                if len(matches) != 1: raise ValueError(error or "stats의 컨테이너 ID와 일치하는 단일 측정이 없습니다")
                row = matches[0]
                cpu, memory = float(row["CPUPerc"].rstrip("%")), float(row["MemPerc"].rstrip("%"))
                if not all(math.isfinite(v) and v >= 0 for v in (cpu, memory)): raise ValueError("invalid counters")
                value = {"available": True, "cpu_percent": cpu, "memory_percent": memory, "memory_usage": row.get("MemUsage"), "sampled_at": datetime.now(timezone.utc).isoformat(), "historical_available": False}
            except (KeyError, TypeError, ValueError) as exc: value["reason"] = redact(str(exc))
        item["usage"] = value


def _image_details(containers, config):
    cache = {}
    for item in containers:
        identity = item["images"].get("image_id")
        if identity not in cache:
            try:
                if not isinstance(identity, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", identity): raise ValueError("invalid immutable image ID")
                rows = docker._json(["docker", "image", "inspect", identity])
                if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("Id") != identity: raise ValueError("image identity mismatch")
                image = rows[0]; labels = (image.get("Config") or {}).get("Labels") or {}
                cache[identity] = {"available": True, "image_id": identity, "size_bytes": image.get("Size"), "architecture": image.get("Architecture"), "os": image.get("Os"),
                                   "base_metadata": {k: labels[k] for k in ("org.opencontainers.image.base.name", "org.opencontainers.image.base.digest") if k in labels}}
            except Exception as exc: cache[identity] = {"available": False, "error": redact(str(exc))}
        detail = dict(cache[identity])
        build = config.get(item["service"], {}).get("build")
        if isinstance(build, dict):
            try:
                context = Path(build.get("context", "."))
                path = context / build.get("dockerfile", "Dockerfile")
                if path.is_absolute(): path = path.relative_to(docker.PROJECT)
                file = docker.ProjectFiles(docker.PROJECT).read(path.as_posix())
                if file.get("ok") and not file.get("truncated"):
                    detail["declared_dockerfile"] = {"path": path.as_posix(), "from": [{"line": n, "instruction": line.strip()} for n, line in enumerate(file["content"].splitlines(), 1) if re.match(r"\s*FROM\s", line, re.I)]}
            except Exception as exc: detail["dockerfile_error"] = redact(str(exc))
        item["image_details"] = detail


def _health(config, *, runtime=False):
    if not isinstance(config, dict) or not config: return {"configured": False}
    test = config.get("Test", config.get("test"))
    disabled = config.get("disable") is True or test == ["NONE"]
    timing = {}
    for key, value in config.items():
        if key.lower() not in {"interval", "timeout", "retries", "startperiod", "start_period", "start_interval", "startinterval"}: continue
        if runtime and key.lower() != "retries" and type(value) is int:
            timing[key + "_seconds"] = value / 1_000_000_000
        else: timing[key] = value
    return {"configured": not disabled, "command_omitted": True, "timing": timing}


def _runtime_fields(item):
    config, host, network = item.get("Config") or {}, item.get("HostConfig") or {}, item.get("NetworkSettings") or {}
    return {"status": {"state": (item.get("State") or {}).get("Status", "unknown"), "health": ((item.get("State") or {}).get("Health") or {}).get("Status", "unknown")},
            "images": {"reference": config.get("Image"), "image_id": item.get("Image")},
            "mounts": [{k: m.get(k) for k in ("Type", "Name", "Source", "Destination", "RW") if k in m} for m in item.get("Mounts", [])],
            "ports": {"exposed": sorted((config.get("ExposedPorts") or {}).keys()), "configured_bindings": host.get("PortBindings") or {}, "active_bindings": network.get("Ports") or {}},
            "networks": sorted((network.get("Networks") or {}).keys()),
            "resources": {k: host[k] for k in ("Memory", "MemorySwap", "NanoCpus", "CpuQuota", "CpuPeriod", "CpusetCpus", "PidsLimit") if k in host},
            "restart": host.get("RestartPolicy") or {}, "healthcheck": _health(config.get("Healthcheck"), runtime=True)}


def _compose_fields(item):
    return {"images": {"reference": item.get("image"), "build": item.get("build")},
            "mounts": item.get("volumes") or [], "ports": {"published": item.get("ports") or [], "expose": item.get("expose") or []},
            "networks": item.get("networks") or {},
            "resources": {k: item[k] for k in ("mem_limit", "cpus", "deploy") if k in item},
            "restart": item.get("restart"), "healthcheck": _health(item.get("healthcheck")),
            "dependencies": item.get("depends_on") or {}, "build": item.get("build")}


def inspect_project_configuration(services: list[str], source: Literal["runtime", "compose", "both"] = "both", include_usage: bool = False, include_image_details: bool = False) -> dict:
    """고정 프로젝트의 선언 설정과 실제 컨테이너 설정을 분리 조회. ENV·로그·명령 원문 제외, 변경 없음."""
    if not services or len(services) > 12 or len(set(services)) != len(services):
        raise ValueError("select 1-12 unique allowed services")
    for service in services: docker._service(service)
    if source not in {"runtime", "compose", "both"}: raise ValueError("invalid source")
    result = {"ok": True, "observed_at": datetime.now(timezone.utc).isoformat(),
              "scope": {"project_dir": str(docker.PROJECT), "mode": docker.MODE}, "services": services}
    service_config = {}
    if source in {"compose", "both"}:
        try:
            config = docker.validate_compose_config()
            if config.get("ok") is not True or not isinstance(config.get("service_config"), dict): raise ValueError("Compose structured configuration unavailable")
            service_config = config["service_config"]
            result["compose"] = {"ok": True, "services": {s: _compose_fields(config["service_config"][s]) for s in services if s in config["service_config"]}}
        except Exception as exc: result["compose"] = {"ok": False, "error": redact(str(exc))}
    if source in {"runtime", "both"}:
        try:
            if docker.MODE != "live": raise ValueError("Runtime configuration requires live inspect; fixture values are not invented")
            rows = docker._json(["docker", "compose", "ps", "--all", "--format", "json"])
            rows = [rows] if isinstance(rows, dict) else rows
            rows = [r for r in rows if r.get("Service") in services]
            if len(rows) > 50: raise ValueError("more than 50 containers; narrow the service scope")
            ids = [r.get("ID", "") for r in rows]
            if any(not re.fullmatch(r"[a-fA-F0-9]{12,64}", cid) for cid in ids): raise ValueError("invalid container identity")
            inspected = docker._json(["docker", "inspect", *ids]) if ids else []
            if not isinstance(inspected, list) or len(inspected) != len(rows): raise ValueError("incomplete container inspect")
            actual = []
            for row, item in zip(rows, inspected):
                if not all(isinstance(item.get(k), dict) for k in ("Config", "HostConfig", "State", "NetworkSettings")) or not isinstance(item.get("Mounts"), list):
                    raise ValueError("incomplete container configuration fields")
                labels = (item.get("Config") or {}).get("Labels") or {}
                if not item.get("Id", "").startswith(row["ID"]) or labels.get("com.docker.compose.service") != row["Service"]:
                    raise ValueError("container identity/service changed during lookup")
                if os.getenv("COMPOSE_PROJECT_NAME") and labels.get("com.docker.compose.project") != os.environ["COMPOSE_PROJECT_NAME"]:
                    raise ValueError("container project mismatch")
                actual.append({"service": row["Service"], "name": item.get("Name", "").lstrip("/"), "container_id": item["Id"],
                               "state": (item.get("State") or {}).get("Status", "unknown"), **_runtime_fields(item)})
            if include_usage: _usage(actual)
            if include_image_details: _image_details(actual, service_config)
            result["runtime"] = {"ok": True, "containers": actual, "services_without_containers": sorted(set(services) - {r["service"] for r in actual})}
        except Exception as exc: result["runtime"] = {"ok": False, "error": redact(str(exc))}
    result["ok"] = any(result.get(stage, {}).get("ok") for stage in ("compose", "runtime"))
    # Preserve structure while redacting strings; never serialize raw inspect or environment.
    def clean(value):
        if isinstance(value, dict): return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list): return [clean(v) for v in value]
        return redact(value) if isinstance(value, str) else value
    return clean(result)


def _display(topic, value, actual=False):
    if value is None: return "명시 설정 없음"
    if topic == "images":
        ref = value.get("reference") or "이미지 이름 명시 없음"
        return ref + (f" · ID {value['image_id']}" if value.get("image_id") else "") + (f" · build {json.dumps(value['build'], ensure_ascii=False)}" if value.get("build") else "")
    if topic == "usage":
        if not value.get("available"): return value.get("reason", "측정 불가") + " 사용량을 0으로 추정하지 않습니다."
        return f"CPU {value['cpu_percent']}% · 메모리 {value.get('memory_usage', '단위 미확인')} ({value['memory_percent']}%) · 측정 {value['sampled_at']}"
    if topic == "image_details":
        if not value.get("available"): return "이미지 inspect 실패: " + value.get("error", "미확인")
        size = value.get("size_bytes")
        return (f"크기 {size} bytes ({size / 1048576:.2f} MiB)" if type(size) is int else "크기 미확인") + f" · 플랫폼 {value.get('os')}/{value.get('architecture')} · 기반 메타데이터 {json.dumps(value.get('base_metadata', {}), ensure_ascii=False)} · 선언 Dockerfile {json.dumps(value.get('declared_dockerfile'), ensure_ascii=False)}"
    if topic == "mounts" and actual:
        if not value: return "마운트 없음 (이 컨테이너의 inspect 결과)"
        return "; ".join(f"{m.get('Type', 'unknown')}{' ' + m['Name'] if m.get('Name') else ''}: {m.get('Source') or ('메모리' if m.get('Type') == 'tmpfs' else '원본 미확인')} → {m.get('Destination', '?')} ({'읽기·쓰기' if m.get('RW') is True else '읽기 전용' if m.get('RW') is False else '권한 미확인'})" for m in value)
    if topic == "healthcheck":
        return ("설정됨 · " + json.dumps(value.get("timing", {}), ensure_ascii=False) + " (명령 원문은 비밀값 보호로 생략)") if value.get("configured") else "명시 healthcheck 없음/비활성 (앱 정상 여부와는 별개)"
    if not value: return "명시 설정 없음" if not actual else "빈 설정 관측 (정상·연결 성공을 뜻하지 않음)"
    return json.dumps(value, ensure_ascii=False)


def inventory_answer(value, topics, source, project):
    lines = [f"선택 프로젝트 **{project}**의 현재 구성 조회입니다. [E1]"]
    stages = [stage for stage in ("runtime", "compose") if source in {stage, "both"}]
    complete = True
    for stage in stages:
        data = value.get(stage, {})
        label = "실제 컨테이너 설정 (docker inspect)" if stage == "runtime" else "Compose 선언 설정 (실제 적용 여부와 별개)"
        lines += ["", f"### {label}", ""]
        if data.get("ok") is not True:
            complete = False
            lines += [f"조회 실패: {data.get('error', value.get('error', '관측 미완료'))}. 설정 없음으로 판단하지 않습니다. [E1]"]
            continue
        rows = data.get("containers", []) if stage == "runtime" else [{"service": s, **fields} for s, fields in data.get("services", {}).items()]
        for row in rows:
            who = row["service"] + (f" / {row['name']} · {row['state']}" if stage == "runtime" else "")
            for topic in topics:
                if stage == "runtime" and topic in {"dependencies", "build"}: continue
                if stage == "compose" and topic in {"status", "usage", "image_details"}: continue
                if stage == "runtime" and topic in {"usage", "image_details"} and not (row.get(topic) or {}).get("available"): complete = False
                lines.append(f"- {who} · {TITLES[topic]}: {_display(topic, row.get(topic), stage == 'runtime')} [E1]")
        missing = data.get("services_without_containers", []) if stage == "runtime" else sorted(set(value.get("services", [])) - set(data.get("services", {})))
        if missing: lines.append(f"- {', '.join(missing)}: {'대응 컨테이너 없음. 선언을 실제 마운트·이미지로 간주하지 않습니다.' if stage == 'runtime' else '선언 서비스가 발견되지 않았습니다.'} [E1]")
        if stage == "runtime" and any(t in topics for t in ("dependencies", "build")):
            lines.append("빌드·depends_on은 Compose 선언 정보이며 컨테이너 inspect로 확정하지 않습니다.")
    if "ports" in topics: lines += ["", "configured_bindings는 생성 시 설정, active_bindings는 현재 inspect 값입니다. 중지 상태의 빈 값으로 포트 미설정을 단정하지 않으며 연결 테스트는 하지 않았습니다."]
    if "mounts" in topics: lines += ["", "volume의 Source는 Docker Engine 내부 경로입니다. Engine이 VM/원격에 있으면 현재 PC의 로컬 경로와 다를 수 있습니다. 마운트 존재만으로 데이터 보존·백업·복구 성공은 보증하지 않습니다."]
    if "resources" in topics: lines += ["", "리소스 값은 제한 설정이며 사용량 측정이 아닙니다. 0/빈 값만으로 호스트·상위 제한이 없다고 단정하지 않습니다."]
    if "usage" in topics: lines += ["", "CPU/메모리는 현재 시점 측정입니다. 종료 당시 메모리·최대 사용량을 저장한 시계열은 없으므로 과거 사용량으로 제시하지 않습니다."]
    if "image_details" in topics: lines += ["", "크기는 로컬 이미지 inspect의 Size이며 압축 전송 크기나 컨테이너 쓰기 계층 크기가 아닙니다. FROM은 현재 파일의 선언으로 실제 빌드 당시 베이스 digest의 증명이 아닙니다. base 메타데이터가 없으면 원본 베이스를 확정하지 않습니다."]
    lines += ["", "선택 프로젝트의 허용 서비스 범위만 조회했고, 변경은 수행하지 않았습니다."]
    any_ok = any(value.get(stage, {}).get("ok") is True for stage in stages)
    return {"final": "\n".join(lines), "result_kind": "configuration_inventory", "approval": None, "proposal": None,
            "answer_status": "validated" if complete else "partial" if any_ok else "unavailable", "execution_status": "completed" if any_ok else "failed",
            "validation_ok": complete, "validation_attempts": 0, "evidence_sufficient": complete, "evidence_coverage": None,
            "analysis_used": False, "analysis_policy": "skip", "analysis_reason": "구조화된 구성 사실 직접 표시: 개선점·원인 추론·Writer 생략"}


async def observe_inventory(deps, state):
    target = state.get("routing_target") or {}
    services = list(deps.allowed_services) if target.get("scope") == "project" else [target.get("service")]
    if any(s not in deps.allowed_services for s in services): raise ValueError("inventory requires a confirmed allowed target")
    args = {"services": services, "source": state["inventory_source"]}
    if "usage" in state["inventory_topics"]: args["include_usage"] = True
    if "image_details" in state["inventory_topics"]: args["include_image_details"] = True
    started = time.perf_counter()
    try: value, retries = await deps.mcp.tool("inspect_project_configuration", args)
    except Exception as exc: value, retries = {"ok": False, "error": redact(str(exc))}, getattr(exc, "retries", 0)
    elapsed = time.perf_counter() - started
    item = {"id": "E1", "tool": "inspect_project_configuration", "args": args, "observation": value, "elapsed_ms": elapsed * 1000, "agent": "runtime"}
    events = list(state.get("events", []))
    emit(events, {"type": "TaskCompleted" if value.get("ok") else "TaskFailed", "id": "E1", "target": item["tool"], "retries": retries, "agent": "runtime", "node": "inventory_observe"})
    answer = inventory_answer(value, state["inventory_topics"], state["inventory_source"], (deps.selected_target or {}).get("project", "현재 선택 대상"))
    return answer | {"events": events, "evidence": [item], "tasks": [{"tool": item["tool"], "args": args}], "retry_count": retries,
                     "tool_time_sum": elapsed, "observe_wall_sum": elapsed}
