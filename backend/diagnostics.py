"""Evidence requirements and conservative, auditable cause classification."""
from __future__ import annotations

import json
import re


READ_TOOLS = {
    "get_docker_engine_status", "list_containers", "inspect_container", "get_container_logs",
    "get_container_stats", "get_container_events", "validate_compose_config", "check_service_health", "inspect_network",
    "test_container_dns", "test_tcp_connection", "inspect_listening_ports",
}
DANGEROUS = ("system prune", "volume prune", "volume rm", "compose down -v", "rm -rf")
# Command-shaped patterns only: a bare word such as "shell" or "powershell" in a symptom report is not an execution request.
DANGEROUS_RE = re.compile(
    r"(?<![a-z])docker(?:\s+compose)?\s+exec\b|\bexec\s+-it\b|(?<![a-z])(?:ba|z|k)?sh\s+-c\b|run_shell_command"
    r"|compose\s+down\s+(?:\S+\s+)*(?:-v|--volumes)\b|\brm\s+-[a-z]*r[a-z]*f"
    r"|(?<![a-z])shell\s+(?:command|exec|execute)\b|(?:임의|arbitrary)\s*(?:shell|셸|쉘)|(?:셸|쉘)\s*(?:명령|실행)")
TERMINAL_STATES = {"exited", "dead", "created"}
DOWN_STATES = TERMINAL_STATES | {"restarting"}


def contains_dangerous(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in DANGEROUS) or bool(DANGEROUS_RE.search(lowered))


def task_key(task: dict) -> str:
    args = dict(task.get("args", {}))
    if task["tool"] == "get_container_logs" and args.get("tail") == 100:
        args.pop("tail")
    return json.dumps([task["tool"], args], sort_keys=True, ensure_ascii=False)


def succeeded(item: dict) -> bool:
    value = item.get("observation", {})
    return isinstance(value, dict) and value.get("ok") is True and value.get("found", True) is not False


def completed_observation(item: dict) -> bool:
    """Usable evidence, not a healthy/present predicate. Legacy tools keep their contract."""
    value = item.get("observation", {})
    return (value.get("observation_status") == "complete" and value.get("ok") is True) or succeeded(item)


def target_service(query: str, allowed: tuple[str, ...]) -> str:
    mentioned = [(m.start(), name) for name in allowed if (m := re.search(r"(?<![a-z0-9_-])" + re.escape(name) + r"(?![a-z0-9_-])", query.lower()))]
    return min(mentioned)[1] if mentioned else ("api" if "api" in allowed else allowed[0])


def required_tasks(query: str, evidence: list[dict], allowed: tuple[str, ...]) -> list[dict]:
    source = target_service(query, allowed)
    absence = any(e.get("args", {}).get("service") == source and e.get("observation", {}).get("subject_status") == "absent" and completed_observation(e) for e in evidence)
    if absence:
        return [{"tool": tool, "args": {}, "why": "project baseline; target is absent"} for tool in ("get_docker_engine_status", "list_containers", "validate_compose_config")] + [{"tool": "check_service_health", "args": {"service": source}, "why": "confirmed absence"}]
    lookup = {task_key(e): e for e in evidence if succeeded(e)}
    inspected = lookup.get(task_key({"tool": "inspect_container", "args": {"service": source}}), {}).get("observation", {})
    # A stopped container has no live cgroup counters; its current stats can never be collected, so they are not demanded.
    # The Docker OOM event and inspect State remain required and still have to corroborate each other.
    stopped = inspected.get("state") in TERMINAL_STATES
    per_service = ("inspect_container", "get_container_logs", *(() if stopped else ("get_container_stats",)), "get_container_events", "check_service_health")
    tasks = [{"tool": "get_docker_engine_status", "args": {}}, {"tool": "list_containers", "args": {}},
             *[{"tool": tool, "args": {"service": source}} for tool in per_service],
             {"tool": "validate_compose_config", "args": {}}]
    config = lookup.get(task_key({"tool": "validate_compose_config"}), {}).get("observation", {})
    logs = lookup.get(task_key({"tool": "get_container_logs", "args": {"service": source}}), {}).get("observation", {})
    context = query.lower() + json.dumps(logs, ensure_ascii=False).lower()
    if any(word in context for word in ("network", "네트워크", "dns", "연결", "connection", "readiness", "depends_on", "준비", "db")):
        deps = config.get("depends_on", {}).get(source, {})
        peers = [name for name in allowed if name != source and (name in deps or re.search(r"(?<![a-z0-9_-])" + re.escape(name) + r"(?![a-z0-9_-])", context))]
        for peer in peers[:2]:
            pair = {"source": source, "target": peer}
            tasks.extend([{"tool": "inspect_network", "args": pair}, {"tool": "test_container_dns", "args": pair},
                          {"tool": "check_service_health", "args": {"service": peer}}])
            for port in config.get("ports", {}).get(peer, [])[:1]:
                tcp = {"tool": "test_tcp_connection", "args": pair | {"port": port}}
                tasks.append(tcp)
                if any(x in context for x in ("connection refused", "not ready", "readiness", "준비")):
                    tasks.append({"tool": "test_tcp_connection", "args": {"source": peer, "target": peer, "port": port}})
                # Same network and a healthy target, yet the port refuses: ask the target which address it actually listens on.
                tcp_result = lookup.get(task_key(tcp), {}).get("observation", {})
                shared = lookup.get(task_key({"tool": "inspect_network", "args": pair}), {}).get("observation", {}).get("shared_networks")
                peer_health = lookup.get(task_key({"tool": "check_service_health", "args": {"service": peer}}), {}).get("observation", {}).get("health")
                if tcp_result.get("connected") is False and shared and peer_health == "healthy":
                    tasks.append({"tool": "inspect_listening_ports", "args": {"service": peer}})
    return [task | {"why": "scenario evidence requirement"} for task in tasks]


