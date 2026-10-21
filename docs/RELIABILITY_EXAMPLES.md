# 실제 실행 예시 — 작성한 예상 출력이 아닙니다

원시 기록: `eval/results/reliability_full01.jsonl`. 아래는 그 중 `status-02`, 반복 1의 필드 발췌입니다. 이 실행은 프로젝트 참조 모순 재확인 보강 전 기록이며 상태 노드의 구조화 관측·직접 렌더링 계약은 동일합니다. 최종 버전 반복 검증 결과는 구현 결과 보고서에 별도로 기재합니다.

## 질문

> frontend 서비스의 Docker 상태를 딱 한 번 확인해주세요. 원인 분석은 필요 없습니다.

당시 고정 대상은 서버의 desktop-linux / bizxray 프로젝트였습니다. 이 질문은 프로젝트를 바꾸라는 요청이 아니며 frontend는 해당 Compose의 허용 서비스입니다.

## 노드 1 — supervisor.route

입력: 위 사용자 질의, 현재 프로젝트/엔진/경로, 허용 서비스 `ai, postgres, backend, frontend`, 대화의 필요한 부분만 사용합니다.

실제 출력 발췌:

```json
{
  "route": "status",
  "route_method": "llm",
  "route_fallback": false,
  "requested_project": null,
  "routing_target": {"scope": "service", "service": "frontend"},
  "query": "확정 대상 서비스: frontend\nfrontend 서비스의 Docker 상태를 딱 한 번 확인해주세요. 원인 분석은 필요 없습니다."
}
```

조건: 서비스가 사용자 발언과 허용 목록에서 확인됨 → status 분기의 `status_observe`. 다른 프로젝트였다면 `target_selection`, 단일 대상 미정이었다면 `human_question`으로 종료하며 도구를 호출하지 않습니다.

## 노드 2 — supervisor.status_observe

입력: `routing_target={scope:service, service:frontend}`와 확정 질의.

실제 도구 호출:

```json
{"tool":"check_service_health","args":{"service":"frontend"}}
```

실제 반환된 관측 발췌:

```json
{
  "id": "E1",
  "observation": {
    "service": "frontend",
    "scope": {
      "project_dir": "/path/to/compose-project",
      "mode": "live"
    },
    "observed_at": "2026-10-21T03:34:54.772894+00:00",
    "ok": true,
    "found": false,
    "count": 0,
    "observation_status": "complete",
    "subject_status": "absent"
  }
}
```

조건: 조회는 완료됐고 대상이 0개 → 부재를 확인한 근거로 종료합니다. 조회 실패가 아니므로 반복 조회하지 않습니다. healthy/unhealthy나 원인·복구 성공으로 바꾸어 해석하지 않습니다.

실제 최종 답변:

> frontend: 현재 선택 프로젝트에서 대응 컨테이너가 발견되지 않았습니다. 미배포 상태이며 장애·정상으로 판정하지 않습니다. [E1]

실행 메타데이터: tool_calls=1, model_calls=1(fast 1 / quality 0), validation_attempts=0, subject_status=absent, answer_status=validated, lifecycle=null, health=unknown. 4,347ms 중 모델 4,060.2ms, 도구 약 278ms였습니다. 이 한 번의 수치를 전체 성능 보장으로 사용하지 않습니다.

`plan/gate/research/analysis/write/validate`는 이 실행에 존재하지 않습니다. 별도 LLM 검증은 생략했지만 안전한 구조화 관측을 코드로 직접 렌더링했으므로 “근거 확인됨”이며, 실제 도구가 아닌 예상 노드를 화면에 추가하지 않습니다.

![실제 상태 화면](../eval/results/reliability_status_ui.png)

위 화면은 같은 버전에서 브라우저로 `ai 상태만 알려주세요`를 별도로 실행한 결과입니다. API 예시와 질의는 다르며, 화면에서도 실제 두 노드와 부재 결과를 확인했습니다.

## 설정 검토에서 달라지는 부분

같은 원시 기록의 `config-01`은 실제로 `route → configuration(config_collect → config_review → config_validate → config_propose)`를 실행했습니다. 수집한 7개 파일 경로의 조회 결과에서 다음 두 위험을 검증해 게시했습니다.

- `AI/.dockerignore` 부재: context 전송 범위 위험. 실제 용량·속도는 미측정.
- `AI/Dockerfile:7–9` 소스 COPY 후 pip 설치: 설치 layer 캐시 무효화 가능성. 패키징 확인 없이 자동 재배치하지 않음.

이미 적용된 npm ci·결합된 apt RUN을 다시 권하거나 AI healthcheck가 실행 성공했다고 말하는 문장은 게시하지 않았습니다. 근거 부족인 모델 후보 1건은 제외됐고, 변경·배포는 수행하지 않았습니다. 수집률 1.0과 검증된 정적 위험 2건은 서로 다른 지표입니다.
