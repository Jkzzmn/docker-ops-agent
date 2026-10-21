"""Small, sequential live regression set; no mutations or approval execution."""
import asyncio
import argparse
import json
from pathlib import Path
import time
import httpx

CASES = [
    ("status", "ai 상태만 알려주세요", "status"),
    ("chat", "안녕하세요", "chat"),
    ("education", "컨테이너를 삭제하고 다시 만들었더니 업로드 사진이 사라졌어요. 보존 원리를 설명해주세요.", "education"),
    ("scope", "bizxray 말고 buttercast의 api 상태를 확인해주세요", "target_selection"),
    ("question", "서비스 하나가 안 됩니다. 이름은 아직 정하지 않았어요. 그 서비스만 점검해주세요.", "clarify"),
    ("network", "/diagnose-network postgres ai", "incident"),
    ("configuration", "현재 프로젝트의 Compose와 Dockerfile을 실제로 읽고 오류나 비효율을 검토해주세요. 변경은 하지 마세요.", "configuration"),
]


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--run", required=True); args = parser.parse_args()
    path = Path(__file__).parent / "results" / f"optimization_{args.run}.jsonl"
    with path.open("x") as out:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8100", timeout=90) as client:
            health = (await client.get("/health")).json()
            for name, query, route in CASES:
                tick = time.perf_counter()
                response = await client.post("/chat", headers={"X-Docker-Target": health["instance_id"]}, json={"message":query,"thread_id":f"opt-{args.run}-{name}"})
                body = response.json(); meta = body.get("metadata", {})
                checks = {"http": response.is_success, "route":meta.get("route")==route, "no_mutation":meta.get("mutation_calls")==0,
                          "budget": bool(meta.get("execution_budget")) and meta["execution_budget"]["stop_reason"] is None,
                          "tracing_off": meta.get("langsmith_run_id") is None}
                if name in {"scope", "education", "question", "chat"}: checks["no_tools"] = meta.get("tool_calls")==0
                if name == "network": checks.update(one_tool=meta.get("tool_calls")==1, no_model=meta["llm_usage"]["model_calls"]==0)
                if name == "configuration": checks["claims"] = (meta.get("config_metrics") or {}).get("claims_supported",0)>0
                row = {"case":name,"query":query,"elapsed_ms":round((time.perf_counter()-tick)*1000,1),"checks":checks,"passed":all(checks.values()),"response":body}
                out.write(json.dumps(row,ensure_ascii=False)+"\n"); out.flush()
                print(json.dumps({"case":name,"elapsed_ms":row["elapsed_ms"],"passed":row["passed"],"route":meta.get("route"),"model_calls":meta.get("llm_usage",{}).get("model_calls"),"router_escalations":meta.get("router_escalations"),"checks":checks},ensure_ascii=False),flush=True)


if __name__ == "__main__": asyncio.run(main())
