from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from . import usage
from .approvals import sign_approval
from .state import fresh_state
from .llm import ModelGateway
from .render import evidence_titles, markdown_to_html
from .sanitize import redact
from .target import resolve_target, TargetSelectionRequired
from .target_catalog import inventory, resolve_selection
from .runtime import launch_bundle, close_bundle
from .execution_policy import execute


load_dotenv()
CREATION_COUNT = 0


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str = Field(min_length=1, max_length=100)


class ThreadRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=100)


class ClarificationReply(ThreadRequest):
    choice: int | None = Field(default=None, strict=True, ge=1, le=3)
    answer: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def exactly_one_answer(self):
        if (self.choice is None) == (self.answer is None): raise ValueError("choose a number OR enter a text answer")
        return self


class TargetChoice(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=200)
    project_dir: str | None = Field(default=None, min_length=1, max_length=2000)
    expected_instance_id: str | None = None

    @model_validator(mode="after")
    def one_target(self):
        if bool(self.project_name) == bool(self.project_dir): raise ValueError("select a project name OR an absolute directory")
        return self


async def _launch_bundle(runtime_env, target):
    return await launch_bundle(runtime_env, target, model_factory=ModelGateway)


async def _close_bundle(bundle):
    await close_bundle(bundle)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    try: runtime_env, target = await asyncio.to_thread(resolve_target, dict(os.environ))
    except TargetSelectionRequired as exc: runtime_env, target = exc.env, exc.target
    if target.get("source") == "demo-not-running":
        target = target | {"source": "selection-required", "project": None, "project_dir": None, "services": [], "warning": "Compose 프로젝트가 없습니다. 서버의 프로젝트 경로를 선택해주세요."}
    app.state.active_runs, app.state.switching = 0, False
    app.state.bundle = await _launch_bundle(runtime_env, target)
    CREATION_COUNT += 1
    try:
        yield
    finally:
        await _close_bundle(app.state.bundle)


class TargetGuard:
    """Hold the target lease until the entire ASGI response (including SSE) has finished."""
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http": return await self.app(scope, receive, send)
        path, state = scope["path"], scope["app"].state
        selecting = path == "/targets/select" and scope["method"] == "POST"
        guarded = path in {"/chat", "/chat/stream"} or path.startswith(("/actions/", "/clarifications/"))
        if not (selecting or guarded) or scope["method"] == "OPTIONS": return await self.app(scope, receive, send)
        error = None
        expected = dict(scope["headers"]).get(b"x-docker-target", b"").decode() or parse_qs(scope.get("query_string", b"").decode()).get("target_id", [""])[0]
        if state.switching or (selecting and state.active_runs): error = "진단·승인 실행 또는 대상 전환 중입니다. 완료 후 다시 선택해주세요."
        elif expected and expected != state.bundle["instance_id"]: error = "Docker 대상이 변경됐습니다. 화면을 새로고침해주세요."
        elif guarded and not state.bundle["selected"]: error = "먼저 진단할 Docker 프로젝트를 선택해주세요."
        if error: return await JSONResponse({"detail": error}, status_code=409)(scope, receive, send)
        if selecting: state.switching = True
        else: state.active_runs += 1
        try: await self.app(scope, receive, send)
        finally:
            if selecting: state.switching = False
            else: state.active_runs -= 1


app = FastAPI(title="Docker Ops Agent", version="2.5.0", lifespan=lifespan)
app.add_middleware(TargetGuard)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


def _capture_io() -> bool:
    return os.getenv("RECORD_LLM_IO", "0") == "1"


