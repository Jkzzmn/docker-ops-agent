# Architecture Decision Records

기준일: 2026-10-20. 구현 상태와 후속 결정을 함께 기록한다.

## ADR-001 — MCP stdio와 제한된 capability

상태: 채택.

FastAPI lifespan이 로컬 MCP subprocess를 연결하고 재사용한다. Agent는 Docker socket을 직접 호출하지 않는다. MCP는 고정 명령의 인자 배열만 구성하며 임의 shell tool을 제공하지 않는다. 서비스는 allowlist로 제한하고, DNS/TCP probe는 허용 서비스명과 Compose에 선언된 container port만 사용한다. Probe에는 해당 이미지의 Python 3가 필요하다.

장점은 도구 스키마와 제어 경계가 분명하다는 점이다. 한계는 Docker daemon 자체가 고권한이며, stdio와 allowlist가 OS 권한 분리를 대체하지 못한다는 점이다.

## ADR-002 — fixture와 live를 분리

상태: 채택.

기본은 실제 Ollama와 `DOCKER_MODE=live`다. 명시적 fixture mode는 Docker 고정 관측 평가와 테스트용으로만 둔다. 앱 모델은 Ollama/OpenAI 두 가지이며 어떤 Docker mode에서도 실제 모델을 호출한다. 실제 Docker 관측은 `eval/run_live_check.py`로 별도 저장한다.

## ADR-003 — 영속 승인과 별도 action API

상태: 채택. 승인 API는 유지하며 graph 저장은 SQLite checkpoint로 변경(ADR-012).

승인 대기를 장시간 열린 HTTP 연결에 묶지 않고 graph 종료 뒤 별도 API에서 처리한다. SQLite에 action과 결과를 저장하고 pending→executing 전이를 조건부 UPDATE로 선점한다. HMAC 토큰은 action ID, 도구, 인자, 만료시간을 묶고 MCP가 이를 검증·소모한다. Docker 프로젝트·mode·Compose 구성 범위가 달라지면 이전 승인은 사용할 수 없다.

Mutation 응답이 유실되면 실행 여부를 알 수 없으므로 자동 재시도하지 않는다. 프로세스가 executing 상태에서 종료돼도 자동 재실행하지 않는다. 이 방식은 LangGraph `interrupt()`/resume API와 다르다. SQLite checkpoint를 연결한 뒤에도(ADR-012) 중단된 실행을 자동 재개하지 않으며, 승인 대기 상태는 SQLite action 레코드가 유일한 원천이다.

## ADR-004 — GET SSE 유지

상태: 개발용 채택, 운영 전 변경 필요.

기존 EventSource와 호환되는 GET SSE를 유지한다. `AnswerToken`, `TaskCompleted`, `TaskFailed`, `PolicyBlocked`, `FinalAnswer`, `[DONE]` 계약을 사용한다. 질의가 URL과 접근 로그에 남을 수 있으므로 로컬 개발에 제한한다. 운영용 인증된 POST streaming은 별도 후속 작업이다.

## ADR-005 — 변경은 검증된 OOM의 제한적 재시작만

상태: 채택.

검증 성공, 필수 evidence coverage 충족, OOM 근거, 대상의 현재 비정상 상태(unhealthy·restarting·exited)가 모두 확인될 때만 재시작 승인 객체를 만든다. 종료된 컨테이너는 현재 stats를 얻을 수 없으므로 stats를 필수 근거에서 제외하되, inspect의 OOMKilled/Exit 137과 Docker OOM 이벤트(또는 OOM 로그)의 독립 교차 확인은 계속 요구한다. readiness/network는 `proposals.py`가 만든 검토용 설정 제안(적용되지 않음)만 제공한다. 자동 Compose patch, recreate, volume 삭제는 제공하지 않는다.

재시작 명령 성공과 정상화 성공은 별개다. API는 command status와 `recovery_ok`를 분리하며, 후자는 Docker lifecycle/health로만 확인한다. HTTP readiness와 장기 안정성 검증은 후속 범위다.

## ADR-006 — 규칙 기반 근거 기준과 LLM 역할 분리

상태: 채택.

필수 관측과 승인 허용 조건은 코드로 판정한다. 부모 Supervisor가 모델로 계획·위임·최종 분석 여부를 결정하고, 관측 전문가는 독립 StateGraph에서 계획·관측·보고를 수행한다. Analysis/Writer/Validator는 다른 state와 prompt의 별도 서브 그래프다. 모델 연결은 재사용하지만 권한과 state를 분리한다.

## ADR-007 — 질의별 실행 비용과 조건부 분석

상태: 채택.

일반 대화와 개념 설명은 fast 모델 한 번으로 종료한다. 상태-only는 요청한 도구만 사용하며 계획용 모델과 원인 분석을 생략한다. Incident에서는 계획의 auto/skip/required 정책을 actual evidence로 다시 검토한다. 근거 부족·unknown·낮은 confidence는 skip을 override하고 분석을 수행한다. 정상 상태나 명확한 원인이면 불필요한 Analysis 호출을 생략한다. 그래도 Writer/Validator와 승인 정책은 우회하지 않는다.

## ADR-008 — 교육 검색과 운영 관측의 분리

상태: 채택.

