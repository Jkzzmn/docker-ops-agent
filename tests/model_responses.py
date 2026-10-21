"""Test-only model contract double; never imported by application code."""
import json


class ScriptedModel:
    def __init__(self):
        self.calls = []
        self.analysis_policy = "auto"
        self.review_next = "write"

    async def json(self, prompt, *, quality=False, system=""):
        self.calls.append(("json", prompt, system))
        if prompt.startswith("Verify live-file intent"):
            from tests.routing_double import route_request
            query = json.loads(prompt.split("Input=", 1)[1])["query"]
            route = route_request(query)[0]
            return {"inspect_current_files": route in {"configuration", "change"}, "wants_edits": route == "change", "request_quote": query if route in {"configuration", "change"} else None}
        if prompt.startswith("Classify intent"):
            from tests.routing_double import route_request
            query = json.loads(prompt.split("Input=", 1)[1])["query"]
            route, reason = route_request(query)
            if route in {"status", "incident"}:
                from backend.diagnostics import target_service
                import re
                allowed = tuple(json.loads(prompt.split("Input=", 1)[1]).get("available_services", []))
                if allowed:
                    project = bool(re.search("목록|전체|모든|list", query))
                    return {"route": route, "reason": reason, "project_reference": "current", "target_scope": "project" if project else "service", "service": None if project else target_service(query, allowed)}
            return {"route": route, "reason": reason, "project_reference": "current"}
        if prompt.startswith("Create a minimal"):
            return {"tasks": [{"tool": "get_docker_engine_status", "args": {}}, {"tool": "list_containers", "args": {}}], "analysis_policy": self.analysis_policy}
        if prompt.startswith("Refine"):
            return {"tasks": [], "reason": "test assignment"}
        if prompt.startswith("Review"):
            return {"next": self.review_next, "reason": "test supervisor decision"}
        if prompt.startswith("Validate"):
            return {"ok": True, "problems": []}
        if prompt.startswith("Select lessons"):
            lessons, _ = json.JSONDecoder().raw_decode(prompt.split("Lessons=", 1)[1])
            return {"lesson_ids": [item["id"] for item in lessons]}
        raise AssertionError(f"unexpected model JSON request: {prompt[:50]}")

    async def text(self, prompt, *, quality=True, system=""):
        self.calls.append(("text", prompt, system))
        return "독립 분석: 관측한 원인과 추가로 확인할 가설을 구분하고 실패한 조회는 근거로 삼지 않습니다 [E1]."

    async def stream_text(self, prompt, *, quality=True, system=""):
        self.calls.append(("stream", prompt, system))
        if prompt.startswith("Conversation="):
            answer = "안녕하세요. 질문하신 개념을 간단히 설명하겠습니다."
        else:
            diagnosis, _ = json.JSONDecoder().raw_decode(prompt.split("Diagnosis=", 1)[1])
            refs = "".join(f"[{item}]" for item in diagnosis["evidence_ids"])
            answer = f"관측 기반 판단: {diagnosis['cause']} {refs}\n{diagnosis['reason']}\n변경 작업은 실행하지 않았습니다. 복구에는 명시적 승인이 필요하며 관측과 추정을 구분합니다."
        for i in range(0, len(answer), 18):
            yield answer[i:i+18]