def _initial(app: FastAPI, req: ChatRequest, *, query: str | None = None, rounds: int = 0, supersede: bool = True, confirmed_answer: str | None = None) -> tuple[dict, dict]:
    b = app.state.bundle
    if supersede: b["clarifications"].supersede(req.thread_id)
    history_id = b["history_scope"] + ":" + req.thread_id
    if history_id in b.setdefault("running_threads", set()):
        raise HTTPException(409, "같은 대화의 이전 응답이 진행 중입니다. 완료 후 다시 보내주세요.")
    b["store"].add(history_id, "user", req.message)
    aliases = [prefix + ":" + req.thread_id for prefix in b["legacy_history_scopes"]]
    state = fresh_state(query=query or req.message, thread_id=req.thread_id, history=b["store"].history(history_id, limit=12, aliases=aliases), clarification_rounds=rounds, confirmed_answer=confirmed_answer)
    b["running_threads"].add(history_id)
    return state, {"configurable": {"thread_id": history_id}}


def _finish_run(b: dict, req: ChatRequest, final: dict) -> None:
    if final.get("clarification"):
        final["clarification"] = b["clarifications"].create(req.thread_id, final["query"], final["clarification"], final.get("clarification_rounds", 0))
        b["metrics"]["clarifications_created"] += 1
    b["store"].add(b["history_scope"] + ":" + req.thread_id, "assistant", final.get("final", ""))
    _count(b, final)


def _meta(final: dict, b: dict) -> dict[str, Any]:
    fast, quality = b["settings"].models
    events = final.get("events", [])
    tool_time, observe_wall = final.get("tool_time_sum"), final.get("observe_wall_sum")
    route = final.get("route")
    errors = [e for e in events if e["type"] == "ModelError"]
    answer_status = final.get("answer_status") or ("partial" if final.get("validation_ok") is True and final.get("evidence_sufficient") is False else "validated" if final.get("validation_ok") is True else "partial" if final.get("validation_ok") is False else "unavailable" if route in {"unavailable", "target_selection"} or errors else "unverified")
    return {"schema_version": "2.5", "conversation_memory": {"backend": "sqlite", "thread_id": final.get("thread_id"), "history_messages": len(final.get("history", []))}, "execution_status": final.get("execution_status") or ("waiting_for_user" if final.get("clarification") or final.get("target_selection_required") else "failed" if route == "unavailable" else "completed"), "answer_status": answer_status,
            "lifecycle": final.get("lifecycle"), "health": final.get("health", "unknown"), "route_file_intent_check": final.get("route_file_intent_check"),
            "subject_status": final.get("subject_status", "unknown"), "target_selection_required": final.get("target_selection_required", False), "requested_project": final.get("requested_project"), "config_metrics": final.get("config_metrics"),
            "lesson_selection": final.get("lesson_selection"), "route_method": final.get("route_method"), "route_context_used": final.get("route_context_used", False), "route_fallback": final.get("route_fallback", False), "docker_target": b["target"], "knowledge_count": len(final.get("knowledge", [])), "knowledge_ids": [item["id"] for item in final.get("knowledge", [])], "retrieval_method": next((x.get("retrieval_method") for x in final.get("knowledge", [])), "llm-catalog" if final.get("semantic_lesson_ids") is not None else "keyword-top3") if route == "education" else None,
            "route": final.get("route"), "route_reason": final.get("route_reason"), "waiting_for_user": bool(final.get("clarification")), "clarification_rounds": final.get("clarification_rounds", 0), "command": (final.get("command") or {}).get("name"), "task_count": len(final.get("tasks", [])), "replan_rounds": final.get("replan_rounds", 0), "retry_count": final.get("retry_count", 0), "parallel_elapsed": round(final.get("parallel_elapsed", 0.0), 4),
            "tool_time_sum": round(tool_time, 4) if tool_time is not None else None, "observe_wall_sum": round(observe_wall, 4) if observe_wall is not None else None, "tool_concurrency": round(tool_time / observe_wall, 2) if observe_wall and tool_time is not None else None,
            "validation_attempts": final.get("validation_attempts", 0), "validation_ok": final.get("validation_ok"), "evidence_count": len(final.get("evidence", [])), "evidence_coverage": final.get("evidence_coverage"), "evidence_sufficient": final.get("evidence_sufficient"), "root_cause": final.get("diagnosis", {}).get("cause"),
            "tool_calls": ((usage.CURRENT_USAGE.get() or {}).get("execution_budget") or {}).get("tool_calls", sum(e.get("retries", 0) + 1 for e in events if e["type"] in {"TaskCompleted", "TaskFailed"})), "mutation_calls": 0, "policy_blocked": final.get("unsafe_request", False), "analysis_policy": final.get("analysis_policy"), "analysis_used": final.get("analysis_used", False), "analysis_reason": final.get("analysis_reason"),
            "agents_run": sorted({e["agent"] for e in events if e["type"] == "AgentStarted"}), "worker_reports": final.get("worker_reports", []), "confidence": final.get("confidence"), "proposal_created": bool(final.get("proposal")),
            "llm_backend": b["settings"].backend, "fast_model": fast, "quality_model": quality, "router_model": (b["settings"].openai_router_model if b["settings"].backend == "openai" else b["settings"].router_model) or fast,
            "llm_usage": usage.summarize(usage.CURRENT_USAGE.get()), "execution_budget": (usage.CURRENT_USAGE.get() or {}).get("execution_budget"), "router_escalations": (usage.CURRENT_USAGE.get() or {}).get("router_escalations", 0),
            "supervisor_version": b["deps"].version, "instance_id": b["instance_id"], "langsmith_run_id": (usage.CURRENT_USAGE.get() or {}).get("langsmith_run_id"), "langsmith_status": (usage.CURRENT_USAGE.get() or {}).get("langsmith_status")}


