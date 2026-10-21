# 미니프로젝트 실행 최적화 · 모델 역할 · LangSmith

2026-10-21. 이 문서는 이전 실행 덤프보다 우선하는 현재 실행 정책입니다. 승인 경계와 SQLite 대화 기억은 유지합니다.

## 모듈 경계

| 파일 | 책임 |
|---|---|
| `server.py` | HTTP·SSE·질문/승인/대상 API |
| `runtime.py` | 모델·MCP·SQLite 연결의 시작/종료와 대상 bundle |
| `state.py` | AgentState와 새 요청의 안전한 상태 초기화 |
| `execution_policy.py` | 요청 예산, 공통 JSON/SSE 실행, 기한 종료·관측 기반 부분 답변 |
| `model_policy.py` | router/fast/quality 역할별 모델 선택 |
| `observability.py` | 선택적·메타데이터 전용 LangSmith 추적 |

기존 `routing`, `config_agent/config_validation`, `network_diagnosis`, `diagnostics/planning`은 유지합니다. 각 노드를 전부 별도 파일로 나누거나 서비스/큐를 추가하지 않았습니다.

## 게이트와 호출 예산

기본 환경변수:

```dotenv
MAX_REQUEST_SECONDS=60
MAX_MODEL_CALLS=8
MAX_TOOL_CALLS=24
READ_TOOL_CONCURRENCY=4
MODEL_CONCURRENCY=1
MAX_REPLANS=2
MAX_VALIDATION_ATTEMPTS=1
WORKER_REFINEMENT=0
```

예산은 요청별 ContextVar의 공유 원장으로 관리합니다. 병렬 자식들이 같은 카운터를 사용하며 모델·MCP 도구 호출 전에 제한을 확인합니다. MCP 통신 재시도도 실제 시도마다 도구 예산을 사용합니다. 모델의 semaphore 대기 시간도 전체 요청 기한 안에 포함합니다. HTTP/SSE는 같은 실행기를 사용합니다.

시간 한도에 도달하면 그래프를 취소하고 마지막 부모 상태에서 확인 가능한 구조화 관측만 반환합니다. 실제 시작했던 미완료 노드만 실패로 종료 표시합니다. 과거 checkpoint를 가져와 현재 관측으로 위장하지 않습니다. 승인·제안은 제거하고 `answer_status=partial`, coverage=null로 반환합니다. 읽기 CLI가 취소 즉시 종료된다는 보장은 없으며, 승인된 파일 변경/배포 API를 이 읽기 진단 timeout으로 취소하거나 재실행하지 않습니다.

일반 장애 분기:

```text
route → plan → gate → 필요한 Worker만 병렬 관측 → replan_check
                                                    ├─ 미시도 필수 관측, 수집 한도 이내 → gap만 수집
                                                    ├─ 컨테이너 부재 → 구조화 상태로 종료
                                                    ├─ 근거 부족/원인 불명 → facts_finish → END
                                                    ├─ 근거 충분 + analysis_policy=required → Analysis
                                                    └─ 근거 충분 → Writer → Validator(기본 1회)
```

Worker는 Supervisor가 정한 역할·서비스·도구 범위의 관측을 수행하는 독립 StateGraph입니다. 기본 모드에서는 각 Worker가 다시 모델 계획을 만들지 않습니다. `WORKER_REFINEMENT=1`로 기존 추가 관측 계획을 켤 수 있지만, 도구 allowlist·중복 제거·호출 예산은 그대로입니다. Supervisor의 다음 경로 선택도 구조화 근거/정책으로 판정해 별도의 `Review worker reports` 모델 호출을 생략합니다. 불확실함을 큰 모델이 분석한다고 없는 관측이 생기는 것은 아니므로, 근거가 부족하면 부분 사실로 종료합니다.

