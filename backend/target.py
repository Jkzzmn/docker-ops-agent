"""Resolve this backend host's Docker context/Compose project before starting MCP."""
import json
import os
import subprocess
from pathlib import Path

from .config import ROOT
from .sanitize import redact


class TargetSelectionRequired(RuntimeError):
    def __init__(self, env, target):
        super().__init__("Compose 프로젝트가 여러 개입니다. COMPOSE_PROJECT_NAME 또는 DOCKER_PROJECT_DIR를 지정하세요.")
        self.env, self.target = env, target


def resolve_target(env: dict[str, str], run=subprocess.run) -> tuple[dict[str, str], dict]:
    resolved = dict(env)
    if env.get("DOCKER_MODE", "live") == "fixture":
        return resolved, {"mode": "fixture", "source": "test-only", "project": env.get("COMPOSE_PROJECT_NAME", "fixture"), "context": "fixture"}

    def command(args, cwd=None):
        result = run(["docker", *args], env=resolved, cwd=cwd, text=True, capture_output=True, timeout=20, check=False)
        if result.returncode:
            raise RuntimeError(redact(result.stderr) or "Docker daemon/Compose 연결을 확인하세요.")
        return result.stdout.strip()

    context = command(["context", "show"])
    if not env.get("DOCKER_HOST") and not env.get("DOCKER_CONTEXT"):
        resolved["DOCKER_CONTEXT"] = context  # Pin it; later global context changes cannot redirect pending actions.
    engine = json.loads(command(["info", "--format", "{{json .}} "]))
    explicit = env.get("DOCKER_PROJECT_DIR") or env.get("COMPOSE_FILE")
    source = "explicit" if explicit else "auto-compose"
    if explicit:
        project_dir = Path(env.get("DOCKER_PROJECT_DIR") or Path(env["COMPOSE_FILE"].split(os.pathsep)[0]).parent).expanduser().resolve()
    else:
        projects = json.loads(command(["compose", "ls", "--all", "--format", "json"]))
        name = env.get("COMPOSE_PROJECT_NAME")
        candidates = [p for p in projects if not name or p["Name"] == name]
        if name and not candidates:
            raise RuntimeError(f"Compose 프로젝트 {name!r}를 찾지 못했습니다. DOCKER_PROJECT_DIR를 지정하세요.")
        if len(candidates) > 1:
            raise TargetSelectionRequired(resolved, {"mode": "live", "source": "selection-required", "context": context,
                "engine_id": engine.get("ID"), "engine_name": engine.get("Name"), "project": None, "project_dir": None,
                "services": [], "warning": "진단할 Compose 프로젝트를 선택해주세요."})
        if candidates:
            chosen = candidates[0]
            files = [Path(p.strip()).expanduser().resolve() for p in chosen.get("ConfigFiles", "").split(",") if p.strip()]
            if not files or not all(p.is_file() for p in files):
                raise RuntimeError("Docker 대상의 Compose 파일을 이 서버에서 읽을 수 없습니다. 로컬 사본의 DOCKER_PROJECT_DIR/COMPOSE_FILE을 지정하세요.")
            project_dir = files[0].parent
            resolved["COMPOSE_FILE"] = os.pathsep.join(str(p) for p in files)
            resolved["COMPOSE_PROJECT_NAME"] = chosen["Name"]
        else:
            project_dir, source = ROOT / "demo_stack", "demo-not-running"
    if not project_dir.is_dir():
        raise RuntimeError("DOCKER_PROJECT_DIR가 존재하지 않습니다.")
    resolved["DOCKER_PROJECT_DIR"] = str(project_dir)
    services = command(["compose", "config", "--services"], cwd=project_dir).splitlines()
    allowed = [s.strip() for s in env.get("DOCKER_ALLOWED_SERVICES", "").split(",") if s.strip()] or services
    if not allowed or not set(allowed) <= set(services):
        raise RuntimeError("DOCKER_ALLOWED_SERVICES는 선택한 Compose 프로젝트의 서비스여야 합니다.")
    resolved["DOCKER_ALLOWED_SERVICES"] = ",".join(allowed)
    return resolved, {"mode": "live", "source": source, "context": context, "endpoint_override": bool(env.get("DOCKER_HOST")),
                      "engine_id": engine.get("ID"), "engine_name": engine.get("Name"), "server_version": engine.get("ServerVersion"),
                      "project": resolved.get("COMPOSE_PROJECT_NAME", project_dir.name), "project_dir": str(project_dir), "services": allowed,
                      "warning": "진단 대상 Compose 컨테이너가 없습니다. 교육은 사용 가능하며 실제 장애 진단에는 대상 스택이 필요합니다." if source == "demo-not-running" else None}