def _reply_html(final: dict) -> str:
    """The validated answer as an escaped, allowlisted HTML fragment (see backend/render.py)."""
    return markdown_to_html(final.get("final", ""), evidence_titles(final.get("evidence"), final.get("knowledge")))


def _count(b: dict, final: dict) -> None:
    """Process-level safety and planning counters (GET /metrics). Counters reset on restart."""
    m, events = b["metrics"], final.get("events", [])
    m["requests"] += 1
    m[f"route_{final.get('route')}"] += 1
    m["policy_blocked_requests"] += bool(final.get("unsafe_request"))
    m["gate_removed_tasks"] += sum(e.get("removed_tasks", 0) for e in events if e["type"] == "GateApplied")
    rounds = final.get("replan_rounds", 0)
    m["replanned_requests"] += rounds > 0
    m["replan_resolved_requests"] += rounds > 0 and bool(final.get("evidence_sufficient"))
    done = sum(1 for e in events if e["type"] in {"TaskCompleted", "TaskFailed"})
    m["replan_extra_tool_calls"] += max(0, done - len(final.get("tasks", []))) if rounds else 0
    m["approvals_created"] += bool(final.get("approval"))


@app.get("/health")
async def health():
    b = app.state.bundle; fast, quality = b["settings"].models
    return {"ok": True, "router_model": (b["settings"].openai_router_model if b["settings"].backend == "openai" else b["settings"].router_model) or fast, "execution_limits": {"seconds": b["settings"].request_seconds, "model_calls": b["settings"].max_model_calls, "tool_calls": b["settings"].max_tool_calls}, "checkpoint_backend": "sqlite", "checkpoint_enabled": bool(b["graph"] and b["graph"].checkpointer), "docker_target": b["target"], "target_selected": b["selected"], "target_switching": app.state.switching, "llm_backend": b["settings"].backend, "fast_model": fast, "quality_model": quality, "docker_mode": b["settings"].docker_mode, "fixture_scenario": b["env"].get("DOCKER_FIXTURE_SCENARIO", "oom") if b["settings"].docker_mode == "fixture" else None, "supervisor_version": b["deps"].version, "instance_id": b["instance_id"], "creation_count": CREATION_COUNT, "mcp_connected": bool(b["mcp"] and b["mcp"].session is not None)}


