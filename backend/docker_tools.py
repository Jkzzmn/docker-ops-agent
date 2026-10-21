"""Fixed Docker capabilities shared by the MCP server and deterministic tests."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .approvals import verify_approval
from .sanitize import redact
from .project_files import ProjectFiles


ROOT = Path(__file__).resolve().parents[1]
MODE = os.getenv("DOCKER_MODE", "live")
PROJECT = Path(os.getenv("DOCKER_PROJECT_DIR", str(ROOT / "demo_stack"))).resolve()
ALLOWED = {x.strip() for x in os.getenv("DOCKER_ALLOWED_SERVICES", "api,db,web").split(",") if x.strip()}
SCENARIO = os.getenv("DOCKER_FIXTURE_SCENARIO", "oom")
FIXTURES = {"oom": "docker_state.json", "oom-exited": "oom_exited.json", "readiness": "readiness.json", "network": "network.json", "healthy": "healthy.json",
            "exit137": "exit137.json", "loopback": "loopback.json", "host-port": "host_port.json"}
RECOVERED: set[str] = set()
USED_APPROVALS: dict[str, float] = {}
APPROVAL_LOCK = threading.Lock()


def _data() -> dict:
    if SCENARIO not in FIXTURES:
        raise ValueError(f"unknown fixture scenario: {SCENARIO}")
    return json.loads((ROOT / "backend" / "fixtures" / FIXTURES[SCENARIO]).read_text())


def _service(name: str) -> str:
    if name not in ALLOWED:
        raise ValueError(f"service '{name}' is outside allowlist")
    return name


def _capture(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    if MODE != "live":
        raise RuntimeError("live Docker command disabled in fixture mode")
    explicit_files = os.getenv("COMPOSE_FILE", "").split(os.pathsep) if os.getenv("COMPOSE_FILE") else []
    valid_compose = all((PROJECT / p).is_file() for p in explicit_files) if explicit_files else any((PROJECT / f).is_file() for f in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"))
    if not PROJECT.is_dir() or not valid_compose:
        raise ValueError("DOCKER_PROJECT_DIR must contain a Compose file")
    return subprocess.run(args, cwd=PROJECT, text=True, capture_output=True, timeout=timeout, check=False)


def _run(args: list[str], timeout: int = 15) -> dict:
    cp = _capture(args, timeout)
    return {"ok": cp.returncode == 0, "exit_code": cp.returncode, "stdout": redact(cp.stdout), "stderr": redact(cp.stderr)}


def _json(args: list[str]) -> dict | list:
    result = _capture(args)
    if result.returncode:
        raise RuntimeError(redact(result.stderr) or "Docker command failed")
    output = result.stdout.strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return [json.loads(line) for line in output.splitlines() if line.strip()]


def _inspect(service: str) -> dict:
    ids = _run(["docker", "compose", "ps", "--all", "-q", service])
    if not ids["ok"] or len(ids["stdout"].splitlines()) != 1:
        raise ValueError("exactly one container per service is required")
    rows = _json(["docker", "inspect", ids["stdout"].strip()])
    return rows[0]


def get_docker_engine_status() -> dict:
    """Docker Engine 연결과 버전을 조회합니다."""
    if MODE == "fixture":
        return _data()["engine"] | {"ok": True, "mode": MODE}
    server = _json(["docker", "version", "--format", "{{json .Server}}"])
    return {"ok": True, "available": bool(server), "server_version": server.get("Version")}


def list_containers() -> dict:
    """고정된 Compose 프로젝트의 허용된 서비스만 조회합니다."""
    if MODE == "fixture":
        rows = _data()["containers"]
        rows = [r | {"state": "running", "health": "healthy"} if r["service"] in RECOVERED else r for r in rows]
    else:
        raw = _json(["docker", "compose", "ps", "--all", "--format", "json"])
        raw = [raw] if isinstance(raw, dict) else raw
        rows = [{"service": r.get("Service"), "state": r.get("State"), "health": r.get("Health", ""), "name": r.get("Name")} for r in raw]
    return {"ok": True, "containers": [r for r in rows if r["service"] in ALLOWED], "mode": MODE,
            "observation_status": "complete", "observed_at": datetime.now(timezone.utc).isoformat(), "scope": {"project_dir": str(PROJECT), "mode": MODE},
            "configured_services_without_containers": sorted(ALLOWED - {r["service"] for r in rows}),
            "other_project_containers": [r for r in rows if r["service"] not in ALLOWED]}


def inspect_container(service: str) -> dict:
    """종료/OOM/health/network 정보만 반환하며 ENV와 mount 비밀정보는 노출하지 않습니다."""
    _service(service)
    if MODE == "fixture":
        value = _data().get("inspect", {}).get(service)
        if value is None:
            return {"ok": False, "service": service, "found": False}
        if service in RECOVERED:
            value = value | {"state": "running", "health": "healthy", "oom_killed": False, "exit_code": 0}
        return {"ok": True, "service": service, **value}
    try: item = _inspect(service)
    except ValueError:
        lookup = check_service_health(service)
        if lookup.get("subject_status") in {"absent", "ambiguous"}: return lookup
        return {"ok": False, "service": service, "observation_status": "failed", "subject_status": "unknown", "error": "inspect failed; absence is not established"}
    state = item.get("State", {})
    return {"ok": True, "service": service, "state": state.get("Status"), "oom_killed": state.get("OOMKilled", False),
            "container_id": item.get("Id"), "image_id": item.get("Image"), "created": item.get("Created"),
            "exit_code": state.get("ExitCode"), "health": state.get("Health", {}).get("Status", ""),
            "restarts": item.get("RestartCount", 0), "networks": list(item.get("NetworkSettings", {}).get("Networks", {}))}


def get_container_logs(service: str, tail: int = 100) -> dict:
    """최근 로그를 길이 제한 및 비밀값 마스킹 후 반환합니다."""
    _service(service)
    tail = max(1, min(tail, 300))
    if MODE == "fixture":
        return {"ok": True, "service": service, "logs": redact(_data().get("logs", {}).get(service, ""))}
    try: _inspect(service)
    except ValueError: return {"ok": False, "service": service, "found": False, "error": "no unique deployed service container"}
    return _run(["docker", "compose", "logs", "--no-color", "--tail", str(tail), service]) | {"service": service}


def get_container_stats(service: str) -> dict:
    """현재 CPU와 메모리 사용률을 조회합니다. 종료 직전 사용률을 추정하지 않습니다."""
    _service(service)
    if MODE == "fixture":
        value = _data().get("stats", {}).get(service)
        return {"service": service, "ok": value is not None, **(value or {})}
    item = _inspect(service)
    if item.get("State", {}).get("Status") != "running":
        return {"ok": False, "service": service, "error": "current stats unavailable for non-running container"}
    raw = _json(["docker", "stats", "--no-stream", "--format", "{{json .}}", item["Id"]])
    raw = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(raw, dict) or not raw.get("MemPerc"):
        return {"ok": False, "service": service, "error": "stats unavailable for stopped container"}
    return {"ok": True, "service": service, "memory_percent": float(raw["MemPerc"].rstrip("%")),
            "cpu_percent": float(raw["CPUPerc"].rstrip("%")), "memory_usage": raw.get("MemUsage")}


def get_container_events(service: str) -> dict:
    """최근 10분 동안 해당 컨테이너에 실제 발생한 Docker OOM 이벤트를 조회합니다."""
    _service(service)
    if MODE == "fixture":
        return {"ok": True, "service": service, "events": _data().get("events", {}).get(service, [])}
    item = _inspect(service)
    until = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = _json(["docker", "events", "--since", "10m", "--until", until,
                  "--filter", "type=container", "--filter", f"container={item['Id']}", "--filter", "event=oom", "--format", "{{json .}}"])
    rows = [rows] if isinstance(rows, dict) else rows
    return {"ok": True, "service": service, "events": [{"action": r.get("Action", r.get("status")), "time": r.get("time")} for r in rows]}


def validate_compose_config() -> dict:
    """Compose의 healthcheck와 dependency 조건을 비밀정보 없이 조회합니다."""
    if MODE == "fixture":
        return _data()["compose"] | {"ok": True}
    raw = _json(["docker", "compose", "config", "--format", "json"])
    services = raw.get("services", {})
    allowed = {name: item for name, item in services.items() if name in ALLOWED}
    safe = {name: {key: value for key, value in item.items() if key in {
        "image", "healthcheck", "depends_on", "restart", "mem_limit", "cpus", "networks", "volumes", "ports",
        "expose", "privileged", "cap_add", "cap_drop", "user", "read_only", "security_opt", "network_mode"}} for name, item in allowed.items()}
    for name, item in allowed.items():
        if isinstance(item.get("build"), dict):
            safe[name]["build"] = {k: v for k, v in item["build"].items() if k in {"context", "dockerfile", "target"}}
        if isinstance(item.get("deploy"), dict): safe[name]["deploy"] = {"resources": item["deploy"].get("resources", {})}
    return {"ok": True, "valid": True, "services": list(allowed),
            "service_config": json.loads(redact(json.dumps(safe), 500000)),
            "healthchecks": [name for name, item in allowed.items() if item.get("healthcheck") and not item["healthcheck"].get("disable")],
            "depends_on": {name: item.get("depends_on", {}) for name, item in allowed.items()},
            "ports": {name: [int(p["target"]) for p in item.get("ports", []) if isinstance(p, dict)] + [int(str(p).split('/')[0]) for p in item.get("expose", [])] for name, item in allowed.items()},
            "published": {name: [{"target": int(p["target"]), "published": int(p["published"])} for p in item.get("ports", []) if isinstance(p, dict) and str(p.get("published", "")).isdigit()] for name, item in allowed.items()}}


def check_service_health(service: str) -> dict:
    """Docker lifecycle와 health 상태를 구조화해 반환합니다."""
    _service(service)
    from datetime import datetime, timezone
    try:
        result = list_containers()
    except Exception as exc:
        return {"ok": False, "service": service, "observation_status": "failed", "subject_status": "unknown", "error": redact(str(exc))}
    scope = {"project_dir": str(PROJECT), "mode": MODE}
    common = {"service": service, "scope": scope, "observed_at": datetime.now(timezone.utc).isoformat()}
    if not result.get("ok"):
        return common | {"ok": False, "observation_status": "failed", "subject_status": "unknown", "error": result.get("error", "incomplete lookup")}
    rows = [r for r in result["containers"] if r["service"] == service]
    if len(rows) != 1:
        return common | {"ok": True, "found": bool(rows), "count": len(rows), "observation_status": "complete", "subject_status": "ambiguous" if rows else "absent"}
    return common | rows[0] | {"ok": True, "found": True, "observation_status": "complete", "subject_status": "present"}


def inspect_network(source: str, target: str) -> dict:
    """허용된 두 서비스의 공유 Docker network를 검사합니다."""
    a, b = inspect_container(_service(source)), inspect_container(_service(target))
    if not a.get("ok") or not b.get("ok"):
        return {"ok": False, "source": source, "target": target, "error": "container missing"}
    shared = sorted(set(a.get("networks", [])) & set(b.get("networks", [])))
    return {"ok": True, "source": source, "target": target, "shared_networks": shared,
            "source_networks": sorted(a.get("networks", [])), "target_networks": sorted(b.get("networks", []))}


# Fixed Python socket probe, never a caller-supplied command or host address.
PROBE = """import json,socket,sys
host,mode,port=sys.argv[1:]
def listeners():
    rows=[]
    for path,v6 in (('/proc/net/tcp',False),('/proc/net/tcp6',True)):
        try: lines=open(path).read().splitlines()[1:]
        except OSError: continue
        for line in lines:
            f=line.split()
            if len(f)<4 or f[3]!='0A': continue
            ip,p=f[1].split(':')
            if v6:
                address='::' if set(ip)=={'0'} else '::1' if ip=='00000000000000000000000001000000' else 'other'
                loop=address=='::1'
            else:
                address=socket.inet_ntoa(bytes.fromhex(ip)[::-1]); loop=address.startswith('127.')
            rows.append({'address':address,'port':int(p,16),'loopback':loop})
    return rows