Writer 검증이 거절되면 미검증 원고는 게시하지 않고 구조화 관측만 표시합니다. 기본 재작성 반복은 없습니다. 교육 원문 검증·변경 승인·파일 hash/identity 검사는 생략하지 않습니다. 명시적인 계획의 분석 필수 조건은 관측이 충분할 때만 적용합니다.

상태 조회는 기존 두 노드·도구 1회, network 명령도 기존 고정 체크리스트입니다. 이 짧은 경로에 모델/Worker를 추가하지 않았습니다.

## 제한된 병렬 읽기

Worker 사이 및 각 Worker의 독립 관측은 기존 asyncio.gather를 유지합니다. 설정 수집은 여러 서비스의 inspect/log를 전체 서비스에 걸쳐 동시에 요청하도록 바꿨습니다. 실제 MCP 클라이언트에서 공통 semaphore로 읽기 도구 동시성을 4로 제한합니다. 결과·근거 ID는 요청 순서로 병합합니다.

파일 수집→검토→검증, 존재 확인→DNS/TCP, 승인→변경→사후 확인의 의존 순서는 유지합니다. 같은 대화의 동시 실행 거절과 SQLite scope 격리도 유지합니다. 모델은 기본 1개씩 실행해 로컬 메모리 경합을 제한합니다. Python 동시 요청 수와 실제 Docker/모델 처리 동시성은 같다는 보장이 없으므로 속도 개선율을 추정해 표기하지 않습니다.

## 역할별 모델과 승격

- `OLLAMA_ROUTER_MODEL` / `OPENAI_ROUTER_MODEL`: 라우팅 전용. 미설정이면 기존 fast 모델.
- 기존 FAST: 일반 답변·계획·설정 검토.
- 기존 QUALITY: 명시적으로 필요한 분석·독립 검증·프로젝트 참조 모순 재확인.
- 구조화 상태/network 답변: 모델 없이 코드 렌더링.

경량 라우터의 JSON/schema·프로젝트 인자 출처·프로젝트/서비스 모순·전송 실패는 fast 모델로 한 번 승격합니다. 실제 사용자에게 필요한 모호함(정상 clarify 결과)은 승격하거나 임의 해결하지 않습니다. fast 이후 기존 프로젝트 안전 검사가 추가로 quality 모델을 요구할 수 있으므로 요청 전체가 항상 모델 2회 이하라는 뜻은 아닙니다. 끝까지 확정 못 하면 unavailable이며 키워드 교육 답변으로 대체하지 않습니다.

`run_ollama.sh`는 설정한 라우터가 설치돼 있지 않으면 fast로 돌아가며 모델을 자동 다운로드하지 않습니다. API metadata에 router_model, router_escalations, llm_usage.router_calls/fast_calls/quality_calls, execution_budget을 구분해 표시합니다.

2B 후보 `qwen3.5:2b`는 이미 설치된 모델로만 실험했습니다. 작은 파라미터 수만으로 더 빠르거나 정확하다고 판단하지 않았습니다. 첫 7개 실험은 5/7 통과했고, 프로젝트 인자 혼동과 승격 추가 비용이 확인돼 기본으로 채택하지 않았습니다. 기본은 검증된 4B이며 경량 모델은 역할별 설정과 승격 경로를 통해 별도 평가할 수 있습니다. 실제 로컬 전력/메모리 비용이나 API 달러 절감률은 측정하지 않았습니다.

## 선택적 LangSmith