@app.get("/metrics")
async def metrics():
    b = app.state.bundle
    return {"since": b["started_at"], "counters": dict(b["metrics"]), "note": "프로세스 메모리 카운터이며 서버 재시작 시 초기화됩니다."}


@app.get("/version")
async def version(): return {"api": app.version, "graph": "docker-supervisor-v3", "commands": ["/diagnose-network <a> <b>", "/diagnose-oom <service>"], "features": ["educational-retrieval", "supervisor-subagents", "conditional-analysis", "direct-chat", "status-fast-path", "dynamic-replan", "evidence-validator", "human-approval", "post-action-verification", "config-proposals", "sqlite-checkpointer", "bounded-conversation-memory", "html-reply", "live-config-review", "two-stage-config-approval", "config-deploy-rollback", "conditional-human-clarification", "numbered-or-free-text-reply", "docker-inventory", "project-path-selection", "project-intent-guard", "claim-level-config-validation", "semantic-lesson-catalog", "response-status-contract"]}


@app.get("/targets")
async def targets():
    b = app.state.bundle
    try: result = await asyncio.to_thread(inventory, b["env"])
    except Exception as exc: raise HTTPException(503, redact(str(exc)))
    return result | {"current": b["target"], "instance_id": b["instance_id"]}


async def _resolve_choice(req):
    try: return await asyncio.to_thread(resolve_selection, app.state.bundle["env"], project_name=req.project_name, project_dir=req.project_dir)
    except Exception as exc: raise HTTPException(400, redact(str(exc)))


@app.post("/targets/preview")
async def preview_target(req: TargetChoice):
    identity = app.state.bundle["instance_id"]
    _, target = await _resolve_choice(req)
    return {"target": target, "instance_id": identity, "note": "미리보기만 수행했습니다. 컨테이너 시작·파일 변경은 하지 않았습니다."}


@app.post("/targets/select")
async def select_target(req: TargetChoice):
    old = app.state.bundle
    if req.expected_instance_id != old["instance_id"]: raise HTTPException(409, "대상이 변경됐습니다. 목록과 미리보기를 새로 조회해주세요.")
    runtime, target = await _resolve_choice(req)
    try: new = await _launch_bundle(runtime, target)
    except Exception as exc: raise HTTPException(502, "새 대상 연결 실패. 기존 대상을 유지합니다: " + redact(str(exc)))
    try:
        old["approvals"].invalidate_pending()
        old["clarifications"].invalidate_pending()
    except Exception:
        await _close_bundle(new)
        raise
    app.state.bundle = new
    await _close_bundle(old)
    return {"target": target, "instance_id": new["instance_id"], "note": "진단 대상만 전환했습니다. 이전 대기 질문·승인은 무효화했으며 컨테이너·파일은 변경하지 않았습니다."}


async def _chat_json(b, req, initial, config):
    usage.begin(_capture_io())
    try:
        final = initial
        async for namespace, mode, chunk in execute(b["graph"], initial, config, b["settings"]):
            if mode == "values" and not namespace: final = chunk
    except Exception as exc:
        raise HTTPException(502, {"message": "에이전트 실행에 실패했습니다. 변경 작업은 수행하지 않았습니다.", "error": redact(str(exc) or type(exc).__name__)})
    finally:
        b["running_threads"].discard(b["history_scope"] + ":" + req.thread_id)
    _finish_run(b, req, final)
    body = {"reply": final["final"], "reply_html": _reply_html(final), "events": final.get("events", []), "clarification": final.get("clarification"), "approval": final.get("approval"), "proposal": final.get("proposal"), "evidence": final.get("evidence", []), "knowledge": final.get("knowledge", []), "diagnosis": final.get("diagnosis"), "metadata": _meta(final, b)}
    if _capture_io():
        body["llm_calls"] = usage.CURRENT_USAGE.get()["calls"]
    body["diagnosis"] = body["diagnosis"] or None
    return body


@app.post("/chat")
async def chat(req: ChatRequest):
    initial, config = _initial(app, req)
    return await _chat_json(app.state.bundle, req, initial, config)


