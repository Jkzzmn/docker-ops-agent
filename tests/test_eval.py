from eval.run_eval import score, summarize


def test_unsafe_metric_uses_only_unsafe_denominator_and_requires_policy_event():
    base = {"id": "safe", "scenario": "healthy", "expected_route": "incident", "expected_cause": "healthy"}
    body = {"reply": "현재 정상 [E1]", "approval": None, "events": [],
            "evidence": [{"id": "E1", "tool": "check_service_health", "observation": {"ok": True}}],
            "metadata": {"route": "incident", "root_cause": "healthy", "supervisor_version": "v3", "evidence_count": 1,
                         "replan_rounds": 1, "validation_attempts": 1, "llm_backend": "ollama", "fast_model": "test-model", "quality_model": "test-model", "mutation_calls": 0, "policy_blocked": True}}
    safe = score(base, body, .1)
    unsafe = score(base | {"unsafe": True}, body, .1)
    assert unsafe["blocked"] is False
    result = summarize([safe, unsafe])
    assert result["unsafe_cases"] == 1
    assert result["unsafe_action_block_rate"] == 0
    assert result["unsupported_claim_rate"] is None
