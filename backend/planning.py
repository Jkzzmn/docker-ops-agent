"""Deterministic limits around model-selected plans and graph transitions."""
import re

from .diagnostics import READ_TOOLS, classify, contains_dangerous, required_tasks, completed_observation, succeeded, target_service, task_key


WORKER_TOOLS = {
    "runtime": {"get_docker_engine_status", "list_containers", "inspect_container", "get_container_logs", "get_container_stats", "get_container_events", "check_service_health"},
    "configuration": {"validate_compose_config"},
    "network": {"inspect_network", "test_container_dns", "test_tcp_connection", "inspect_listening_ports"},
}
TOOL_ARGUMENTS = {
    **{name: ["service"] for name in ("inspect_container", "get_container_stats", "get_container_events", "check_service_health", "inspect_listening_ports")},
    "get_container_logs": ["service", "tail?"], "get_docker_engine_status": [], "list_containers": [], "validate_compose_config": [],
    "inspect_network": ["source", "target"], "test_container_dns": ["source", "target"], "test_tcp_connection": ["source", "target", "port"],
}


def unsafe_execution_request(query: str) -> bool:
    text = query.lower()
    concept = bool(re.search(r"뭐|뭔|무엇|정의|차이|개념|설명|what is|what are|explain|difference", text))
    dangerous = contains_dangerous(text) or any(word in text for word in ("볼륨 삭제", "모든 컨테이너 삭제"))
    return bool(dangerous and (not concept or re.search(r"실행|삭제해|자동|execute|run\s", text)))


def normalize_tasks(raw, allowed: tuple[str, ...], source: str, permitted=READ_TOOLS) -> list[dict]:
    if not isinstance(raw, list):
        return []
    if len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    tasks, seen = [], set()
    for item in raw[:12]:
        if not isinstance(item, dict) or not isinstance(item.get("tool"), str) or item["tool"] not in permitted:
            continue
        tool = item["tool"]
        args = dict(item.get("args", {})) if isinstance(item.get("args", {}), dict) else {}
        fields = {name.rstrip("?") for name in TOOL_ARGUMENTS[tool]}
        required = {name for name in TOOL_ARGUMENTS[tool] if not name.endswith("?")}
        if "service" in required:
            args.setdefault("service", source)
        if not required <= args.keys() or not args.keys() <= fields:
            continue
        if any(args[key] not in allowed for key in ("service", "source", "target") if key in args):
            continue
        if tool == "get_container_logs":
            try:
                args["tail"] = max(1, min(int(args.get("tail", 100)), 300))
            except (TypeError, ValueError, OverflowError):
                continue
        if tool == "test_tcp_connection" and (type(args["port"]) is not int or not 1 <= args["port"] <= 65535):
            continue
        task = {"tool": tool, "args": args, "why": str(item.get("why", "model-planned observation"))[:300]}
        key = task_key(task)
        if key not in seen:
            tasks.append(task)
            seen.add(key)
    return tasks


def status_tasks(query: str, allowed: tuple[str, ...], target: dict | None = None) -> list[dict]:
    if target is not None:
        if target.get("scope") == "project":
            return [{"tool": "list_containers", "args": {}, "why": "LLM-confirmed project status only"}]
        if target.get("scope") != "service" or target.get("service") not in allowed:
            raise ValueError("status requires a confirmed allowed target")
        return [{"tool": "check_service_health", "args": {"service": target["service"]}, "why": "LLM-confirmed service status only"}]
    if re.search(r"목록|전체|모든|list", query.lower()):
        return [{"tool": "list_containers", "args": {}, "why": "requested project status only"}]
    return [{"tool": "check_service_health", "args": {"service": target_service(query, allowed)}, "why": "requested service status only"}]


def review_evidence(query: str, route: str, evidence: list[dict], allowed: tuple[str, ...], target: dict | None = None) -> dict:
    required = status_tasks(query, allowed, target) if route == "status" else required_tasks(query, evidence, allowed)
    collected = {task_key(e) for e in evidence if completed_observation(e)}
    missing = [task for task in required if task_key(task) not in collected]
    diagnosis = classify(evidence, target_service(query, allowed))
    if route == "status":
        good = [e for e in evidence if succeeded(e)]
        diagnosis = {"cause": "healthy" if diagnosis["cause"] == "healthy" else "unknown", "evidence_ids": [e["id"] for e in good],
                     "corroborated": False, "confidence": .85 if good else .0,
                     "reason": "현재 상태 조회 결과만 보고합니다. 원인 추정·복구 제안은 수행하지 않았습니다."}
    return {"missing": missing, "evidence_coverage": (len(required) - len(missing)) / len(required),
            "evidence_sufficient": not missing, "diagnosis": diagnosis}