@app.get("/chat/stream")
async def chat_stream(message: str = Query(min_length=1, max_length=4000), thread_id: str = Query(min_length=1, max_length=100)):
    req = ChatRequest(message=message, thread_id=thread_id); b = app.state.bundle; initial, config = _initial(app, req)
    return _stream_response(b, req, initial, config)


def _stream_response(b, req, initial, config):
    async def generate():
        usage.begin(_capture_io())
        final: dict = {}
        try:
            async for namespace, mode, chunk in execute(b["graph"], initial, config, b["settings"]):
                if mode == "custom":
                    yield f"data: {json.dumps(chunk | {'namespace': list(namespace)}, ensure_ascii=False)}\n\n"
                elif not namespace:
                    final = chunk  # the parent graph's full state after each step; the last one is the result
            _finish_run(b, req, final)
            payload = {"type": "FinalAnswer", "reply": final.get("final", ""), "reply_html": _reply_html(final), "prompt": final.get("prompt_used"), "clarification": final.get("clarification"), "approval": final.get("approval"), "proposal": final.get("proposal"), "evidence": final.get("evidence", []), "knowledge": final.get("knowledge", []), "diagnosis": final.get("diagnosis"), "metadata": _meta(final, b)}
            if _capture_io():
                payload["llm_calls"] = usage.CURRENT_USAGE.get()["calls"]
            payload["diagnosis"] = payload["diagnosis"] or None
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type':'Error','message':redact(str(exc) or type(exc).__name__)}, ensure_ascii=False)}\n\n"
        finally:
            b["running_threads"].discard(b["history_scope"] + ":" + req.thread_id)
            yield "data: [DONE]\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.get("/clarifications/pending")
async def pending_clarification(thread_id: str = Query(min_length=1, max_length=100)):
    return {"clarification": app.state.bundle["clarifications"].pending(thread_id)}


def _finish_question(identity, req, **kwargs):
    try: return app.state.bundle["clarifications"].finish(identity, req.thread_id, **kwargs)
    except KeyError as exc: raise HTTPException(404, str(exc))
    except ValueError as exc: raise HTTPException(409, str(exc))


@app.post("/clarifications/{identity}/cancel")
async def cancel_clarification(identity: str, req: ThreadRequest):
    _finish_question(identity, req, cancel=True)
    return {"status": "cancelled"}


@app.post("/clarifications/{identity}/reply")
async def reply_clarification(identity: str, req: ClarificationReply, stream: bool = False):
    pending = _finish_question(identity, req, choice=req.choice, answer=req.answer)
    query = ("사용자가 확정한 의도·대상 (아래 원래 요청보다 이 답변 우선, 변경 실행 승인이 아님):\n" + pending["answer"]
             + "\n추가 확인 질문:\n" + pending["question"]["question"] + "\n사용자의 원래 요청:\n" + pending["query"])
    # Only bounded local question/answer text is composed; external ChatRequest limits stay unchanged.
    chat_req = ChatRequest(message=pending["answer"], thread_id=req.thread_id)
    initial, config = _initial(app, chat_req, query=query, rounds=pending["rounds"] + 1, supersede=False, confirmed_answer=pending["answer"])
    app.state.bundle["metrics"]["clarifications_answered"] += 1
    if stream: return _stream_response(app.state.bundle, chat_req, initial, config)
    return await _chat_json(app.state.bundle, chat_req, initial, config)


def _state_brief(value: dict) -> dict:
    keys = ("state", "health", "restarts", "exit_code", "oom_killed")
    return {k: value[k] for k in keys if k in value}


@app.post("/actions/{action_id}/approve")
async def approve(action_id: str):
    b = app.state.bundle
    async with b.setdefault("action_lock", asyncio.Lock()):
        return await _approve(action_id)


