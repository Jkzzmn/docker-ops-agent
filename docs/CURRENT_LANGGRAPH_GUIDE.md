# 현재 LangGraph 구조와 구현 설명

기준일: 2026-10-21. 기획상의 이상적인 구조가 아니라 현재 소스의 노드·간선·실행 정책을 정리했습니다. 이 작업은 설명 자료 작성이며 서비스 동작은 변경하지 않았습니다.

먼저 [3장짜리 흐름도 PDF](../output/pdf/current_langgraph.pdf)를 보세요.

- [1장: 질문별 전체 분기](../output/pdf/current_langgraph-1.png)
- [2장: 일반 장애 진단과 게이트](../output/pdf/current_langgraph-2.png)
- [3장: 서브그래프와 실행 인프라](../output/pdf/current_langgraph-3.png)

그림은 가능한 경로를 설명하는 정적 구조도입니다. 프론트 실행 화면은 이 전체 그림을 재생하는 것이 아니라 실제 `NodeStarted/NodeCompleted/NodeFailed` 이벤트만 표시합니다. 같은 `plan`이라는 이름도 `scope`가 다르면 부모 계획과 Worker 계획처럼 서로 다른 노드입니다.

핵심 요약: **LLM이 자연어 의도를 분류하고, 코드가 허용 범위·필수 근거·예산·승인을 통제합니다.** 모든 질문에 Supervisor 계획, 분석, Writer, Validator가 전부 실행되는 것은 아닙니다.

## 1. 싱글톤: 요청마다 재생성하지 않는 실행 자원

정확히는 `Singleton` 패턴 클래스가 아니라 **프로세스당 현재 선택 대상의 활성 bundle 하나를 재사용하는 구조**입니다. FastAPI lifespan에서 `launch_bundle()`을 호출해 모델 게이트웨이, MCP 클라이언트, SQLite 연결, 승인·질문 저장소와 컴파일된 부모 그래프를 준비합니다. 요청마다 그래프를 다시 컴파일하거나 MCP 서버를 새로 띄우지 않습니다.

| 재사용하는 것 | 요청마다 새로 만드는 것 |
|---|---|
| 컴파일된 그래프, ModelGateway, MCP 세션, checkpointer, 저장소 | `fresh_state`, 요청 예산, 토큰 사용량 장부, 실행 이벤트·관측·원고 |

대상 프로젝트를 바꾸면 새 bundle을 준비하고 기존 자원을 닫습니다. 진행 중 실행과 대상 전환을 함께 허용하지 않으며 이전 대상의 대기 승인·질문을 무효화합니다. `/health`의 `instance_id`, `creation_count`로 인스턴스 생성을 확인할 수 있습니다.

한계: 서버 worker 프로세스를 여러 개 띄우면 프로세스마다 bundle이 생깁니다. 분산 싱글톤이나 여러 서버 사이의 전역 잠금이 아닙니다. 공유 자원을 재사용한다는 말과 서로 다른 사용자의 질문 상태를 공유한다는 말은 다릅니다.

