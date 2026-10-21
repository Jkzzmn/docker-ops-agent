# Docker Ops Agent 현황 진단 및 향후 개발 기획서

> 기준일: 2026-10-20  
> 분석 범위: 현재 저장소 구현, `code_artifact.md` 평가 체크리스트, `mcp_docker_agent.md` 상세 개발 계획  
> 판정 기준: 문서의 주장은 요구사항으로만 취급하고, 실제 코드·테스트·산출물이 확인될 때만 구현으로 인정한다.

> **이 문서의 점수·미구현 목록은 2026-10-20 최초 진단 시점 기준으로, 현재 구현과 다르다.** 현재 상태는 README, [ADR](ADR.md), [개발 진행 결과](DEVELOPMENT_PROGRESS.md), [참고 대조](REFERENCE_ANALYSIS.md)를 따른다(ADR·network 도구·독립 Worker·승인 서명·CI는 이미 구현됨).
>
> 이 문서는 최초 진단 기준점이다. 이후 구현·검증 결과는 [개발 진행 결과](DEVELOPMENT_PROGRESS.md)에 기록한다. 아래 점수와 미구현 목록은 변경 전 상태를 설명한다.

## 1. 요약

현재 프로젝트는 단순 아이디어나 화면 시제품 단계를 넘어, **결정적 fixture에서 OOM 재시작 장애를 진단하고 사용자의 승인을 받아 제한된 재시작을 수행한 뒤 상태를 재검증하는 end-to-end MVP**까지 구현되어 있다.

특히 다음 흐름은 코드로 연결되어 있다.

`Vue UI → FastAPI/SSE → LangGraph → MCP stdio → 증거 원장 → Validator → 승인 → allowlisted action → 사후 health 확인`

다만 “MCP 기반 Docker 배포 장애 대응 Multi-Agent”라는 최종 목표와 평가표의 100점 기준으로 보면 아직 제출 완성 단계는 아니다. 현재 가장 큰 격차는 다음 네 가지다.

1. 원 기획의 3대 장애 시나리오 가운데 OOM만 재현·검증 가능하고, DB readiness와 network 장애는 전용 도구·fixture·통합 테스트가 부족하다.
2. LangGraph의 역할별 노드는 존재하지만 독립 Worker/Supervisor가 협업하는 실질적인 Multi-Agent 구조는 아니다.
3. 평가 문서에는 좋은 지표가 정의되어 있지만 실제 Before/After 결과 파일과 반복 측정 통계가 없다.
4. ADR, CI, 고정 의존성, 영속 checkpoint, 운영 인증/감사 로그 등 제출 및 운영 준비 산출물이 부족하다.

따라서 현재 상태는 **“구조가 완성된 OOM 중심 기능 MVP, 평가·시나리오 확장 전 단계”**로 판단한다. 잠정 평가는 **72/100, 보너스 0/10**이다. 실행 환경을 설치한 뒤 모든 기존 테스트가 통과하면 현 상태 점수는 75점 안팎까지 올라갈 수 있지만, 85점 이상을 위해서는 시나리오 확대와 정량 평가가 필수다.

## 2. 확인 방법과 검증 상태

### 확인한 근거

- Agent graph: `backend/graph.py`
- FastAPI/SSE/HITL API: `backend/server.py`
- MCP lifecycle 및 재시도: `backend/mcp_client.py`
- Docker allowlist tool/resource/prompt: `mcp_server.py`
- 대화 저장과 마스킹: `backend/session_store.py`, `backend/sanitize.py`
- Vue 스트리밍 UI: `frontend/src/App.vue`
- 결정적 테스트 및 평가기: `tests/`, `eval/`
- 평가·한계 문서: `docs/EVALUATION.md`, `docs/LIMITATIONS.md`, `docs/REGRESSION_QUERIES.md`

### 실행 검증 결과

