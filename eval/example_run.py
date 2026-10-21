"""Run ONE real request against a running API and record every input and output.

Writes the raw capture (eval/results/*.json) and a Markdown transcript (docs/EXAMPLE_RUN.md) that lists, in execution
order: every graph node's full input/output, every MCP tool call and result, every model prompt/response (when the
server runs with RECORD_LLM_IO=1), the raw SSE order, the final answer and the approval/recovery round trip.

The server must be started with RECORD_LLM_IO=1 to include model prompts. Approval is only sent with --approve.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FENCE = "````"


def capture(base_url: str, query: str, thread_id: str) -> dict:
    sse, started = [], time.perf_counter()
    with httpx.Client(base_url=base_url, timeout=900) as client:
        health = client.get("/health").json()
        with client.stream("GET", "/chat/stream", params={"message": query, "thread_id": thread_id}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                at = round((time.perf_counter() - started) * 1000, 1)
                sse.append({"t_ms": at, "event": "[DONE]" if payload == "[DONE]" else json.loads(payload)})
    return {"health": health, "sse": sse, "wall_ms": round((time.perf_counter() - started) * 1000, 1)}


def approve(base_url: str, approval_id: str) -> dict:
    started = time.perf_counter()
    with httpx.Client(base_url=base_url, timeout=300) as client:
        response = client.post(f"/actions/{approval_id}/approve")
    return {"http_status": response.status_code, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "body": response.json()}


def docker_state(service: str) -> dict:
    """Independent check with the Docker CLI, not through the agent."""
    ids = subprocess.run(["docker", "compose", "ps", "--all", "-q", service], capture_output=True, text=True, cwd=ROOT / "demo_stack")
    if ids.returncode or not ids.stdout.strip():
        return {"error": ids.stderr.strip() or "container not found"}
    fmt = "{{json .State}}|{{.RestartCount}}"
    out = subprocess.run(["docker", "inspect", "--format", fmt, ids.stdout.split()[0]], capture_output=True, text=True)
    state, restarts = out.stdout.strip().rsplit("|", 1)
    keep = ("Status", "Running", "OOMKilled", "ExitCode", "StartedAt", "FinishedAt", "Health")
    value = json.loads(state)
    return {"State": {k: (value[k]["Status"] if k == "Health" and k in value else value.get(k)) for k in keep}, "RestartCount": int(restarts)}


def pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def block(value, lang="json") -> str:
    text = value if isinstance(value, str) else pretty(value)
    return f"{FENCE}{lang}\n{text}\n{FENCE}"


def details(summary: str, body: str) -> str:
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>\n"


def summary_block(run: dict, final: dict, meta: dict) -> str:
    result = (run.get("approval_result") or {}).get("body", {})
    after = run.get("docker_after", {})
    later = list(after.values())[-1].get("State", {}) if len(after) > 1 else {}
    usage = meta.get("llm_usage", {})
    diagnosis = final.get("diagnosis") or {}
    rows = [("분류", f"`{meta.get('route')}` ({meta.get('route_method')})"), ("원인 판정", f"`{diagnosis.get('cause')}` · 신뢰도 {diagnosis.get('confidence')} · 근거 {', '.join(diagnosis.get('evidence_ids', []))} · 교차 확인 {diagnosis.get('corroborated')}"),
            ("근거 충족 / 재계획", f"{round(meta.get('evidence_coverage', 0) * 100)}% / {meta.get('replan_rounds')}회"), ("MCP 도구 호출", f"{meta.get('tool_calls')}회 (동시 실행 비율 {meta.get('tool_concurrency')}배)"),
            ("모델 호출", f"{usage.get('model_calls')}회 (fast {usage.get('fast_calls')} · quality {usage.get('quality_calls')}) · 토큰 {usage.get('total_tokens'):,}" if usage.get("total_tokens") else str(usage.get("model_calls"))),
            ("검증", f"{'통과' if meta.get('validation_ok') else '불합격'} ({meta.get('validation_attempts')}회 시도)"), ("승인 요청", f"`{final['approval']['tool']}` {final['approval']['args']}" if final.get("approval") else "없음")]
    if result:
        rows += [("승인 후 확인", f"status `{result.get('status')}` · recovery_ok `{result.get('recovery_ok')}` · {result.get('waited_seconds')}초 대기"),
                 ("조치 전 → 후 (inspect)", f"`{result.get('inspect_before')}` → `{result.get('inspect_after')}`")]
        if later:
            rows.append(("승인 뒤 늦은 독립 확인", f"`{later.get('Status')}` · OOMKilled `{later.get('OOMKilled')}` — 재시작은 임시 복구이며 결함이 남아 있으면 다시 종료됩니다"))
    return "### 요약\n\n| 항목 | 값 |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows) + "\n"


def render(run: dict) -> str:
    sse = run["sse"]
    events = [x["event"] for x in sse if x["event"] != "[DONE]"]
    final = next((e for e in events if e.get("type") == "FinalAnswer"), {})
    meta = final.get("metadata", {})
    starts = [e for e in events if e.get("type") == "NodeStarted"]
    ends = {e["run_id"]: e for e in events if e.get("type") in {"NodeCompleted", "NodeFailed"}}
    depth = {}
    for e in starts:
        depth[e["run_id"]] = depth.get(e.get("parent_run_id"), -1) + 1
    health = run["health"]
    out = [f"실행 시각: {run['created_at']} (UTC) · 전체 소요 {run['wall_ms'] / 1000:.1f}초 · 모델 {health['llm_backend']} `{health['fast_model']}` / `{health['quality_model']}` · Docker `{health['docker_mode']}` · 대상 프로젝트 `{health['docker_target']['project']}` (서비스 {', '.join(health['docker_target']['services'])})\n",
           summary_block(run, final, meta),
           "### 0. 입력\n", f"- 질문: `{run['query']}`", f"- thread_id: `{run['thread_id']}`", "- 호출: `GET /chat/stream?message=…&thread_id=…` (SSE)\n",
           "### 1. 실행된 노드 (실행 순서)\n", "들여쓰기는 부모-자식 호출 관계입니다. 실행하지 않은 노드는 나열하지 않습니다.\n",
           "| # | 노드 | agent | 소요(ms) | 상태 |", "|---:|---|---|---:|---|"]
    for i, e in enumerate(starts, 1):
        end = ends.get(e["run_id"], {})
        out.append(f"| {i} | {'&nbsp;&nbsp;' * depth[e['run_id']]}`{e['scope']}.{e['node']}` | {e['agent']} | {end.get('elapsed_ms', '—')} | {'완료' if end.get('type') == 'NodeCompleted' else '실패' if end else '미완료'} |")
    out += ["", "### 2. 노드별 입력과 출력 (전체)\n", "각 항목을 펼치면 해당 순간의 state 전체(입력)와 노드가 반환한 변경분(출력)을 볼 수 있습니다. 비밀 패턴은 마스킹되고 24,000자를 넘으면 `truncated`로 표시됩니다.\n"]
    for i, e in enumerate(starts, 1):
        end = ends.get(e["run_id"], {})
        body = f"run_id `{e['run_id']}` · parent `{e.get('parent_run_id') or 'root'}`\n\n**입력**\n\n{block(e.get('input'))}\n\n**출력**\n\n{block(end.get('output', end.get('error', '출력 없음')))}"
        out.append(details(f"{i}. {e['scope']}.{e['node']} — {end.get('elapsed_ms', '—')}ms", body))
    evidence = final.get("evidence", [])
    out += ["### 3. MCP 도구 호출과 결과 (증거 원장)\n", "| ID | worker | 도구 | 인자 | 재시도 | 소요(ms) | 성공 |", "|---|---|---|---|---:|---:|---|"]
    for item in evidence:
        ok = item["observation"].get("ok", True) is not False
        out.append(f"| {item['id']} | {item.get('agent', 'supervisor')} | `{item['tool']}` | `{json.dumps(item['args'], ensure_ascii=False)}` | {item.get('retries', '—')} | {item.get('elapsed_ms', '—')} | {'✅' if ok else '❌'} |")
    out.append("")
    for item in evidence:
        out.append(details(f"{item['id']} · {item['tool']} 결과 원문", block(item["observation"])))
    calls = final.get("llm_calls") or []
    out += ["### 4. 모델 호출 입력/출력\n"]
    if calls:
        out += ["| # | 종류 | 모델 | 입력 토큰 | 출력 토큰 | 소요(ms) |", "|---:|---|---|---:|---:|---:|"]
        out += [f"| {c['index']} | {c['kind']}/{c['tier']} | `{c['model']}` | {c['prompt_tokens']} | {c['completion_tokens']} | {c['elapsed_ms']} |" for c in calls]
        out.append("")
        for c in calls:
            out.append(details(f"모델 호출 {c['index']} · {c['kind']} · {c['model']}", f"**system**\n\n{block(c['system'], 'text')}\n\n**prompt**\n\n{block(c['prompt'], 'text')}\n\n**response**\n\n{block(c['response'], 'text')}"))
    else:
        out.append("서버가 `RECORD_LLM_IO=1` 없이 실행되어 프롬프트 원문은 기록되지 않았습니다.\n")
    kinds = {}
    for x in sse:
        name = "[DONE]" if x["event"] == "[DONE]" else x["event"]["type"]
        kinds[name] = kinds.get(name, 0) + 1
    out += ["### 5. SSE 이벤트 (수신 순서)\n", f"총 {len(sse)}개. 종류별 개수: " + ", ".join(f"`{k}` {v}" for k, v in kinds.items()) + "\n",
            details("업무 이벤트 순서 (NodeStarted/NodeCompleted/AnswerToken 제외)", "\n".join(
                f"- t+{x['t_ms']}ms `{x['event']['type']}` {x['event'].get('agent', '')}.{x['event'].get('node', '')} " + json.dumps({k: v for k, v in x['event'].items() if k not in {'type', 'agent', 'node', 'event_id', 'timestamp', 'namespace', 'metadata', 'evidence', 'reply', 'knowledge', 'prompt'}}, ensure_ascii=False)[:300]
                for x in sse if x["event"] != "[DONE]" and x["event"]["type"] not in {"NodeStarted", "NodeCompleted", "AnswerToken", "FinalAnswer"}))]
    tokens = [x for x in sse if x["event"] != "[DONE]" and x["event"]["type"] == "AnswerToken"]
    out += [f"`AnswerToken` {len(tokens)}개가 도착한 시각: 처음 t+{tokens[0]['t_ms'] if tokens else '—'}ms, 마지막 t+{tokens[-1]['t_ms'] if tokens else '—'}ms (응답이 끝난 뒤 한꺼번에 오지 않고 생성 중에 흘러옴)\n",
            "### 6. 최종 응답\n", f"{FENCE}text\n{final.get('reply', '')}\n{FENCE}\n", "**진단(diagnosis)**\n", block(final.get("diagnosis")), "\n**설정 변경 제안(proposal, 적용되지 않음)**\n", block(final.get("proposal")),
            "\n**승인 요청(approval)**\n", block(final.get("approval")), "\n" + details("실행 메타데이터 전체", block(meta))]
    result = run.get("approval_result")
    out += ["### 7. 승인 → 재시작 → 검증\n"]
    if result:
        body = result["body"]
        out += [f"요청: `POST /actions/{{approval_id}}/approve` → HTTP {result['http_status']} ({result['elapsed_ms'] / 1000:.1f}초 소요)\n", block(body), ""]
        if run.get("docker_after"):
            out += ["**Docker CLI 독립 확인** (에이전트를 거치지 않은 `docker inspect`)\n", "| 시점 | 상태 |", "|---|---|"]
            out += [f"| {label} | `{json.dumps(value, ensure_ascii=False)}` |" for label, value in run["docker_after"].items()]
            out.append("")
    else:
        out.append("이 실행은 `--approve` 없이 진행되어 승인 요청에서 멈췄습니다. 변경 작업은 실행되지 않았습니다.\n")
    if run.get("metrics"):
        out += ["### 8. 누적 지표 (`GET /metrics`)\n", block(run["metrics"])]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--query", default="api 컨테이너가 계속 종료돼요. 현재 원인을 점검해주세요")
    parser.add_argument("--approve", action="store_true", help="send the approval for the proposed restart")
    parser.add_argument("--settle", type=float, default=15, help="seconds to wait before the final independent docker check")
    parser.add_argument("--json-out", type=Path, default=ROOT / "eval" / "results" / "example_live_oom.json")
    parser.add_argument("--md-out", type=Path, default=ROOT / "docs" / "EXAMPLE_RUN.md")
    parser.add_argument("--from-json", type=Path, help="re-render a previous capture instead of calling the server")
    parser.add_argument("--readme", action="store_true", help="also inject the transcript between the EXAMPLE_RUN markers in README.md")
    args = parser.parse_args()
    if args.from_json:
        run = json.loads(args.from_json.read_text())
    else:
        thread = f"example-{int(time.time())}"
        run = {"created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "query": args.query, "thread_id": thread}
        run["docker_before"] = {"api": docker_state("api")}
        run |= capture(args.base_url, args.query, thread)
        final = next((x["event"] for x in run["sse"] if x["event"] != "[DONE]" and x["event"].get("type") == "FinalAnswer"), {})
        approval = final.get("approval")
        run["docker_after"] = {"에이전트 실행 전": run["docker_before"]["api"]}
        if approval and args.approve:
            run["approval_result"] = approve(args.base_url, approval["id"])
            run["docker_after"]["승인 직후"] = docker_state(approval["args"]["service"])
            time.sleep(args.settle)
            run["docker_after"][f"승인 {args.settle:.0f}초 뒤"] = docker_state(approval["args"]["service"])
        run["metrics"] = httpx.get(f"{args.base_url}/metrics").json()
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    # The home directory of whoever ran the example is not part of the evidence.
    anon = lambda text: text.replace(str(ROOT), "<repo>")
    args.json_out.write_text(anon(pretty(run)))
    markdown = anon(render(run))
    readme_markdown = re.sub(r"(?m)^### ", "#### ", markdown)
    docs_markdown = re.sub(r"(?m)^### ", "## ", markdown)
    args.md_out.write_text("# 실제 실행 예시 (자동 생성)\n\n`eval/example_run.py`가 실행 중인 서버에 요청 하나를 보내고 기록한 원문입니다. 값은 손으로 고치지 않았습니다.\n\n" + docs_markdown)
    if args.readme:
        readme = (ROOT / "README.md").read_text()
        start, end = "<!-- EXAMPLE_RUN:START -->", "<!-- EXAMPLE_RUN:END -->"
        head, rest = readme.split(start, 1)
        _, tail = rest.split(end, 1)
        (ROOT / "README.md").write_text(head + start + "\n\n" + readme_markdown + "\n\n" + end + tail)
    print(f"saved {args.json_out} ({args.json_out.stat().st_size // 1024} KB), {args.md_out} ({args.md_out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
