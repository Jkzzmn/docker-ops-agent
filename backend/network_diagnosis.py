"""Bounded read-only network checklist; structured observations need no LLM rewrite."""
import asyncio
import time

from .events import emit
from .observations import observation_status
from .sanitize import redact


async def diagnose_network(mcp, source, target, events):
    evidence, started = [], time.perf_counter()

    async def observe(tool, args):
        tick = time.perf_counter()
        try:
            # One attempt, five seconds per observation, twenty seconds overall.
            remaining = 20 - (tick - started)
            if remaining <= 0: raise TimeoutError("진단 시간 한도 초과")
            value, _ = await asyncio.wait_for(mcp.tool(tool, args, attempts=1), min(5, remaining))
            if not isinstance(value, dict): value = {"ok": False, "error": "invalid tool response"}
        except Exception as exc:
            value = {"ok": False, "error": redact(str(exc) or type(exc).__name__)}
        item = {"id": f"E{len(evidence)+1}", "tool": tool, "args": args, "observation": value,
                "elapsed_ms": (time.perf_counter()-tick)*1000, "agent": "network"}
        evidence.append(item)
        emit(events, {"type": "TaskCompleted" if observation_status(value) == "complete" else "TaskFailed",
                      "id": item["id"], "target": tool, "retries": 0, "agent": "network", "node": "network_diagnose"})
        return value, item["id"]

    def finish(text, cause="unknown", subject="present", complete=True):
        emit(events, {"type": "Finalize", "pipeline": "network-checklist", "agent": "supervisor", "node": "network_diagnose"})
        return {"final": f"{source} → {target} 연결 점검\n\n{text}\n\n변경은 수행하지 않았습니다.", "events": events,
                "evidence": evidence, "approval": None, "proposal": None, "analysis_used": False, "analysis_policy": "skip",
                "analysis_reason": "고정된 읽기 전용 네트워크 체크리스트: 계획·재계획·Writer·LLM 검증 생략",
                "validation_ok": complete, "validation_attempts": 0, "replan_rounds": 0, "retry_count": 0,
                "evidence_sufficient": complete, "evidence_coverage": None, "subject_status": subject,
                "answer_status": "validated" if complete else "partial", "execution_status": "completed",
                "diagnosis": {"cause": cause, "evidence_ids": [e["id"] for e in evidence], "corroborated": False},
                "tool_time_sum": sum(e["elapsed_ms"] for e in evidence)/1000, "observe_wall_sum": time.perf_counter()-started}

    listing, eid = await observe("list_containers", {})
    if observation_status(listing) != "complete" or not isinstance(listing.get("containers"), list) or any(not isinstance(r, dict) or not isinstance(r.get("service"), str) for r in listing["containers"]):
        return finish(f"컨테이너 목록 조회에 실패했습니다. 존재 여부나 연결 상태는 알 수 없습니다. [{eid}]", subject="unknown", complete=False)
    rows = {name: [r for r in listing["containers"] if r.get("service") == name] for name in (source, target)}
    missing = [name for name, items in rows.items() if not items]
    if missing:
        states = ", ".join(f"{name}: {items[0].get('state', 'unknown')}" if len(items) == 1 else f"{name}: {len(items)}개" for name, items in rows.items() if items)
        return finish(f"현재 선택 프로젝트에서 {', '.join(missing)} 대응 컨테이너가 없습니다. [{eid}]" + (f"\n다른 대상 관측: {states}. [{eid}]" if states else "") + "\n실행 중인 두 컨테이너가 있어야 실제 DNS·TCP 연결을 검사할 수 있어 여기서 종료했습니다. 네트워크 장애로 판정하지 않습니다. 프로젝트 선택과 배포 여부를 먼저 확인해주세요.", "not_deployed", "absent")
    if any(len(items) != 1 for items in rows.values()):
        return finish(f"대응 컨테이너가 여러 개여서 단일 연결을 선택하지 않았습니다. [{eid}]", subject="ambiguous", complete=False)
    stopped = [f"{name}: {items[0].get('state', 'unknown')}" for name, items in rows.items() if items[0].get("state") != "running"]
    if stopped:
        return finish(f"실행 중이 아닌 대상: {', '.join(stopped)}. [{eid}]\nDNS·TCP 검사를 생략했습니다. 컨테이너 실행 상태를 먼저 확인해야 하며, 중지 사실만으로 네트워크 원인을 단정하지 않습니다.", "not_running")
    pair = {"source": source, "target": target}
    network, eid = await observe("inspect_network", pair)
    if network.get("ok") is not True or not isinstance(network.get("shared_networks"), list):
        return finish(f"공유 네트워크 조회를 완료하지 못했습니다. [{eid}]", complete=False)
    if not network["shared_networks"]:
        return finish(f"두 컨테이너의 공유 Docker 네트워크가 없습니다. [{eid}]\n일반적인 Compose 서비스명 통신을 위해 동일 네트워크 연결을 검토해주세요. host 모드·외부 경로 통신 여부는 별도 확인이 필요합니다.", "network")
    dns, eid = await observe("test_container_dns", pair)
    if dns.get("ok") is not True or type(dns.get("resolved")) is not bool:
        return finish(f"DNS probe를 실행하지 못했습니다(python3 미설치 등 가능). DNS 장애로 단정하지 않습니다. [{eid}]", complete=False)
    if dns["resolved"] is False:
        return finish(f"{source}에서 {target} 서비스명 DNS 해석에 실패했습니다. 서비스명·네트워크 alias를 확인해주세요. [{eid}]", "network")
    config, eid = await observe("validate_compose_config", {})
    ports = config.get("ports", {}).get(target, []) if config.get("ok") is True else []
    ports = list(dict.fromkeys(p for p in ports if type(p) is int and 1 <= p <= 65535))
    if len(ports) != 1:
        return finish(f"공유 네트워크와 DNS 해석은 확인했습니다. 대상의 선언된 TCP 포트를 하나로 확정하지 못해 연결 검사는 생략했습니다(선언: {ports}). 애플리케이션 연결 성공을 의미하지 않습니다. [{eid}]", complete=False)
    tcp, eid = await observe("test_tcp_connection", pair | {"port": ports[0]})
    if tcp.get("ok") is not True or type(tcp.get("connected")) is not bool:
        return finish(f"TCP probe를 완료하지 못했습니다. 연결 실패로 단정하지 않습니다. [{eid}]", complete=False)
    if tcp["connected"] is False:
        return finish(f"DNS는 해석되지만 {target}:{ports[0]} TCP 연결에 실패했습니다. 리스너·포트·서비스 준비 상태를 확인해주세요. 정확한 원인은 아직 확정하지 않았습니다. [{eid}]", "network")
    return finish(f"{target}:{ports[0]} TCP 연결에 성공했습니다. 이 결과는 해당 방향·포트의 현재 연결만 확인하며 인증·업무 요청 성공까지 보장하지 않습니다. [{eid}]", "tcp_reachable")
