"""Small real-model persistence check, split around an actual dev-server restart."""
import argparse
import json
import sqlite3
import time
import uuid
from pathlib import Path
import httpx
from dotenv import load_dotenv
from backend.config import Settings

load_dotenv()
p = argparse.ArgumentParser(); p.add_argument("phase", choices=("before", "after")); p.add_argument("--run", default=""); args = p.parse_args()
root = Path(__file__).parent / "results"
before_path = root / f"memory_sqlite_before_20261021{('_' + args.run) if args.run else ''}.json"
record = {"thread_id": "memory-live-" + uuid.uuid4().hex, "results": []} if args.phase == "before" else json.loads(before_path.read_text())
questions = ["현재 프로젝트의 ai 서비스 상태만 확인해주세요."] if args.phase == "before" else ["아까 제가 점검해달라고 한 서비스 이름이 무엇이었나요? Docker를 새로 조회하지 말고 이름만 답해주세요.", "그 서비스의 현재 상태만 다시 확인해주세요."]
with httpx.Client(base_url="http://127.0.0.1:8100", timeout=100) as client:
    health = client.get("/health").raise_for_status().json()
    assert health["checkpoint_enabled"] and health["checkpoint_backend"] == "sqlite"
    if args.phase == "after": assert health["instance_id"] != record["before_instance_id"]
    for question in questions:
        start = time.perf_counter()
        body = client.post("/chat", json={"message": question, "thread_id": record["thread_id"]}).raise_for_status().json()
        row = {"question": question, "elapsed_ms": round((time.perf_counter()-start)*1000, 1), "response": body}
        record["results"].append(row)
        print(json.dumps({"elapsed_ms": row["elapsed_ms"], "reply": body["reply"], "route": body["metadata"]["route"], "memory": body["metadata"]["conversation_memory"]}, ensure_ascii=False), flush=True)
    db = Settings().session_db.with_suffix(".checkpoints.sqlite")
    with sqlite3.connect(db) as conn:
        threads = conn.execute("SELECT thread_id, COUNT(*) FROM checkpoints WHERE thread_id LIKE ? GROUP BY thread_id", ("%:" + record["thread_id"],)).fetchall()
    assert len(threads) == 1 and threads[0][1] > 0
    if args.phase == "before": record.update(before_instance_id=health["instance_id"], before_checkpoint_thread=threads[0][0], before_checkpoint_count=threads[0][1])
    else:
        final = record["results"][-1]["response"]
        recall = record["results"][-2]["response"]
        record["checks"] = {"same_persisted_thread": threads[0][0] == record["before_checkpoint_thread"], "new_checkpoints": threads[0][1] > record["before_checkpoint_count"],
                            "recall_without_tools": "ai" in recall["reply"].lower() and recall["metadata"]["tool_calls"] == 0,
                            "followup_service": [e["args"].get("service") for e in final["evidence"]] == ["ai"], "fresh_evidence_only": len(final["evidence"]) == 1}
        print(json.dumps(record["checks"], ensure_ascii=False), flush=True)
    path = before_path if args.phase == "before" else root / f"memory_sqlite_after_20261021{('_' + args.run) if args.run else ''}.json"
    with path.open("x") as out: json.dump(record, out, ensure_ascii=False, indent=2)
    if args.phase == "after": assert all(record["checks"].values())
