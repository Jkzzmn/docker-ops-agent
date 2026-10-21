"""Deterministic, never-applied configuration proposals for causes that have no safe automatic action.

The output is review text for a human. Nothing here touches a file or calls a mutation tool.
"""
from __future__ import annotations

from .diagnostics import succeeded

HEALTHCHECKS = {
    5432: 'pg_isready -U "$${POSTGRES_USER:-postgres}" -h 127.0.0.1',
    3306: "mysqladmin ping -h 127.0.0.1",
    6379: "redis-cli ping",
    27017: "mongosh --quiet --eval 'db.runCommand({ping:1})'",
}


def _find(evidence: list[dict], tool: str, **args) -> dict | None:
    return next((e for e in reversed(evidence) if succeeded(e) and e["tool"] == tool and all(e.get("args", {}).get(k) == v for k, v in args.items())), None)


def build_proposal(diagnosis: dict, evidence: list[dict], source: str) -> dict | None:
    cause = diagnosis.get("cause")
    config = _find(evidence, "validate_compose_config")
    cfg = config["observation"] if config else {}
    if cause == "readiness":
        deps = cfg.get("depends_on", {}).get(source, {})
        peer = next(iter(deps), None)
        if not peer:
            return None
        port = (cfg.get("ports", {}).get(peer) or [None])[0]
        command = HEALTHCHECKS.get(port, "# 서비스 준비 상태를 확인하는 명령으로 교체하세요")
        yaml = (f"services:\n  {peer}:\n    healthcheck:\n      test: [\"CMD-SHELL\", \"{command}\"]\n      interval: 5s\n      timeout: 3s\n      retries: 10\n      start_period: 10s\n"
                f"  {source}:\n    depends_on:\n      {peer}:\n        condition: service_healthy\n")
        return {"kind": "compose-override", "title": f"{peer} healthcheck와 {source}의 service_healthy 조건", "applied": False,
                "evidence_ids": diagnosis.get("evidence_ids", []), "yaml": yaml,
                "note": "자동으로 적용하지 않습니다. compose.override.yaml 등에 병합하고 `docker compose up -d`로 다시 기동한 뒤 healthy 전이를 확인하세요. 이미지에 맞는 healthcheck 명령인지 먼저 확인하세요."}
    if cause == "network":
        reason = diagnosis.get("reason", "")
        net = _find(evidence, "inspect_network", source=source)
        if net and not net["observation"].get("shared_networks"):
            peer, observed = net["args"]["target"], net["observation"]
            target_networks = observed.get("target_networks", [])
            if target_networks:
                short = lambda name: name.split("_", 1)[-1]  # strip the Compose project prefix
                joined = list(dict.fromkeys([*map(short, observed.get("source_networks", [])), short(target_networks[0])]))
                yaml = f"services:\n  {source}:\n    networks: [{', '.join(joined)}]\n  {peer}:\n    networks:\n      {short(target_networks[0])}:\n        aliases: [{peer}]\n"
                return {"kind": "compose-override", "title": f"{source}와 {peer}가 같은 network를 공유하도록 연결", "applied": False,
                        "evidence_ids": diagnosis.get("evidence_ids", []), "yaml": yaml,
                        "note": "자동으로 적용하지 않습니다. Compose의 network 키 이름과 맞는지 확인하세요(실제 이름에는 프로젝트 접두사가 붙습니다)."}
        if "호스트 게시 포트" in reason:
            return {"kind": "connection-setting", "title": "컨테이너 사이 접속 포트를 컨테이너 포트로 변경", "applied": False,
                    "evidence_ids": diagnosis.get("evidence_ids", []), "yaml": None, "note": reason}
        if "loopback" in reason:
            return {"kind": "service-setting", "title": "서비스를 0.0.0.0에 바인딩", "applied": False,
                    "evidence_ids": diagnosis.get("evidence_ids", []), "yaml": None,
                    "note": reason + " 서버 설정의 listen/bind 주소를 127.0.0.1에서 0.0.0.0으로 바꾸고 외부 공개가 필요하면 `ports`의 호스트 IP로만 제한하세요."}
    return None
