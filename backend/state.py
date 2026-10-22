"""Shared graph state and safe per-turn reset, independent of graph construction."""
from typing import TypedDict

class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict[str, str]]
    route: str
    runtime_operation: str
    inventory_topics: list[str]
    inventory_source: str
    result_kind: str
    route_reason: str
    route_method: str
    route_fallback: bool
    route_context_used: bool
    route_file_intent_check: dict
    clarification: dict | None
    clarification_rounds: int
    confirmed_answer: str | None
    routing_target: dict
    unsafe_request: bool
    tasks: list[dict]
    pending: list[dict]
    evidence: list[dict]
    knowledge: list[dict]
    lesson_selection: dict
    worker_reports: list[dict]
    replan_rounds: int
    evidence_coverage: float
    evidence_sufficient: bool
    diagnosis: dict
    confidence: float
    analysis_policy: str
    analysis_reason: str
    analysis_used: bool
    next_step: str
    analysis: str
    draft: str
    final: str
    validation_attempts: int
    validation_ok: bool
    validation_problems: list
    events: list[dict]
    parallel_elapsed: float
    retry_count: int
    prompt_used: str
    approval: dict | None
    command: dict | None
    proposal: dict | None
    tool_time_sum: float
    observe_wall_sum: float
    requested_project: str | None
    target_selection_required: bool
    semantic_lesson_ids: list | None
    education_context: dict
    config_review: dict
    config_validation: dict
    config_metrics: dict
    subject_status: str
    answer_status: str
    execution_status: str
    lifecycle: str | None
    health: str


def fresh_state(**inputs):
    # Keep conversational history, never reuse a previous turn's tools/approvals/command.
    state = dict.fromkeys(AgentState.__annotations__)
    for key in ("history", "tasks", "pending", "evidence", "knowledge", "worker_reports", "validation_problems", "events"):
        state[key] = []
    for key in ("routing_target", "diagnosis", "education_context", "config_review", "config_validation"):
        state[key] = {}
    for key in ("replan_rounds", "retry_count", "validation_attempts", "clarification_rounds", "parallel_elapsed"):
        state[key] = 0
    for key in ("query", "thread_id", "analysis", "analysis_reason", "draft", "final"):
        state[key] = ""
    for key in ("unsafe_request", "analysis_used", "target_selection_required"):
        state[key] = False
    return state | inputs
