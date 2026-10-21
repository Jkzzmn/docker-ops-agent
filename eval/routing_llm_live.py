"""Actual local Ollama intent regression; no Docker tools, doubles or configuration mutations."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from dotenv import load_dotenv
from backend import usage
from backend.config import Settings
from backend.knowledge import lessons, teaching_guide
from backend.llm import ModelGateway
from backend.routing import classify_request


QUERY = "현재 도커 컴포즈 파일과 도커파일들을 확인해서 오류가 있는지, 혹은 비효율적인 부분이 있는지 확인해주세요"


async def main():
    load_dotenv()
    model = ModelGateway(Settings())
    education = [{"role": "user", "content": "도커 컨테이너에 저장된 내용이 모두 사라졌어요"},
                 {"role": "assistant", "content": teaching_guide(lessons()[:2]) * 10}]
    cases = [(f"reported-{context}-{n}", QUERY, "configuration", history) for context, history in (("new", []), ("after-long-education", education)) for n in range(3)]
    cases += [
        ("config-paraphrase", "지금 프로젝트의 컴포즈와 도커 파일을 직접 읽고 낭비되는 부분과 개선점을 알려주세요", "configuration", education),
        ("config-build-cache", "실제 Dockerfile 빌드 캐시와 이미지 크기에서 비효율적인 부분을 검토해주세요", "configuration", []),
        ("config-not-lesson", "볼륨 개념 설명은 나중에 하고, 지금 Compose 파일들을 읽고 문제를 검토해주세요", "configuration", education),
        ("chat", "안녕하세요", "chat", []),
        ("definition", "Dockerfile이 뭔가요?", "chat", []),
        ("education", "볼륨과 바인드 마운트 차이를 설명해주세요", "education", []),
        ("lost-data", "도커 컨테이너에 저장된 내용이 모두 사라졌어요", "education", []),
        ("status", "ai 상태만 알려주세요", "status", []),
        ("incident", "현재 ai가 계속 재시작되는 원인을 실제로 진단해주세요", "incident", []),
        ("network", "현재 backend에서 postgres DNS 연결이 안 됩니다. 원인을 점검해주세요", "incident", []),
        ("change", "AI/.dockerignore를 추가하는 정확한 diff를 제안해주세요. 승인 전 파일이나 배포를 변경하지 마세요", "change", education),
        ("followup", "그럼 그걸 고쳐주세요", "change", [{"role": "user", "content": "현재 Dockerfile과 Compose를 읽고 오류를 검토해주세요"}]),
        ("blocked", "docker system prune 자동 실행해주세요", "blocked", []),
    ]
    results = []
    for name, query, expected, history in cases:
        usage.begin(True)
        result = await classify_request(model, query, history + [{"role": "user", "content": query}], allowed_services=("ai", "postgres", "backend", "frontend"))
        calls = usage.CURRENT_USAGE.get()["calls"]
        item = {"case": name, "query": query, "history_chars": sum(len(x["content"]) for x in history), "expected": expected,
                "actual": result, "passed": result["route"] == expected, "model_calls": len(calls),
                "llm": [{k: v for k, v in c.items() if k not in {"system", "prompt"}} for c in calls],
                "classifier_inputs": [json.loads(c["prompt"].split("Input=", 1)[1]) for c in calls]}
        results.append(item)
        print(json.dumps({k: item[k] for k in ("case", "expected", "actual", "passed")}, ensure_ascii=False), flush=True)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "model": model.fast, "backend": model.s.backend,
              "passed": sum(r["passed"] for r in results), "total": len(results), "docker_calls": 0, "mutations": 0,
              "note": "Local repeated sample, not a general accuracy guarantee. Safety-block cases do not call the model.", "results": results}
    path = Path(__file__).resolve().parent / "results" / "routing_llm_20261021.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": report["passed"], "total": report["total"], "report": str(path)}), flush=True)


if __name__ == "__main__": asyncio.run(main())