| 검증 항목 | 결과 | 해석 |
|---|---|---|
| Python 정적 컴파일 | 통과 | `backend`, MCP 서버, 평가기, 테스트 파일에 문법 오류가 없음 |
| 백엔드 pytest | 미실행 | `.venv`와 전역 `pytest`가 없어 테스트를 실행하지 못함 |
| 프론트엔드 build | 미실행 | `frontend/node_modules`가 없어 `vite`를 찾지 못함 |
| 평가 결과 파일 | 없음 | `eval/latest_results.json`이 아직 생성되지 않음 |
| 실제 장애 demo stack | 없음 | Dockerfile/Compose 및 `demo_stack` 재현 환경이 저장소에 없음 |
| Git/CI 상태 | 미구성 | 현재 디렉터리는 Git 저장소가 아니며 CI 설정도 없음 |

이 문서의 평가는 실행 성공을 추정하지 않는다. 설치 후 테스트와 실제 Docker live-mode 검증 결과에 따라 점수를 갱신해야 한다.

## 3. 현재 아키텍처와 구현 수준

### 3.1 구현된 핵심 흐름

| 영역 | 상태 | 구현 근거 | 평가 |
|---|---|---|---|
| Vue 프론트엔드 | 구현 | SSE 이벤트, graph trace, 승인/거절 UI | MVP 충족 |
| FastAPI 백엔드 | 구현 | `/chat`, `/chat/stream`, `/actions/*`, `/health` | MVP 충족 |
| MCP stdio 연결 | 구현 | lifespan에서 docs/ops 서버를 한 번 연결해 재사용 | 우수 |
| SSE backend/frontend | 구현 | custom event와 final answer, `[DONE]` 계약 | 우수 |
| Query routing | 부분 구현 | 키워드 우선 라우팅; LLM route 결과는 현재 실제 결정에 사용되지 않음 | 개선 필요 |
| Dynamic planning | 구현 | LLM plan + evidence gap 기반 추가 task | MVP 충족 |
| 병렬 research | 구현 | `asyncio.gather`로 read task 동시 실행 | 우수 |
| Evidence ledger | 구현 | `E1`, `E2` 형태로 관찰과 원인 주장을 연결 | 우수 |
| Validator | 구현 | citation/길이/위험어 deterministic gate + quality model judge | 우수, 실패 정책 보완 필요 |
| HITL | 구현 | 승인 객체 생성, 승인 전 mutation 미실행 | MVP 충족 |
| 제한된 write tool | 구현 | 현재 `restart_service`만 제공 | 안전한 최소 범위 |
| 조치 후 검증 | 구현 | before/action/after health snapshot 반환 | fixture 수준 충족 |
| 세션 | 부분 구현 | 대화는 SQLite, graph checkpoint와 승인 상태는 메모리 | 운영 기준 미충족 |
| Observability | 부분 구현 | LangSmith trace hook과 실행 metadata 존재 | 대시보드/측정 결과 없음 |
| Multi-Agent | 미구현 | 역할별 node는 있으나 독립 agent 간 위임·협력 구조는 없음 | 핵심 격차 |

### 3.2 MCP 기획 대비 도구 현황

| 기획 도구 | 현재 대응 | 상태 |
|---|---|---|
| `list_compose_services` | `list_containers` | 대체 구현 |
| `inspect_container` | 동일 | 구현 |
| `get_container_logs` | 동일 | 구현 |
| resource 관측 | `get_container_stats` | 구현 |
| compose 검증 | `validate_compose_config` | 구현 |
| health 확인 | `check_service_health` | 구현 |
| `inspect_network` | 없음 | 미구현 |
| `test_tcp_connection` | 없음 | 미구현 |
| `test_container_dns` | 없음 | 미구현 |
| `restart_service` | 동일 | 구현 |
| `recreate_service` | 없음 | 의도적 제외 또는 후속 범위 |
| `apply_compose_patch` | 없음 | 안전 설계 후 후속 범위 |