설정은 로컬 `.env`에서만 합니다. 키를 채팅이나 저장소에 올리지 마세요.

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<로컬에서 직접 설정>
LANGSMITH_PROJECT=docker-ops-agent
```

flag와 key가 모두 있을 때만 요청·실제 노드·MCP·모델 사용량 span을 기록합니다. 자동 LangGraph 원문 상태 업로드는 요청 문맥에서 끄고 명시적인 안전 span을 사용합니다. 입력·출력 텍스트·Docker 파일·로그·도구 인자·승인 토큰·원문 traceback을 전송하지 않습니다. 대화 식별자도 해시로 그룹화합니다. 모델명/역할/provider, 실제 시간·토큰과 노드 관계만 기록합니다. 모델 토큰을 보고받지 못한 실패는 임의의 0토큰으로 만들지 않습니다.

LangSmith UI의 요청 trace ID는 응답 metadata.langsmith_run_id에 연결됩니다. 로컬 토큰 장부는 LangSmith 없이도 유지됩니다. 알려진 외부 모델의 가격 매핑과 Ollama의 실제 운영 비용은 다르며, 모델 이름을 붙인 것만으로 로컬 달러 비용이 계산되는 것은 아닙니다. SDK 전송 오류가 애플리케이션 진단을 실패시키지 않도록 처리합니다.

현재 계정 키가 없어 실제 클라우드 수신은 검증하지 않았고 활성화하지 않았습니다. SDK 대역 테스트로 안전한 필드만 전송되는지 확인했습니다. 공식 참고: [사용량 기록](https://docs.langchain.com/langsmith/log-llm-trace), [입출력 숨김](https://docs.langchain.com/langsmith/mask-inputs-outputs).

## 검증 기록

`eval/results/optimization_router2b_01.jsonl`, `optimization_router2b_02.jsonl`은 중간 실패를 포함한 후보 실험입니다. 삭제하거나 최종 성공으로 바꾸지 않았습니다. 최종 기본 모델 결과는 `optimization_default4b_final.jsonl`에 별도로 기록합니다. 실제 모델 결과와 자동 실패 주입 테스트를 구분하며, 대규모 반복 평가를 추가하지 않았습니다.

최종 자동 테스트 **332개 통과(52.53초)**, 프론트 계약 **7개 통과**, production build 성공. 모델/도구 예산·동시성·JSON/SSE 기한 종료·실제 시작 노드의 중단 표시·역할 복원·한 번의 승격·원문 없는 LangSmith span을 포함합니다. SQLite 재시작/대상 격리/기존 승인 안전 테스트도 유지합니다.

실제 Ollama + Docker의 대표 사례는 최종 **7/7 통과**였습니다(각 1회, 일반화 정확도 아님).

| 사례 | 경로 | 왕복 시간 | 모델 호출 |
|---|---|---:|---:|
| ai 상태 | status | 4627.7ms | 1 |
| 인사 | chat | 4069.5ms | 2 |
| 삭제·재생성 후 사진 유실 교육 | education | 5364.0ms | 1 |
| 다른 프로젝트 요청 | target_selection | 4462.0ms | 1 |
| 서비스 미정 | clarify | 7033.9ms | 1 |
| 네트워크 명령·대상 부재 | incident의 고정 체크리스트 | 231.6ms | 0 |
| 실제 Compose/Dockerfile 검토 | configuration | 23229.0ms | 2 |

모두 변경/승인 실행 없이 읽기 전용으로 수행했고 호출 예산이 중단되지 않았습니다. 2B의 2차 후보 실험도 5/7이었으며 일부 인자 출처 검증이 fast 승격 뒤까지 적용돼 중단된 것을 확인했습니다. 이후 승격된 fast에는 기존 scope 안전 검사를 적용하도록 보완하고 기본 4B에서 위 결과를 얻었습니다. 최종 2B 정확도나 통제된 성능 개선율을 주장하지 않습니다.

브라우저에서도 실제 `/diagnose-network ai postgres`를 실행해 시작 순간 두 노드가 나타나고 종료 후 **단계 2개·업무 이벤트 3개·관측 1개·토큰 0·근거 충족 미측정**이 표시되는 것을 확인했습니다. verification 절차로 화면→SSE→실제 관측→최종 표시를 확인했으며 별도의 fixture 재생 화면이 아닙니다.

![최종 실제 화면](../eval/results/optimization_ui_final.png)
