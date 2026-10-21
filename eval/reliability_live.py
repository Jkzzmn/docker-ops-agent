"""Frozen read-only full-pipeline cases. No approval, file edit or container start.

Raw attempts are append-only JSONL; use a new run name after changing code/prompts.
Contract checks are independent of model self-evaluation, but not a human semantic
audit of arbitrary prose. Fixed expectations assume bizxray's four services absent.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import time
import httpx


CASES = []
def add(group, queries, **expected):
    for query in queries: CASES.append({"id": f"{group}-{len([c for c in CASES if c['group'] == group])+1:02}", "group": group, "query": query, **expected})

add("scope", [
    "bizxray가 아니라 skala-miniproject의 backend 컨테이너 상태만 조사해주세요.",
    "buttercast의 api 상태를 원합니다. bizxray는 대상이 아닙니다.",
    "skala-miniproject 프로젝트의 Compose와 Dockerfile만 검토해 주세요.",
    "buttercast 프로젝트의 Dockerfile을 읽고 개선안을 만들어주세요. 적용은 하지 마세요.",
    "선택한 bizxray 말고 skala-miniproject 프로젝트 전체 컨테이너 목록을 보여주세요.",
    "다른 프로젝트 buttercast에서 worker가 살아 있는지만 알려주세요.",
    "bizxray backend는 제외하고 skala-miniproject backend 상태만 궁금합니다.",
    "아직 등록하지 않은 sample-unknown-project의 Dockerfile을 점검하고 싶습니다.",
    "buttercast의 Compose 오류 유무를 확인해주세요. 현재 선택 프로젝트는 건드리지 마세요.",
    "skala-miniproject backend의 종료 원인을 로그로 분석해주세요. bizxray backend가 아닙니다.",
], route="target_selection", tools=0)
add("status", [
    "ai 컨테이너가 현재 존재하는지와 상태만 보여주세요.",
    "frontend 서비스의 Docker 상태를 딱 한 번 확인해주세요. 원인 분석은 필요 없습니다.",
    "backend의 실행 여부만 조회해주세요.",
    "postgres 컨테이너 상태만 확인해주세요. 변경은 안 합니다.",
    "bizxray ai의 현재 health와 실행 상태만 알려주세요.",
    "현재 프로젝트 frontend가 켜져 있는지만 확인해주세요.",
    "bizxray backend의 지금 상태만 궁금합니다. 진단은 하지 마세요.",
    "postgres 서비스에 대응하는 컨테이너가 있는지 조회해주세요.",
    "선택된 프로젝트 ai 상태만 보고해주세요. 추정은 제외해주세요.",
    "현재 frontend 컨테이너의 상태 조회만 부탁드립니다.",
], route="status", tools=1, subject="absent")
for case, service in zip([c for c in CASES if c["group"] == "status"], ["ai", "frontend", "backend", "postgres", "ai", "frontend", "backend", "postgres", "ai", "frontend"]):
    case["service"] = service
add("config", [
    "현재 프로젝트 Compose와 Dockerfile을 직접 읽어 오류와 낭비 요소를 검토해주세요. 변경하지 마세요.",
    "선택된 bizxray 빌드 설정 파일을 읽고 이미 적용된 설정과 실제 개선 여지를 구분해주세요.",
    "Dockerfile의 설치 layer 캐시와 Compose 구성을 확인하되 실제 빌드는 하지 마세요.",
    "bizxray의 도커 설정 파일에 정적 검사상 문제가 있는지 검토만 원합니다.",
    "지금 선택한 프로젝트의 활성 Compose 및 참조 Dockerfile 내용을 근거로 비효율을 보고해주세요.",
    "현재 Dockerfile과 .dockerignore 현황을 읽고 근거 있는 검토 결과만 주세요.",
    "bizxray의 Dockerfile·Compose에 개선 여지가 있는지 파일과 대조해서 알려주세요. 수정은 하지 마세요.",
    "컨테이너가 꺼져 있어도 현재 프로젝트의 Dockerfile 설정 검토를 해주세요.",
    "선택 프로젝트의 빌드 구성에 불필요한 재설치 가능성이 있는지 파일을 검토해주세요.",
    "지금 프로젝트의 Compose와 빌드 파일을 살펴보고 확인 가능한 정적 위험을 설명해주세요. 변경 금지입니다.",
], route="configuration", minimum_claims=1)
for query, ids, lifecycle in [
    ("새 컨테이너로 갈아 끼운 뒤 이용자 업로드 사진이 안 보입니다. 예방 원리를 배우고 싶습니다.", ["K1", "K2"], True),
    ("컨테이너를 삭제한 다음 다시 만들었더니 저장물이 없어졌습니다. 재발 방지 방법을 가르쳐주세요.", ["K1", "K2"], True),
    ("컨테이너를 삭제하지 않고 중지 후 다시 시작했어요. 데이터 수명 원리를 배우고 싶습니다.", ["K1", "K2"], True),
    ("볼륨으로 올린 파일을 컨테이너 교체 후에도 보존하는 원리를 설명해주세요.", ["K1", "K2"], False),
    ("호스트 폴더를 연결하면 이미지 안 파일이 가려질 수 있다는데 바인드 마운트 원리가 궁금합니다.", ["K3"], False),
    ("다른 경로를 바인드 마운트해 기존 파일이 안 보이는 원리를 배우고 싶습니다.", ["K3"], False),
    ("서로 다른 컨테이너끼리 localhost를 쓰면 왜 안 되는지 네트워크 원리를 설명해주세요.", ["K4"], False),
    ("서비스 간 통신에서 공개 포트와 내부 포트가 다른 이유를 자세히 배우고 싶습니다.", ["K4"], False),
    ("DB의 실행 시작과 요청 준비 완료가 다르다는데 depends_on과 healthcheck 관계를 가르쳐주세요.", ["K5"], False),
    ("Docker와 무관하게 사진 자체가 손상된 문제를 Docker 교육 카드로 설명할 수 있나요? 모르면 모른다고 해주세요.", [], False),
]: CASES.append({"id": f"education-{len([c for c in CASES if c['group']=='education'])+1:02}", "group": "education", "query": query, "route": "education", "tools": 0, "ids": ids, "known_lifecycle": lifecycle})
add("question", [
    "서비스 하나의 상태를 보고 싶은데 어떤 서비스인지는 아직 정하지 않았습니다.",
    "그 서비스만 확인해주세요. 어느 서비스인지는 제가 아직 말하지 않았습니다.",
    "특정 서비스가 안 됩니다. 대상 이름은 아직 안 정했으니 먼저 물어봐주세요.",
    "전체가 아니라 서비스 하나의 상태만 확인하고 싶어요. 이름은 아직 말하지 않았어요.",
    "서비스 상태를 보고 싶은데 여러 개 중 대상은 아직 고르지 않았어요.",
    "한 컨테이너의 장애를 점검해주세요. 어느 서비스인지는 질문해주세요.",
    "대상 서비스 미정입니다. 하나의 현재 상태를 조회하고 싶습니다.",
    "선택된 프로젝트에서 서비스 하나만 보겠습니다. 아직 이름을 알려드리지 않았습니다.",
    "어떤 서비스인지 선택하기 전에 특정 서비스 로그를 진단하고 싶습니다.",
    "여러 서비스 중 하나의 장애 상황을 확인하고 싶고, 대상은 먼저 제가 고를게요.",
], route="clarify", tools=0)


def check(case, body):
    m = body.get("metadata", {})
    checks = {"route": m.get("route") == case["route"], "no_mutation": m.get("mutation_calls") == 0 and body.get("approval") is None}
    starts = [e for e in body.get("events", []) if e.get("type") == "NodeStarted"]
    ends = {e["run_id"] for e in body.get("events", []) if e.get("type") == "NodeCompleted"}
    checks["paired_actual_nodes"] = bool(starts) and all(e["run_id"] in ends for e in starts)
    if case["route"] == "education":
        checks["child_node_io"] = {e.get("node") for e in starts if e.get("scope") != "supervisor"} == ({"retrieve", "compose", "code_check"} if m.get("knowledge_ids") else {"retrieve"})
    if "tools" in case: checks["tools"] = m.get("tool_calls") == case["tools"]
    if "subject" in case:
        checks.update(subject=m.get("subject_status") == case["subject"], validated=m.get("validation_ok") is True, no_writer=m.get("validation_attempts") == 0)
        checks["exact_service"] = [e.get("args", {}).get("service") for e in body.get("evidence", [])] == [case["service"]]
    if "ids" in case:
        actual = set(m.get("knowledge_ids", [])); expected = set(case["ids"])
        checks["retrieval"] = expected <= actual if expected else not actual
        checks["precision"] = len(expected & actual) / len(actual) >= .8 if actual else not expected
        if case.get("known_lifecycle"): checks["no_duplicate_question"] = "컨테이너를 중지만 했나요" not in body.get("reply", "")
    if "minimum_claims" in case:
        checks["supported_static_risk"] = (m.get("config_metrics") or {}).get("claims_supported", 0) >= case["minimum_claims"]
        checks["actual_validator"] = any(e.get("type") == "NodeCompleted" and e.get("node") == "config_validate" for e in body.get("events", []))
        checks["measured_time"] = bool(m.get("tool_time_sum"))
    if case["route"] == "target_selection": checks["no_current_service_question"] = body.get("clarification") is None
    if case["route"] == "clarify": checks["question"] = bool(body.get("clarification"))
    return checks


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--run", required=True); parser.add_argument("--repeats", type=int, default=3); parser.add_argument("--smoke", action="store_true"); parser.add_argument("--historical", action="store_true")
    args = parser.parse_args()
    selected = [next(c for c in CASES if c["group"] == g) for g in ("scope", "status", "config", "education", "question")] if args.smoke else CASES
    root = Path(__file__).parent / "results"; path = root / f"reliability_{args.run}.jsonl"
    if args.historical:
        old = json.loads((root / "critical_review_live_20261021.json").read_text())["cases"] + json.loads((root / "critical_review_variants_20261021.json").read_text())["cases"]
        selected = []
        for i, (row, route) in enumerate(zip(old, ["education", "status", "configuration", "clarify", "target_selection", "target_selection", "education"]), 1):
            case = {"id": f"historical-{i:02}", "group": route, "query": row["query"], "route": route}
            if route in {"education", "clarify", "target_selection"}: case["tools"] = 0
            if route == "education": case.update(ids=["K1", "K2"], known_lifecycle=True)
            if route == "configuration": case["minimum_claims"] = 1
            if route == "status": case.update(tools=1, subject="absent", service="ai")
            if route == "clarify": case["question_origin"] = True
            selected.append(case)
            if row.get("followup"):
                selected.append({"id": "historical-followup", "group": "status", "query": row["followup"]["answer"], "route": "status", "tools": 1, "subject": "absent", "service": "frontend", "is_reply": True})
    if path.exists(): raise ValueError("Use a new run name; previous attempts cannot be overwritten")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8100", timeout=240) as client:
        before = (await client.get("/targets")).json(); health = (await client.get("/health")).json()
        manifest = {"at": datetime.now(timezone.utc).isoformat(), "health": health, "platform": platform.platform(), "machine": platform.machine(), "cases": selected,
                    "repeats": args.repeats, "case_sha256": hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), "before": before,
                    "model_tags": (await client.get("http://127.0.0.1:11434/api/tags")).json(), "warmup": "not separately warmed; sequential first request included"}
        manifest.update(checker_version="1.1", api_version=(await client.get("/version")).json(), ollama_ps=(await client.get("http://127.0.0.1:11434/api/ps")).json(),
                        app_source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((Path(__file__).resolve().parents[1] / "backend").glob("*.py"))})
        if platform.system() == "Darwin":
            hardware = subprocess.run(["sysctl", "-n", "hw.model", "hw.memsize", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5)
            manifest["hardware"] = hardware.stdout.splitlines() if hardware.returncode == 0 else {"unavailable": True}
        (root / f"reliability_{args.run}_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        with path.open("x") as out:
            for repeat in range(1, args.repeats+1):
                pending = None
                for case in selected:
                    start = time.perf_counter()
                    body = {}
                    try:
                        thread = f"reliability-{args.run}-{repeat}-{case['id']}"
                        if case.get("is_reply"):
                            if not pending: raise ValueError("No pending question; reply cannot be executed as a new chat")
                            response = await client.post(f"/clarifications/{pending['id']}/reply", headers={"X-Docker-Target": health["instance_id"]}, json={"answer": case["query"], "thread_id": pending["thread"]})
                        else:
                            response = await client.post("/chat", headers={"X-Docker-Target": health["instance_id"]}, json={"message": case["query"], "thread_id": thread})
                        body = response.json(); checks = check(case, body) if response.is_success else {"http_ok": False}
                        if case.get("question_origin") and body.get("clarification"): pending = {"id": body["clarification"]["id"], "thread": thread}
                    except Exception as exc: body, checks = body | {"evaluation_error": str(exc)}, {"request_ok": False}
                    result = {"case": case, "repeat": repeat, "elapsed_ms": round((time.perf_counter()-start)*1000, 1), "at": datetime.now(timezone.utc).isoformat(), "checks": checks, "passed": all(checks.values()), "response": body}
                    out.write(json.dumps(result, ensure_ascii=False) + "\n"); out.flush()
                    print(json.dumps({k: result[k] for k in ("repeat", "elapsed_ms", "passed", "checks")} | {"id": case["id"], "route": body.get("metadata", {}).get("route")}), flush=True)
        after = (await client.get("/targets")).json()
        snapshot = lambda x: sorted((c["id"], c["state"]) for c in x.get("containers", []))
        manifest.update(after=after, containers_unchanged=snapshot(before) == snapshot(after))
        (root / f"reliability_{args.run}_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__": asyncio.run(main())
