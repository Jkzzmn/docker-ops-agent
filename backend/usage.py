"""Per-request model usage ledger (tokens, latency, optional prompt/response capture) carried in a ContextVar."""
from __future__ import annotations

from contextvars import ContextVar

CURRENT_USAGE: ContextVar[dict | None] = ContextVar("docker_ops_llm_usage", default=None)


def begin(capture_io: bool = False) -> dict:
    ledger = {"calls": [], "capture_io": capture_io}
    CURRENT_USAGE.set(ledger)
    return ledger


def record(kind: str, model: str, system: str, prompt: str, response: str, prompt_tokens: int | None,
           completion_tokens: int | None, elapsed_ms: float, quality: bool, role: str | None = None) -> None:
    ledger = CURRENT_USAGE.get()
    if ledger is None:
        return
    call = {"index": len(ledger["calls"]) + 1, "kind": kind, "model": model, "tier": role or ("quality" if quality else "fast"),
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "elapsed_ms": round(elapsed_ms, 1)}
    if ledger["capture_io"]:
        call |= {"system": system, "prompt": prompt, "response": response}
    ledger["calls"].append(call)
    from .observability import model_usage
    model_usage(call)


def summarize(ledger: dict | None) -> dict:
    calls = (ledger or {}).get("calls", [])
    known = [c for c in calls if c["prompt_tokens"] is not None and c["completion_tokens"] is not None]
    complete = len(known) == len(calls)
    return {"model_calls": len(calls), "router_calls": sum(c["tier"] == "router" for c in calls), "fast_calls": sum(c["tier"] == "fast" for c in calls), "quality_calls": sum(c["tier"] == "quality" for c in calls),
            "prompt_tokens": sum(c["prompt_tokens"] for c in known) if complete else None, "completion_tokens": sum(c["completion_tokens"] for c in known) if complete else None,
            "total_tokens": sum(c["prompt_tokens"] + c["completion_tokens"] for c in known) if complete else None,
            "reported_tokens": sum(c["prompt_tokens"] + c["completion_tokens"] for c in known) if known or not calls else None,
            "model_time_ms": round(sum(c["elapsed_ms"] for c in calls), 1),
            "tokens_reported_for": f"{len(known)}/{len(calls)} calls"}