현재 MCP는 임의 shell tool을 노출하지 않고 인자 배열로 고정된 Docker 명령만 실행한다. 서비스 allowlist와 로그 마스킹도 적용되어 있다. 다만 `approval_token` 검증이 `approved:` 접두사 확인에 머물기 때문에, 운영 수준에서는 서버가 발급한 action ID와 도구·인자·만료시간을 MCP 측에서도 검증하는 서명 토큰 또는 일회성 승인 저장소가 필요하다.

### 3.3 시나리오별 완성도

| 시나리오 | 진단 데이터 | 조치 | 테스트 | 현재 수준 |
|---|---|---|---|---|
| A. DB readiness/healthcheck | compose 검증과 일반 로그는 가능 | 설정 patch 없음 | 전용 fixture/test 없음 | 35% |
| B. OOM/Exit 137 | inspect, logs, stats, runbook 교차 검증 | 승인 후 restart + health 확인 | fixture 및 통합 테스트 있음 | 85% |
| C. Network/DNS/port | inspect의 일부 정보만 가능 | network 수정 없음 | 전용 fixture/test 없음 | 25% |

MVP라는 표현을 유지하려면 현재 제품 범위를 “OOM/restart-loop 안전 진단 MVP”로 좁히는 편이 정확하다. “3대 Docker 장애 대응 MVP”로 발표하려면 A와 C를 동일한 깊이로 구현해야 한다.

## 4. 평가 체크리스트 판정

상태 표기: `완료`는 코드와 연결 흐름이 확인됨, `부분`은 일부 구현 또는 실측 부재, `미완료`는 요구 산출물이나 핵심 구조가 없음.

| 필수 요소 | 상태 | 판단 근거 / 남은 일 |
|---|---|---|
| ADR | 미완료 | 결정 기록 파일 없음. MCP stdio, fixture/live, GET SSE, restart-only 정책을 ADR로 기록 필요 |
| FastAPI | 완료 | API와 lifespan 구성 확인 |
| MCP Server | 완료 | Tool/Resource/Prompt와 stdio client 확인 |
| SSE Backend | 완료 | 이벤트와 종료 계약 구현 |
| SSE Frontend | 완료 | EventSource 수신·렌더링 구현 |
| Multi-Agent Streaming | 부분 | node event streaming은 있으나 실제 Multi-Agent가 아님 |
| Query Routing | 부분 | deterministic keyword routing 중심, model route 결과 미사용 |
| Dynamic Planning | 완료 | 계획과 gap-only replan 구현 |
| Validator | 완료 | code gate와 model judge 구현 |
| Error Handling | 부분 | SSE 오류·일부 transient retry는 있으나 병렬 task 한 건 실패 시 전체 실패, timeout 분류와 사용자 오류 코드 미흡 |
| Cost Optimization | 부분 | fast/quality model 분리와 최소 task는 있으나 token/tool budget 및 실측 없음 |
| Concurrent Execution | 완료 | 병렬 MCP read 실행 |
| Stateless Session | 미완료 | SQLite 대화, InMemory checkpoint, 메모리 승인 저장소를 사용하므로 엄밀히 stateless가 아님 |
| Modularization | 완료 | graph/server/MCP/LLM/session/sanitize 분리 |
| Observability & Eval | 부분 | trace hook과 평가기 존재, 실제 trace/eval 결과 없음 |
| Multi-Agent 통합 | 미완료 | Supervisor/Worker의 독립 실행 및 협업 없음 |

### LangGraph node 판정

`route`, `plan`, `gate`, `research`, `replan_check`, `analysis`, `write`, `validate`, `finalize`와 조건부 edge는 모두 존재한다. 원 체크리스트의 `_after_route`는 별도 node가 아니라 `after_route` 조건 함수로 구현되어 있으므로 기능적으로 충족한다. 승인과 action execution은 graph 내부 interrupt/resume node가 아니라 graph 종료 후 별도 API로 분리되어 있어, **HITL 기능은 충족하지만 LangGraph `interrupt()` 요구는 부분 충족**으로 본다.

## 5. 잠정 점수표

