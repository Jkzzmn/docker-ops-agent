"""Actual LLM routing matrix and live read-only response checks. Never approves changes."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx
from backend.config import Settings
from backend.llm import ModelGateway
from backend.routing import classify_request


CASES = [
    ("현재 도커 볼륨 마운트는 어떻게 되어있나요?", "inventory", "mounts"),
    ("현재 도커 이미지 사용하고 있는거 이름은 뭔가요?", "inventory", "images"),
    ("api에 연결된 호스트 경로와 컨테이너 경로를 보여주세요", "inventory", "mounts"),
    ("어떤 이미지 태그로 만든 컨테이너인지 알려줘", "inventory", "images"),
    ("현재 외부에 노출한 포트 번호는?", "inventory", "ports"),
    ("worker는 어느 네트워크에 붙어 있어?", "inventory", "networks"),
    ("메모리 제한을 얼마로 걸어뒀어?", "inventory", "resources"),
    ("재시작 정책이 어떻게 설정되어 있나요?", "inventory", "restart"),
    ("api healthcheck 간격은 몇 초로 설정되어 있나요?", "inventory", "healthcheck"),
    ("Compose에 선언된 볼륨 연결만 보여주세요", "inventory", "mounts"),
    ("Compose의 depends_on 대상이 누구야?", "inventory", "dependencies"),
    ("현재 프로젝트의 빌드 컨텍스트 경로는?", "inventory", "build"),
    ("이미지 이름과 마운트 경로를 같이 알려줘", "inventory", "images"),
    ("볼륨과 바인드 마운트의 차이를 설명해주세요", "education", None),
    ("Docker 이미지란 무엇인가요?", "chat", None),
    ("도커 컨테이너에 저장된 내용이 모두 사라졌어요", "education", None),
    ("현재 프로젝트 Dockerfile과 Compose의 오류·비효율을 검토해주세요", "configuration", None),
    ("마운트 설정에 잘못된 부분이 있는지 파일을 검토해줘", "configuration", None),
    ("api 상태만 알려주세요", "status", None),
    ("api에서 worker 연결이 왜 안 되는지 진단해줘", "incident", None),
    ("도커 띄워줘", "runtime", None),
    ("api 중지해줘", "runtime", None),
    ("모든 볼륨 삭제해줘", "blocked", None),
    ("buttercast 말고 bizxray 프로젝트 이미지 이름 알려줘", "target_selection", None),
]


async def main():
    parser = argparse.ArgumentParser(); modes = parser.add_mutually_exclusive_group(); modes.add_argument("--routing-only", action="store_true"); modes.add_argument("--app-only", action="store_true"); args = parser.parse_args()
    run = "inventory-" + uuid.uuid4().hex
    path = Path(__file__).with_name("results") / (run + ".jsonl"); path.parent.mkdir(exist_ok=True)
    failed = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8100", timeout=90) as client:
        health = (await client.get("/health")).json()
        target = health["docker_target"]
        assert target["project"] == "buttercast", "This reproducible matrix expects the reported buttercast scope."
        headers = {"X-Docker-Target": health["instance_id"]}
        llm = ModelGateway(Settings())
        with path.open("x") as out:
            for query, expected, topic in ([] if args.app_only else CASES):
                started = time.monotonic()
                decision = await classify_request(llm, query, [], allowed_services=("api", "worker"), selected_target=target | {"known_projects": ["buttercast", "bizxray"]})
                passed = decision["route"] == expected and (not topic or topic in decision.get("inventory_topics", []))
                if expected == "inventory":
                    if query in {c[0] for c in CASES[:9]}: passed = passed and decision.get("inventory_source") in {"runtime", "both"}
                    if query in {c[0] for c in CASES[9:12]}: passed = passed and decision.get("inventory_source") in {"compose", "both"}
                row = {"phase": "routing", "query": query, "expected": expected, "seconds": round(time.monotonic()-started, 2), "passed": passed, "decision": decision}
                out.write(json.dumps(row, ensure_ascii=False) + "\n"); out.flush()
                print(json.dumps({k:v for k,v in row.items() if k != "decision"} | {"actual": decision["route"]}, ensure_ascii=False), flush=True)
                if not passed: failed.append(query)
            if not args.routing_only:
                # Same thread intentionally follows the old file-review topic, testing stale-topic bleed.
                for query, expected, topic in [CASES[i] for i in (16, 0, 1, 4, 9, 10, 11, 18, 23, 13, 15)]:
                    started = time.monotonic()
                    response = await client.post("/chat", headers=headers, json={"message": query, "thread_id": run})
                    response.raise_for_status(); body = response.json(); meta = body["metadata"]
                    checks = {"route": meta["route"] == expected, "no_mutation": meta["mutation_calls"] == 0, "no_approval": not body.get("approval"), "bounded": not meta["execution_budget"]["stop_reason"]}
                    if expected == "inventory":
                        checks.update(actual_observation=any(e.get("target") == "inspect_project_configuration" for e in body["events"]), topic=topic in meta.get("inventory_topics", []),
                                      no_review_template="확정할 수 있는 비효율" not in body["reply"] and "Compose에 healthcheck가 없는 서비스" not in body["reply"],
                                      answer=meta["answer_status"] == "validated", one_tool=meta["tool_calls"] == 1)
                        if query in {c[0] for c in CASES[:9]}:
                            observations = [e["observation"] for e in body["evidence"] if e["tool"] == "inspect_project_configuration"]
                            checks["runtime_not_declaration_only"] = bool(observations) and observations[0].get("runtime", {}).get("ok") is True
                    if expected == "education":
                        checks["lesson_coverage"] = set(meta["knowledge_ids"]) == ({"K1", "K2", "K3"} if query == CASES[13][0] else {"K1", "K2"})
                    row = {"phase": "app", "query": query, "seconds": round(time.monotonic()-started, 2), "checks": checks, "response": body}
                    out.write(json.dumps(row, ensure_ascii=False) + "\n"); out.flush()
                    print(json.dumps({k:v for k,v in row.items() if k != "response"} | {"models":meta["llm_usage"]["model_calls"], "tools":meta["tool_calls"]}, ensure_ascii=False), flush=True)
                    if not all(checks.values()): failed.append(query)
    print(str(path), flush=True)
    assert not failed, failed


if __name__ == "__main__": asyncio.run(main())
