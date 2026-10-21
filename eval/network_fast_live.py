"""Two read-only live API reproductions. Never approves changes or overwrites evidence."""
import json
from datetime import datetime, timezone
from pathlib import Path
import time
import httpx


with httpx.Client(base_url="http://127.0.0.1:8100", timeout=30) as client:
    health = client.get("/health").raise_for_status().json()
    before = client.get("/targets").raise_for_status().json()
    results = []
    for query in ("/diagnose-network postgres ai", "/diagnose-network postgres ai\\"):
        start = time.perf_counter()
        response = client.post("/chat", headers={"X-Docker-Target": health["instance_id"]}, json={"message": query, "thread_id": "network-fast-live"}).raise_for_status().json()
        metadata = response["metadata"]
        nodes = [e["node"] for e in response["events"] if e["type"] == "NodeStarted"]
        result = {"query": query, "elapsed_ms": round((time.perf_counter()-start)*1000, 1), "response": response,
                  "checks": {"nodes": nodes == ["route", "network_diagnose"], "no_model": metadata["llm_usage"]["model_calls"] == 0,
                             "one_tool": metadata["tool_calls"] == 1, "absent": metadata["subject_status"] == "absent",
                             "validated": metadata["validation_ok"] is True, "no_approval": response.get("approval") is None}}
        results.append(result)
        print(json.dumps({"query": query, "elapsed_ms": result["elapsed_ms"], "checks": result["checks"], "nodes": nodes, "metadata": metadata}, ensure_ascii=False), flush=True)
    after = client.get("/targets").raise_for_status().json()
    snapshot = lambda value: sorted((c["id"], c["state"]) for c in value.get("containers", []))
    record = {"at": datetime.now(timezone.utc).isoformat(), "health": health, "results": results, "containers_unchanged": snapshot(before) == snapshot(after)}
    path = Path(__file__).parent / "results" / "network_fast_live_20261021.json"
    with path.open("x") as out: json.dump(record, out, ensure_ascii=False, indent=2)
    assert record["containers_unchanged"] and all(all(row["checks"].values()) for row in results)