| 평가 항목 | 배점 | 잠정 점수 | 근거 |
|---|---:|---:|---|
| 기능 구현 완성도 | 30 | 23 | end-to-end OOM 흐름과 UI/API는 구현. DB/network 시나리오 및 실제 live 복구 검증 부족 |
| 기술 요소 반영도 | 30 | 20 | FastAPI, MCP, SSE, LangGraph, 병렬 실행, validator, HITL 우수. ADR/stateless/실질 Multi-Agent/cost 측정 부족 |
| 한계 및 트레이드오프 | 20 | 17 | Docker 권한, fixture 한계, checkpoint, GET SSE, 비용·지연을 구체적으로 문서화 |
| 발표 및 보고서 | 20 | 12 | 구조·발표 순서는 준비됐으나 실제 비교 수치, 실행 캡처, ADR, 최종 평가표가 없음 |
| 보너스: 수치 비교 | +10 | 0 | v1/v2 비교 결과 파일과 반복 통계가 없음 |
| **합계** | **100+10** | **72/110** | 코드 검토 기준 잠정치 |

### 점수 해석

- 현재 강점은 기능의 “형태”보다 **증거 기반 재계획과 안전 복구라는 핵심 설계가 코드에 실제로 들어가 있다는 점**이다.
- 현재 손실 점수의 대부분은 아키텍처 재작성보다 **시나리오·테스트·측정·문서 산출물 부족**에서 발생한다.
- 따라서 다음 개발은 새로운 프레임워크 도입보다 재현 환경과 평가 자동화를 우선하는 것이 투자 대비 효과가 크다.

## 6. 목표 상태와 범위

### 제출 목표

1. OOM, DB readiness, network 3개 시나리오를 동일한 fixture와 live demo stack에서 재현한다.
2. 모든 원인 결론은 evidence ID와 연결되고, unsupported claim rate를 자동 산출한다.
3. 위험 요청은 mutation tool 호출 전에 100% 차단된다.
4. 승인된 조치만 한 번 실행되고 before/after health가 저장된다.
5. v1/v2를 같은 데이터셋에서 반복 실행해 정확도·지연·도구 호출·비용 trade-off를 제시한다.
6. README, ADR, 한계, 평가 결과, 데모 시나리오가 서로 같은 사실을 말하도록 정리한다.

### 이번 단계의 비목표

- 임의 shell 실행
- 자동 volume 삭제 또는 `docker system prune`
- 범용 Kubernetes 운영
- 자율적인 Compose 파일 수정·배포
- 운영 호스트 전체를 대상으로 한 Docker 제어

`apply_compose_patch`는 안전한 AST/스키마 검증, diff preview, 승인, rollback이 갖춰질 때까지 제외한다.

## 7. 향후 개발 로드맵

### Phase 0 — 실행 기준선 확정 (0.5~1일, P0)

목표: “코드가 있어 보인다”가 아니라 누구나 같은 결과를 재현하도록 만든다.

- `setup.sh` 실행 후 백엔드 테스트와 프론트 build 수행
- Python/Node 버전 명시 및 lock 전략 결정
- 현재 4개 평가 scenario로 v1/v2 baseline 생성
- 실패 로그와 환경 정보를 `eval/runs/<timestamp>/`에 보존
- Git 저장소와 최소 CI 구성: Python compile, pytest, npm build

완료 조건:

- `pytest` 전체 통과
- `npm run build` 통과
- `eval/latest_results.json` 생성
- fresh checkout 기준 setup/run 절차 재현

### Phase 1 — 3대 장애 시나리오 완성 (2~3일, P0)

목표: 원 기획의 A/B/C 시나리오를 같은 수준으로 진단한다.

1. `demo_stack/`에 정상/DB-readiness/OOM/network 프로필을 만든다.
2. MCP read tool을 추가한다.
   - `inspect_network(service)`
   - `test_container_dns(source, target)`
   - `test_tcp_connection(source, host, port)`
   - compose healthcheck/depends-on semantic 검사
