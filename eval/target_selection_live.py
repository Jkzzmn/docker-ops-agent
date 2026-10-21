"""Verify project selection and real MCP file collection; never call mutation approvals."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx


async def main():
    result = {"at": datetime.now(timezone.utc).isoformat(), "mutations": 0}
    file = Path(__file__).resolve().parent / "results" / "target_selection_live_20261021.json"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8100", timeout=240) as client:
        async def request(method, url, **kwargs):
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        before = result["before"] = await request("GET", "/targets")
        original_dir = before["current"]["project_dir"]
        preview = result["preview"] = await request("POST", "/targets/preview", json={"project_name": "buttercast"})
        try:
            result["selected"] = await request("POST", "/targets/select", json={"project_name": "buttercast", "expected_instance_id": preview["instance_id"]})
            result["stale_request_status"] = (await client.post("/chat", headers={"X-Docker-Target": before["instance_id"]}, json={"message": "안녕하세요", "thread_id": "target-switch-live"})).status_code
            file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            result["file_review"] = await request("POST", "/chat", json={"message": "현재 프로젝트의 Dockerfile과 Compose 실제 파일을 읽고 검토만 해주세요. 파일 변경은 하지 마세요.", "thread_id": "target-switch-live"})
        finally:
            current = await request("GET", "/health")
            result["manual_preview"] = await request("POST", "/targets/preview", json={"project_dir": original_dir})
            result["restored"] = await request("POST", "/targets/select", json={"project_dir": original_dir, "expected_instance_id": current["instance_id"]})
            result["after"] = await request("GET", "/targets")
            file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        evidence = result["file_review"]["evidence"]
        compose = next(e["observation"] for e in evidence if e["tool"] == "read_compose_files")
        snapshot = lambda value: sorted((c["id"], c["state"]) for c in value["containers"])
        result["checks"] = {"selected_named_project": result["selected"]["target"]["project"] == "buttercast",
                            "actual_mcp_root_changed": compose["project_dir"] == preview["target"]["project_dir"],
                            "configuration_route": result["file_review"]["metadata"]["route"] == "configuration",
                            "old_client_blocked": result["stale_request_status"] == 409,
                            "manual_path_restored": result["restored"]["target"]["project_dir"] == original_dir,
                            "containers_unchanged": snapshot(before) == snapshot(result["after"]),
                            "no_approval_created": result["file_review"]["approval"] is None}
        result["passed"] = all(result["checks"].values())
        file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"passed": result["passed"], "checks": result["checks"], "report": str(file)}, ensure_ascii=False))


if __name__ == "__main__": asyncio.run(main())
