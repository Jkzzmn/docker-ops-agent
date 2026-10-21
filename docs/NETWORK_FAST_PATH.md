# 미니프로젝트용 짧은 네트워크 진단

## 수정 이유

`/diagnose-network postgres ai`가 범용 incident 루프에 들어가 계획·Worker·재계획·Analysis·Writer·Validator를 돌았습니다. 대상 부재는 구조화 관측으로 설명할 수 있는데도 최대 3회 재작성 후 검증 실패로 끝나는 불필요한 경로였습니다.

## 현재 실행 경로와 조건

`route → network_diagnose → END`입니다. 명령 인자의 서비스 allowlist는 유지하며 붙여넣은 마지막 `\` 한 개는 서비스 이름에서 제외합니다. 자연어 라우팅을 규칙으로 교체한 것이 아니라 명시적인 명령만 고정 체크리스트로 실행합니다.

1. `list_containers({})`: 선택된 프로젝트의 목록을 한 번 조회합니다. 대상이 없거나 중지·재시작·paused 상태이면 이유를 설명하고 종료합니다. 목록 실패는 부재로 간주하지 않습니다. 복제본이 여러 개면 임의 선택하지 않습니다.
2. 둘 다 running인 경우만 `inspect_network({source,target})`. 공유 네트워크가 없으면 관측 사실과 같은 네트워크 연결 검토를 안내하고 종료합니다. host/외부 경로 통신까지 불가능하다고 단정하지 않습니다.
3. 공유 네트워크가 있으면 `test_container_dns({source,target})`. 실행 실패(python3 미설치 등)와 실제 DNS 해석 실패를 구분합니다.
4. DNS가 성공하면 `validate_compose_config({})`에서 target의 선언된 컨테이너 TCP 포트를 확인합니다. 포트 0개/여러 개이면 추측하지 않고 확인한 부분만 반환합니다.
5. 유효한 포트가 하나일 때만 `test_tcp_connection({source,target,port})`. 성공도 해당 방향·포트의 TCP까지만 의미하며 인증·업무 요청 성공은 보장하지 않습니다. 실패도 세부 원인을 확정하지 않습니다.

최대 논리 도구 5회, 모델 0회, 재계획·재작성 0회입니다. 도구당 응답 대기는 5초, 합산 20초 한도이며 자동 재시도하지 않습니다. 시간 초과가 호스트 CLI의 즉시 종료를 보장하는 것은 아닙니다. 도구 내부에서 제한된 여러 Docker CLI 조회를 할 수 있어 논리 도구 횟수와 CLI 프로세스 수는 다릅니다. 데이터·파일 변경이나 컨테이너 기동은 없습니다.

보고서는 구조화된 관측을 코드로 렌더링합니다. “검증됨”은 LLM 자기 검증이 아니라 해당 관측 사실의 계약 확인입니다. 런타임 연결을 확인하지 못한 부재에서도 “근거 100%”라는 오해를 주지 않도록 coverage는 null입니다. 일반 incident 경로도 `not_deployed`가 확인되면 Writer·Validator로 보내지 않고 종료합니다.

## 실제 재현

원본: `eval/results/network_fast_live_20261021.json`. Docker Desktop / 선택 프로젝트 bizxray / 실제 MCP 도구를 사용했습니다. fixture가 아닙니다.

| 입력 | HTTP 왕복 시간 | 도구 / 근거 / 실제 노드 / 토큰 |
|---|---:|---|
| `/diagnose-network postgres ai` | 90.8ms | 1 / 1 / 2 / 0 |
| `/diagnose-network postgres ai\` | 55.8ms | 1 / 1 / 2 / 0 |

두 요청 모두 postgres·ai의 대응 컨테이너 부재를 확인하고 END. `validation_ok=true`, `validation_attempts=0`, `replan_rounds=0`, 승인 없음. 실행 전후 컨테이너 ID·state 동일. 단 두 번의 로컬 측정이며 모든 PC/가동 중 네트워크의 시간 보장은 아닙니다.

화면이 사용하는 `/chat/stream`도 별도 실제 요청으로 확인했습니다(237.1ms). 전송 순서는 `NodeStarted(route) → RouteDecided → NodeCompleted(route) → NodeStarted(network_diagnose) → TaskCompleted → Finalize → NodeCompleted(network_diagnose) → FinalAnswer`였습니다. 업무 이벤트 3개, 실제 노드 2개, 근거 1개입니다. 브라우저 자동 조작 결과가 아니라 실제 SSE 응답 검사 결과입니다.

최종 전체 자동 테스트 **316개 통과(44.89초)**. 새 체크리스트의 20개 계약 테스트를 포함하며 실행 중 네트워크 분기는 대역 관측을 사용한 테스트입니다. 실제 업무 컨테이너를 기동하거나 수정하지 않았습니다.

실제 반환 예:

> postgres → ai 연결 점검
>
> 현재 선택 프로젝트에서 postgres, ai 대응 컨테이너가 없습니다. [E1]
> 실행 중인 두 컨테이너가 있어야 실제 DNS·TCP 연결을 검사할 수 있어 여기서 종료했습니다. 네트워크 장애로 판정하지 않습니다. 프로젝트 선택과 배포 여부를 먼저 확인해주세요.
>
> 변경은 수행하지 않았습니다.

running/네트워크 분리/DNS 실패/TCP 실패/조회 실패/다중 포트 분기는 자동 테스트로 검증합니다. 업무 스택을 임의로 기동해 실험하지 않습니다. 과제의 Supervisor·서브 에이전트 구조는 일반 진단에 유지하지만 이 명확한 체크리스트에 불필요한 모델 라운드를 붙이지 않습니다.