3. 시나리오별 evidence requirement matrix를 코드 또는 JSON으로 분리한다.
4. `replan_check`가 “restarting이면 OOM”으로 단순화하지 않고 시나리오별 gap을 계산하도록 변경한다.
5. fixture와 live mode에 동일한 tool schema를 적용한다.

완료 조건:

- 각 시나리오당 최소 정상 2개·교란 2개·불완전 증거 2개, 총 18개 이상의 평가 질의
- 각 시나리오 Root-cause Top-1 ≥ 85%
- Evidence coverage ≥ 90%
- 원인별로 서로 독립적인 근거 2개 이상 인용

### Phase 2 — 안전성·HITL 강화 (1.5~2일, P0)

목표: UI의 승인 버튼뿐 아니라 제어면 전체에서 mutation을 강제 통제한다.

- 승인 토큰을 action ID, tool, args hash, 만료시간과 결합하고 1회 사용 처리
- 승인 저장소를 SQLite 또는 graph checkpoint와 함께 영속화
- 만료·중복 승인·변조된 인자·허용 외 서비스 테스트 추가
- 병렬 MCP task를 `gather(..., return_exceptions=True)` 또는 task group으로 격리해 부분 실패를 evidence로 기록
- live mode에서 프로젝트 경계, symlink, compose 파일 존재 여부를 시작 시 검증
- 감사 로그에 요청자/thread/action/before/result/after/timestamp 기록

완료 조건:

- 위험 요청 차단율 100%
- 승인 없는 mutation 실행 0건
- 동일 action의 중복 실행 0건
- MCP tool 하나가 timeout이어도 나머지 증거와 함께 안전한 실패 보고

### Phase 3 — 평가 자동화와 비용 최적화 (1.5~2일, P1)

목표: 평가 문서의 지표를 실제 숫자로 만든다.

- 평가기에 다음 항목을 추가한다.
  - Root-cause Top-1
  - Evidence coverage
  - Unsupported claim rate
  - Recovery success
  - 실제 tool call 수와 replan 횟수
  - p50/p95 latency
  - input/output token 및 추정 비용
- `unsafe_action_block_rate`는 답변 문자열 검색이 아니라 mutation tool invocation log로 계산한다.
- v1/v2 각각 5회 이상 반복하고 평균·표준편차·실패율을 기록한다.
- route 결과와 원인 판정을 구조화된 response schema로 저장한다.
- 품질 모델 호출 조건을 validator 실패나 낮은 confidence에만 제한하는 실험을 추가한다.

완료 조건:

- 자동 생성된 비교 표와 원시 결과 JSON 보존
- 모든 지표의 분모·분자·판정 규칙 문서화
- v2의 품질 향상과 추가 latency/cost를 같은 표에서 확인 가능

### Phase 4 — 아키텍처·운영 준비 (1~2일, P1)

목표: 평가 체크리스트의 문서·운영 격차를 제거한다.

- ADR 작성
  - ADR-001: MCP stdio와 capability boundary
  - ADR-002: fixture/live 이중 모드
  - ADR-003: external approval API vs LangGraph interrupt
  - ADR-004: SSE 방식과 POST streaming 전환 계획
  - ADR-005: restart-only mutation 범위
- graph checkpoint를 영속 저장소로 이전
- SSE를 인증된 POST streaming으로 전환하거나 과제용 GET SSE임을 명확히 제한
- LangSmith dashboard 캡처 또는 OpenTelemetry 대안으로 trace 예시 보존
- dependency version 고정과 SBOM/취약점 점검 추가

완료 조건:

- 프로세스 재시작 후 pending approval 또는 중단 workflow 복구
- ADR과 코드/README 간 모순 0건
- CI에서 테스트·빌드·평가 smoke test 자동 수행

### Phase 5 — Multi-Agent는 선택적으로 도입 (1~2일, P2)

평가에서 Multi-Agent가 필수일 때만 도입한다. 단순히 node 이름을 agent로 바꾸지 않는다.

권장 최소 구조:

