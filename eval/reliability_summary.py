"""Summarize recorded checks, independent target checks and retrieval annotations."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
from .reliability_live import CASES


def percentiles(values):
    if not values: return None
    ordered = sorted(values)
    # P95 uses nearest rank; P50 is the ordinary sample median.
    import math
    return {"n": len(values), "p50_ms": round(statistics.median(values), 1), "p95_ms": round(ordered[math.ceil(len(values)*.95)-1], 1)}


def summarize(path):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    groups = {}
    wrong_scope, wrong_service = [], []
    oracle = {c["id"]: c for c in CASES}
    recall_n = recall_d = precision_n = precision_d = 0
    for row in rows:
        case, body = row["case"], row.get("response", {})
        m = body.get("metadata", {})
        group = groups.setdefault(case["group"], {"attempts": 0, "recorded_passed": 0, "failures": [], "elapsed": [], "successful_elapsed": [], "routes": Counter()})
        group["attempts"] += 1; group["recorded_passed"] += bool(row["passed"])
        group["elapsed"].append(row["elapsed_ms"]); group["routes"][m.get("route", "http_failure")] += 1
        if row["passed"]: group["successful_elapsed"].append(row["elapsed_ms"])
        else: group["failures"].append({"id": case["id"], "repeat": row["repeat"], "checks": row["checks"], "reason": m.get("route_reason") or body.get("evaluation_error")})
        if case["route"] == "target_selection" and m.get("tool_calls", 0): wrong_scope.append([case["id"], row["repeat"]])
        expected_service = case.get("service") or oracle.get(case["id"], {}).get("service")
        if expected_service and m.get("tool_calls", 0) and [e.get("args", {}).get("service") for e in body.get("evidence", [])] != [expected_service]:
            wrong_service.append([case["id"], row["repeat"]])
        if "ids" in case:
            actual, expected = set(m.get("knowledge_ids", [])), set(case["ids"])
            recall_n += len(actual & expected); recall_d += len(expected)
            precision_n += len(actual & expected); precision_d += len(actual)
    for group in groups.values():
        group["latency_all"] = percentiles(group.pop("elapsed"))
        group["latency_successful"] = percentiles(group.pop("successful_elapsed"))
    return {"source": str(path), "attempts": len(rows), "recorded_passed": sum(r["passed"] for r in rows), "groups": groups,
            "wrong_scope_reads": wrong_scope, "wrong_service_reads": wrong_service,
            "education_recall_at_3": recall_n/recall_d if recall_d else None, "education_precision_at_3": precision_n/precision_d if precision_d else None,
            "recall_denominator_card_count": recall_d, "precision_denominator_returned_card_count": precision_d,
            "note": "Recorded pass reflects that run's frozen checker. Wrong service is additionally audited from annotated expected tool arguments. No model self-grading. Latency includes first requests and failures; no fresh pre-change baseline."}


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("paths", nargs="+")
    args = p.parse_args()
    print(json.dumps([summarize(path) for path in args.paths], ensure_ascii=False, indent=2))
