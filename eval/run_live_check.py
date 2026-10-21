"""Read-only MCP/API smoke check against an explicitly named demo project."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", required=True, choices=["healthy", "memory", "readiness", "network", "unknown"])
    parser.add_argument("--query", default="api 컨테이너의 현재 장애 상태를 점검해주세요")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.getenv("DOCKER_MODE") != "live" or not os.getenv("DOCKER_PROJECT_DIR") or not os.getenv("COMPOSE_PROJECT_NAME"):
        parser.error("DOCKER_MODE=live, DOCKER_PROJECT_DIR, and COMPOSE_PROJECT_NAME must explicitly identify the isolated demo")
    from fastapi.testclient import TestClient
    from backend.server import app
    with tempfile.TemporaryDirectory(prefix="docker-ops-live-") as tmp:
        with patch.dict(os.environ, {"SESSION_DB": str(Path(tmp) / "sessions.db"), "LANGSMITH_TRACING": "false"}), TestClient(app) as client:
            response = client.post("/chat", json={"message": args.query, "thread_id": "live-smoke"})
            response.raise_for_status()
            body = response.json()
            # This command never calls approval endpoints or changes containers.
            body["checked_at"] = datetime.now(timezone.utc).isoformat()
            body["expected_cause"] = args.expected
            body["scope"] = {key: os.environ.get(key) for key in ("DOCKER_PROJECT_DIR", "COMPOSE_PROJECT_NAME", "COMPOSE_FILE")}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            print(json.dumps({"diagnosis": body["diagnosis"], "metadata": body["metadata"]}, ensure_ascii=False, indent=2))
            if body["metadata"]["root_cause"] != args.expected:
                raise SystemExit("observed cause does not match expected scenario; see saved evidence")


if __name__ == "__main__":
    main()
