"""Aggregate eval/results/eval_<scenario>.json into one Markdown table (no numbers are typed by hand)."""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
ORDER = ["oom", "oom-exited", "readiness", "network", "loopback", "host-port", "healthy", "exit137"]


def load(directory: Path) -> dict[str, dict]:
    return {p.stem.removeprefix("eval_"): json.loads(p.read_text()) for p in directory.glob("eval_*.json")}


def mean(values, default=0.0):
    values = list(values)
    return statistics.mean(values) if values else default


def median(values, default=0.0):
    values = list(values)
    return statistics.median(values) if values else default


def table(data: dict[str, dict]) -> str:
    lines = ["| 시나리오 | 질의 | 라우팅 정답 | 원인 Top-1 | 근거 충족 | 위험 차단 | 평균 토큰 | 도구 동시성 | p50 지연(초) |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    pct = lambda value: "—" if value is None else f"{value * 100:.0f}%"
    for name in [n for n in ORDER if n in data]:
        s = data[name]["summary"]
        lines.append(f"| {name} | {s['cases']} | {pct(s['route_accuracy'])} | {pct(s['root_cause_top1'])} | {pct(s['evidence_coverage'])} | {pct(s['unsafe_action_block_rate'])} | {s['avg_total_tokens']:,.0f} | {s.get('avg_tool_concurrency') or 0:.2f} | {s['latency_p50_ms'] / 1000:.0f} |")
    rows = [r for d in data.values() for r in d["details"]]
    cause = [r for r in rows if r["root_cause_ok"] is not None]
    unsafe = [r for r in rows if r["unsafe"]]
    incident = [r for r in rows if r["tool_calls"] > 0]
    light = [r for r in rows if r["tool_calls"] == 0]
    total = (f"| **합계** | {len(rows)} | {sum(r['route_ok'] for r in rows)}/{len(rows)} | {sum(r['root_cause_ok'] for r in cause)}/{len(cause)} | — | "
             f"{sum(r['blocked'] for r in unsafe)}/{len(unsafe)} | {mean(r['total_tokens'] for r in rows):,.0f} | — | {median(r['latency_ms'] for r in rows) / 1000:.0f} |")
    lines.append(total)
    extra = [f"\n- 장애 진단 경로 {len(incident)}건: 평균 토큰 {mean(r['total_tokens'] for r in incident):,.0f}, 평균 모델 호출 {mean(r['model_calls'] for r in incident):.1f}회(quality {mean(r['quality_calls'] for r in incident):.1f}회), p50 {median(r['latency_ms'] for r in incident) / 1000:.0f}초, 분석 에이전트 사용 {sum(r['analysis_used'] for r in incident)}건",
             f"- 대화·차단 등 도구 없는 경로 {len(light)}건: 평균 토큰 {mean(r['total_tokens'] for r in light):,.0f}, p50 {median(r['latency_ms'] for r in light) / 1000:.1f}초",
             f"- 검증이 2회 이상 걸린 질의 {sum(r['validation_attempts'] > 1 for r in rows)}건, 허구/실패 증거 인용 {sum(r['invalid_citations'] for r in rows)}/{sum(r['citation_count'] for r in rows)}건"]
    wrong = [f"`{r['id']}`" for r in rows if not r["route_ok"] or r["root_cause_ok"] is False]
    extra.append("- 틀린 질의: " + (", ".join(wrong) if wrong else "없음"))
    return "\n".join(lines + extra)


if __name__ == "__main__":
    print(table(load(Path(sys.argv[1]) if len(sys.argv) > 1 else RESULTS)))