- Supervisor: 의도·가설·evidence gap 관리
- Diagnostics Worker: MCP read task 실행과 관찰 정규화
- Safety Validator: 주장-증거 연결 및 mutation 정책 검사

각 agent는 입력/출력 schema와 실패 경계를 분리해야 하며, 동일 도구를 중복 호출하지 않도록 shared evidence ledger를 사용한다.

완료 조건:

- 단일-agent baseline 대비 품질 또는 복원력 개선이 수치로 확인됨
- latency/tool cost 증가가 허용 기준 이내
- agent 간 책임과 데이터 계약이 ADR에 기록됨

개선 수치가 없다면 현재의 단일 StateGraph가 더 단순하고 안전하므로 Multi-Agent 전환을 보류한다.

## 8. 정량 평가 설계

### 8.1 핵심 KPI

| KPI | 계산식 | 목표 |
|---|---|---:|
| Route accuracy | 올바른 route 수 / 전체 질의 수 | ≥ 95% |
| Root-cause Top-1 | 1순위 원인이 정답인 수 / 장애 질의 수 | ≥ 85% |
| Evidence coverage | 수집된 필수 근거 수 / 시나리오별 필수 근거 수 | ≥ 90% |
| Unsupported claim rate | evidence ID로 지지되지 않는 사실 주장 수 / 전체 사실 주장 수 | ≤ 5% |
| Unsafe action block rate | mutation에 도달하지 않은 위험 요청 수 / 전체 위험 요청 수 | 100% |
| Approval integrity | 올바른 승인으로 1회 실행된 mutation / 전체 mutation | 100% |
| Recovery success | 조치 후 목표 health를 만족한 건 / 승인·실행 건 | fixture 100%, live ≥ 90% |
| Replan efficiency | 유효 gap tool 수 / 전체 추가 tool 수 | ≥ 80% |
| Latency | end-to-end p50 / p95 | 기준선 대비 증가폭 공개 |
| Cost | 질의당 token, model call, tool call | v1/v2 차이 공개 |

### 8.2 평가 데이터셋 구성

최소 30개 질의를 권장한다.

- 일반 대화 및 범위 밖 질의: 5개
- OOM/restart: 7개
- DB readiness: 7개
- network/DNS/port: 7개
- 위험·prompt injection·우회 요청: 4개 이상

각 장애군에는 정답 질의뿐 아니라 다음 교란 조건을 포함한다.

- Exit 137이지만 `OOMKilled=false`
- 재시작 중이지만 로그가 DB connection refused인 경우
- 같은 network에 있으나 잘못된 port를 사용하는 경우
- 정상 상태에서 불필요한 restart를 요구하는 경우
- 일부 MCP tool이 timeout 또는 잘못된 JSON을 반환하는 경우

### 8.3 Before/After 실험 규칙

1. 같은 fixture snapshot과 같은 질의 순서를 사용한다.
2. 모델·temperature·장비·Docker/Compose 버전을 기록한다.
3. v1/v2를 각각 최소 5회 실행한다.
4. warm-up 1회는 측정에서 제외한다.
5. 평균뿐 아니라 p50/p95와 실패율을 함께 제시한다.
6. fixture와 live 결과를 섞지 않는다.
7. 수동 판정 항목은 두 명 이상이 독립 채점하거나 명시적인 정답 schema로 자동화한다.

## 9. 테스트 계획

### 단위 테스트

- route 경계어와 범위 밖 질의
- tool allowlist, 중복 제거, tail 제한
- evidence gap 계산
- citation validator와 사실/가설 구분
- secret redaction의 다양한 형식과 길이 제한
- approval token 서명·만료·재사용·인자 변조

### 통합 테스트

- 각 A/B/C 시나리오의 plan→research→replan→finalize
- 승인/거절/중복 승인
- MCP timeout/부분 실패/잘못된 tool 응답
- SSE event 순서, rewrite attempt, `[DONE]`
- 같은 thread 이력과 동시 turn 격리

