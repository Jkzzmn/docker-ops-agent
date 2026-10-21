# 필요한 경우에만 묻는 Human Question 노드

2026-10-21, API 2.3.0. 신규 프레임워크·의존성 없이 기존 LangGraph·SQLite·Vue를 확장합니다.

## 묻는 조건

의도 분류 LLM이 **반드시 필요한 대상·작업이 빠져 서로 다른 답변에 따라 다음 route/서비스·도구 인자가 달라진다**고 판단한 경우에만 `clarify`를 선택합니다. 모델은 현재 선택된 프로젝트와 실제 허용 서비스명을 제공받습니다.

| 예 | 처리 |
|---|---|
| “현재 Compose와 Dockerfile의 오류·비효율을 확인해주세요” | configuration 직행, 추가 질문 없음 |
| “ai 상태만 알려주세요” | status 직행 |
| “전체 서비스 목록과 상태를 보여주세요” | 전체 조회, 특정 서비스 선택을 강요하지 않음 |
| “볼륨을 설명해주세요” | education 직행 |
| “서비스가 안 돼요. 다른 건 보지 말고 그 서비스 하나만 진단해주세요” | 여러 서비스 중 대상이 없으므로 확인 질문 |
| “그거 처리해주세요” (앞선 대상 없음) | 의도·대상 확인 |
| “그럼 그걸 고쳐주세요” (직전 Dockerfile 검토 요청 있음) | LLM 2차 문맥 분류로 해결되면 질문 생략 |
| 모델 연결/JSON 실패 | 확인 질문을 꾸며내지 않고 기존 unavailable 안내 |

선호·교육 수준을 매번 묻지 않습니다. 읽기 도구가 알아낼 수 있는 파일 경로나 이미 명시된 대상을 다시 묻지 않도록 지시합니다. 질문 필요성도 LLM 판단이므로 완벽한 분류 보장은 아닙니다.

`status/incident`는 LLM이 `target_scope=project|service|unspecified`와 실제 `service`를 반환합니다. 특정 서비스가 사용자 문맥에서 확인되지 않으면 모델이 추측한 서비스나 기본 서비스로 조회하지 않고 질문합니다. 답변 후에는 가장 최근 확정 답변을 우선합니다. 실제 서비스명 문자열의 출처를 검증하므로 별칭만 입력하면 다시 물을 수 있습니다. 이는 키워드 의도 분류가 아니라 도구 인자 검증입니다.

번호 선택지는 실제 허용 서비스명 또는 고정된 읽기 전용 목적만 허용합니다. 모델이 임의로 만든 실행·복구 선택지는 버튼으로 제공하지 않고 자유 입력으로 확인합니다. 질문의 전체/목록 같은 단어가 조회 범위를 바꾸지 않도록, 상태 계획·근거 충족 검사에는 `routing_target={scope,service}`를 구조화해 전달합니다.

## 노드 입출력

```text
route ─ 명확 → 기존 chat / education / status / incident / configuration / change
      └ 필수 정보 부족 → human_question → END (답변 대기)
                                ↓ SQLite
번호/직접 답변 → 검증·1회 접수 → 원래 요청 + 확인 질문 + 사용자 답변 → 새 route 실행
```

| 단계 | 입력 | 출력/조건 |
|---|---|---|
| route | 현재 질문, 필요한 경우 짧은 사용자 문맥, available_services | route=clarify와 question/reason/options(0~3개). 명확하면 기존 route |
| human_question | 질문·이유·선택지·확인 횟수 | 실제 질문 표시, clarification 반환. 조회·변경·승인 객체 없음 |
| API 완료 처리 | question, original query, thread_id, Docker scope | 랜덤 ID·15분 만료의 pending 질문을 SQLite에 저장 |
| reply API | thread_id 및 choice 정수 **또는** answer 문자열 | 선택 번호를 서버에 저장된 문구로 매핑. 원래 요청/질문/답변을 합쳐 재실행 |

`1`, `2`, `3` 또는 `1번` 등을 직접 입력할 수 있고, “ai의 상태만 확인해주세요” 같은 자연어도 가능합니다. 선택지가 적절하지 않으면 직접 답변하면 됩니다. 질문을 취소한 뒤 새 요청도 보낼 수 있습니다.

