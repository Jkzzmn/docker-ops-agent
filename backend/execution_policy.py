"""Per-request budgets shared by concurrent graph children; no mutation retries."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
import uuid
import re
from .observations import observation_status, health_status
from .sanitize import redact

CURRENT_BUDGET = ContextVar("docker_ops_request_budget", default=None)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class RequestBudget:
    seconds: float = 60
    model_limit: int = 8
    tool_limit: int = 24
    started: float = field(default_factory=time.perf_counter)
    model_calls: int = 0
    tool_calls: int = 0
    reason: str | None = None

    def remaining(self):
        return max(0, self.seconds - (time.perf_counter() - self.started))

    def reserve(self, kind):
        if self.remaining() <= 0: self.reason = "request_timeout"
        field_name = kind + "_calls"
        if getattr(self, field_name) >= getattr(self, kind + "_limit"): self.reason = kind + "_call_limit"
        if self.reason: raise BudgetExceeded(self.reason)
        setattr(self, field_name, getattr(self, field_name) + 1)

    def summary(self):
        return {"seconds": self.seconds, "model_limit": self.model_limit, "tool_limit": self.tool_limit,
                "model_calls": self.model_calls, "tool_calls": self.tool_calls, "stop_reason": self.reason}


def facts_only(state, reason):
    lines = []
    for item in state.get("evidence", []):
        value, eid = item.get("observation", {}), item["id"]
        name = item.get("args", {}).get("service") or item["tool"]
        if observation_status(value) != "complete":
            lines.append(f"- {name}: 조회 미완료 [{eid}]")
        elif value.get("subject_status") == "absent":
            lines.append(f"- {name}: 대응 컨테이너 없음 [{eid}]")
        elif item["tool"] == "list_containers":
            lines.extend(f"- {row.get('service')}: {row.get('state', 'unknown')} [{eid}]" for row in value.get("containers", []))
            missing = value.get("configured_services_without_containers", [])
            if missing: lines.append(f"- 대응 컨테이너 없음: {', '.join(missing)} [{eid}]")
        elif item["tool"] in {"inspect_container", "check_service_health"}:
            lines.append(f"- {name}: {value.get('state', 'unknown')} · health {health_status(value)} [{eid}]")
            details = [f"{label}={value[key]}" for key, label in (("exit_code", "ExitCode"), ("oom_killed", "OOMKilled"), ("restarts", "RestartCount")) if key in value]
            if details: lines.append(f"- {name}: {' · '.join(details)} [{eid}]")
        elif item["tool"] == "get_container_logs":
            log = value.get("logs", value.get("stdout", ""))
            excerpt = ("[앞부분 생략; 최근 끝부분] " if len(log) > 900 else "") + redact(log[-900:], 900)
            lines.append(f"- {name} 최근 로그: {excerpt if log else '수집 범위 내 로그 없음'} [{eid}]")
        elif item["tool"] == "get_container_events":
            lines.append(f"- {name} 조회 기간 내 OOM 이벤트: {len(value.get('events', []))}건 (기간 밖 이력은 미확인) [{eid}]")
        elif item["tool"] == "get_container_stats":
            lines.append(f"- {name} 현재 CPU {value.get('cpu_percent', '미확인')}% · 메모리 {value.get('memory_usage', '미확인')} ({value.get('memory_percent', '미확인')}%) [{eid}]")
        elif item["tool"] == "test_container_dns" and type(value.get("resolved")) is bool:
            lines.append(f"- DNS 해석: {value['resolved']} [{eid}]")
        elif item["tool"] == "test_tcp_connection" and type(value.get("connected")) is bool:
            lines.append(f"- TCP 연결: {value['connected']} [{eid}]")
        elif item["tool"] == "validate_compose_config":
            lines.append(f"- Compose 구문 검사: {value.get('valid', 'unknown')} (실행 성공 여부 아님) [{eid}]")
    memory_note = "\n\nExitCode=137만으로 OOM을 확정하지 않습니다. OOMKilled와 조회 기간의 이벤트를 구분합니다. 현재 stats는 종료 당시 사용량이 아니며, 과거 메모리 시계열은 저장하지 않아 당시 메모리 사용량을 복원할 수 없습니다." if re.search(r"oom|memory|메모리", state.get("query", ""), re.I) else ""
    return {"final": "확인된 관측만 표시합니다.\n" + ("\n".join(lines[:24]) or "완료된 상태 관측이 없습니다.")
            + memory_note
            + "\n\n추가 분석은 중단했습니다. 원인·복구 성공은 확정하지 않았으며 변경은 수행하지 않았습니다.",
            "approval": None, "proposal": None, "clarification": None, "target_selection_required": False,
            "answer_status": "partial", "execution_status": "completed", "validation_ok": False,
            "evidence_coverage": None, "evidence_sufficient": False, "analysis_reason": reason}


def align_answer(state):
    """A correct observation in the wrong workflow is not an answer to this request."""
    expected = {"inventory": "configuration_inventory", "runtime": "runtime_action", "configuration": "configuration_review", "change": "configuration_review"}.get(state.get("route"))
    if state.get("final") and expected and state.get("result_kind") != expected:
        return state | {"final": "요청한 작업에 맞는 결과를 만들지 못했습니다. 다른 종류의 검토 답변으로 대신 처리하지 않습니다. 변경은 수행하지 않았습니다.",
                        "approval": None, "proposal": None, "clarification": None, "answer_status": "unavailable", "validation_ok": False}
    return state


async def execute(graph, initial, config, settings):
    from . import usage
    from .observability import request_trace
    budget = RequestBudget(settings.request_seconds, settings.max_model_calls, settings.max_tool_calls)
    token = CURRENT_BUDGET.set(budget)
    latest, events = dict(initial), {}
    try:
        with request_trace(config["configurable"]["thread_id"], settings.backend):
            try:
                async with asyncio.timeout(budget.remaining()):
                    async for namespace, mode, chunk in graph.astream(initial, config=config, stream_mode=["custom", "values"], subgraphs=True):
                        if mode == "custom": events[chunk["event_id"]] = chunk
                        elif not namespace:
                            chunk = align_answer(chunk)
                            latest = chunk
                        yield namespace, mode, chunk
            except (BudgetExceeded, TimeoutError) as exc:
                budget.reason = budget.reason or ("request_timeout" if isinstance(exc, TimeoutError) else str(exc))
            if budget.reason:
                # Only spans that really started are marked stopped, after graph cancellation.
                finished = {e["run_id"] for e in events.values() if e["type"] in {"NodeCompleted", "NodeFailed"}}
                for event in list(events.values()):
                    if event["type"] == "NodeStarted" and event["run_id"] not in finished:
                        stopped = {k:v for k,v in event.items() if k != "input"} | {"type": "NodeFailed", "error": budget.reason, "timestamp": time.time(), "event_id": uuid.uuid4().hex}
                        events[stopped["event_id"]] = stopped
                        yield (), "custom", stopped
                latest = latest | facts_only(latest, budget.reason) | {"events": list(events.values())}
                yield (), "values", latest
    finally:
        if usage.CURRENT_USAGE.get() is not None: usage.CURRENT_USAGE.get()["execution_budget"] = budget.summary()
        CURRENT_BUDGET.reset(token)
