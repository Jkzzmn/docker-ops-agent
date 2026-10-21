"""Smoke-test graph decisions against real local Ollama inference and stdio MCP."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--quality-model", help="Use a different installed model for writing and independent validation")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/supervisor_ollama_smoke.json")
    parser.add_argument("--education-only", action="store_true", help="Check teaching retrieval, mount explanation and no-knowledge abstention")
    args = parser.parse_args()
    from fastapi.testclient import TestClient
    from backend.server import app
    cases = [("concept", "Docker가 무엇인가요?", "chat"),
             ("status", "api 상태만 알려주세요", "status"),
             ("incident", "api 컨테이너가 계속 재시작됩니다. 관측 근거로 원인을 진단해주세요", "incident")]
    if args.education_only:
        cases = [("education-data-loss", "도커 컨테이너에 저장된 내용이 모두 사라졌어요", "education"),
                 ("education-mounts", "볼륨과 바인드 마운트의 차이를 설명해주세요", "education"),
                 ("education-unknown", "Docker 양자 스토리지 예제로 가르쳐주세요", "education")]
    results = []
    with tempfile.TemporaryDirectory(prefix="docker-ops-model-smoke-") as tmp:
        env = {"LLM_BACKEND": "ollama", "OLLAMA_FAST_MODEL": args.model, "OLLAMA_QUALITY_MODEL": args.quality_model or args.model,
               "DOCKER_MODE": "fixture", "DOCKER_FIXTURE_SCENARIO": "oom", "DOCKER_ALLOWED_SERVICES": "api,db,web",
               "SESSION_DB": str(Path(tmp) / "sessions.db"), "LANGSMITH_TRACING": "false"}
        with patch.dict(os.environ, env), TestClient(app) as client:
            for name, query, expected_route in cases:
                print(f"running {name} with real Ollama {args.model}", flush=True)
                response = client.post("/chat", json={"message": query, "thread_id": name})
                response.raise_for_status()
                body = response.json()
                meta = body["metadata"]
                ok = meta["route"] == expected_route and bool(body["reply"])
                if name == "concept": ok = ok and meta["tool_calls"] == 0 and meta["agents_run"] == []
                if name == "status": ok = ok and meta["tool_calls"] == 1 and not meta["analysis_used"] and body["approval"] is None and meta["validation_ok"]
                if name == "incident": ok = ok and meta["root_cause"] == "memory" and {"runtime", "configuration", "writer", "validator"} <= set(meta["agents_run"]) and meta["validation_ok"]
                if name.startswith("education"):
                    ok = ok and meta["tool_calls"] == 0 and body["approval"] is None and not body["evidence"] and not body["diagnosis"]
                    if name == "education-unknown":
                        ok = ok and meta["knowledge_count"] == 0 and meta["agents_run"] == ["knowledge"]
                    else:
                        ok = ok and meta["validation_ok"] and meta["knowledge_count"] > 0 and "[K" in body["reply"] and "https://docs.docker.com/" in body["reply"]
                results.append({"case": name, "ok": bool(ok), "response": body})
                print(json.dumps({"case": name, "ok": bool(ok), "metadata": meta}, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"model": args.model, "quality_model": args.quality_model or args.model, "llm_backend": "ollama", "docker_mode": "fixture", "note": "Real local inference; deterministic Docker observations. No mutation approved. Automated smoke pass is not a semantic accuracy score.", "cases": results}, ensure_ascii=False, indent=2))
    if not all(row["ok"] for row in results):
        raise SystemExit("model smoke assertions failed; inspect saved responses")


if __name__ == "__main__":
    main()
