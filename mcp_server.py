from __future__ import annotations

from fastmcp import FastMCP
from backend import docker_tools
from backend import config_control

mcp = FastMCP(name="docker-ops-safe-control-plane")

for tool_name in (
    "get_docker_engine_status", "list_containers", "inspect_container", "get_container_logs",
    "get_container_stats", "get_container_events", "validate_compose_config", "check_service_health", "inspect_network",
    "test_container_dns", "test_tcp_connection", "inspect_listening_ports", "restart_service",
):
    mcp.tool()(getattr(docker_tools, tool_name))

for tool_name in ("list_project_config_files", "read_compose_files", "read_dockerfile", "preview_project_config_patch", "apply_project_config_patch", "deploy_project_config"):
    mcp.tool()(getattr(config_control, tool_name))

RUNBOOKS = {
    "oom": "OOM: inspect OOMKilled/exit 137, compare usage and limit, inspect leak. Restart is temporary; verify health and memory trend.",
    "restart-loop": "Restart loop: inspect exit code, logs, healthcheck, dependency readiness and restart policy. Never remove volumes automatically.",
    "network": "Network: inspect shared networks, DNS service names and listening ports. Validate from the caller container. Containers talk to the container port, never the host-published port, and a server bound to 127.0.0.1 is unreachable from other containers.",
    "readiness": "Readiness: compare dependency health with depends_on service_healthy and healthchecks. Connection refused is not proof of a network mismatch. Propose a configuration diff for human review; do not apply it automatically.",
}
RUNBOOKS["oom-killed"] = RUNBOOKS["oom"]


@mcp.resource("docker://runbooks/{incident_type}")
def runbook(incident_type: str) -> str:
    return RUNBOOKS.get(incident_type, "Generic: collect engine, ps, inspect, logs, stats and compose config before changing state.")


@mcp.resource("docker://projects/{project}/compose")
def project_compose(project: str) -> str:
    """현재 고정된 Compose 프로젝트의 설정 파일(비밀값 마스킹)."""
    return docker_tools.read_compose_text(project)


@mcp.prompt()
def docker_incident_prompt(symptom: str) -> str:
    """Docker 장애에 적용할 표준 증거 수집 및 안전 절차입니다."""
    return f"Symptom: {symptom}. Route first, plan least-privilege observations, collect independent evidence, replan only missing evidence, cite IDs, require approval for mutations, then verify health."


@mcp.prompt()
def diagnose_network(service_a: str, service_b: str) -> str:
    """두 서비스 사이의 네트워크 진단 절차(/diagnose-network)."""
    return (f"Diagnose connectivity from {service_a} to {service_b}. Required order: inspect_network (shared network), test_container_dns, "
            f"check_service_health of {service_b}, test_tcp_connection on the declared container port, then inspect_listening_ports of {service_b} "
            "if the port refuses on a shared network. Distinguish host-published ports from container ports. Cite evidence IDs. Propose configuration changes only for human review.")


@mcp.prompt()
def diagnose_oom(service: str) -> str:
    """특정 서비스의 OOM 특화 진단 절차(/diagnose-oom)."""
    return (f"Diagnose a possible OOM kill of {service}. Required order: inspect_container (State.OOMKilled, ExitCode), get_container_events (oom), "
            f"get_container_logs, get_container_stats only if {service} is still running, validate_compose_config for the memory limit. "
            "Exit 137 alone is not proof of OOM. Warn about a possible leak before suggesting a larger limit. A restart is temporary and needs human approval.")


if __name__ == "__main__":
    try: mcp.run(show_banner=False)
    except TypeError: mcp.run()
