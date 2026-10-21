"""Exact-diff configuration transactions. No shell strings or arbitrary file-write capability.

Only the MCP process uses private originals. The model sees redacted files and preview plans.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

import yaml

from .approvals import verify_approval
from .project_files import ProjectFiles, MAX_BYTES, sha
from .sanitize import redact


LOCK = threading.RLock()
USED: dict[str, float] = {}


def digest(value) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


class ConfigControl:
    def __init__(self, docker, backup_root: Path):
        self.d = docker
        self.files = ProjectFiles(docker.PROJECT)
        self.backup_root = backup_root

    def normalized(self) -> dict:
        if self.d.MODE != "live":
            raise ValueError("configuration transactions require a live Docker Engine")
        return self.d._json(["docker", "compose", "config", "--format", "json"])

    def sources(self, raw: dict) -> dict[str, set[str]]:
        """Only active Compose files and Dockerfiles/ignores actually referenced by local builds."""
        result = {p: set(self.d.ALLOWED) for p in self.files.active_compose()}
        for service, config in raw.get("services", {}).items():
            build = config.get("build")
            if not isinstance(build, dict): continue
            context = Path(build.get("context", "."))
            context = context if context.is_absolute() else self.files.root / context
            try:
                relative = context.relative_to(self.files.root)
                dockerfile = relative / build.get("dockerfile", "Dockerfile")
                for candidate in (dockerfile, relative / ".dockerignore"):
                    path = self.files.relative(candidate.as_posix())
                    result.setdefault(path, set()).add(service)
            except ValueError:
                continue  # remote/outside build sources are explicitly not readable or editable
        return result

    def scope(self) -> dict:
        raw = self.normalized()
        info = self.d._json(["docker", "info", "--format", "{{json .}}"])
        context = self.d._capture(["docker", "context", "show"])
        if context.returncode or not info.get("ID"):
            raise ValueError("cannot establish current Engine/context identity")
        hashes = {p: sha(content) if (content := self.files.raw(p)) is not None else None for p in sorted(self.sources(raw))}
        return {"engine_id": info["ID"], "context": context.stdout.strip(), "docker_host": os.getenv("DOCKER_HOST", ""),
                "project": raw.get("name", os.getenv("COMPOSE_PROJECT_NAME", "")), "project_dir": str(self.files.root),
                "compose_files": self.files.active_compose(), "file_hashes": hashes, "config_sha256": digest(raw),
                "allowed_services": sorted(self.d.ALLOWED)}

    def assert_scope(self, expected: dict) -> None:
        if self.scope() != expected:
            raise ValueError("Docker target/config/file hashes changed; request a fresh proposal and approval")

    def content(self, edits: list[dict], services: list[str]) -> tuple[dict, dict]:
        if not edits or len(edits) > 12 or not services or len(set(services)) != len(services):
            raise ValueError("1–12 exact edits and unique affected services are required")
        if set(services) - self.d.ALLOWED: raise ValueError("service outside allowlist")
        raw = self.normalized()
        sources = self.sources(raw)
        originals, updated = {}, {}
        for edit in edits:
            if set(edit) != {"path", "base_hash", "find", "replace"}:
                raise ValueError("edit schema: path, base_hash, find, replace")
            path = self.files.relative(edit["path"])
            if path not in sources or (path not in self.files.active_compose() and sources[path] - set(services)):
                raise ValueError("edit must belong only to the approved services and active build/Compose sources")
            if path not in originals:
                original = self.files.raw(path)
                originals[path] = original
                updated[path] = original.decode() if original is not None else ""
            original = originals[path]
            if edit["base_hash"] != (sha(original) if original is not None else None):
                raise ValueError("source hash mismatch")
            find, replacement = edit["find"], edit["replace"]
            if not isinstance(find, str) or not isinstance(replacement, str) or len(replacement.encode()) > MAX_BYTES:
                raise ValueError("invalid replacement")
            if any(redact(s, len(s)) != s or "[REDACTED]" in s for s in (find, replacement)):
                raise ValueError("masked/secret values cannot be edited; use a public exact snippet")
            if original is None:
                if Path(path).name != ".dockerignore" or find or updated[path]:
                    raise ValueError("only a missing .dockerignore can be created")
                updated[path] = replacement
            else:
                if not find or updated[path].count(find) != 1:
                    raise ValueError("find must match exactly once")
                updated[path] = updated[path].replace(find, replacement, 1)
            if len(updated[path].encode()) > MAX_BYTES: raise ValueError("updated file exceeds size limit")
        if len(updated) > 6: raise ValueError("at most six files per proposal")
        for path, text in updated.items():
            if text.encode() == originals[path]: raise ValueError("no-op edit")
            if path in self.files.active_compose():
                before = yaml.safe_load(originals[path]) or {}
                after = yaml.safe_load(text) or {}
                if not isinstance(before, dict) or not isinstance(after, dict): raise ValueError("Compose must be a mapping")
                # Global volumes/networks/extensions are not silently modified in a service-scoped proposal.
                if {k: v for k, v in before.items() if k != "services"} != {k: v for k, v in after.items() if k != "services"}:
                    raise ValueError("global Compose sections require manual review")
                a, b = before.get("services", {}), after.get("services", {})
                if set(a) != set(b) or any(a[k] != b[k] for k in a if k not in services):
                    raise ValueError("cannot add/remove services or change unapproved services")
                # This path does not provision new host access or change persistent-data mapping.
                sensitive = ("volumes", "devices", "privileged", "cap_add", "network_mode", "pid", "ipc", "env_file", "secrets")
                for service in services:
                    for key in sensitive:
                        if (a.get(service) or {}).get(key) != (b.get(service) or {}).get(key):
                            raise ValueError(f"{key} changes need manual review; no host/data access expansion")
                    if (a.get(service) or {}).get("build") != (b.get(service) or {}).get("build"):
                        raise ValueError("build context/Dockerfile selection changes require manual review")
        return originals, {p: text.encode() for p, text in updated.items()}

    def preview(self, edits: list[dict], services: list[str]) -> dict:
        originals, updated = self.content(edits, services)
        diff = "\n".join("".join(difflib.unified_diff((originals[p] or b"").decode().splitlines(True), updated[p].decode().splitlines(True),
                                    fromfile="a/" + p, tofile="b/" + p, n=0)) for p in updated)
        if redact(diff, len(diff)) != diff:
            raise ValueError("diff would expose secret material")
        plan = {"stage": "files", "scope": self.scope(), "edits": edits, "services": sorted(services), "diff": diff,
                "diff_sha256": sha(diff.encode()), "new_hashes": {p: sha(v) for p, v in updated.items()},
                "validation_commands": [["docker", "compose", "config", "--quiet"]],
                "runtime_commands": [["docker", "compose", "build", *sorted(services)],
                                     ["docker", "compose", "up", "-d", "--no-deps", "--no-build", "--force-recreate", *sorted(services)]],
                "risk": "1차 승인은 파일만 변경합니다. 2차 승인은 빌드 및 해당 서비스 재생성으로 중단을 유발하며 빌드 명령을 실행합니다. DB 데이터/마이그레이션은 되돌릴 수 없습니다.",
                "rollback": "파일 원본 백업 → 구문 실패 시 파일 복원. 배포 실패 시 원본 파일과 이전 이미지 및 실행/중지 상태 복원 후 health 재확인. 볼륨은 삭제하지 않습니다."}
        plan["plan_sha256"] = digest(plan)
        return {"ok": True, "plan": plan}

    def authorize(self, tool: str, plan: dict, token: str) -> None:
        payload = verify_approval(os.getenv("MCP_APPROVAL_SECRET", ""), token, tool, {"plan": plan})
        for action_id, expiry in list(USED.items()):
            if expiry <= time.time(): del USED[action_id]
        if payload["id"] in USED: raise PermissionError("approval already consumed")
        USED[payload["id"]] = payload["expires_at"]  # consume before checking drift; never retry a mutation
        self.assert_scope(plan["scope"])

    def backup(self, originals: dict, plan: dict) -> tuple[str, Path]:
        tx = uuid.uuid4().hex
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = self.backup_root / tx
        folder.mkdir(mode=0o700)
        for index, (path, content) in enumerate(originals.items()):
            if content is not None:
                target = folder / str(index)
                with target.open("xb") as stream: stream.write(content)
                target.chmod(0o600)
        manifest = {"plan": plan, "paths": list(originals), "original_hashes": {p: sha(v) if v is not None else None for p, v in originals.items()}}
        with (folder / "manifest.json").open("x") as stream: json.dump(manifest, stream)
        (folder / "manifest.json").chmod(0o600)
        return tx, folder

    def originals(self, transaction: str) -> tuple[dict, dict]:
        if not re.fullmatch(r"[a-f0-9]{32}", transaction): raise ValueError("invalid transaction")
        folder = self.backup_root / transaction
        manifest = json.loads((folder / "manifest.json").read_text())
        result = {}
        for index, path in enumerate(manifest["paths"]):
            content = (folder / str(index)).read_bytes() if manifest["original_hashes"][path] is not None else None
            if (sha(content) if content is not None else None) != manifest["original_hashes"][path]: raise ValueError("backup hash mismatch")
            result[path] = content
        return manifest, result

    def restore(self, originals: dict, expected: dict) -> dict:
        errors = []
        for path, content in originals.items():
            try:
                current = self.files.raw(path)
                if (sha(current) if current is not None else None) != expected[path]:
                    raise ValueError("file changed externally; refusing to overwrite")
                self.files.replace(path, content)
            except Exception as exc: errors.append({"path": path, "error": redact(str(exc))})
        return {"ok": not errors, "errors": errors}

    def apply(self, plan: dict, approval_token: str) -> dict:
        with LOCK:
            self.authorize("apply_project_config_patch", plan, approval_token)
            fresh = self.preview(plan["edits"], plan["services"])["plan"]
            if fresh != plan: raise ValueError("preview changed; obtain a new approval")
            originals, updated = self.content(plan["edits"], plan["services"])
            transaction, _ = self.backup(originals, plan)
            written = {}
            try:
                for path, content in updated.items():
                    if self.files.raw(path) != originals[path]: raise ValueError("source changed during write")
                    self.files.replace(path, content)
                    written[path] = sha(content)
                validation = self.d._run(["docker", "compose", "config", "--quiet"])
                if not validation["ok"]: raise ValueError("Compose validation failed: " + validation.get("stderr", ""))
                after_scope = self.scope()
                if any(after_scope[k] != plan["scope"][k] for k in after_scope if k not in {"file_hashes", "config_sha256"}):
                    raise ValueError("Docker target changed during validation")
                if any(after_scope["file_hashes"].get(p) != h for p, h in plan["new_hashes"].items()):
                    raise ValueError("files changed during validation")
                if any(after_scope["file_hashes"].get(p) != h for p, h in plan["scope"]["file_hashes"].items() if p not in updated):
                    raise ValueError("another source file changed during validation")
            except Exception as exc:
                rollback = self.restore({p: originals[p] for p in written}, written)
                return {"ok": False, "stage": "files", "transaction": transaction, "error": redact(str(exc)), "rollback": rollback}
            runtime = {"stage": "runtime", "transaction": transaction, "scope": after_scope, "services": plan["services"],
                       "diff": plan["diff"], "diff_sha256": plan["diff_sha256"], "commands": plan["runtime_commands"],
                       "risk": plan["risk"], "rollback": plan["rollback"]}
            try:
                runtime["before"] = {s: self.d._inspect(s) for s in plan["services"]}
                # Persist only required identity/state. NEVER send container Env to the API/model.
                runtime["before"] = {s: {"id": v["Id"], "image": v["Image"], "state": v["State"]["Status"],
                                           "health": v["State"].get("Health", {}).get("Status", "")} for s, v in runtime["before"].items()}
                runtime["rollback_images"] = {s: f"docker-ops-rollback-{transaction}:{s}" for s in plan["services"]}
                runtime["commands"] = [["docker", "image", "tag", previous["image"], runtime["rollback_images"][s]]
                                       for s, previous in runtime["before"].items()] + runtime["commands"]
            except Exception:
                return {"ok": True, "stage": "files", "transaction": transaction, "validation": validation,
                        "runtime_blocked": "기존 컨테이너/이미지의 정확한 복원 상태를 확보하지 못해 배포 승인을 생성하지 않았습니다."}
            return {"ok": True, "stage": "files", "transaction": transaction, "validation": validation, "runtime_plan": runtime}

    def state(self, services: list[str]) -> dict:
        return {s: self.d.inspect_container(s) for s in services}

    def verify(self, services: list[str]) -> dict:
        deadline = time.monotonic() + min(60, max(0, float(os.getenv("RECOVERY_WAIT_SECONDS", "20"))))
        while True:
            state = self.state(services)
            healthy = all(v.get("state") == "running" and v.get("health") == "healthy" and not v.get("oom_killed") for v in state.values())
            if healthy or time.monotonic() >= deadline: break
            time.sleep(1)
        return {"ok": healthy, "containers": state,
                "logs": {s: self.d.get_container_logs(s, 30) for s in services},
                "note": "Docker health 기준이며 업무 트랜잭션/데이터 무결성 검증을 대신하지 않습니다."}

    def deploy(self, plan: dict, approval_token: str) -> dict:
        with LOCK:
            self.authorize("deploy_project_config", plan, approval_token)
            manifest, originals = self.originals(plan["transaction"])
            if plan["services"] != manifest["plan"]["services"] or plan["diff_sha256"] != manifest["plan"]["diff_sha256"]:
                raise ValueError("transaction scope mismatch")
            # Ensure rollback identity has not drifted while the second approval was on screen.
            for service, previous in plan["before"].items():
                current = self.d._inspect(service)
                if (current["Id"], current["Image"], current["State"]["Status"]) != (previous["id"], previous["image"], previous["state"]):
                    raise ValueError("container changed since approval proposal")
                if not current.get("Config", {}).get("Healthcheck") or current["Config"]["Healthcheck"].get("Test", [""])[0] == "NONE":
                    return {"ok": False, "stage": "runtime", "mutation_started": False, "error": f"{service}: healthcheck 없는 서비스는 자동 배포/정상 판정을 지원하지 않습니다."}
            rollback_images = {s: f"docker-ops-rollback-{plan['transaction']}:{s}" for s in plan["services"]}
            if plan.get("rollback_images") != rollback_images: raise ValueError("unsupported rollback image tags")
            expected_commands = [["docker", "image", "tag", previous["image"], rollback_images[s]] for s, previous in plan["before"].items()]
            expected_commands += [["docker", "compose", "build", *plan["services"]],
                                 ["docker", "compose", "up", "-d", "--no-deps", "--no-build", "--force-recreate", *plan["services"]]]
            if plan["commands"] != expected_commands: raise ValueError("unsupported runtime commands")
            results, verification, started = [], None, False
            try:
                for command in expected_commands:
                    self.assert_scope(plan["scope"])
                    started = True
                    result = self.d._run(command, timeout=600)
                    results.append(result)
                    if not result["ok"]: raise ValueError("build/recreate failed")
                self.assert_scope(plan["scope"])
                verification = self.verify(plan["services"])
                config = self.normalized()
                verification["network_checks"] = []
                for source in plan["services"]:
                    for target in config.get("services", {}).get(source, {}).get("depends_on", {}):
                        if target not in self.d.ALLOWED: continue
                        network = self.d.inspect_network(source, target)
                        dns = self.d.test_container_dns(source, target)
                        ports = config["services"][target].get("expose", [])
                        ports += [p["target"] for p in config["services"][target].get("ports", []) if isinstance(p, dict)]
                        tcp = self.d.test_tcp_connection(source, target, int(str(ports[0]).split('/')[0])) if ports else None
                        verification["network_checks"].append({"source": source, "target": target, "network": network, "dns": dns, "tcp": tcp})
                        # Probe tools require python3. Unavailable is inconclusive, not a false success.
                        if not network.get("shared_networks") or not dns.get("resolved") or (tcp is not None and not tcp.get("connected")):
                            verification["ok"] = False
                if not verification["ok"]: raise ValueError("post-deploy health failed")
                return {"ok": True, "stage": "runtime", "transaction": plan["transaction"], "commands": results,
                        "verification": verification, "mutation_started": started}
            except Exception as exc:
                # Build/up timeout is ambiguous: inspect and restore, never blindly retry the command.
                rollback = self.restore(originals, manifest["plan"]["new_hashes"])
                if rollback["ok"]:
                    rollback = self.rollback_runtime(plan, rollback)
                return {"ok": False, "stage": "runtime", "transaction": plan["transaction"], "error": redact(str(exc)),
                        "commands": results, "verification": verification, "rollback": rollback, "mutation_started": started}

    def rollback_runtime(self, plan: dict, result: dict) -> dict:
        override = self.backup_root / plan["transaction"] / "rollback.compose.json"
        # Generated from inspected immutable image IDs, not caller-supplied YAML or command strings.
        override.write_text(json.dumps({"services": {s: {"image": plan["rollback_images"][s]} for s in plan["before"]}}))
        override.chmod(0o600)
        commands = []
        base = ["docker", "compose"]
        for path in plan["scope"]["compose_files"]: base.extend(["-f", str(self.files.root / path)])
        base.extend(["-f", str(override)])
        try:
            info = self.d._json(["docker", "info", "--format", "{{json .}}"])
            context = self.d._capture(["docker", "context", "show"])
            if info.get("ID") != plan["scope"]["engine_id"] or context.stdout.strip() != plan["scope"]["context"]:
                raise ValueError("Engine/context changed; refusing Docker rollback on a different target")
            validation = self.d._run(["docker", "compose", "config", "--quiet"])
            if not validation["ok"]: raise ValueError("restored Compose is invalid")
            for service, previous in plan["before"].items():
                if previous["state"] not in {"running", "exited", "created"}: raise ValueError("unsupported previous container state")
                command = base + ["up", "--no-deps", "--no-build", "--pull", "never", "--force-recreate"]
                command += ["-d"] if previous["state"] == "running" else ["--no-start"]
                run = self.d._run(command + [service], timeout=120)
                commands.append(run)
                if not run["ok"]: raise ValueError("old image recreation failed")
            running = [s for s, v in plan["before"].items() if v["state"] == "running"]
            verification = self.verify(running) if running else {"ok": True, "containers": self.state(plan["services"])}
            for service, previous in plan["before"].items():
                item = self.d._inspect(service)
                if item["Image"] != previous["image"] or (item["State"]["Status"] == "running") != (previous["state"] == "running"):
                    raise ValueError("rollback image/state verification failed")
            return result | {"ok": verification["ok"], "commands": commands, "verification": verification,
                             "note": "컨테이너 ID는 재생성으로 바뀝니다. DB 데이터/외부 부작용은 롤백하지 않습니다."}
        except Exception as exc:
            return result | {"ok": False, "commands": commands, "error": redact(str(exc)), "manual_intervention": True}


def controller() -> ConfigControl:
    from . import docker_tools
    return ConfigControl(docker_tools, docker_tools.ROOT / "data" / "config_backups")


def list_project_config_files() -> dict:
    """선택 프로젝트의 허용 설정 파일 목록·해시만 조회합니다. .env는 읽지 않습니다."""
    control = controller()
    return control.files.inventory()


def read_compose_files() -> dict:
    """실제 활성 base/override Compose 파일을 크기 제한·마스킹 후 반환합니다."""
    return controller().files.compose()


def read_dockerfile(path: str) -> dict:
    """프로젝트 내부 실제 build 참조 Dockerfile/.dockerignore만 읽습니다."""
    control = controller()
    path = control.files.relative(path)
    if path not in control.sources(control.normalized()): raise ValueError("not an active build config source")
    return control.files.read(path)


def preview_project_config_patch(edits: list[dict], services: list[str]) -> dict:
    """수정하지 않고 정확한 diff·hash·대상·고정 명령·위험·복원 계획을 생성합니다."""
    with LOCK: return controller().preview(edits, services)


def apply_project_config_patch(plan: dict, approval_token: str) -> dict:
    """1차 서명 승인: 백업, 정확한 파일 패치, Compose 검증. 컨테이너는 변경하지 않습니다."""
    return controller().apply(plan, approval_token)


def deploy_project_config(plan: dict, approval_token: str) -> dict:
    """2차 서명 승인: 고정 build/up, health 검증, 실패 시 파일·이전 이미지·상태 롤백."""
    return controller().deploy(plan, approval_token)