def target_is_down(evidence: list[dict], service: str) -> bool:
    """True only when a successful live health observation shows the target is unhealthy, restarting or stopped."""
    return any(e["tool"] == "check_service_health" and e.get("args", {}).get("service") == service and succeeded(e) and
               (e["observation"].get("health") == "unhealthy" or e["observation"].get("state") in DOWN_STATES) for e in evidence)


def classify(evidence: list[dict], source: str) -> dict:
    absent = [e for e in evidence if e.get("args", {}).get("service") == source and e.get("observation", {}).get("subject_status") == "absent" and completed_observation(e)]
    if absent:
        return {"cause": "not_deployed", "evidence_ids": [e["id"] for e in absent], "corroborated": False, "confidence": .9,
                "reason": "완료된 프로젝트 조회에서 대응 컨테이너가 없습니다. 미배포이며 런타임 장애·정상·복구 성공을 판정할 수 없습니다."}
    good = [e for e in evidence if succeeded(e)]

    def find(tool: str, **args) -> dict | None:
        return next((e for e in reversed(good) if e["tool"] == tool and all(e.get("args", {}).get(k) == v for k, v in args.items())), None)

    def result(cause: str, items: list[dict | None], reason: str, confidence: float) -> dict:
        ids = list(dict.fromkeys(e["id"] for e in items if e))
        return {"cause": cause, "evidence_ids": ids, "corroborated": len(ids) >= 2, "reason": reason, "confidence": confidence}

    inspect = find("inspect_container", service=source)
    logs = find("get_container_logs", service=source)
    stats = find("get_container_stats", service=source)
    events = find("get_container_events", service=source)
    config = find("validate_compose_config")
    health = find("check_service_health", service=source)
    observed = inspect["observation"] if inspect else {}
    log_text = json.dumps(logs["observation"], ensure_ascii=False).lower() if logs else ""
    if observed.get("oom_killed") is True and observed.get("exit_code") == 137:
        corroboration = stats if stats and stats["observation"].get("memory_percent", 0) >= 90 else None
        if events and any(e.get("action") == "oom" for e in events["observation"].get("events", [])):
            corroboration = events
        if corroboration or (logs and any(x in log_text for x in ("oom", "out of memory", "memory limit"))):
            return result("memory", [inspect, corroboration or logs], "OOMKilled=true와 Exit 137을 독립 메모리·로그·Docker 이벤트 관측으로 교차 확인했습니다. 재시작은 임시 복구입니다.", .95)
        return result("memory", [inspect], "Docker inspect의 OOMKilled=true와 Exit 137이 OOM 종료를 나타냅니다. 독립 메모리·이벤트 근거가 부족하므로 복구 승인을 생성하지 않습니다.", .85)
    if logs and config and any(x in log_text for x in ("connection refused", "not ready")):
        cfg = config["observation"]
        for peer, condition in cfg.get("depends_on", {}).get(source, {}).items():
            dependency = find("check_service_health", service=peer)
            condition = condition.get("condition") if isinstance(condition, dict) else condition
            missing_gate = peer not in cfg.get("healthchecks", []) or condition != "service_healthy"
            dns = find("test_container_dns", source=source, target=peer)
            tcp = find("test_tcp_connection", source=source, target=peer)
            local_tcp = find("test_tcp_connection", source=peer, target=peer)
            not_ready = dependency and (dependency["observation"].get("health") in {"starting", "unhealthy"} or
                (dependency["observation"].get("health") != "healthy" and ((local_tcp and local_tcp["observation"].get("connected") is False) or
                 (dns and dns["observation"].get("resolved") is True and tcp and tcp["observation"].get("connected") is False))))
            if missing_gate and not_ready:
                return result("readiness", [logs, config, dependency, local_tcp or tcp], "DB 준비 상태와 무관한 시작 순서로 연결 거부가 발생했습니다. DB healthcheck와 service_healthy 조건을 검토하세요.", .9)
    if logs and config:
        cfg = config["observation"]
        for peer in cfg.get("services", list(cfg.get("ports", {}))):
            container = set(cfg.get("ports", {}).get(peer, []))
            host_only = {int(p["published"]) for p in cfg.get("published", {}).get(peer, []) if str(p.get("published", "")).isdigit() and int(p["published"]) not in container}
            used = {int(x) for x in re.findall(r"(?<![\w-])" + re.escape(peer) + r"[:/ ](\d{2,5})\b", log_text)}
            if used & host_only:
                wrong = sorted(used & host_only)
                return result("network", [logs, config], f"로그가 {peer}의 호스트 게시 포트 {wrong}로 접속했습니다. 컨테이너 사이에서는 컨테이너 포트 {sorted(container)}를 써야 합니다.", .9)
    for tcp in good:
        if tcp["tool"] != "test_tcp_connection" or tcp.get("args", {}).get("source") != source or tcp["observation"].get("connected") is not False:
            continue
        peer, port = tcp["args"]["target"], tcp["args"]["port"]
        listeners = find("inspect_listening_ports", service=peer)
        bound = [x for x in (listeners["observation"].get("listeners", []) if listeners else []) if x.get("port") == port]
        if bound and all(x.get("loopback") for x in bound):
            return result("network", [tcp, listeners], f"{peer}의 {port} 포트는 컨테이너 내부 loopback에만 바인딩되어 다른 컨테이너에서 접근할 수 없습니다. 0.0.0.0(모든 인터페이스)에 바인딩하도록 서비스 설정을 검토하세요.", .9)
    for item in good:
        if item["tool"] != "inspect_network" or item.get("args", {}).get("source") != source:
            continue
        peer = item["args"]["target"]
        dns = find("test_container_dns", source=source, target=peer)
        if not item["observation"].get("shared_networks") and dns and dns["observation"].get("resolved") is False:
            return result("network", [item, dns], "서비스 사이에 공유 Docker network가 없으며 DNS 해석도 실패했습니다. 동일 network 및 alias 설정을 검토하세요.", .95)
        tcp = find("test_tcp_connection", source=source, target=peer)
        dependency = find("check_service_health", service=peer)
        if dns and dns["observation"].get("resolved") is False:
            return result("network", [item, dns], "공유 network 정보와 DNS 실패를 확인했습니다. 서비스명 및 network alias를 검토하세요.", .8)
        if tcp and tcp["observation"].get("connected") is False and dependency and dependency["observation"].get("health") == "healthy":
            return result("network", [item, tcp, dependency], "대상은 healthy이지만 선언된 container port 연결에 실패했습니다. listener와 port 설정을 추가 확인하세요.", .75)
    if health and health["observation"].get("state") == "running" and health["observation"].get("health") == "healthy":
        return result("healthy", [health], "현재 대상 서비스의 Docker health는 정상입니다. 이 관측만으로 과거 장애가 없었다고 판단하지는 않습니다.", .85)
    return result("unknown", [inspect, logs], "원인을 확정할 근거가 부족합니다. Exit 137만으로 OOM을 단정하지 않으며 자동 복구를 제안하지 않습니다.", .3)