try:
    if mode=='listen':
        value={'listeners':listeners()}
    elif mode=='dns':
        value={'resolved':True,'addresses':sorted({x[4][0] for x in socket.getaddrinfo(host,None)})}
    else:
        with socket.create_connection((host,int(port)),timeout=2): pass
        value={'connected':True}
except OSError as exc:
    value={'resolved':False,'error':str(exc)} if mode=='dns' else {'connected':False,'error':str(exc)}
print(json.dumps(value))
"""


def test_container_dns(source: str, target: str) -> dict:
    """source 컨테이너에서 allowlist 서비스명만 DNS 조회합니다. python3가 필요합니다."""
    _service(source); _service(target)
    if MODE == "fixture":
        shared = inspect_network(source, target)["shared_networks"]
        return {"ok": True, "source": source, "target": target, "resolved": bool(shared)}
    result = _json(["docker", "compose", "exec", "-T", source, "python3", "-c", PROBE, target, "dns", "0"])
    return {"ok": True, "source": source, "target": target, **result}


def test_tcp_connection(source: str, target: str, port: int) -> dict:
    """선언된 container port와 allowlist 서비스 사이에만 고정 TCP probe를 실행합니다."""
    _service(source); _service(target)
    declared = validate_compose_config().get("ports", {}).get(target, [])
    if isinstance(port, bool) or port not in declared:
        raise ValueError("port must be a declared container port of the target service")
    if MODE == "fixture":
        override = _data().get("tcp", {}).get(f"{source}->{target}:{port}")
        if override is not None:
            return {"ok": True, "source": source, "target": target, "port": port, "connected": override}
        shared = inspect_network(source, target)["shared_networks"]
        health = check_service_health(target)
        return {"ok": True, "source": source, "target": target, "port": port,
                "connected": bool(shared) and health.get("state") == "running" and health.get("health") == "healthy"}
    result = _json(["docker", "compose", "exec", "-T", source, "python3", "-c", PROBE, target, "tcp", str(port)])
    return {"ok": True, "source": source, "target": target, "port": port, **result}


def inspect_listening_ports(service: str) -> dict:
    """allowlist 서비스 컨테이너가 실제로 listen 중인 주소·포트를 조회합니다(loopback 전용 바인딩 탐지). python3가 필요합니다."""
    _service(service)
    if MODE == "fixture":
        rows = _data().get("listeners", {}).get(service)
        if rows is None:
            return {"ok": False, "service": service, "error": "listener data unavailable in this fixture"}
        return {"ok": True, "service": service, "listeners": rows}
    result = _json(["docker", "compose", "exec", "-T", service, "python3", "-c", PROBE, service, "listen", "0"])
    return {"ok": True, "service": service, "listeners": result.get("listeners", [])}


def read_compose_text(project: str) -> str:
    """고정된 Compose 프로젝트의 설정 파일 원문(비밀값 마스킹)을 반환합니다. 다른 경로는 읽지 않습니다."""
    names = {PROJECT.name, os.getenv("COMPOSE_PROJECT_NAME", PROJECT.name)} | ({"demo-app"} if PROJECT.name == "demo_stack" else set())
    if project not in names:
        raise ValueError(f"project '{project}' is not the pinned Compose project")
    if MODE == "fixture":
        return redact(json.dumps(_data().get("compose", {}), ensure_ascii=False, indent=2))
    sources = ProjectFiles(PROJECT).compose()
    return "\n".join(f"# {item['path']}\n{item.get('content', '')}" for item in sources["files"])


def restart_service(service: str, approval_token: str) -> dict:
    """유효한 서명과 만료시간을 가진 승인을 정확히 한 번 사용해 재시작합니다."""
    _service(service)
    payload = verify_approval(os.getenv("MCP_APPROVAL_SECRET", ""), approval_token, "restart_service", {"service": service})
    with APPROVAL_LOCK:
        now = time.time()
        for key, expiry in list(USED_APPROVALS.items()):
            if expiry <= now:
                del USED_APPROVALS[key]
        if payload["id"] in USED_APPROVALS:
            raise PermissionError("approval already consumed")
        USED_APPROVALS[payload["id"]] = payload["expires_at"]
    if MODE == "fixture":
        RECOVERED.add(service)
        return {"ok": True, "service": service, "action": "restart", "simulated": True}
    return _run(["docker", "compose", "restart", service], timeout=60)
