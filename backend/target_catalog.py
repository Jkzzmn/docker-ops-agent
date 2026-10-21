"""Read-only inventory and explicit project selection on the backend's pinned engine."""
import json
import os
from pathlib import Path
import subprocess

from .project_files import CONFIG_NAME
from .sanitize import redact
from .target import resolve_target


SELECTION_VARS = {"DOCKER_PROJECT_DIR", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "DOCKER_ALLOWED_SERVICES", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES", "COMPOSE_PATH_SEPARATOR"}


def docker_json(env: dict, args: list[str], cwd=None):
    result = subprocess.run(["docker", *args], env=env, cwd=cwd, text=True, capture_output=True, timeout=20, check=False)
    if result.returncode: raise ValueError(redact(result.stderr) or "Docker 조회에 실패했습니다.")
    output = result.stdout.strip()
    if not output: return []
    try: return json.loads(output)
    except json.JSONDecodeError: return [json.loads(line) for line in output.splitlines() if line.strip()]


def inventory(env: dict) -> dict:
    if env.get("DOCKER_MODE", "live") != "live": raise ValueError("실제 Docker 모드에서만 대상을 선택할 수 있습니다.")
    projects = docker_json(env, ["compose", "ls", "--all", "--format", "json"])
    containers = docker_json(env, ["ps", "--all", "--format", "{{json .}}"])
    if isinstance(containers, dict): containers = [containers]
    rows = []
    for item in containers[:500]:
        labels = dict(pair.split("=", 1) for pair in item.get("Labels", "").split(",") if "=" in pair)
        rows.append({"id": item.get("ID"), "name": item.get("Names"), "state": item.get("State"), "status": item.get("Status"),
                     "project": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service")})
    return {"projects": [{"name": p["Name"], "status": p.get("Status", ""), "compose_files": p.get("ConfigFiles", "").split(",")} for p in projects[:100]],
            "containers": rows, "truncated": len(projects) > 100 or len(containers) > 500,
            "note": "서버가 연결한 Docker 엔진입니다. 중지된 컨테이너도 포함하며 선택·새로고침으로 컨테이너를 시작하지 않습니다."}


def resolve_selection(base_env: dict, *, project_name: str | None = None, project_dir: str | None = None):
    if bool(project_name) == bool(project_dir): raise ValueError("프로젝트 이름 또는 경로 하나를 선택해주세요.")
    if base_env.get("DOCKER_MODE", "live") != "live": raise ValueError("실제 Docker 모드가 필요합니다.")
    env = {k: v for k, v in base_env.items() if k not in SELECTION_VARS}
    if project_name:
        env["COMPOSE_PROJECT_NAME"] = project_name
    else:
        path = Path(project_dir).expanduser()
        if not path.is_absolute() or path.resolve() == Path(path.anchor) or not path.is_dir():
            raise ValueError("서버에 존재하는 Compose 프로젝트의 절대 폴더 경로를 입력해주세요.")
        env["DOCKER_PROJECT_DIR"] = str(path.resolve())
    runtime, target = resolve_target(env)
    root = Path(target["project_dir"])
    if runtime.get("COMPOSE_FILE"):
        files = runtime["COMPOSE_FILE"].split(os.pathsep)
    else:
        base = next((root / n for n in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml") if (root / n).is_file()), None)
        if base is None: raise ValueError("이 경로에 Compose 파일이 없습니다.")
        override = next((root / n for n in ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml") if (root / n).is_file()), None)
        files = [str(base)] + ([str(override)] if override else [])
    for file in files:
        path = Path(file).resolve()
        if Path(file).is_symlink() or not path.is_relative_to(root) or not CONFIG_NAME.fullmatch(path.name) or not path.is_file():
            raise ValueError("활성 Compose 파일은 프로젝트 폴더 안의 지원되는 설정 파일이어야 합니다.")
    runtime["COMPOSE_FILE"] = os.pathsep.join(files)
    raw = docker_json(runtime, ["compose", "config", "--format", "json"], cwd=root)
    name = raw.get("name")
    if not isinstance(name, str) or not name: raise ValueError("Compose 프로젝트 이름을 확인하지 못했습니다.")
    runtime["COMPOSE_PROJECT_NAME"] = name
    target.update(project=name, compose_files=files)
    target["build_sources"] = []
    for service, config in raw.get("services", {}).items():
        build = config.get("build")
        if not isinstance(build, dict): continue
        context = Path(build.get("context", "."))
        context = context if context.is_absolute() else root / context
        dockerfile = (context / build.get("dockerfile", "Dockerfile")).resolve()
        target["build_sources"].append({"service": service, "dockerfile": str(dockerfile), "readable": dockerfile.is_relative_to(root) and dockerfile.is_file() and bool(CONFIG_NAME.fullmatch(dockerfile.name))})
    return runtime, target
