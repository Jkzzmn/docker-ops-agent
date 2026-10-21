"""Actual Ollama gating checks: clear requests bypass humans, indispensable gaps ask once."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from dotenv import load_dotenv
from backend.config import Settings
from backend.llm import ModelGateway
from backend.routing import classify_request
from backend import usage


async def main():
    load_dotenv()
    model = ModelGateway(Settings())
    cases = [
        ("clear-review", "현재 도커 컴포즈 파일과 도커파일들을 확인해서 오류가 있는지, 혹은 비효율적인 부분이 있는지 확인해주세요", "configuration", []),
        ("clear-service", "ai 상태만 알려주세요", "status", []),
        ("clear-whole-project", "현재 프로젝트의 전체 서비스 목록과 상태만 보여주세요", "status", []),
        ("education", "볼륨과 바인드 마운트 차이를 설명해주세요", "education", []),
        ("greeting", "안녕하세요", "chat", []),
        ("resolved-followup", "그럼 그걸 고쳐주세요", "change", [{"role": "user", "content": "현재 Dockerfile과 Compose 오류를 검토해주세요"}]),
        ("missing-service", "서비스가 안 돼요. 다른 건 보지 말고 그 서비스 하나만 진단해주세요.", "clarify", []),
        ("missing-status-target", "서비스 상태를 보고 싶은데 어느 서비스인지 아직 고르지 않았습니다. 임의로 다른 서비스를 조회하지 말고 제가 고른 서비스 하나만 상태를 확인해주세요.", "clarify", []),
        ("missing-referent", "그거 처리해주세요", "clarify", []),
    ]
    results = []
    for name, query, expected, history in cases:
        usage.begin(True)
        result = await classify_request(model, query, history, allowed_services=("ai", "postgres", "backend", "frontend"))
        row = {"case": name, "query": query, "expected": expected, "result": result, "passed": result["route"] == expected,
               "model_outputs": [c.get("response") for c in usage.CURRENT_USAGE.get()["calls"]]}
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    report = {"at": datetime.now(timezone.utc).isoformat(), "model": model.fast, "passed": sum(r["passed"] for r in results),
              "total": len(results), "results": results, "docker_calls": 0, "mutations": 0}
    target = Path(__file__).resolve().parent / "results" / "clarification_gating_live_20261021.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": report["passed"], "total": report["total"], "report": str(target)}), flush=True)


if __name__ == "__main__": asyncio.run(main())
