"""Status evidence is usable even when a complete lookup found no subject."""


def observation_status(value):
    if value.get("ok") is not True: return "failed"
    return value.get("observation_status", "complete")


def subject_status(value):
    return value.get("subject_status", "present" if value.get("ok") is True and value.get("found", True) else "unknown")


def health_status(value):
    if value.get("health") in {"healthy", "unhealthy", "starting"}:
        return value["health"]
    return "not_configured" if value.get("health_configured") is False else "unknown"


def status_answer(item):
    value, eid = item["observation"], item["id"]
    complete, subject = observation_status(value) == "complete", subject_status(value)
    service = item["args"].get("service", "프로젝트")
    if not complete:
        text = f"{service} 상태 조회를 완료하지 못했습니다. 대상 존재·정상 여부는 알 수 없습니다. 오류: {value.get('error', '관측 불완전')}. 변경은 수행하지 않았습니다."
    elif item["tool"] == "list_containers":
        rows = [f"- {r.get('service')}: {r.get('state', 'unknown')} · health {health_status(r)} [{eid}]" for r in value.get("containers", [])]
        missing = value.get("configured_services_without_containers", [])
        text = "현재 선택 프로젝트의 관측 결과입니다.\n" + "\n".join(rows)
        if missing: text += f"\n대응 컨테이너 없음: {', '.join(missing)}. 미배포이며 healthy/unhealthy가 아닙니다. [{eid}]"
        if not rows and not missing: text += f"\n허용 범위에서 컨테이너가 발견되지 않았습니다. [{eid}]"
        subject = "present" if rows else "absent"
    elif subject == "absent":
        text = f"{service}: 현재 선택 프로젝트에서 대응 컨테이너가 발견되지 않았습니다. 미배포 상태이며 장애·정상으로 판정하지 않습니다. [{eid}]"
    elif subject == "ambiguous":
        text = f"{service}: 대응 컨테이너가 여러 개여서 단일 상태로 판정하지 않았습니다. 개수: {value.get('count', '알 수 없음')}. [{eid}]"
    else:
        text = f"{service}: {value.get('state', 'unknown')} · health {health_status(value)}. [{eid}]"
        if health_status(value) == "unknown": text += " 빈 health 값만으로 healthcheck 설정 여부나 정상 여부를 판단하지 않습니다."
    return {"final": text, "subject_status": subject, "answer_status": "validated" if complete else "unavailable",
            "execution_status": "completed" if complete else "failed", "lifecycle": value.get("state"), "health": health_status(value) if subject == "present" else "unknown",
            "validation_ok": complete, "validation_attempts": 0, "evidence_sufficient": complete, "evidence_coverage": 1.0 if complete else 0.0,
            "analysis_used": False, "analysis_policy": "skip", "analysis_reason": "구조화된 상태를 직접 렌더링: 원인 분석·Writer 생략", "approval": None, "proposal": None}
