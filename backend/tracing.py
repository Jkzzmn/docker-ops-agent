"""Actual node lifecycle and redacted state snapshots, not a simulated progress bar."""
import json
import time
import uuid
import re
from contextvars import ContextVar

from .events import emit
from .sanitize import redact


CURRENT_SPAN = ContextVar("docker_ops_node_span", default=None)


def snapshot(value: dict) -> dict:
    def clean(item):
        if isinstance(item, dict):
            return {k: "[REDACTED]" if re.search(r"password|passwd|secret|api[_-]?key|access_token", str(k), re.I) else clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        return redact(item, limit=len(item)) if isinstance(item, str) else item
    text = json.dumps(clean({k: v for k, v in value.items() if k != "events"}), ensure_ascii=False, default=str)
    if len(text) > 24000:
        return {"truncated": True, "limit_chars": 24000, "preview": text[:24000]}
    return json.loads(text)


def traced_node(fn, *, scope: str, node: str, agent: str, output_keys: tuple[str, ...] | None = None):
    async def run(state):
        span = uuid.uuid4().hex
        parent = CURRENT_SPAN.get()
        token = CURRENT_SPAN.set(span)
        events = list(state.get("events", []))
        identity = {"run_id": span, "parent_run_id": parent, "scope": scope, "node": node, "agent": agent}
        started = time.perf_counter()
        emit(events, identity | {"type": "NodeStarted", "input": snapshot(state)})
        from .observability import node_started, node_finished
        node_started(identity)
        failure = None
        try:
            result = await fn(dict(state, events=events))
            # A child may start a fresh business-event ledger. Keep its real
            # NodeStarted too, so JSON and streamed traces describe the same run.
            ledger = events + list(result.get("events", []))
            visible_output = {k: v for k, v in result.items() if k in output_keys} if output_keys else result
            emit(ledger, identity | {"type": "NodeCompleted", "output": snapshot(visible_output), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
            # Parallel children return cumulative ledgers; retain each event only once.
            unique = {event["event_id"]: event for event in ledger}
            result["events"] = sorted(unique.values(), key=lambda event: event["timestamp"])
            return result
        except BaseException as exc:
            failure = type(exc).__name__
            emit(events, identity | {"type": "NodeFailed", "error": redact(str(exc) or type(exc).__name__), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
            raise
        finally:
            node_finished(identity, failure)
            CURRENT_SPAN.reset(token)
    return run