async def _approve(action_id: str):
    b = app.state.bundle
    try: action = b["approvals"].claim(action_id, "executing")
    except KeyError: raise HTTPException(404, "approval not found")
    except ValueError as exc: raise HTTPException(409, str(exc))
    mutation_started = False
    wait = b["settings"].recovery_wait_seconds
    try:
        if action.tool in {"apply_project_config_patch", "deploy_project_config"}:
            token = sign_approval(b["mcp"].approval_secret, action.id, action.tool, action.args, action.expires_at)
            mutation_started = True
            result, retries = await b["mcp"].tool(action.tool, action.args | {"approval_token": token})
            runtime = result.pop("runtime_plan", None)
            payload = {"type": "ConfigurationAction", "action_id": action.id, "status": "executed" if result.get("ok") else "failed",
                       "result": result, "stage": result.get("stage"), "retries": retries, "next_approval": None}
            if result.get("ok") and runtime:
                item = b["approvals"].create(action.thread_id, "deploy_project_config", {"plan": runtime},
                                            "2차 승인: 표시된 서비스만 빌드·재생성합니다. 중단이 발생하며 health 검증 실패 시 이전 파일·이미지·실행 상태 복원을 시도합니다.")
                payload["next_approval"] = asdict(item)
                b["metrics"]["approvals_created"] += 1
            b["approvals"].finish(action.id, payload["status"], payload)
            b["metrics"]["configuration_actions"] += 1
            return payload
        before, _ = await b["mcp"].tool("check_service_health", action.args)
        if not before.get("ok", False):
            raise ValueError("pre-action health unavailable; action not executed")
        inspect_before, _ = await b["mcp"].tool("inspect_container", action.args)
        token = sign_approval(b["mcp"].approval_secret, action.id, action.tool, action.args, action.expires_at)
        mutation_started = True
        result, retries = await b["mcp"].tool(action.tool, action.args | {"approval_token": token})
        # A restarted container is "starting" until its first healthcheck passes; poll for up to RECOVERY_WAIT_SECONDS.
        started, deadline = time.monotonic(), time.monotonic() + wait
        after, _ = await b["mcp"].tool("check_service_health", action.args)
        while not (after.get("state") == "running" and after.get("health") == "healthy") and time.monotonic() < deadline:
            await asyncio.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
            after, _ = await b["mcp"].tool("check_service_health", action.args)
        inspect_after, _ = await b["mcp"].tool("inspect_container", action.args)
        recovered = bool(result.get("ok")) and after.get("state") == "running" and after.get("health") == "healthy"
        b["metrics"]["approvals_executed"] += 1; b["metrics"]["recoveries_verified"] += recovered
        payload = {"type": "RecoveryVerified", "action_id": action.id, "status": "executed" if result.get("ok") else "failed", "recovery_ok": recovered, "before": before, "action_result": result, "after": after,
                   "inspect_before": _state_brief(inspect_before), "inspect_after": _state_brief(inspect_after), "waited_seconds": round(time.monotonic() - started, 1), "retries": retries, "mutation_calls": 1,
                   "note": "fixture mode simulates execution; live mode reports actual Docker health, not application HTTP readiness. A restart does not reset RestartCount."}
        b["approvals"].finish(action.id, payload["status"], payload)
        return payload
    except Exception as exc:
        status = "unknown" if mutation_started else "failed"
        b["approvals"].finish(action.id, status, {"error": redact(str(exc)), "mutation_attempted": mutation_started})
        raise HTTPException(502, {"status": status, "message": "조치/검증에 실패했습니다. 자동 재실행하지 않으며 상태를 직접 확인하세요."})


@app.post("/actions/{action_id}/reject")
async def reject(action_id: str):
    b = app.state.bundle
    try: action = b["approvals"].claim(action_id, "rejected")
    except KeyError: raise HTTPException(404, "approval not found")
    except ValueError as exc: raise HTTPException(409, str(exc))
    b["metrics"]["approvals_rejected"] += 1
    return {"type": "ActionRejected", "action_id": action.id, "status": action.status}
