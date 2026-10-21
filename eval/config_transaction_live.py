"""Opt-in real Docker smoke test. Creates ONLY a unique, volume-free test project.

Uses a pre-existing local image; never pulls, modifies or starts bizxray.
Run: .venv/bin/python -m eval.config_transaction_live
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from backend import docker_tools
from backend.approvals import sign_approval
from backend.config_control import ConfigControl
from backend.project_files import sha


def main():
    base = "docker-ops-example-web:latest"
    subprocess.run(["docker", "image", "inspect", base], check=True, capture_output=True)
    project = "docker-ops-config-check-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="docker-ops-config-check-") as temp:
        root = Path(temp).resolve()
        source = root / "project"
        source.mkdir()
        dockerfile = f'FROM {base}\nLABEL transaction_version="one"\nCMD ["python3", "-m", "http.server", "8080"]\n'
        (source / "Dockerfile").write_text(dockerfile)
        (source / "compose.yaml").write_text('services:\n  probe:\n    build: .\n    healthcheck:\n      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen(\'http://127.0.0.1:8080\')"]\n      interval: 1s\n      timeout: 1s\n      retries: 2\n')
        os.environ.update(COMPOSE_PROJECT_NAME=project, COMPOSE_FILE=str(source / "compose.yaml"),
                          MCP_APPROVAL_SECRET=uuid.uuid4().hex, RECOVERY_WAIT_SECONDS="10")
        docker_tools.MODE, docker_tools.PROJECT, docker_tools.ALLOWED = "live", source, {"probe"}
        control = ConfigControl(docker_tools, root / "backups")
        def token(tool, plan):
            return sign_approval(os.environ["MCP_APPROVAL_SECRET"], uuid.uuid4().hex, tool, {"plan": plan}, time.time() + 300)
        def apply(find, replacement):
            plan = control.preview([{"path": "Dockerfile", "base_hash": sha(control.files.raw("Dockerfile")), "find": find, "replace": replacement}], ["probe"])["plan"]
            before = docker_tools._inspect("probe")["Id"]
            result = control.apply(plan, token("apply_project_config_patch", plan))
            assert result["ok"], result
            assert docker_tools._inspect("probe")["Id"] == before, "file approval recreated a container!"
            return result["runtime_plan"]
        report = {"project": project, "base_image": base, "bizxray_mutated": False}
        try:
            result = docker_tools._run(["docker", "compose", "up", "-d", "--build"], timeout=180)
            assert result["ok"], result
            assert control.verify(["probe"])["ok"], "baseline did not become healthy"
            runtime = apply('transaction_version="one"', 'transaction_version="two"')
            result = control.deploy(runtime, token("deploy_project_config", runtime))
            assert result["ok"], result
            report["deploy_success"] = result
            runtime = apply('CMD ["python3", "-m", "http.server", "8080"]', 'CMD ["python3", "-c", "raise SystemExit(1)"]')
            result = control.deploy(runtime, token("deploy_project_config", runtime))
            assert not result["ok"] and result["rollback"]["ok"], result
            assert '"http.server"' in (source / "Dockerfile").read_text()
            report["health_failure_and_rollback"] = result
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            # Exact isolated Compose project; no -v, prune or user-project cleanup.
            cleanup = docker_tools._run(["docker", "compose", "down", "--remove-orphans"], timeout=60)
            if not cleanup["ok"]: raise RuntimeError("test project cleanup failed: " + json.dumps(cleanup))


if __name__ == "__main__": main()