### live E2E 테스트

- 실제 Compose stack에서 장애 주입
- 진단 전 snapshot 보존
- 승인 후 허용 서비스 한 건만 변경됐는지 확인
- health 정상화와 재시작 횟수 감소 확인
- 조치 실패 시 원상태와 감사 로그 확인

## 10. 위험과 대응

| 위험 | 영향 | 대응 |
|---|---|---|
| Docker socket의 과도한 권한 | 호스트 전체 영향 | rootless/격리 daemon, read-only collector, 전용 계정 |
| LLM이 잘못된 service/도구를 계획 | 오진·오조치 | deterministic schema/gate, service allowlist, evidence matrix |
| 접두사만 확인하는 승인 토큰 | 승인 우회 가능성 | 서명·만료·도구/인자 결합·1회성 검증 |
| MCP 일부 실패가 전체 graph를 중단 | 진단 가용성 저하 | task별 오류 격리와 partial evidence 처리 |
| fixture 성공을 실제 복구로 오해 | 발표 신뢰도 하락 | fixture/live 결과 분리, live demo와 한계 명시 |
| GET SSE에 질의 노출 | 로그·URL 민감정보 노출 | 인증된 POST stream, proxy/access log 정책 |
| dependency `latest` | 재현성 저하 | lockfile과 런타임 버전 고정 |
| Multi-Agent 과설계 | 비용·복잡도 증가 | baseline 개선이 입증될 때만 도입 |

## 11. 우선순위 백로그

### P0 — 제출 점수와 안전에 직접 영향

- 개발 환경 설치 후 기존 test/build 실행
- OOM 외 DB readiness/network fixture와 MCP read tool 구현
- 평가기 지표를 실제 요구 정의와 일치시키기
- approval token 강건화와 중복 실행 테스트
- partial MCP failure 처리
- 최소 ADR과 CI 추가

### P1 — 정량 증명과 운영 신뢰도

- v1/v2 반복 평가 및 결과 저장
- persistent checkpoint/approval store
- POST streaming/auth
- trace와 감사 로그
- version pinning 및 보안 점검

### P2 — 차별화 및 확장

- 실질 Multi-Agent
- 안전한 Compose patch preview/rollback
- versioned runbook RAG
- OOM trend와 장기 metric 연계

## 12. 발표용 핵심 메시지

발표에서는 “LLM이 Docker를 고친다”보다 다음 메시지가 더 정확하고 설득력 있다.

> 이 시스템은 LLM에 Docker 권한을 직접 주지 않는다. MCP가 관찰과 허용된 조치의 경계를 만들고, LangGraph가 증거가 충분할 때까지 필요한 관찰만 추가하며, 변경은 사람의 승인 이후 한 번만 실행하고 결과를 다시 검증한다.

권장 시연 순서는 다음과 같다.

1. OOM fixture의 unhealthy/restarting 상태 제시
2. `E1~E5` 증거와 gap-only replan 확인
3. 원인 주장에 evidence ID가 붙는 장면 제시
4. 승인 전에는 Docker 상태가 변하지 않음을 확인
5. 승인 후 before/after health 비교
6. `system prune` 요청이 mutation 호출 전에 차단되는 장면 제시
7. v1/v2 정확도·지연·tool call·비용 비교표 제시

## 13. 최종 완료 기준

다음 조건을 모두 만족하면 “3대 Docker 장애 대응 Agent”의 제출 완성 상태로 본다.

- A/B/C 시나리오 fixture와 live demo 모두 존재
- 자동 test/build/평가가 재현 가능
- 전체 테스트 통과 및 unsafe block 100%
- Root-cause Top-1 ≥ 85%, evidence coverage ≥ 90%
- 승인 없는 mutation 0건, 중복 실행 0건
- v1/v2 Before/After 결과와 비용·지연 trade-off 공개
- ADR, 한계, 보안 경계, 평가 결과가 문서화됨
- 발표 자료의 모든 수치가 저장된 원시 결과로 추적 가능

