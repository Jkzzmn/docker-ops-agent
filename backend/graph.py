from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from .agents import SupervisorAgent, build_analysis_agent, build_knowledge_agent, build_observation_agent, build_validator_agent, build_writer_agent
from .approvals import ApprovalStore
from .config_agent import build_config_agent
from .diagnostics import completed_observation, succeeded, target_is_down, target_service, task_key
from .events import emit
from .llm import ModelGateway
from .mcp_client import DockerMCPClient
from .sanitize import redact
from .planning import WORKER_TOOLS, normalize_tasks, review_evidence
from .proposals import build_proposal
from .routing import classify_request, parse_command
from .tracing import traced_node
from .observations import status_answer, health_status
from .network_diagnosis import diagnose_network
from .config import Settings
from .execution_policy import CURRENT_BUDGET, facts_only


from .state import AgentState, fresh_state

@dataclass
class Dependencies:
    llm: ModelGateway
    mcp: DockerMCPClient
    approvals: ApprovalStore
    version: str = "v3"
    allowed_services: tuple[str, ...] = ("api", "db", "web")
    selected_target: dict | None = None


def _emit(state: dict, event: dict) -> None:
    emit(state.setdefault("events", []), event)



def build_graph(deps: Dependencies, *, checkpointer=None):
    supervisor = SupervisorAgent(deps.llm, deps.allowed_services)
    workers = {name: build_observation_agent(name, deps.llm, deps.mcp, deps.allowed_services) for name in WORKER_TOOLS}
    analyst = build_analysis_agent(deps.llm)
    writer = build_writer_agent(deps.llm)
    validator = build_validator_agent(deps.llm)
    knowledge_agent = build_knowledge_agent()
    configuration_agent = build_config_agent(deps)

    async def configuration(state: AgentState) -> dict:
        result = await configuration_agent.ainvoke(dict(state))
        return {k: v for k, v in result.items() if k in AgentState.__annotations__}

    async def network_diagnose(state: AgentState) -> dict:
        args = state["command"]["args"]
        return await diagnose_network(deps.mcp, args["service_a"], args["service_b"], list(state.get("events", [])))

    async def facts_finish(state: AgentState) -> dict:
        out = facts_only(state, state.get("analysis_reason") or "관측 한계로 조기 종료") | {"events": list(state.get("events", []))}
        _emit(out, {"type": "Finalize", "pipeline": "facts-only", "agent": "supervisor", "node": "facts_finish"})
        return out

    async def route(state: AgentState) -> dict:
        command = parse_command(state["query"], deps.allowed_services)
        if command and command.get("error"):
            decision = {"route": "blocked", "route_reason": command["error"], "route_method": "slash-command", "route_fallback": False}
        elif command:
            decision = {"route": "incident", "route_reason": f"{command['name']} 명령이 지정한 표준 진단 절차", "route_method": "slash-command", "route_fallback": False,
                        "query": command["query"], "command": command}
        else:
            decision = await classify_request(deps.llm, state["query"], state.get("history", []), allowed_services=deps.allowed_services, confirmed_answer=state.get("confirmed_answer"), selected_target=deps.selected_target)
        out = decision | {"unsafe_request": decision["route"] == "blocked", "events": list(state.get("events", []))}
        _emit(out, {"type": "RouteDecided", "route": decision["route"], "reason": decision["route_reason"], "method": decision["route_method"], "context_used": decision.get("route_context_used", False), "agent": "supervisor", "node": "route"})
        return out

    async def human_question(state: AgentState) -> dict:
        question = state["clarification"]
        if state.get("clarification_rounds", 0) >= 2:
            return {"final": "두 차례 추가 확인 후에도 대상·작업을 확정하지 못했습니다. 서비스명과 원하는 작업을 한 문장으로 구체적으로 적어 새 요청을 보내주세요. 조회·변경은 실행하지 않았습니다.",
                    "clarification": None, "approval": None, "events": list(state.get("events", []))}
        out = {"final": question["question"] + "\n\n" + question["reason"] + "\n\n번호를 선택하거나 직접 답변해주세요. 이 답변은 변경 작업 승인이 아닙니다.",
               "clarification": question, "approval": None, "proposal": None, "events": list(state.get("events", []))}
        _emit(out, {"type": "HumanInputRequired", "question": question, "agent": "supervisor", "node": "human_question"})
        return out

    async def target_selection(state: AgentState) -> dict:
        current = (deps.selected_target or {}).get("project", "미선택")
        return {"final": f"요청 대상: {state.get('requested_project') or '확인 필요'} · 현재 선택: {current}.\n프로젝트 선택 패널에서 원하는 대상을 명시적으로 선택한 뒤 원래 질문을 다시 보내주세요. 현재 프로젝트의 Docker·파일 조회나 변경은 수행하지 않았습니다.",
                "target_selection_required": True, "approval": None, "proposal": None, "answer_status": "unavailable"}

    async def status_observe(state: AgentState) -> dict:
        from .planning import status_tasks
        from .observations import observation_status
        task = status_tasks(state["query"], deps.allowed_services, state.get("routing_target"))[0]
        start = time.perf_counter()
        try: value, retries = await deps.mcp.tool(task["tool"], task["args"])
        except Exception as exc: value, retries = {"ok": False, "error": redact(str(exc))}, getattr(exc, "retries", 0)
        elapsed = time.perf_counter() - start
        item = task | {"id": "E1", "observation": value, "elapsed_ms": elapsed * 1000, "agent": "runtime"}
        out = {"evidence": [item], "tasks": [task], "retry_count": retries, "tool_time_sum": elapsed, "observe_wall_sum": elapsed, "events": list(state.get("events", []))}
        _emit(out, {"type": "TaskCompleted" if observation_status(value) == "complete" else "TaskFailed", "id": "E1", "target": task["tool"], "retries": retries, "agent": "runtime", "node": "status_observe"})
        _emit(out, {"type": "AnalysisSkipped", "reason": "단일 구조화 상태 조회: 분석·Writer 생략", "agent": "supervisor", "node": "status_observe"})
        return out | status_answer(item)

    async def policy_block(state: AgentState) -> dict:
        text = "요청한 실행은 허용된 Docker 도구 범위를 벗어나 차단했습니다. 실행된 변경 작업은 없습니다. 허용 서비스의 읽기 전용 진단과 명시적으로 승인된 제한적 재시작만 수행할 수 있습니다."
        out = {"final": text, "approval": None, "events": list(state.get("events", []))}
        _emit(out, {"type": "PolicyBlocked", "agent": "supervisor", "node": "policy_block", "mutation_calls": 0})
        _emit(out, {"type": "Finalize", "pipeline": "policy-blocked", "agent": "supervisor", "node": "policy_block"})
        return out

    async def routing_unavailable(state: AgentState) -> dict:
        out = {"final": "LLM이 요청의 의도·대상을 확정하지 못했습니다. 모델 서버 상태를 확인하거나 점검할 대상·요청을 구체적으로 적어 다시 시도해주세요. 규칙 분류나 교육 안내로 대체하지 않았으며 Docker·파일 조회 및 변경은 수행하지 않았습니다.",
               "approval": None, "proposal": None, "events": list(state.get("events", []))}
        _emit(out, {"type": "RoutingUnavailable", "reason": state["route_reason"], "agent": "supervisor", "node": "routing_unavailable"})
        _emit(out, {"type": "Finalize", "pipeline": "routing-unavailable", "agent": "supervisor", "node": "routing_unavailable"})
        return out

    async def chat_answer(state: AgentState) -> dict:
        out, chunks = {"events": list(state.get("events", []))}, []
        history = [{"role": item["role"], "content": item["content"][:1500]} for item in state.get("history", [])[-8:]]
        prompt = f"Conversation={history}. User={state['query']}. Reply briefly in Korean. Explain concepts without observing live system state."
        try:
            async for delta in deps.llm.stream_text(prompt, quality=False, system="You are a helpful assistant. Answer the question directly. Do not claim you inspected Docker or performed actions."):
                chunks.append(delta)
                _emit(out, {"type": "AnswerToken", "delta": delta, "attempt": 1, "agent": "supervisor", "node": "chat_answer"})
        except Exception as exc:
            notice = "\n\n[모델 응답이 중단되었습니다. 모델 서버 상태를 확인하고 다시 질문해주세요. Docker 조회나 변경은 수행하지 않았습니다.]" if chunks else "모델 응답을 받지 못했습니다. 모델 서버(Ollama/OpenAI) 상태를 확인하고 다시 질문해주세요. Docker 조회나 변경은 수행하지 않았습니다."
            chunks.append(notice)
            _emit(out, {"type": "AnswerToken", "delta": notice, "attempt": 1, "agent": "supervisor", "node": "chat_answer"})
            _emit(out, {"type": "ModelError", "message": redact(str(exc) or type(exc).__name__), "agent": "supervisor", "node": "chat_answer"})
        _emit(out, {"type": "Finalize", "pipeline": "chat-direct", "agent": "supervisor", "node": "chat_answer"})
        return out | {"draft": "".join(chunks), "final": "".join(chunks), "approval": None}

    async def plan(state: AgentState) -> dict:
        # A status-only request needs no MCP prompt or planning-model round trip.
        standard, events = "", list(state.get("events", []))
        if state["route"] == "incident":
            try:
                command = state.get("command")
                standard = await (deps.mcp.named_prompt(command["prompt"], command["args"]) if command else deps.mcp.prompt(state["query"]))
            except Exception as exc:
                _emit({"events": events}, {"type": "PromptUnavailable", "message": redact(str(exc) or type(exc).__name__), "agent": "supervisor", "node": "plan"})
        result = await supervisor.plan(state["query"], state["route"], standard, state.get("routing_target"))
        out = {"tasks": result["tasks"], "pending": result["tasks"], "analysis_policy": result["analysis_policy"],
               "analysis_reason": result["reason"], "prompt_used": standard, "events": events}
        _emit(out, {"type": "PlanCreated", "task_count": len(result["tasks"]), "targets": [t["args"].get("service", "project") for t in result["tasks"]],
                    "analysis_policy": result["analysis_policy"], "reason": result["reason"], "agent": "supervisor", "node": "plan"})
        return out

    async def education_retrieve(state: AgentState) -> dict:
        result = await knowledge_agent.ainvoke({"query": state["query"], "semantic_lesson_ids": state.get("semantic_lesson_ids")})
        return {"knowledge": result["knowledge"], "evidence": [], "approval": None,
                "analysis_policy": "skip", "analysis_reason": "교육 지식 검색만 수행: 실시간 진단·분석 생략",
                "events": list(state.get("events", [])) + result["events"]}

    async def education_unknown(state: AgentState) -> dict:
        text = "현재 검토된 교육 자료에서 이 질문의 근거를 찾지 못했습니다. 아는 내용인 것처럼 설명하지 않겠습니다. 배우려는 Docker 개념이나 오류 메시지, 사용한 설정을 알려주세요. 실제 환경 점검이나 변경 작업은 수행하지 않았습니다."
        out = {"final": text, "approval": None, "answer_status": "unavailable", "events": list(state.get("events", []))}
        _emit(out, {"type": "KnowledgeUnavailable", "agent": "supervisor", "node": "education_unknown"})
        _emit(out, {"type": "Finalize", "pipeline": "education-no-knowledge", "agent": "supervisor", "node": "education_unknown"})
        return out

    async def gate(state: AgentState) -> dict:
        pending = state.get("pending", [])
        safe = normalize_tasks(pending, deps.allowed_services, target_service(state["query"], deps.allowed_services))
        collected = {task_key(e) for e in state.get("evidence", []) if completed_observation(e)}
        safe = [t for t in safe if task_key(t) not in collected]
        policy = state.get("analysis_policy", "auto")
        if policy not in {"auto", "skip", "required"}:
            policy = "auto"
        if state["route"] == "status":
            policy = "skip"
        out = {"pending": safe, "analysis_policy": policy, "events": list(state.get("events", []))}
        _emit(out, {"type": "GateApplied", "removed_tasks": len(pending) - len(safe), "analysis_policy": policy, "agent": "supervisor", "node": "gate"})
        return out

    async def research(state: AgentState) -> dict:
        out = {"events": list(state.get("events", []))}
        jobs = {name: [t for t in state.get("pending", []) if t["tool"] in tools] for name, tools in WORKER_TOOLS.items()}
        jobs = {name: tasks for name, tasks in jobs.items() if tasks}
        _emit(out, {"type": "AgentsDispatched", "agents": list(jobs), "agent": "supervisor", "node": "research"})
        started = time.perf_counter()
        known = [task_key(e) for e in state.get("evidence", []) if completed_observation(e)]
        results = await asyncio.gather(*(workers[name].ainvoke({
            "query": state["query"], "assigned_tasks": tasks, "known_tasks": known,
            "minimal": state["route"] == "status",
        }) for name, tasks in jobs.items()))
        evidence, reports = list(state.get("evidence", [])), list(state.get("worker_reports", []))
        retries, tool_time = state.get("retry_count", 0), state.get("tool_time_sum") or 0.0
        observe_wall = (state.get("observe_wall_sum") or 0.0) + sum(r["report"]["observe_wall_ms"] for r in results) / 1000
        for result in results:
            out["events"].extend(result["events"])
            reports.append(result["report"])
            for observation in result["observations"]:
                count = observation.pop("retries")
                item = observation | {"id": f"E{len(evidence)+1}"}
                evidence.append(item); retries += count; tool_time += item.get("elapsed_ms", 0) / 1000
                _emit(out, {"type": "TaskCompleted" if completed_observation(item) else "TaskFailed", "id": item["id"], "source": "mcp-tool",
                            "target": item["tool"], "retries": count, "agent": item["agent"], "node": "research"})
        return out | {"evidence": evidence, "worker_reports": reports, "pending": [], "retry_count": retries, "tool_time_sum": tool_time, "observe_wall_sum": observe_wall,
                      "parallel_elapsed": state.get("parallel_elapsed", 0) + time.perf_counter() - started}

    async def replan_check(state: AgentState) -> dict:
        budget = CURRENT_BUDGET.get()
        if budget and budget.reason:
            return {"next_step": "facts", "pending": [], "analysis_reason": budget.reason}
        evidence = list(state.get("evidence", []))
        review = review_evidence(state["query"], state["route"], evidence, deps.allowed_services, state.get("routing_target"))
        if review["diagnosis"]["cause"] == "not_deployed":
            item = next(e for e in evidence if e["id"] in review["diagnosis"]["evidence_ids"])
            return status_answer(item) | {"diagnosis": review["diagnosis"], "evidence_coverage": None, "evidence": evidence,
                                        "next_step": "done", "pending": [], "events": list(state.get("events", []))}
        next_step, reason = await supervisor.next_step(state, review)
        missing = review.pop("missing")
        attempted = {task_key(e) for e in evidence}
        gap_tasks = [t for t in missing if task_key(t) not in attempted]
        rounds = state.get("replan_rounds", 0)
        if next_step == "collect":
            rounds += 1
        elif state["route"] == "incident" and review["evidence_sufficient"] and not any(e["tool"] == "runbook_resource" for e in evidence):
            book_type = {"memory": "oom", "network": "network", "readiness": "readiness"}.get(review["diagnosis"]["cause"], "restart-loop")
            try:
                book = await deps.mcp.resource(f"docker://runbooks/{book_type}")
                evidence.append({"id": f"E{len(evidence)+1}", "tool": "runbook_resource", "args": {"incident_type": book_type},
                                 "observation": {"ok": True, "text": book}, "why": "interpretation only, not live evidence"})
            except Exception:
                pass
        out = review | {"evidence": evidence, "pending": gap_tasks if next_step == "collect" else [], "replan_rounds": rounds,
                        "next_step": next_step, "analysis_reason": reason, "confidence": review["diagnosis"]["confidence"], "events": list(state.get("events", []))}
        from .observations import subject_status
        service = (state.get("routing_target") or {}).get("service") or target_service(state["query"], deps.allowed_services)
        latest = next((e["observation"] for e in reversed(evidence) if e["tool"] == "check_service_health" and e.get("args", {}).get("service") == service), {})
        out.update(subject_status=subject_status(latest), lifecycle=latest.get("state"), health=health_status(latest) if subject_status(latest) == "present" else "unknown")
        _emit(out, {"type": "ReplanCheck", "round": rounds, "is_sufficient": review["evidence_sufficient"],
                    "missing": [t["tool"] for t in missing], "exhausted": bool(missing) and next_step != "collect",
                    "gap_tasks": [t["tool"] for t in out["pending"]], "agent": "supervisor", "node": "replan_check"})
        _emit(out, {"type": "SupervisorDecision", "next": next_step, "reason": reason, "agent": "supervisor", "node": "replan_check"})
        if next_step == "write":
            _emit(out, {"type": "AnalysisSkipped", "reason": reason, "agent": "supervisor", "node": "replan_check"})
        return out

    async def analysis(state: AgentState) -> dict:
        result = await analyst.ainvoke({"query": state["query"], "evidence": state["evidence"], "diagnosis": state["diagnosis"], "reason": state["analysis_reason"]})
        out = {"analysis": result["analysis"], "analysis_used": True, "events": list(state.get("events", [])) + result["events"]}
        _emit(out, {"type": "AnalysisCompleted", "length": len(result["analysis"]), "agent": "analysis", "node": "analysis"})
        return out

    async def write(state: AgentState) -> dict:
        attempt = state.get("validation_attempts", 0) + 1
        result = await writer.ainvoke({"query": state["query"], "route": state["route"], "evidence": state["evidence"],
                                      "diagnosis": state.get("diagnosis", {}), "knowledge": state.get("knowledge", []), "education_context": state.get("education_context", {}), "analysis": state.get("analysis", ""), "problems": state.get("validation_problems", []), "attempt": attempt})
        out = {"draft": result["draft"], "events": list(state.get("events", [])) + result["events"]}
        if "lesson_selection" in result:
            out["lesson_selection"] = result["lesson_selection"]
        _emit(out, {"type": "DraftWritten", "attempt": attempt, "length": len(result["draft"]), "agent": "writer", "node": "write"})
        return out

    async def validate(state: AgentState) -> dict:
        result = await validator.ainvoke({"route": state["route"], "draft": state["draft"], "evidence": state["evidence"], "knowledge": state.get("knowledge", []), "education_context": state.get("education_context", {})})
        attempt = state.get("validation_attempts", 0) + 1
        maxed = attempt >= Settings().max_validation_attempts and not result["ok"]
        out = {"validation_ok": result["ok"], "validation_problems": result["problems"], "validation_attempts": attempt,
               "events": list(state.get("events", [])) + result["events"]}
        if maxed:
            out["draft"] = ("[교육 답변 검증 미통과]\n검색한 자료로 안전하고 정확한 설명을 확인하지 못했습니다. 중지인지 삭제·재생성인지, 앱의 저장 경로와 마운트 설정을 먼저 알려주세요. 데이터 변경이나 복구 작업은 수행하지 않았습니다."
                            if state["route"] == "education" else "[검증 미통과]\n답변 검증에 실패해 검증되지 않은 원고를 게시하지 않았습니다. 수집한 관측은 근거 패널에서 확인할 수 있으며 원인·복구 성공을 확정할 수 없습니다. 변경은 수행하지 않았습니다.")
            if state["route"] != "education":
                safe = facts_only(state, "검증 거절: 추가 재작성 없이 구조화 관측만 반환")
                out.update({key: value for key, value in safe.items() if key != "final"})
                out["draft"] = "[검증 미통과]\n" + safe["final"]
        _emit(out, {"type": "Validation", "attempt": attempt, "verdict": "검증실패(최대시도 종료)" if maxed else ("합격" if result["ok"] else "불합격"),
                    "problems": result["problems"], "agent": "validator", "node": "validate"})
        return out

    async def finalize(state: AgentState) -> dict:
        approval = None
        source = target_service(state["query"], deps.allowed_services)
        unhealthy = target_is_down(state.get("evidence", []), source)
        diagnosis = state.get("diagnosis", {})
        proposal = build_proposal(diagnosis, state.get("evidence", []), source) if state["route"] == "incident" and state.get("validation_ok") and state.get("evidence_sufficient") else None
        if state["route"] == "incident" and state.get("validation_ok") and state.get("evidence_sufficient") and diagnosis.get("cause") == "memory" and diagnosis.get("corroborated") and unhealthy:
            item = deps.approvals.create(state["thread_id"], "restart_service", {"service": source},
                                         f"관측 근거로 {source}의 OOM을 확인했습니다. 재시작은 임시 복구이며 실행 후 health를 재검증합니다.")
            approval = {"id": item.id, "tool": item.tool, "args": item.args, "reason": item.reason, "status": item.status, "expires_at": item.expires_at}
        out = {"final": state.get("draft", ""), "approval": approval, "proposal": proposal, "events": list(state.get("events", []))}
        if state.get("validation_ok"):
            _emit(out, {"type": "AnswerToken", "delta": out["final"], "attempt": state.get("validation_attempts", 1), "agent": "supervisor", "node": "finalize"})
        if proposal:
            _emit(out, {"type": "ProposalCreated", "kind": proposal["kind"], "title": proposal["title"], "applied": False, "agent": "supervisor", "node": "finalize"})
        _emit(out, {"type": "ApprovalRequired", "approval": approval, "agent": "supervisor", "node": "finalize"} if approval else
                    {"type": "ActionNotRequired", "agent": "supervisor", "node": "finalize"})
        _emit(out, {"type": "Finalize", "pipeline": "docker-supervisor-v3", "agent": "supervisor", "node": "finalize"})
        return out

    def after_route(state):
        budget = CURRENT_BUDGET.get()
        if budget and budget.reason: return "facts_finish"
        if (state.get("command") or {}).get("prompt") == "diagnose_network": return "network_diagnose"
        return {"target_selection": "target_selection", "status": "status_observe", "clarify": "human_question", "unavailable": "routing_unavailable", "blocked": "policy_block", "chat": "chat_answer", "education": "education_retrieve", "configuration": "configuration", "change": "configuration"}.get(state["route"], "plan")

    def after_review(state):
        return {"collect": "gate", "analysis": "analysis", "write": "write", "facts": "facts_finish", "done": END}[state["next_step"]]

    def after_validate(state):
        return "finalize" if state.get("validation_ok") or state.get("validation_attempts", 0) >= Settings().max_validation_attempts else "write"

    graph = StateGraph(AgentState)
    graph.add_node("facts_finish", traced_node(facts_finish, scope="supervisor", node="facts_finish", agent="supervisor"))
    graph.add_edge("facts_finish", END)
    graph.add_node("network_diagnose", traced_node(network_diagnose, scope="supervisor", node="network_diagnose", agent="network"))
    graph.add_edge("network_diagnose", END)
    graph.add_node("target_selection", traced_node(target_selection, scope="supervisor", node="target_selection", agent="supervisor"))
    graph.add_node("status_observe", traced_node(status_observe, scope="supervisor", node="status_observe", agent="supervisor"))
    for name, fn in (("route", route), ("human_question", human_question), ("routing_unavailable", routing_unavailable), ("configuration", configuration), ("policy_block", policy_block), ("chat_answer", chat_answer), ("education_retrieve", education_retrieve), ("education_unknown", education_unknown), ("plan", plan), ("gate", gate),
                     ("research", research), ("replan_check", replan_check), ("analysis", analysis), ("write", write), ("validate", validate), ("finalize", finalize)):
        graph.add_node(name, traced_node(fn, scope="supervisor", node=name, agent="supervisor"))
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", after_route, {n: n for n in ("facts_finish", "network_diagnose", "target_selection", "status_observe", "human_question", "routing_unavailable", "policy_block", "chat_answer", "education_retrieve", "plan", "configuration")})
    graph.add_edge("target_selection", END); graph.add_edge("status_observe", END)
    graph.add_edge("human_question", END)
    graph.add_edge("routing_unavailable", END)
    graph.add_edge("configuration", END)
    graph.add_edge("policy_block", END); graph.add_edge("chat_answer", END)
    graph.add_conditional_edges("education_retrieve", lambda state: "write" if state.get("knowledge") else "education_unknown", {"write": "write", "education_unknown": "education_unknown"})
    graph.add_edge("education_unknown", END)
    graph.add_edge("plan", "gate"); graph.add_edge("gate", "research"); graph.add_edge("research", "replan_check")
    graph.add_conditional_edges("replan_check", after_review, {n: n for n in ("gate", "analysis", "write", "facts_finish", END)})
    graph.add_edge("analysis", "write"); graph.add_edge("write", "validate")
    graph.add_conditional_edges("validate", after_validate, {"finalize": "finalize", "write": "write"})
    graph.add_edge("finalize", END)
    return graph.compile(name="docker_supervisor", checkpointer=checkpointer)

