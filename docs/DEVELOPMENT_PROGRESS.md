# 개발 진행 결과 — Supervisor v3

## 코드 리뷰 반영 (2026-10-20)

코드 리뷰에서 확인한 미구현·결함을 고쳤고 회귀 테스트(tests/test_hardening.py)로 고정했다. 자동 테스트 137개 통과.

- 종료된 OOM 컨테이너(`restart: "no"`, live 데모)가 승인까지 가지 못하던 문제: 종료 상태에서는 현재 stats를 필수 근거에서 제외하고, `exited`도 복구 대상 상태로 인정. inspect(OOMKilled·137)와 OOM 이벤트·로그의 교차 확인은 유지.
- 비밀 마스킹: JSON 키, 접속 URL의 비밀번호, Bearer/Basic, `*_SECRET_KEY` 등 접두·접미 키, AWS/GitHub/Slack 토큰 형태.
- 위험 명령 판별: 단어 `shell`/`powershell`이 아니라 명령 모양(`docker exec`, `sh -c`, `compose down -v` 등)만 차단. Validator도 같은 규칙.
- Writer/Analysis 프롬프트: 실패한 관측은 ID 없는 "조회 불가" 목록으로 분리해 인용할 수 없게 함(실제 실행에서 실패한 stats [E3]를 인용해 검증이 3회 실패한 사례의 원인).
- graph checkpointer 제거(ADR-009): 요청마다 독립 실행, 서버 메모리 누적 없음. SSE는 `values` 스트림의 마지막 부모 상태를 사용.
- 승인 후 복구 확인: health가 정상이 될 때까지 폴링(`RECOVERY_WAIT_SECONDS`, 기본 20초), 조치 전후 inspect 비교.
- 에러 처리: MCP Prompt 조회 실패, 채팅·Writer·Analysis 모델 실패, `POST /chat` 그래프 예외를 각각 명시적으로 처리.
- 노드 이름을 체크리스트와 일치(`research`, `replan_check`), gate가 분석 정책을 검증.
- MCP 표면: Prompt `diagnose_oom`/`diagnose_network`(슬래시 명령), Resource `docker://projects/{project}/compose`, runbook 별칭 `oom-killed`, Tool `inspect_listening_ports`.
- 시나리오 C 보강: 호스트 게시 포트와 컨테이너 포트 혼동, loopback 전용 바인딩 탐지. 시나리오 A/C의 검토용 설정 제안(`proposals.py`, 적용하지 않음).
- 평가: 모델 호출 수·토큰 계측, 도구 동시 실행 비율, `/metrics` 카운터, 새 fixture 3종과 질의 9개, `eval/example_run.py`.
- 프런트엔드: 흰색·청록·오렌지 디자인 시스템으로 재작성, 실행 경로 레일, 승인/복구 비교 표, 설정 제안 패널.

## 교육용 확장 (2026-10-20)

- education 전용 경로와 독립 Knowledge 서브 그래프를 추가했다. 로컬 카드 5개를 키워드 검색하여 실제 Writer/Validator 입력에 전달한다.
- 공식 문서에 기반한 쓰기 계층·이름 있는 볼륨·바인드 마운트·네트워크/포트·준비 상태를 설명한다. 카드 개정 번호·검토일·공식 문서 URL을 포함한다.
- “저장한 내용이 모두 사라졌다”는 질문에는 원리·가설·안전한 질문·예방을 안내한다. 실시간 점검을 명시하지 않으면 MCP 관측과 복구 승인을 실행하지 않는다.
- K 교육 인용과 E 실시간 근거를 분리했다. 자료 없음은 모델 없이 유보하고, 교육 검증 3회 실패 시 최종 초안을 숨긴다.
- API/SSE에 knowledge와 retrieval 메타데이터를 추가하고 UI에 교육 이벤트·공식 출처·학습 예제를 표시했다.
- 교육 자료와 라우팅·검증·API/SSE 회귀 테스트는 tests/test_education.py에 있다. 이는 테스트 전용 모델 double이며 제품에는 실제 모델만 사용한다.
- 확장 후 자동 테스트 83개 통과, frontend production build 성공. 아래 60개 결과는 이전 Supervisor 구현 시점의 기록이다.
- 최초 실제 Ollama qwen3:4b-instruct의 3개 자동 smoke는 통과했지만 문장 검토에서 재시작과 데이터 손실·마운트 가림을 혼동한 설명을 발견했다. 원시 eval/results/education_ollama_smoke.json은 초기 실패 사례로 보관하며 교육 정확성의 합격 자료로 사용하지 않는다. 이후 설명 길이·불변 원리·Validator의 문장별 모순 검사를 강화했다.
- 8B 자유 생성 재검증에서도 오류가 남아 교육을 추출형 RAG로 변경했다. 모델이 카드 ID만 선택하고 검토 원문·예시·출처를 코드로 조합한다. 교육 Validator는 원문 완전 일치·안전 문맥 보존만 검사하고 LLM judge는 생략한다. 임의의 모델 사실 문장·추가 명령은 출력에 들어가지 않는다.
- 최종 추출형 교육을 실제 Ollama qwen3:4b-instruct로 3개 smoke 검증했다. 데이터 유실/마운트 설명은 1차 원문 일치 검증 통과, 미지원 주제는 유보, 모두 Docker Tool/변경 0회였다. 원시 eval/results/education_extractive_smoke.json을 기준으로 한다. 검토 카드 자체의 정확성과 최신성은 사람이 유지해야 한다.
- 임베딩·벡터 DB·자동 웹 갱신·데이터 복구·저장소 전용 실시간 원인 분류는 구현하지 않았다.