Docker 학습과 일반적인 데이터 유실 상담에는 education 경로를 추가한다. Knowledge 독립 StateGraph가 공식 문서와 대조한 한국어 지식 카드 5개에서 상위 3개를 검색한다. 소규모 corpus이므로 외부 의존성·임베딩·벡터 DB 대신 키워드 검색을 사용한다. 이는 검색 문맥을 실제 생성 입력에 넣는 소형 RAG이며 실시간 웹검색이나 자동 문서 수집은 아니다.

카드는 source_url/reviewed_at/revision을 가진다. 교육 인용 K와 live evidence E를 별개로 유지하며 Writer/Validator에 서로 다른 계약을 적용한다. 자료가 없으면 모델 호출 없이 유보한다. 교육은 계획용 LLM·관측 Worker·Analysis·MCP를 생략하고 변경 승인을 생성하지 않는다. 실제 점검을 명시하면 기존 진단 경로를 사용한다. 저장소 장애 자동 진단·데이터 복구는 별도 범위다.

실제 4B/8B 자유 생성과 LLM judge 조합에서 교육 오류를 놓친 사례를 확인하여 교육만 추출형 RAG로 제한했다. 모델은 검색된 카드 ID를 선택하고 코드는 검토 원문·예시·출처를 발췌·조합한다. Validator는 원문과의 완전 일치를 검사하며 교육 LLM judge를 생략한다. 미등록 ID·모델 실패는 검색 결과 전체를 사용하고 K1/K2 안전 문맥을 보존한다. 자유 생성의 자연스러운 맞춤 설명보다 검토 문서의 신뢰성을 택했다. 카드 자체의 의미 정확성·최신성은 사람이 검토해야 한다.

## ADR-009 — Stateless graph: checkpointer 제거

상태: 과거 결정. 2026-10-21 ADR-012로 대체. 아래는 당시 결정 기록이다.

LangGraph는 `InMemorySaver`를 쓰면 실행마다 새 thread의 체크포인트를 프로세스 메모리에 계속 쌓는다. 요청마다 고유한 thread id를 쓰는 이 서비스에서는 해제되지 않는 메모리 증가이고, `/chat/stream`이 최종 상태를 `aget_state`로 읽으면 서버 프로세스에 의존한다. 그래서 graph를 checkpointer 없이 컴파일하고, SSE는 `stream_mode=["custom","values"]`로 부모 graph의 마지막 values를 최종 상태로 사용한다. 대화 이력은 클라이언트가 보유한 `thread_id`와 SQLite, 승인은 SQLite에 있으므로 어떤 서버 인스턴스가 요청을 받아도 같은 결과를 낸다. 대가는 graph 수준 interrupt/resume을 쓸 수 없다는 점이며, HITL은 ADR-003의 별도 API가 이미 담당한다. MCP 서버 프로세스의 사용된 승인 ID 집합은 방어 계층일 뿐 권위는 SQLite의 조건부 UPDATE에 있다.

## ADR-010 — 설정 변경은 제안만, 적용하지 않는다

상태: 채택.

readiness와 network 원인에는 승인할 수 있는 안전한 자동 조치가 없다. `proposals.py`는 증거에서 결정적으로 `compose override` YAML(DB healthcheck와 `service_healthy`, 공유 network와 alias) 또는 설정 안내(호스트 포트 대신 컨테이너 포트, loopback 바인딩)를 만들어 `applied:false`로 반환한다. 파일을 쓰거나 MCP write tool을 호출하지 않는다. 사람이 검토해 병합한다. 미리보기·롤백을 갖춘 `apply_compose_patch`는 이 범위에 없다.

## ADR-011 — MCP Prompt를 슬래시 명령으로 노출

상태: 채택.

`/diagnose-oom <service>`는 같은 이름의 MCP Prompt를 실제로 조회해 plan에 주입한다. network 명령은 이후 짧은 체크리스트로 변경했으며 `NETWORK_FAST_PATH.md`를 참조한다. 정확한 인자 수와 allowlist 서비스만 명령으로 인정하며, 모양이 다르면 일반 질의로 분류하고 허용 밖 서비스는 도구 호출 없이 차단한다. 명령은 LLM 분류 호출을 건너뛰어 호출 한 번과 오분류 위험을 줄인다.

## ADR-012 — 로컬 SQLite checkpoint와 제한된 대화 맥락

상태: 2026-10-21 채택, ADR-009 대체.

미니프로젝트의 대화 연속성을 위해 공식 AsyncSqliteSaver를 서버 생명주기에 연결한다. engine/project scope + 브라우저 thread_id로 저장하고 서버 재시작 후 복원한다. SQLite 메시지 12개 중 제한된 사용자 문맥·직전 답변을 첫 라우팅부터 제공한다. 모델의 needs_context 플래그가 아니라 실제 사용자 발언으로 서비스 출처를 검증한다. 새 요청은 fresh_state로 명령·이벤트·관측·승인을 초기화하며 미완료 mutation 자동 재개나 interrupt/resume API는 노출하지 않는다. 동시 같은 대화 실행은 거부한다. DB는 제출 대상에서 제외하며 장기 기억 요약·자동 보존 정책·화면 대화 목록 복원은 별도 범위다.