**LangGraph interrupt/Command.resume 방식은 아닙니다.** 기존 그래프의 무상태 구조를 유지하고 첫 실행을 끝낸 뒤, SQLite에 저장한 질문과 답변으로 새 실행을 만듭니다. 현재 확인 지점은 작업 시작 전 의도·대상 확인입니다. 모든 Worker 중간 지점에 임의로 삽입하거나 이전 stack/checkpoint에서 이어 실행하지 않습니다. 따라서 답변 후 Docker 증거는 새로 조회합니다.

## 안전·재사용·복원

- 질문은 동일 thread_id 및 Docker context/engine/project/root/allowlist scope에 묶입니다. 다른 대화/대상의 질문 ID는 접수하지 않습니다.
- 한 질문은 한 번만 답변/취소할 수 있습니다. 잘못된 번호는 소비하지 않습니다. 만료·재사용·새 요청으로 대체된 질문은 거부합니다.
- 같은 대화에서 새로운 일반 /chat 요청을 보내면 기존 대기를 superseded 처리합니다. UI는 대기 카드에 답하거나 취소하도록 안내해 “1”을 새 일반 질문으로 오해하지 않습니다.
- 같은 원래 요청에서는 추가 확인을 최대 두 차례 허용합니다. 그래도 불명확하면 새 구체적 요청을 안내하고 끝내 질문 루프를 막습니다.
- 대기는 SQLite에 저장되므로 같은 Docker 대상의 서버 재시작·브라우저 새로고침 후 복원할 수 있습니다. 이전 노드를 실행한 것처럼 다시 그리지는 않습니다.
- **답변은 실행 승인이 아닙니다.** 기존 파일 변경/배포 승인 API와 완전히 분리됩니다. 위험 명령 답변은 안전 정책으로 차단되고, 번호 선택으로 restart/build/patch를 실행하지 않습니다.
- 비밀번호/API 키/.env 원문 요구나 위험 실행 선택지는 거부합니다. 사용자 자유 입력도 평소처럼 안전 정책·도구 allowlist를 거칩니다.
- 답변 재개는 POST + SSE로 실제 NodeStarted/Completed를 실시간 표시합니다. 최종 응답을 받은 뒤 모든 노드를 일괄 만들어 붙이지 않습니다.
- 답변 접수 직후 네트워크가 끊기면 해당 질문은 이미 소비됐을 수 있습니다. 자동 재전송하지 말고 대기 상태를 확인하거나 새 요청을 보내세요. 여러 그래프 요청을 동시에 같은 대화로 보내는 작업의 순서 보장은 하지 않습니다.
- 로컬 무인증 API와 브라우저 localStorage 대화 식별자를 사용합니다. 인터넷 공개/다중 사용자 운영을 위한 인증은 별도 필요합니다.

## API 예

```json
POST /clarifications/<id>/reply?stream=true
{"thread_id":"...","choice":2}
```

```json
POST /clarifications/<id>/reply
{"thread_id":"...","answer":"ai 서비스 상태만 확인해주세요"}
```

`GET /clarifications/pending?thread_id=...`는 새로고침 시 pending 객체를 반환하고, `POST /clarifications/<id>/cancel`은 thread_id를 받아 취소합니다. 번호와 자유 입력을 동시에 보내면 422입니다. 다른 대화/대상은 404, 만료·재사용은 409입니다. mutation approval 토큰을 생성하거나 실행하는 API가 아닙니다.

SQLite에는 질문·답변이 남습니다. 15분은 **답변 가능 기간**이지 보관 데이터 자동 삭제 기간이 아닙니다. 원래 요청과 답변에 민감 정보를 입력하지 마세요.

## 실제 실행 검증 — 2026-10-21

실행기: `python -m eval.human_question_live`. Ollama `qwen3:4b-instruct`/`qwen3:8b`, live Docker MCP, 프로젝트 bizxray. 테스트 모델 대역이나 승인 실행은 사용하지 않았습니다.

입력: “서비스 상태를 보고 싶은데 어느 서비스인지 아직 고르지 않았습니다. 임의로 다른 서비스를 조회하지 말고 제가 고른 서비스 하나만 상태를 확인해주세요.”