구현: [runtime.py](../backend/runtime.py#L20), [server lifespan](../backend/server.py#L72).

## 2. MCP server: 모델과 실제 도커 작업 사이의 도구 경계

`DockerMCPClient.start()`는 로컬 Python 서버 두 개와 **stdio**로 연결하고 각각 `ClientSession.initialize()`를 수행합니다. 채팅의 SSE와 MCP의 stdio는 서로 다른 통신입니다.

| 서버 | 실제 책임 |
|---|---|
| `mcp_docs_server.py` | `docs://reference/{topic}` 문서 resource |
| `mcp_server.py` | Docker 읽기·제한적 변경 tools, runbook·Compose resources, 진단 prompts |

예를 들어 Worker가 `check_service_health(service="ai")`를 요청하면 MCP 클라이언트 → ops 서버 → 허용된 Docker 도구 구현 → Docker CLI → 구조화 결과의 순서로 돌아옵니다. 모델이 임의의 터미널 명령 문자열을 실행하는 구조가 아닙니다. 도구명·인자·서비스·프로젝트 경로를 코드가 제한합니다. 로그와 파일 내용도 명령이 아닌 신뢰할 수 없는 관측 데이터로 취급합니다.

실제 대상은 선택한 Docker context/engine, 프로젝트 경로, Compose 설정과 허용 서비스에 고정됩니다. 실행 중 컨테이너만 보는 것이 아니라 해당 범위의 중지 컨테이너와 설정 파일도 읽을 수 있습니다. 다른 프로젝트는 사용자가 대상 패널에서 명시적으로 선택해야 하며 모델이 자동 전환하지 않습니다. 다른 컴퓨터에서는 그 컴퓨터의 Docker와 접근 가능한 프로젝트 경로를 선택해야 합니다. 현재 구현은 원격 PC의 임의 파일을 자동 탐색하는 에이전트가 아닙니다.

읽기 동시성은 기본 4입니다. 통신 오류의 제한된 재시도와 달리 변경 도구는 결과 불확실 상태에서 중복 실행하지 않도록 1회만 시도합니다. 변경에는 별도 승인·서명·만료·대상 일치 검사가 적용됩니다.

구현: [MCP client](../backend/mcp_client.py#L32), [ops server](../mcp_server.py), [docs server](../mcp_docs_server.py), [Docker 도구](../backend/docker_tools.py).

## 3. 단발성 응답과 SSE: 같은 그래프, 다른 전달 방식

| 방식 | 입력 | 실행 | 클라이언트가 받는 것 |
|---|---|---|---|
| `POST /chat` | JSON `message`, `thread_id` | 공통 `execute()`가 그래프를 끝까지 소비 | 최종 JSON 한 번: reply, events, evidence, metadata 등 |
| `GET /chat/stream` | query parameter의 message, thread_id | 같은 `execute()` | SSE 이벤트를 순차 전달하고 `FinalAnswer`, `[DONE]` |
| 사람 질문 답변의 POST streaming | 질문 ID, 번호 또는 자연어 답변 | 확인된 답변을 포함한 후속 요청 | `stream=true`이면 같은 형식의 SSE |

`execute()`는 `graph.astream(..., stream_mode=["custom", "values"], subgraphs=True)`를 사용합니다. `custom`은 실제 노드·도구·업무 이벤트이고, 부모의 `values`는 마지막 완료 상태를 추적합니다. SSE가 별도의 가짜 단계 실행기를 갖는 것은 아닙니다.

프론트는 `run_id`, `parent_run_id`, `scope`, `node`를 사용해 실제 시작 순간 노드를 생성하고 완료·실패를 갱신합니다. 일반 대화는 모델 토큰을 바로 전달하지만, 일반 진단 Writer의 미검증 원고는 사용자 답변으로 스트리밍하지 않습니다. 검증된 최종 답변은 finalize에서 전달합니다. 개발용 노드 입출력 패널의 중간 원고와 검증된 최종 답변은 구별해야 합니다.

두 방식 모두 동일한 시간·모델·도구 예산을 적용합니다. 브라우저의 스트림 수신 중지는 서버 작업과 Docker CLI가 반드시 즉시 중지되었다는 보장이 아닙니다. GET SSE의 질의는 URL에 포함되므로 민감 내용을 URL/접속 로그에 남길 가능성이 있습니다. 현재 로컬 과제용 설계에 대한 한계이며 공개 다중 사용자 서비스의 보안 완성을 뜻하지 않습니다.

구현: [공통 실행기](../backend/execution_policy.py#L69), [JSON/SSE API](../backend/server.py#L236), [실제 노드 이벤트](../backend/tracing.py#L31), [프론트 추적](../frontend/src/trace.js).

## 4. 슈퍼바이저 에이전트와 서브에이전트

부모 그래프 이름은 `docker_supervisor`입니다. Supervisor는 의도에 맞는 경로를 선택하고, 일반 장애에서는 계획·위임·근거 검토·다음 단계 선택·최종 게시를 통제합니다. Supervisor의 모든 판단이 LLM 호출이라는 뜻은 아닙니다. 계획은 LLM을 쓰지만 재수집/분석/종료 선택은 현재 구조화 근거에 대한 코드 정책입니다.

| 역할 | 내부 실제 노드 | 입력 → 출력 | 실행되는 경우 |
|---|---|---|---|
| runtime Worker | plan → observe → report | 지정 tasks, query, known_tasks → observations, report | 상태·로그·이벤트 등 해당 도구 작업이 배정됨 |
| configuration Worker | plan → observe → report | 동일 → Compose 검사 관측 | 일반 장애의 `validate_compose_config` 배정 |
| network Worker | plan → observe → report | 동일 → 네트워크 관측 | 네트워크 도구 작업 배정 |
| Analysis | cross_check | query, evidence, diagnosis, reason → analysis | 충분한 근거 + 분석 필수 정책 |
| Knowledge | retrieve | query, semantic_lesson_ids → knowledge | 교육 경로 |
| Writer | compose | query, route, evidence/knowledge, analysis, problems → draft | 일반 진단 보고서 또는 교육 원문 조립 |
| Validator | code_check → 조건부 judge | draft와 근거 → ok, problems | Writer 결과 검증. 교육은 judge 생략 |
| 설정 검토 전용 Agent | config_collect → config_review → config_validate → config_propose | 요청·대상 → 검증된 설정 관측, 조건부 diff/승인 | configuration/change 경로 |

여기서 `configuration Worker`와 `configuration` 전용 서브그래프는 다른 것입니다. 전자는 일반 장애의 Compose 검사 담당, 후자는 Dockerfile/Compose 파일 검토 전체를 담당합니다.

관측 Worker는 각자 독립 `StateGraph`이지만 기본 `WORKER_REFINEMENT=0`에서는 배정된 계획을 정리하고 도구를 실행합니다. 매 Worker마다 LLM을 추가 호출하는 자율 토론 구조가 아닙니다. 분석·Writer·Validator에는 변경 도구나 승인 권한이 없습니다.

구현: [부모 그래프](../backend/graph.py#L44), [서브에이전트](../backend/agents.py#L35), [설정 전용 그래프](../backend/config_agent.py#L75).

## 5. 쿼리 라우팅: 자연어는 LLM, 명령은 엄격한 파싱

`route`의 입력은 현재 `query`, 제한된 `history`, 선택한 프로젝트·서비스 정보입니다. 출력은 `route`, `route_reason`, `route_method`, 필요시 `routing_target`, `clarification`, `semantic_lesson_ids`, `requested_project` 등입니다.

1. 명시적 `/diagnose-network <a> <b>`, `/diagnose-oom <service>`는 인자·서비스 allowlist를 검사해 표준 절차로 연결합니다. 잘못된 명령은 blocked입니다. 네트워크 명령은 LLM 라우터 없이 처리합니다.
2. 자연어는 라우터 모델의 구조화 JSON을 Pydantic으로 검증합니다. 최근 사용자 발화와 직전 답변을 제한적으로 참고하되 현재 질문이 우선합니다.
3. 모델이 선택한 프로젝트·서비스가 실제 사용자 발화에서 근거를 갖는지 확인합니다. 모호한 대상은 질문하고, 다른 프로젝트는 target_selection으로 돌립니다.
4. 경량 전용 라우터가 실패하면 FAST로 한 번 승격할 수 있습니다. 이후 별도의 프로젝트 모순 확인에 QUALITY가 쓰일 수도 있습니다. 끝까지 분류하지 못하면 unavailable이며 키워드 교육 답변으로 대체하지 않습니다.

실제 파일 점검 요청을 교육으로 잘못 분류한 경우에 대비한 파일 의도 재검사도 있습니다. 파일 언급과 요청 원문을 확인해 실제 configuration/change 의도인지 모델로 다시 확인합니다. 이 보완도 완벽한 의미 분류를 보장하지는 않습니다.

예: “현재 Compose와 Dockerfile의 오류·비효율을 확인해주세요”는 **configuration**, “그 내용을 실제로 수정해주세요”는 **change**, “볼륨이 뭔가요?”는 **education**이 의도한 분류입니다. 조회 대상이 정해지지 않은 “왜 안 되죠?”는 임의의 ai/db를 고르기보다 **clarify**가 맞습니다.

중요: “규칙 기반 라우팅을 전부 없앴다”는 설명은 부정확합니다. 자연어 의도는 LLM이 선택하고, 명령 파싱·서비스 출처 검사·위험 실행 차단·범위 제한은 코드로 남겨둡니다.

구현: [routing.py](../backend/routing.py#L194), [route 노드](../backend/graph.py#L66).

## 6. 컨디셔널 라우팅(게이트): 분기 함수와 gate 노드는 다름

`add_conditional_edges`에 전달하는 함수는 상태를 보고 다음 노드를 고르는 **조건부 간선**입니다. `gate`는 실행할 task를 정제하는 **실제 노드**입니다. 둘을 구분해야 합니다.

| 위치 | 조건 | 다음 경로 |
|---|---|---|
| route 직후 | 예산이 이미 소진됨 | facts_finish → END |
| route 직후 | 정상 파싱된 diagnose_network 명령 | network_diagnose → END |
| route 직후 | chat / status | chat_answer / status_observe → END |
| route 직후 | clarify / target_selection | human_question / target_selection → END |
| route 직후 | unavailable / blocked | routing_unavailable / policy_block → END |
| route 직후 | education | education_retrieve |
| route 직후 | configuration / change | configuration → END |
| route 직후 | 일반 incident | plan → gate → research → replan_check |
| education_retrieve 직후 | knowledge 있음 / 없음 | write / education_unknown |
| replan_check | not_deployed | 상태를 구성하고 END. 네트워크 장애로 단정하지 않음 |
| replan_check | 미시도 필수 관측이 남고 replan_rounds < 2 | gap task만 gate로 반환 |
| replan_check | 근거 부족 또는 원인 unknown | facts_finish → END |
| replan_check | 충분한 근거 + analysis_policy=required | analysis → write |
| replan_check | 충분한 근거, 분석 불필요 | write |
| Validator 내부 | code_ok이고 교육이 아님 | judge. 나머지는 서브그래프 END |
| validate 직후 | 합격 또는 설정된 시도 한도 도달 | finalize → END |
| validate 직후 | 거절 + 시도 여유 | write로 재작성. 기본 1회라 반복하지 않음 |

주의: 재수집 조건은 일반적인 근거 부족 종료보다 먼저 검사합니다. 아직 해보지 않은 필수 조회가 있고 한도가 남으면 먼저 수집합니다. 정상 healthy가 충분한 근거로 확인되면 분석 없이 write로 갑니다. 예산 초과는 이 간선들 외에 공통 실행기에서도 차단합니다.

실제 `gate` 노드는 `pending`을 입력받아 허용 도구·인자·서비스를 정규화하고 완료된 동일 관측을 제외한 `pending`, 정규화한 `analysis_policy`를 출력합니다. 사람에게 질문하는 노드는 `human_question`이며 gate와 다릅니다. 최대 두 차례 확인 후에도 대상을 확정하지 못하면 추가 실행 없이 새 구체적 요청을 안내합니다. 질문 응답 자체는 변경 승인이 아닙니다.

구현: [간선 선언 및 after_route/after_review/after_validate](../backend/graph.py#L299), [Supervisor 다음 단계 정책](../backend/agents.py#L54).

## 7. 다이나믹 플래닝: 실행 중 필요한 관측만 보완

일반 incident의 `plan`은 query, 허용 서비스, MCP의 표준 진단 prompt를 받아 `tasks:[{tool,args,why}]`, `analysis_policy`, reason을 만듭니다. 모델의 계획은 그대로 실행하지 않고 `normalize_tasks()`로 읽기 도구·인자·서비스·포트·로그 길이·중복·최대 12개 후보를 검사합니다. 계획 실패 시 엔진 상태와 컨테이너 목록이라는 최소 읽기 계획으로 시작합니다.

`research`는 task의 도구에 따라 Worker에 배정합니다. 이후 `review_evidence()`가 필수 관측의 누락, 원인 분류, 충분성을 판정하고 **미시도 gap만** 다시 요청합니다. 실패한 동일 task를 그래프가 끝없이 재계획하지 않습니다. MCP 통신 계층의 일시적 오류 재시도는 별도입니다.

분석 정책은 auto/skip/required지만 근거가 부족한 경우 required라고 해서 분석 모델로 억지 결론을 생성하지 않습니다. 충분한 관측이 있을 때 필요한 추가 해석만 Analysis가 수행합니다. `WORKER_REFINEMENT=1`이면 Worker가 역할 범위에서 최대 두 개의 추가 관측을 제안할 수 있으나 기본은 OFF입니다.

“다이나믹”은 컴파일된 그래프 안에서 task·서비스·호출할 Worker·다음 분기를 바꾼다는 뜻입니다. 실행 중 임의의 새 도구나 노드를 코드로 만들어 붙이지 않습니다. 상태 조회·교육·설정 전용 흐름·네트워크 고정 명령에는 이 일반 장애 계획 루프를 추가하지 않았습니다.

구현: [plan과 next_step](../backend/agents.py#L38), [task 정규화·근거 검토](../backend/planning.py#L31).

## 8. 발리데이션: 경로마다 검증 대상이 다름

| 경로 | 확인하는 내용 | 실패·제약 처리 |
|---|---|---|
| 자연어 라우팅 | JSON schema, 허용 enum, 인자 출처, 선택 프로젝트 일치 | 제한 승격, 질문/대상 선택/unavailable |
| 상태·네트워크 단축 경로 | 구조화 관측의 완료/존재/실행 상태·값 타입 | 관측 불가와 부재를 구별하고 코드로 답변 |
| 일반 진단 보고서 | 유효한 live `[Ex]` 인용, 길이, 위험 내용 + QUALITY judge의 근거 대조 | 기본 1회. 거절 시 원고 대신 구조화 관측만 게시 |
| 교육 | `[Kx]`, 허용 URL, 필수 안전 카드, 검토 원문과 정확한 일치 | judge 생략. 실패하면 안전한 한계 안내 |
| 설정 검토 | 모델 주장별 파일 snapshot, hash, 줄·지원 정적 검사 결과 | supported만 게시. contradicted/insufficient는 확정 주장·수정 근거에서 제외 |
| 실제 변경 직전 | 승인 유효성, 범위, 정확한 diff/hash, 대상 identity | 불일치 시 변경 거부. 파일 승인과 배포 승인은 별개 |

교육의 `K` 자료와 실시간 Docker의 `E` 관측은 섞지 않습니다. runbook도 해석용 문서이지 현재 장애를 입증하는 live evidence가 아닙니다. 설정 정적 검증에 통과했다고 실제 빌드·성능·복구까지 성공한 것은 아닙니다. `validation_ok` 한 값만 보지 말고 `answer_status`, `subject_status`, coverage의 측정 여부를 함께 봐야 합니다. 단축 경로의 coverage=null은 실패율 0%나 충족률 100%가 아니라 미측정입니다.

현재 Validator의 모델 판단 역시 무오류 보증이 아닙니다. 안전성을 코드 검사·정적 근거·승인 경계로 보완하는 설계입니다. 설정의 자동 수정안 생성은 현재 **활성 build context에서 관측상 없는 .dockerignore를 추가하는 범위**로 제한됩니다. 다른 Dockerfile/Compose 위험을 정적으로 지적할 수는 있지만 모든 개선 항목에 자동 수정·승인을 만들지는 않습니다.

구현: [Validator](../backend/agents.py#L275), [설정 검증](../backend/config_validation.py), [승인 제어](../backend/config_control.py).

## 9. 에러 핸들링: 실패를 정상 관측처럼 바꾸지 않음

라우터 실패는 unavailable로 끝내고 교육 답변을 대신 내지 않습니다. Worker 도구 오류는 실패 관측으로 보존하며 없던 컨테이너, 조회 실패, stopped, unhealthy를 구별합니다. 일반 관측 Worker의 도구 작업에는 20초 timeout이 있고 전체 요청 기한도 적용됩니다.

MCP 읽기는 일부 TimeoutError/ConnectionError에 최대 3번 시도하지만, 스키마 오류나 불허 인자 등 모든 오류를 재시도하지 않습니다. 네트워크 단축 명령은 도구당 1회·최대 5초, 체크리스트 전체 약 20초의 한도를 사용합니다. 변경 도구는 불확실한 결과를 이유로 재실행하지 않습니다.

모델 계획 실패는 최소 안전 읽기, Writer 실패는 관측 기반의 제한된 코드 요약, Validator 모델 실패는 거절로 처리합니다. 설정 검토 모델 실패는 수집 관측과 검토 실패를 명시하고 수정안을 만들지 않습니다.

공통 요청 예산을 넘으면 그래프를 취소하고 **마지막으로 완료된 부모 상태**에서 표현할 수 있는 관측만 반환합니다. 아직 자식 노드에만 있던 결과까지 전부 최종 답변에 복구되는 것은 아닙니다. 승인·제안은 제거하고 answer_status=partial, coverage=null로 표시합니다. 실제 시작했던 미완료 노드만 실패로 마감합니다. 예상하지 못한 예외는 JSON API에서 502, SSE에서 Error와 `[DONE]`으로 끝나며 모든 오류가 부분 답변으로 전환되는 것은 아닙니다.

구현: [실행 예산과 부분 답변](../backend/execution_policy.py), [네트워크 단축 진단](../backend/network_diagnosis.py), [MCP 재시도](../backend/mcp_client.py#L55).

## 10. 코스트 옵티마이제이션: 작은 모델만 쓰는 것이 아님

현재 기본 Ollama 설정은 FAST=`qwen3:4b-instruct`, QUALITY=`qwen3:8b`입니다. 라우터 전용 모델은 `OLLAMA_ROUTER_MODEL`로 별도 지정할 수 있으며 미설정이면 FAST를 사용합니다. OpenAI 모드도 역할별 별도 설정이 가능합니다. 아래는 역할이지 모델이 언제나 서로 다른 서버라는 의미는 아닙니다.

| 역할 | 사용처 |
|---|---|
| ROUTER | 자연어 의도·교육 카드 의미 선택. 전용 경량 모델 오류 시 FAST 승격 |
| FAST | 일반 대화, 최소 진단 계획, 설정 검토. 선택적으로 Worker 추가 계획 |
| QUALITY | 일반 incident Writer, 필요한 Analysis, 독립 Validator judge, 프로젝트 참조 모순 재확인. 교육의 비의미 검색 예외에서는 카드 선택에도 사용 가능 |
| 모델 없음 | 상태 관측 요약, 네트워크 체크리스트, 의미 선택된 교육 원문 조립, 충분성 판정, facts_only |

최적화의 핵심은 불필요한 호출을 없애는 것입니다. 상태 조회에 보고서 모델을 붙이지 않고, 네트워크 명령은 라우터·Planner·Writer를 생략하며, Worker 재계획과 반복 재작성도 기본 OFF입니다. 교육은 라우터에서 고른 카드를 다시 모델로 설명하지 않고 검토된 원문을 조립합니다.

| 설정 | 기본값 | 의미 |
|---|---:|---|
| MAX_REQUEST_SECONDS | 60 | 요청 전체 시간. 모델 슬롯 대기도 포함 |
| MAX_MODEL_CALLS | 8 | 요청 전체 모델 호출 상한 |
| MAX_TOOL_CALLS | 24 | MCP tool 호출 시도 상한. 통신 재시도도 소비 |
| MAX_REPLANS | 2 | 추가 수집 라운드 상한 |
| MAX_VALIDATION_ATTEMPTS | 1 | 기본 재작성 반복 없음 |
| WORKER_REFINEMENT | 0 | Worker별 추가 모델 계획 생략 |
| READ_TOOL_CONCURRENCY / MODEL_CONCURRENCY | 4 / 1 | 읽기 병렬과 모델 경합 제한 |

카운터는 요청 ContextVar의 공유 원장이므로 병렬 자식도 같은 상한을 사용합니다. 이 tool 예산은 모든 MCP resource/prompt RPC의 수와 동일하지 않습니다. resource/prompt도 전체 시간 한도에는 포함됩니다.

2B 후보는 기존 실험에서 추가 승격과 인자 혼동이 확인돼 기본으로 채택하지 않았습니다. 작은 모델이라고 실제 요청이 반드시 더 빠르거나 저렴하지 않습니다. 로컬 Ollama는 API 달러 요금 절감률이나 전력 비용을 측정하지 않았으므로 “비용 N% 절감”을 주장하지 않습니다. 토큰·역할별 호출·실측 시간만 기록합니다.

구현: [모델 역할 선택](../backend/model_policy.py), [모델 게이트웨이](../backend/llm.py), [기본 설정](../backend/config.py#L13).

## 11. 병렬화: 독립 읽기는 동시에, 의존 단계는 순서대로

`research`에서 필요한 여러 Worker를 `asyncio.gather()`로 호출하고 각 Worker의 독립 관측도 gather로 수행합니다. MCP 클라이언트의 공통 semaphore가 실제 읽기 도구 동시성을 기본 4로 제한합니다. 모든 작업이 요청 예산을 함께 소비합니다.

설정 수집은 ① Compose·구문검사·컨테이너 목록·파일 목록·엔진 상태의 독립 기초 읽기, ② 그 결과로 알아낸 build 경로의 Dockerfile/.dockerignore 읽기(최대 12개 경로), ③ 허용 서비스 전반의 inspect/log 읽기를 단계별로 병렬 처리합니다. 결과 ID는 완료 속도에 따라 뒤섞기보다 요청 순서로 부여합니다.

반대로 파일 수집→모델 검토→주장 검증, 존재·running 확인→DNS/TCP, 파일 승인→파일 변경→별도 배포 승인→사후 확인은 의존성이 있으므로 순서를 유지합니다. 모델 호출도 로컬 메모리 경합을 피하려고 기본 1개씩 처리합니다. asyncio 동시 요청과 실제 Docker/Ollama 내부의 병렬 처리량은 같다는 보장이 없습니다.

같은 대화의 동시 요청은 409로 막습니다. 다른 대화는 독립 실행할 수 있지만 같은 모델 슬롯과 MCP 읽기 슬롯을 나눠 씁니다. 현재 구조는 큐 기반 분산 병렬 실행 플랫폼은 아닙니다.

구현: [Worker 병렬 관측](../backend/agents.py#L108), [설정 병렬 수집](../backend/config_agent.py#L76), [MCP 슬롯](../backend/mcp_client.py#L47).

## 12. 체크포인터: 실행 상태 저장과 대화 기억은 별개

현재 **AsyncSqliteSaver가 실제 부모 그래프에 연결되어 있습니다.** runtime에서 DB 연결과 setup을 수행하고 `build_graph(..., checkpointer=checkpointer)` → `graph.compile(..., checkpointer=checkpointer)`로 전달합니다.

| 저장소 | 기본 파일 | 저장 목적 |
|---|---|---|
| LangGraph checkpointer | data/sessions.checkpoints.sqlite | 부모 그래프 실행 단계의 상태·체크포인트 |
| SessionStore | data/sessions.db | 사용자·어시스턴트 메시지 이력 |
| 승인·질문 저장소 | 같은 sessions.db의 별도 테이블 | 대기 승인·조건부 확인 질문과 유효성 |

checkpoint `configurable.thread_id`는 단순 브라우저 ID가 아니라 `대상 scope hash:브라우저 thread_id`입니다. scope에는 엔진/context, 프로젝트 경로, 모드, Compose 설정, 정렬된 허용 서비스 등이 포함됩니다. 다른 프로젝트의 관측이나 대화를 같은 ID라는 이유로 섞지 않습니다. 서비스 순서만 달라진 기존 메시지 키는 읽기 호환 처리합니다.

매 요청 `fresh_state()`가 이전 command·evidence·tasks·원고·승인·이벤트 등 일시 상태를 초기화합니다. 메시지 이력은 별도로 최근 12개를 읽어 프롬프트에 필요한 부분만 전달합니다. 라우터는 최근 사용자 발화 최대 3개(각 500자)와 직전 답변 최대 800자, 일반 대화는 최근 최대 8개(각 1500자)를 사용합니다. **checkpointer를 연결하는 것만으로 모델이 모든 대화를 기억하는 것은 아닙니다.** 실제 이력을 프롬프트에 넣는 코드가 함께 필요합니다.

관측 Worker/Analysis/Knowledge/Writer/Validator 그래프는 `checkpointer=False`로 컴파일됩니다. 설정 그래프는 독립 SQLite saver를 생성하지 않습니다. 자식 작업을 별도 SQLite 세션으로 무제한 저장·자동 복구하는 구조로 설명하면 안 됩니다.

현재 사람 질문·승인은 별도 저장소와 API로 후속 요청을 실행합니다. LangGraph `interrupt()` → `Command(resume=...)` 기반의 중단 지점 재개는 구현되어 있지 않습니다. 서버 재시작 뒤 변경 작업을 자동 재실행하지도 않습니다. 브라우저 thread ID 유지로 백엔드 대화 맥락을 이어갈 수 있지만 화면 전체 대화 이력의 재로드는 별개이며 현재 완전한 UI 복원 기능은 아닙니다.

DB는 로컬 파일이며 자동 암호화·보존 기간 관리가 있는 것은 아닙니다. 체크포인트와 디버그 기록에는 민감한 로컬 내용이 남을 수 있으므로 제출물에 실제 DB·로그를 포함하지 마세요.

구현: [SQLite 생성과 scope](../backend/runtime.py#L35), [이력 주입](../backend/server.py#L120), [fresh_state](../backend/state.py#L63), [기억 검증 기록](CONVERSATION_MEMORY.md).

## 13. 모듈화: 책임 경계로 분리된 현재 파일

| 모듈 | 책임 | 여기서 하지 않는 일 |
|---|---|---|
| server.py | HTTP, JSON/SSE, 대상·질문·승인 API | Worker 관측 알고리즘 구현 |
| runtime.py | 공유 자원의 시작/종료, 대상 bundle, SQLite 연결 | 질문별 진단 계획 |
| state.py | 공통 상태 타입, 안전한 요청 초기화 | 모델 호출 |
| graph.py | 부모 노드·조건부 간선·에이전트 조립 | 임의 shell 실행 |
| agents.py | 범위가 나뉜 Worker·Analysis·Knowledge·Writer·Validator | HTTP 전송 |
| routing.py / model_policy.py | 의도 분류·인자 출처 확인 / 모델 역할 선택 | 변경 승인 |
| planning.py / diagnostics.py | 허용 계획 정규화, 필수 관측, 근거 판정 | 없는 관측의 추측 생성 |
| config_agent.py / config_validation.py | 파일 검토 흐름 / 주장·스냅샷 정적 검증 | 검토 요청만으로 파일 변경 |
| network_diagnosis.py | 제한된 네트워크 단축 체크리스트 | 일반 LLM 재계획 루프 |
| mcp_client.py / docker_tools.py | MCP 전송·재시도·동시성 / 범위 제한된 Docker 작업 | 자유 명령 실행 |
| knowledge.py + knowledge/docker_lessons.json | 검토된 교육 카드 검색·원문 조립 | 벡터 DB, 자동 웹 수집 |
| execution_policy.py | 공통 예산·실행·관측 기반 부분 종료 | 승인된 변경을 자동 재시도 |
| tracing.py / usage.py / observability.py | 로컬 노드 I/O / 토큰 장부 / 선택적 안전 LangSmith span | 추적 데이터를 근거로 변경 승인 |
| session_store.py / approvals.py / clarifications.py | 메시지 / 승인 / 질문 저장 | 그래프 의도 분류 |

노드 하나마다 파일·서버를 만드는 방식이 아니라 함께 바뀌는 책임을 묶었습니다. 그래프 오케스트레이션, 실제 Docker 권한, 외부 추적, 지속 저장의 경계가 핵심입니다. `agents.py`, `graph.py` 안에 여러 역할이 남아 있으므로 “모든 기능이 독립 서비스로 완전히 분리됐다”는 상태는 아닙니다.

## 14. LangSmith: 선택적 관측 도구이지 실행 엔진이 아님

현재 연동 코드는 있으며 아래 두 값이 모두 설정되어야 활성화됩니다. 키는 로컬 환경에만 넣고 Git이나 채팅에 올리지 마세요.

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<로컬에서 직접 설정>
LANGSMITH_PROJECT=docker-ops-agent
```

공통 채팅 실행에서 수동 `RunTree` root를 만들고 실제 노드·MCP 도구·모델 사용량을 자식 span으로 연결합니다. 자동 LangGraph tracing은 해당 요청 문맥에서 꺼서 원문 state 전체를 외부로 업로드하지 않습니다.

전송하는 것은 모델명·역할·provider, 소요 시간·보고받은 토큰, 노드 관계, 해시된 대화 식별자 등 메타데이터입니다. 질문·이력·파일·로그·모델 출력·도구 인자·승인 토큰·원문 traceback은 전송하지 않습니다. 토큰을 보고받지 못한 실패는 임의의 0토큰으로 만들지 않습니다. 로컬 노드 I/O 패널과 로컬 DB 기록은 이 외부 전송 정책과 별개입니다.

응답 metadata의 `langsmith_run_id`, `langsmith_status`로 연결 상태를 확인할 수 있습니다. trace ID가 생성되었다는 것은 외부 클라우드 수신까지 검증했다는 뜻은 아닙니다. 현재 실제 계정 키로 수신 확인을 하지 않았으며, 안전 필드만 전달하는 SDK 대역 테스트를 검증했습니다. SDK 전송 오류가 진단 자체를 실패시키지 않도록 처리합니다. 별도 승인 실행 API까지 모든 요청을 같은 root trace로 감싸는 구조도 아직 아닙니다.

LangSmith는 graph의 분기를 대신 결정하거나 체크포인트를 저장하지 않습니다. “어느 노드가 오래 걸렸는지, 어떤 모델을 몇 번 썼는지”를 보는 도구입니다. 로컬 Ollama 사용에 달러 가격이 자동으로 정확히 매겨지는 것도 아닙니다.

구현: [안전 추적](../backend/observability.py#L18), [실제 노드 span](../backend/tracing.py#L31), [최적화 및 검증 기록](OPTIMIZATION_IMPLEMENTATION.md).

## 부록 A. RAG는 현재 어떻게 동작하나요?

현재는 **검토된 로컬 카드 검색을 결합한 교육 파이프라인**입니다. 카드 5개는 K1 쓰기 계층·데이터 수명, K2 볼륨, K3 bind mount, K4 네트워크, K5 readiness 관련 자료입니다. 모델이 모르는 사실을 인터넷에서 자유 검색하는 파이프라인이나 임베딩 벡터 검색은 아닙니다.

```text
자연어 질문
 → route: LLM이 교육 의도 + topic/lesson_ids 의미 분류
 → education_retrieve → knowledge.retrieve
     → 로컬 docker_lessons.json에서 최대 3개 검색
 → write → writer.compose: 선택한 검토 원문 + 출처 + 미적용 예시 조립
 → validate → validator.code_check: 자료·인용·URL·원문·안전성 일치 검사
 → finalize → END
```

의미 선택 ID가 없을 때에는 키워드 top3 검색과 별도 카드 선택 경로가 코드에 남아 있습니다. 자연어 의도 라우팅 자체를 키워드로 대체한다는 뜻은 아닙니다. 지원하지 않는 주제 또는 검색 결과 없음은 education_unknown으로 끝납니다.

예를 들어 “삭제 후 사진이 사라졌다, 보존 원리를 설명해달라”는 K1/K2로 쓰기 계층과 볼륨을 설명합니다. 다만 현재 앱 경로를 관측하지 않았으므로 예시 `/data`를 실제 경로처럼 주장하지 않고, 볼륨을 나중에 붙인다고 이미 삭제된 데이터가 되살아난다고 말하지 않습니다. “실제 Compose/Dockerfile을 점검해달라”는 이 교육 경로가 아니라 configuration에서 실제 파일을 읽어야 합니다.

자료의 공식 URL은 카드의 출처이며 매 질문마다 해당 웹페이지를 새로 가져오지는 않습니다. 검토 일자 이후 최신성은 자동 보장되지 않습니다.

## 부록 B. 실제 실행 기록의 노드 입출력 예시

다음은 이번 문서 작성 중 새 모델 호출을 한 결과가 아니라, 앞선 최종 검증의 **실제 Ollama + live Docker 기록**에서 뽑은 예시입니다. 원본: [optimization_default4b_final.jsonl](../eval/results/optimization_default4b_final.jsonl). 각 1회 측정이며 모든 환경에서의 속도·정확도 보장은 아닙니다. 아래 I/O는 읽기 좋게 핵심 필드만 축약했습니다.

### B1. 상태 조회: 두 노드로 종료

질문: `ai 상태만 알려주세요`. 선택 대상: bizxray. 기록상 ai 대응 컨테이너는 부재였습니다. 중지된 컨테이너와 혼동하지 않습니다.

| 실제 노드 | 핵심 입력 | 핵심 출력 |
|---|---|---|
| supervisor.route | query, 사용자 이력, 선택 대상·허용 서비스 | route=status, routing_target={scope:service, service:ai} |
| supervisor.status_observe | 확정 서비스 ai | check_service_health(ai) 결과를 E1로 저장. subject_status=absent, 미배포이며 정상/장애로 판정하지 않는 final |

`START → route → status_observe → END`. plan, Worker, analysis, write, validate는 실행하지 않았습니다. 모델 1회(라우터), 도구 1회, 총 4627.7ms, 보고된 토큰 2557입니다.

### B2. 네트워크 명령: 대상 부재를 발견하면 즉시 종료

질문: `/diagnose-network postgres ai`.

| 실제 노드 | 핵심 입력 | 핵심 출력 |
|---|---|---|
| supervisor.route | 명령과 허용 서비스 | route=incident, route_method=slash-command, command.prompt=diagnose_network, service_a=postgres, service_b=ai |
| supervisor.network_diagnose | 명령 인자 | list_containers 결과 E1. 대응 컨테이너 부재 확인 → cause=not_deployed, subject_status=absent. DNS/TCP 생략 사유와 변경 없음 안내 |

`START → route → network_diagnose → END`. 모델 0회, 도구 1회, 총 231.6ms, 토큰 0입니다. coverage는 null(미측정)입니다. “연결 장애를 해결했다”가 아니라 **연결을 검사할 실행 대상이 없어 더 검사하지 않았다**는 결론입니다.

### B3. 교육: 검색·작성·검증은 있지만 Docker 조회는 없음

질문: `컨테이너를 삭제하고 다시 만들었더니 업로드 사진이 사라졌어요. 보존 원리를 설명해주세요.`

| 실제 시작 순서 | 노드 | 핵심 입력 → 출력 |
|---:|---|---|
| 1 | supervisor.route | query → education, 저장 보존 주제, K1/K2 의미 선택 |
| 2 | supervisor.education_retrieve | query, semantic_lesson_ids → knowledge 수신 |
| 3 | knowledge_agent.retrieve | K1/K2 → 검토된 로컬 카드·출처. 실시간 evidence는 없음 |
| 4 | supervisor.write | knowledge, education_context → draft 수신 |
| 5 | writer_agent.compose | 검토 카드 → teaching_guide 원문 조립. 변경 미실행 고지 |
| 6 | supervisor.validate | draft, knowledge → validation_ok와 문제 목록 |
| 7 | validator_agent.code_check | K 인용·URL·원문 일치·안전성 → ok. judge 생략 |
| 8 | supervisor.finalize | 검증 결과·draft → final. approval 없음 |

부모 노드 안에서 자식 노드가 시작되므로 위 순서는 완료 순서와 다릅니다. 실제 trace의 parent_run_id로 관계를 확인할 수 있습니다. 모델은 라우터 1회만 호출했고 도구 0회, 총 5364.0ms, 토큰 2623입니다. 여기서 Writer라는 이름이 보인다고 반드시 생성 모델을 추가 호출한 것은 아닙니다.

## 부록 C. 부모 노드의 상태 입출력 빠른 참조

모든 노드는 AgentState 전체를 받을 수 있으며 아래는 주로 읽고 갱신하는 필드입니다. 관측 Worker 등의 세부 I/O는 4절과 B 예시를 함께 보세요.

| 부모 노드 | 주 입력 | 주 출력 |
|---|---|---|
| route | query, history, confirmed_answer | route 및 분류·대상·질문 정보 |
| human_question | clarification, clarification_rounds | final 질문, clarification, approval=null |
| target_selection | requested_project, 고정 대상 | final, target_selection_required=true |
| routing_unavailable / policy_block | 분류 실패 또는 허용 범위 위반 | 조회·변경 미실행을 알리는 final |
| chat_answer | query, 제한된 history | draft=final, 직접 AnswerToken |
| status_observe | query, routing_target | evidence 1개, subject_status/lifecycle/health, final |
| network_diagnose | command.args | 체크리스트 evidence, diagnosis, final, coverage=null |
| education_retrieve | query, semantic_lesson_ids | knowledge, evidence=[], analysis_policy=skip |
| education_unknown | 검색 결과 없음 | 근거 부재 안내 final, answer_status=unavailable |
| configuration | query, route, 현재 대상 | 내부 4노드의 evidence/config_review/config_validation/config_metrics/final과 조건부 proposal/approval |
| plan | query, route, routing_target, 표준 prompt | tasks, pending, analysis_policy, analysis_reason |
| gate | pending, 기존 evidence | 정규화·중복 제거된 pending, 안전한 analysis_policy |
| research | pending, query, known observations | evidence, worker_reports, pending=[], 시간·재시도 합계 |
| replan_check | evidence, analysis_policy, replan_rounds | diagnosis, evidence_sufficient, pending gaps, next_step. 부재면 final도 구성 |
| analysis | query, evidence, diagnosis, analysis_reason | analysis, analysis_used=true |
| write | query, route, evidence/knowledge, analysis, validation_problems | draft, 교육이면 lesson_selection |
| validate | draft, route, evidence/knowledge | validation_ok/problems/attempts. 한도 거절 시 안전 원고로 대체 |
| finalize | draft, validation, 충분성·원인·관측 | final, 조건을 만족할 때만 proposal/approval. 실제 변경 없음 |
| facts_finish | 마지막 evidence, 중단 사유 | 관측 한계가 드러나는 final, partial, 승인·제안 제거 |

노드 개수, 업무 이벤트 개수, 도구 호출 수는 서로 다른 지표입니다. 같은 노드가 재수집으로 반복될 수 있고 부모/자식이 별도 노드로 보입니다. 실제 사용되지 않은 노드를 프론트에 미리 표시하지 않는 현재 방식과 이 전체 구조도를 구분해 읽어주세요.

## 작성·검증 범위

부모 그래프의 간선 선언, 서브그래프 노드, 자원 수명, JSON/SSE 공통 실행기, SQLite 주입, 모델 역할과 LangSmith 구현을 직접 대조했습니다. 저장된 graphify 그래프가 없어 그래프 검색 대신 소스를 확인했습니다. PDF는 벡터로 생성하고 전 페이지를 PNG로 렌더링해 한글·글자·화살표·잘림을 점검했습니다. 기존 실제 실행 기록을 사용했으며 이번 문서 작성으로 Docker 컨테이너를 변경하거나 새로운 모델 성능 실험을 하지는 않았습니다.

흐름도 재생성 코드: [render_current_langgraph.py](../scripts/render_current_langgraph.py). 구조 변경 시 먼저 소스와 이 문서를 갱신한 뒤 PDF/PNG를 다시 생성해야 합니다.
