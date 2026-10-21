"""Opt-in, metadata-only LangSmith spans. Never send query/history/files/tool args."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
import os
import hashlib
import langsmith as ls
from langsmith.run_trees import RunTree

CURRENT_TRACE = ContextVar("docker_ops_safe_trace", default=None)


def safe_outputs(value):
    return {"usage_metadata": value["usage_metadata"]} if isinstance(value, dict) and "usage_metadata" in value else {}


@contextmanager
def request_trace(thread_id, provider):
    from . import usage
    ledger = usage.CURRENT_USAGE.get()
    root = None
    if os.getenv("LANGSMITH_TRACING", "false").lower() == "true" and os.getenv("LANGSMITH_API_KEY"):
        try:
            client = ls.Client(hide_inputs=True, hide_outputs=safe_outputs)
            root = RunTree(name="docker_ops.request", run_type="chain", inputs={}, client=client,
                           project_name=os.getenv("LANGSMITH_PROJECT", "docker-ops-agent"),
                           extra={"metadata": {"thread_id": hashlib.sha256(thread_id.encode()).hexdigest(), "provider": provider}})
            root.post()
        except Exception: root = None
    if ledger is not None:
        ledger["langsmith_run_id"] = str(root.id) if root else None
        ledger["langsmith_status"] = "enabled" if root else "disabled_or_unavailable"
    token = CURRENT_TRACE.set({"root": root, "nodes": {}, "provider": provider} if root else None)
    # Explicit safe spans replace automatic raw LangGraph state / traceback uploads.
    with ls.tracing_context(enabled=False):
        try: yield
        finally:
            if root:
                try: root.end(outputs={}); root.patch()
                except Exception: pass
            CURRENT_TRACE.reset(token)


def parent_span():
    trace = CURRENT_TRACE.get()
    if not trace: return None
    from .tracing import CURRENT_SPAN
    return trace["nodes"].get(CURRENT_SPAN.get(), trace["root"])


@contextmanager
def span(name, run_type="chain", metadata=None):
    run = None
    parent = parent_span()
    if parent:
        try: run = parent.create_child(name=name, run_type=run_type, inputs={}, extra={"metadata": metadata or {}}); run.post()
        except Exception: run = None
    try: yield run
    except BaseException as exc:
        if run:
            try: run.end(error=type(exc).__name__); run.patch()
            except Exception: pass
        raise
    else:
        if run:
            try: run.end(outputs={}); run.patch()
            except Exception: pass


def node_started(identity):
    trace = CURRENT_TRACE.get()
    if not trace: return
    try:
        parent = trace["nodes"].get(identity.get("parent_run_id"), trace["root"])
        run = parent.create_child(name=identity["scope"] + "." + identity["node"], run_type="chain", inputs={},
                                  extra={"metadata": {"agent": identity["agent"], "local_span_id": identity["run_id"]}})
        trace["nodes"][identity["run_id"]] = run
        run.post()
    except Exception: pass


def node_finished(identity, error=None):
    trace = CURRENT_TRACE.get()
    run = trace["nodes"].get(identity["run_id"]) if trace else None
    if run:
        try: run.end(outputs={}, error=error); run.patch()
        except Exception: pass


def model_usage(call):
    parent = parent_span()
    trace = CURRENT_TRACE.get()
    if not parent: return
    try:
        now = datetime.now(timezone.utc)
        run = parent.create_child(name="llm." + call["tier"], run_type="llm", inputs={},
            start_time=now-timedelta(milliseconds=call["elapsed_ms"]),
            extra={"metadata": {"ls_provider": trace["provider"], "ls_model_name": call["model"], "role": call["tier"]}})
        run.post()
        tokens = {"input_tokens": call["prompt_tokens"], "output_tokens": call["completion_tokens"]}
        outputs = {"usage_metadata": tokens | {"total_tokens": sum(tokens.values())}} if all(v is not None for v in tokens.values()) else {}
        run.end(outputs=outputs, end_time=now, error="ModelCallFailed" if call["kind"] == "error" else None)
        run.patch()
    except Exception: pass
