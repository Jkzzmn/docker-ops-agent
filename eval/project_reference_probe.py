"""Real-model schema regression, classifier only; no MCP/Docker tools exist here."""
import asyncio
import argparse
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv
from backend.config import Settings
from backend.llm import ModelGateway
from backend.routing import classify_request
from backend import usage


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--run", default="01"); parser.add_argument("--files-only", action="store_true"); args = parser.parse_args()
    load_dotenv()
    path = Path(__file__).parent / "results" / f"project_reference_probe_{args.run}.json"
    if path.exists(): raise ValueError("Do not overwrite a previous probe")
    async with httpx.AsyncClient() as client:
        health = (await client.get("http://127.0.0.1:8100/health")).json()
        catalog = (await client.get("http://127.0.0.1:8100/targets")).json()
    target = health["docker_target"] | {"known_projects": [p["name"] for p in catalog["projects"]]}
    model = ModelGateway(Settings())
    rows = []
    cases = [
        ("ai 컨테이너가 현재 존재하는지와 상태만 보여주세요.", "status"),
        ("선택된 프로젝트 ai 상태만 보고해주세요. 추정은 제외해주세요.", "status"),
        ("ai 프로젝트의 frontend 상태만 보여주세요. 현재 선택 프로젝트는 조회하지 마세요.", "target_selection"),
        ("buttercast의 api 상태를 원합니다. bizxray는 대상이 아닙니다.", "target_selection"),
        ("지금 프로젝트의 Compose와 빌드 파일을 살펴보고 확인 가능한 정적 위험을 설명해주세요. 변경 금지입니다.", "configuration"),
    ]
    if args.files_only:
        cases = [cases[-1], ("새 인스턴스 교체 후 사진이 안 보입니다. 예방 원리를 배우고 싶습니다.", "education"),
                 ("Compose에서 준비 상태를 기다리는 원리를 예시로 가르쳐주세요. 실제 파일 조회는 원하지 않습니다.", "education")]
    for query, expected in cases:
        ledger = usage.begin(True)
        result = await classify_request(model, query, [], allowed_services=tuple(target["services"]), selected_target=target)
        rows.append({"query": query, "expected": expected, "result": result, "passed": result["route"] == expected, "calls": ledger["calls"]})
        path.write_text(json.dumps({"kind": "classifier-only; no MCP/Docker tools; not a latency benchmark", "health": health, "rows": rows}, ensure_ascii=False, indent=2))
        print(json.dumps({"query": query, "expected": expected, "route": result["route"], "passed": rows[-1]["passed"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__": asyncio.run(main())
