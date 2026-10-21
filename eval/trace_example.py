"""Record real API SSE node input/output. No fixture selection, mutation or approval calls."""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--query", default="도커 컨테이너에 저장된 내용이 모두 사라졌어요")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/node_trace_example.json")
    args = parser.parse_args()
    received = []
    started = time.perf_counter()
    with httpx.Client(base_url=args.base_url, timeout=300) as client:
        health = client.get("/health"); health.raise_for_status()
        with client.stream("GET", "/chat/stream", params={"message": args.query, "thread_id": "trace-example-" + uuid.uuid4().hex}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                received.append({"received_ms": round((time.perf_counter() - started) * 1000, 3), "event": event})
                if event["type"] in {"NodeStarted", "NodeCompleted", "NodeFailed", "Error"}:
                    print(json.dumps({k: v for k, v in event.items() if k not in {"input", "output"}}, ensure_ascii=False), flush=True)
    final = next((row["event"] for row in received if row["event"]["type"] == "FinalAnswer"), None)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "query": args.query, "health": health.json(),
              "duration_ms": round((time.perf_counter() - started) * 1000, 3), "note": "Actual SSE arrival order and redacted node snapshots. No approvals submitted. Snapshots over 24000 chars are explicitly marked truncated.", "received": received, "final": final}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if final is None:
        raise SystemExit("No FinalAnswer; see saved failure trace")
    meta = final["metadata"]
    print(json.dumps({"route": meta["route"], "route_method": meta["route_method"], "validation_ok": meta["validation_ok"], "tool_calls": meta["tool_calls"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