| 단계 | 실제 입출력 |
|---|---|
| route | 위 입력 → clarify, 대상 미확정 |
| human_question | “어느 서비스를 대상으로 확인할까요?” → 1. ai / 2. postgres / 3. backend, 직접 입력 가능 |
| 첫 실행 종료 | 실행 노드 route·human_question만 2개, 도구 0회, RAG 0개, 승인 없음 |
| 번호 답변 | choice=2 → 서버가 postgres로 매핑 → 새 route=status, routing_target={scope:service,service:postgres} |
| 자연어 답변 (별도 실행) | “frontend 상태만 알려주세요” → status, service=frontend. 세 버튼에 없는 네 번째 서비스도 가능 |
| plan·gate·runtime 관측 | 각 실행에서 선택한 서비스의 check_service_health만 호출. 다른 서비스 조회·전체 목록 조회 없음 |
| replan·write·validate·finalize | 대상 컨테이너가 없어 found=false, 근거 보강 2회까지 동일 서비스 재조회. 작성·검증 최대 3회 후 검증 미통과 안내로 종료 |

두 흐름 모두 접수·선택 대상·실시간 이벤트·중복 답변 거부(409) 검증은 통과했습니다. **실제 컨테이너 상태를 관측하거나 보고서 검증에 성공했다는 뜻은 아닙니다.** 현재 postgres/frontend 컨테이너가 없어 각 3회의 조회가 실패했고 `validation_ok=false`입니다. 실패 관측을 E1처럼 인용하는 기존 작성기 문제도 검증기가 잡았습니다. 이번 변경에서 컨테이너를 만들거나 이 작성기 품질 문제를 해결하지 않았습니다.

[전체 원시 이벤트·노드별 입출력](../eval/results/human_question_live_20261021.json)에 각 노드 NodeStarted.input / NodeCompleted.output와 SSE 수신 시간이 있습니다. 최초 이벤트는 번호 답변 13ms, 자연어 답변 10ms, 최종 응답은 각각 약 16.3초/16.2초였습니다. 완료 뒤 가짜 노드 목록을 만드는 방식이 아닙니다.

브라우저에서도 대기 질문 → 서버 재시작·새로고침 복원 → frontend 자연어 답변 → route가 먼저 실행 중으로 표시 → 실제 33개 실행(반복 포함) 종료를 확인했습니다. 복원 시 과거 노드가 실행된 것처럼 만들지 않고 “대기 질문 복원 · 이전 실행 기록 없음”을 표시합니다.

- [대기 카드 화면](../eval/results/human_question_ui_pending_20261021.jpg) (복원 표시 문구 보완 전 캡처)
- [자연어 답변 결과 화면](../eval/results/human_question_ui_result_20261021.jpg)
- [초기 범위 오류 기록](../eval/results/human_question_live_20261021_scope_attempt.json): 질문 문구의 “목록” 때문에 전체 조회한 실패. 구조화된 routing_target 전달로 수정 후 두 흐름 재검증.

자동 검증: backend 222개, 프론트 trace/SSE 4개, production build 통과. 보관/만료/취소/다른 대화·대상/동시 중복 답변/위험 답변/최대 확인 횟수를 포함합니다. 소규모 실제 모델 평가는 일반적인 정확도 보장이 아니며, 반복 실패 기록도 보존합니다.

실제 모델 확인 조건 평가는 [최근 실행 9/9](../eval/results/clarification_gating_live_20261021.json), 기존 의도 회귀는 [19/19](../eval/results/routing_llm_20261021.json)였습니다. 하지만 **동일 조건의 앞선 반복은 7/9**였고, 필수 질문 생성 대신 잘못된 모델 출력으로 unavailable이 발생했습니다([실패 기록](../eval/results/clarification_gating_live_20261021_attempt2.json)). 이후 출력 기록을 추가한 반복에서 9/9였으므로 이를 완벽히 해결했다거나 항상 같은 문구·선택지를 만든다고 주장하지 않습니다. 잘못된 모델 출력에서는 도구를 실행하지 않습니다. 추가적인 독립 질의 집합·반복 안정성 평가는 남아 있습니다.
