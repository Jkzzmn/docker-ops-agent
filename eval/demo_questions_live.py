"""The user's ten demo questions. Real model/API, no mutation approvals."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx


QUESTIONS = [
    "현재 프로젝트에서 실행 중인 컨테이너들의 전반적인 구동 상태와 CPU/메모리 점검해줘.",
    "현재 docker-compose.yml 설정에서 리소스 제한(Limit)이나 보안 관점에서 누락된 부분이 있어?",
    "현재 backend 컨테이너에 사용된 도커 이미지의 세부 정보(용량, 베이스 이미지 등)를 확인해줘.",
    "지금 설정된 Dockerfile에서 빌드 캐시를 방해하거나 이미지 크기 비효율을 야기하는 부분은 어디야?",
    "Backend 컨테이너가 계속 재시작(Restart Loop)되고 있는데, 종료 코드(Exit Code)와 에러 로그를 보고 원인을 알려줘.",
    "API 컨테이너가 트래픽을 받다가 갑자기 죽었어. OOM(Out of Memory)으로 종료된 건지 당시 메모리 상태와 함께 분석해줘.",
    "Backend에서 DB 컨테이너로 통신이 안 돼. 두 컨테이너가 같은 브릿지 네트워크에 있는지, DNS 해석은 정상인지 점검해줘.",
    "DB 컨테이너는 running 상태인데 Backend에서 연결을 거부당했어. 시작 순서(depends_on)나 DB Readiness(Health Check) 문제인지 분석해줘.",
    "그럼 네가 분석한 대로 DB 서비스에 Health Check 조건을 추가하고 Backend를 재시작해줄래?",
    "현재 8000번 포트 충돌을 일으키는 좀비 컨테이너를 찾아서 종료하고, 데모 서비스를 정상 궤도로 띄워줘.",
]


def check_reply(number, body, services):
    """Content/scope assertions, not HTTP-200 or model self-grading. Not an incident reproduction score."""
    meta, reply = body["metadata"], body["reply"]
    tools = {e["tool"] for e in body.get("evidence", [])}
    absent = {3: {"backend"}, 5: {"backend"}, 7: {"backend", "db"}, 8: {"backend", "db"}, 9: {"backend", "db"}}.get(number, set()) - {s.casefold() for s in services}
    if absent:
        return {"scope": "target clarification, NOT demonstrated fault recovery", "passed": meta["route"] in {"clarify", "target_selection"} and meta["tool_calls"] == 0 and not body.get("approval"), "checks": ["unknown named target must not be replaced by another service"]}
    checks = [not body.get("approval"), meta["route"] != "unavailable"]
    if number == 1: checks += [meta["route"] == "inventory", "inspect_project_configuration" in tools, "사용량" in reply, "현재" in reply]
    if number == 2: checks += [meta["route"] == "configuration", "CPU 제한=" in reply, "메모리 제한=" in reply, "보안 선언" in reply]
    if number == 4: checks += [meta["route"] == "configuration", "Dockerfile" in reply, "[E" in reply, "측정" in reply]
    if number == 6: checks += [meta["route"] == "incident", "OOMKilled=" in reply, "ExitCode=" in reply, "로그" in reply, "당시 메모리" in reply]
    if number == 10: checks += ["inspect_port_owners" in tools, "8000" in reply, "아직 중지" in reply]
    return {"scope": "request/answer contract; no destructive actions", "passed": all(checks), "checks": checks}


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--cases", default="1,2,3,4,5,6,7,8,9,10"); args = parser.parse_args()
    run = "demo-questions-" + uuid.uuid4().hex
    failures = []
    path = Path(__file__).with_name("results") / (run + ".jsonl"); path.parent.mkdir(exist_ok=True)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8100", timeout=95) as client:
        health = (await client.get("/health")).json()
        headers = {"X-Docker-Target": health["instance_id"]}
        print(json.dumps({"target": health["docker_target"], "record": str(path)}, ensure_ascii=False), flush=True)
        with path.open("x") as output:
            for number in map(int, args.cases.split(",")):
                started = time.monotonic()
                # Q9 deliberately follows Q8 in the same conversation.
                thread = run + ("-readiness" if number in {8, 9} else f"-{number}")
                response = await client.post("/chat", headers=headers, json={"message": QUESTIONS[number-1], "thread_id": thread})
                response.raise_for_status(); body = response.json(); meta = body["metadata"]
                cleanup = {}
                if body.get("approval"):
                    result = await client.post("/actions/" + body["approval"]["id"] + "/reject", headers=headers)
                    result.raise_for_status(); cleanup["approval"] = result.json()
                if body.get("clarification"):
                    result = await client.post("/clarifications/" + body["clarification"]["id"] + "/cancel", headers=headers, json={"thread_id": thread})
                    result.raise_for_status(); cleanup["clarification"] = result.json()
                row = {"case": number, "query": QUESTIONS[number-1], "seconds": round(time.monotonic()-started, 2), "target": health["docker_target"], "response": body, "cleanup": cleanup,
                       "evaluation": check_reply(number, body, health["docker_target"]["services"])}
                if not row["evaluation"]["passed"]: failures.append(number)
                output.write(json.dumps(row, ensure_ascii=False)+"\n"); output.flush()
                print(json.dumps({"case": number, "seconds": row["seconds"], "evaluation": row["evaluation"], "route": meta["route"], "status": meta["answer_status"], "tools": meta["tool_calls"], "models": meta["llm_usage"]["model_calls"], "budget": meta["execution_budget"]["stop_reason"], "reply": body["reply"][:1800]}, ensure_ascii=False), flush=True)
    print(path, flush=True)
    if failures: raise SystemExit("Request/answer contract failures: " + ",".join(map(str, failures)))


if __name__ == "__main__": asyncio.run(main())
