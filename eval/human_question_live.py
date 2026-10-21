"""Real local API/Ollama/MCP clarification round trip. Read-only; no approval endpoint."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx


QUERY = "서비스 상태를 보고 싶은데 어느 서비스인지 아직 고르지 않았습니다. 임의로 다른 서비스를 조회하지 말고 제가 고른 서비스 하나만 상태를 확인해주세요."


async def main():
    report = {"at": datetime.now(timezone.utc).isoformat(), "cases": [], "mutations": 0}
    target = Path(__file__).resolve().parent / "results" / "human_question_live_20261021.json"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8100", timeout=240) as client:
        for name, reply, expected in [("numbered", {"choice": 2}, "postgres"), ("free-text-fourth-service", {"answer": "frontend 상태만 알려주세요"}, "frontend")]:
            thread = "human-live-" + name + "-20261021"
            question_response = await client.post("/chat", json={"message": QUERY, "thread_id": thread})
            question_response.raise_for_status()
            asked = question_response.json()
            record = {"name": name, "asked": asked, "reply_request": reply}
            report["cases"].append(record)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            question = asked.get("clarification")
            assert question and asked["metadata"]["tool_calls"] == 0 and asked["approval"] is None, "must ask before using default target"
            pending = (await client.get("/clarifications/pending", params={"thread_id": thread})).json()
            assert pending["clarification"]["id"] == question["id"]
            if "choice" in reply: assert question["options"][reply["choice"]-1] == expected
            started = time.monotonic()
            events = []
            async with client.stream("POST", f"/clarifications/{question['id']}/reply?stream=true", json={"thread_id": thread, **reply}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line == "data: [DONE]": break
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        events.append({"received_ms": round((time.monotonic()-started)*1000), "event": event})
            record["stream"] = events
            final = next(item["event"] for item in events if item["event"]["type"] == "FinalAnswer")
            actual_targets = [e.get("args", {}).get("service") for e in final["evidence"] if e.get("args", {}).get("service")]
            record["passed"] = final["metadata"]["route"] == "status" and not final.get("clarification") and actual_targets and all(e["tool"] == "check_service_health" and e["args"] == {"service": expected} for e in final["evidence"]) and final["metadata"]["mutation_calls"] == 0 and final["approval"] is None
            record["replay_status"] = (await client.post(f"/clarifications/{question['id']}/reply", json={"thread_id": thread, **reply})).status_code
            record["passed"] = bool(record["passed"] and record["replay_status"] == 409)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({"case": name, "passed": record["passed"], "targets": actual_targets, "route": final["metadata"]["route"], "nodes": [x["event"]["node"] for x in events if x["event"]["type"] == "NodeStarted"]}, ensure_ascii=False), flush=True)
    report["passed"] = sum(c["passed"] for c in report["cases"])
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": report["passed"], "total": len(report["cases"]), "report": str(target)}), flush=True)


if __name__ == "__main__": asyncio.run(main())