기준: 사용자의 실행 경로 제거·조건부 분석·Supervisor/서브 에이전트 설계 요청.

## 구현

- 모델은 Ollama/OpenAI만 지원. 기본 Ollama + 실제 Docker(live).
- 모델 대체 응답 코드와 전용 데모 실행 스크립트 제거.
- 일반 질문·Docker 개념 질문은 fast 모델이 바로 스트리밍하고 종료.
- 상태-only는 단일 상태 Tool. 계획용 모델과 Worker 계획 모델, Analysis 생략.
- Supervisor가 최초 계획, 역할별 위임, 근거 재검토, 다음 단계 결정을 담당.
- Runtime/Configuration/Network는 독립 state·system prompt·capability를 가진 컴파일된 LangGraph 서브 그래프.
- Worker가 자기 범위 안에서 관측을 보완하고 결과·성공/실패·계획 이유를 보고.
- Supervisor가 추가 수집/분석/직접 보고 중 선택. 근거 부족은 계획의 분석 생략을 override.
- Analysis/Writer/Validator도 별도 state와 역할 prompt를 가진 독립 서브 그래프.
- evidence 원장은 부모에서 병합하고 고유 ID를 부여.
- UI에 agent/node, 위임, Supervisor 결정 이유, 분석 생략·실행을 표시.
- README에 전체 state, 모든 부모 노드의 입력·출력, 조건부 edge 우선순위, Worker 계약, 질문별 경로, 승인 API 경계 작성.
- 기존 서명 승인·중복 차단·Validator 실패 종료·부분 관측 실패 처리는 유지.

## 검증 범위

자동 테스트는 테스트 전용 모델 double로 제어 흐름을 검증하며 실제 stdio MCP를 사용한다. 실제 모델 요청 검증은 `eval/check_supervisor.py`로 별도 실행한다. 프론트엔드는 production build로 검사한다.

- 자동 테스트 60개 통과, production build 성공.
- 실제 Ollama `qwen3:4b-instruct`로 개념 질문(chat/Tool 0회), 상태-only(status/Tool 1회/분석 없음), OOM incident(Supervisor→Runtime/Configuration→Analysis→Writer→Validator)를 검증했다.
- 위 실모델 검증의 Docker 관측은 고정 fixture이며 변경은 승인하지 않았다. 원시는 `eval/results/supervisor_ollama_smoke.json`에 있다.
- SSE는 `subgraphs=True`로 자식 namespace·agent 이벤트와 Writer 토큰을 전달한다. 최종 응답과 토큰 결합 결과가 같은지 통합 테스트로 확인했다.

자동 테스트의 상세 결과와 실제 모델 smoke 파일을 기준으로 판단하며 과거 실행 경로의 점수를 현재 성과로 표현하지 않는다. GitHub CI에는 테스트·빌드·Compose config 검사만 두고 모델 요청을 자동 과금하지 않는다.

## 상세 설계

[README](../README.md)에 노드별 input/output과 trigger를 코드와 같은 순서로 기록했다.

- 부모: route, chat_answer, policy_block, plan, gate, dispatch, supervisor_review, analysis, write, validate, finalize.
- 관측 서브 그래프: plan→observe→report.
- 분석: cross_check.
- 작성: compose.
- 검증: code_check→조건부 judge.

## 보관 및 남은 범위

제거된 실행 경로의 이전 평가 JSON·구조 그래프·화면은 `/private/tmp/docker-ops-previous-reports.DtvVVh`에 보관했다. 데이터 원본을 삭제하지 않았지만 임시 디렉터리이므로 장기 보관 위치는 아니다.

현재 HITL은 graph 종료 후 별도 action API 방식이다. LangGraph interrupt/resume, 영속 graph checkpoint, 운영 인증, POST streaming, 독립 정답셋을 이용한 모델 정확도·비용 측정은 후속 범위다.
