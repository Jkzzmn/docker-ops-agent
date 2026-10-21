# 신뢰성 개선 구현 계약 — API 2.5

2026-10-21 개선 계획 F1–F5의 현재 구현입니다. 이전 README의 긴 실행 덤프·평가 보고서는 당시 버전의 기록이며, 현재 계약은 이 문서를 우선합니다. 일반 자연어 전체의 무오류를 보장하지 않습니다.

후속 [실행 최적화 정책](OPTIMIZATION_IMPLEMENTATION.md)에 따라 기본 검증 시도는 1회이며, 근거 부족/unknown은 모델 분석 대신 구조화 관측만 반환합니다. JSON/SSE 공통 실행 예산과 역할별 라우터 설정을 사용합니다.

## 1. LLM 라우팅과 대상 경계

`route` 입력은 현재 질의, 허용 서비스, 고정된 서버 Docker engine/context/project/path, 검토된 교육 카드 카탈로그와 최근 사용자 발언 3개(각 500자)·직전 답변 800자입니다. 처음부터 제한된 맥락을 제공해 추가 분류 라운드를 없앴습니다. 현재 질문이 우선이며 과거 답변은 실관측·승인 근거가 아닙니다. [SQLite 체크포인터·재시작·상태 초기화](CONVERSATION_MEMORY.md)를 참조하세요.

분류 결과는 `route`, `project_reference(current/explicit/ambiguous)`, `requested_project`, `target_scope(project/service/unspecified)`, `target_cardinality(all/one/unknown)`, `service`, `lesson_ids`, `known_lifecycle`, `lifecycle_quote`를 포함합니다. Ollama는 JSON Schema로 출력을 제한하고 서버에서도 Pydantic·allowlist·사용자 발언 내 서비스명 출처를 검증합니다. [Ollama 구조화 출력](https://docs.ollama.com/capabilities/structured-outputs)을 사용하며 키워드 intent 분류로 대체하지 않습니다.

처음 education으로 분류했지만 원문에 Dockerfile/Compose/빌드·설정 파일 같은 실제 조회 가능 자원 참조가 있을 때만, 카드 카탈로그가 없는 별도 **quality LLM 의도 검사**를 수행합니다. `inspect_current_files`, `wants_edits`, 원문의 정확한 조회 요청 `request_quote`를 확인합니다. 모델이 조회라고 판단해도 인용이 원문에 없거나 파일 참조가 인용에 없으면 도구 경로로 바꾸지 않습니다. 일반 사진 유실·영속성 교육처럼 파일 참조가 없는 요청에는 검증 모델을 추가 호출하지도, true 추측만으로 파일을 읽지도 않습니다. 의도 선택은 계속 LLM이며 이 자원 출처 검사는 서비스명·파일 경로 allowlist와 같은 실행 경계입니다. `metadata.route_file_intent_check`와 실제 모델 호출 장부에 표시합니다.

| 조건 | 다음 노드 / 결과 |
|---|---|
| 요청 프로젝트가 고정 대상과 다름 / 모호함 | `target_selection` → 종료, Docker·파일 도구 0회 |
| 한 서비스가 필요하지만 대상 미정 / 허용되지 않은 이름 | `human_question` → 사용자 번호·자연어 답변 대기 |
| 명확한 상태 조회 | `status_observe` → 종료 |
| 교육 | `education_retrieve` → 근거가 있으면 `write → validate → finalize`, 없으면 `education_unknown` |
| 파일 검토 / 변경 제안 | `configuration` 서브그래프 |
| 실제 장애 | 기존 Supervisor 계획·관측·조건부 분석·Writer·Validator |
| 분류 실패 / 스키마 누락 / 모델 장애 | `routing_unavailable` → 조회 없이 실패 안내 |

대상 전환은 기존 TargetPanel에서 사용자가 명시적으로 수행합니다. 이전 pending 질문·승인은 무효화됩니다. 원래 질문 불러오기는 입력창에만 복원하며 사용자가 전송해야 실행합니다. 서비스 동명이 있다고 다른 프로젝트를 대신 조회하지 않습니다. `/diagnose-*`는 인자가 제한된 단축 명령이며 **현재 고정 프로젝트**만 대상으로 합니다. 다른 프로젝트·경로를 슬래시 인자에 추가하면 정상 명령으로 실행하지 않습니다.

프로젝트명 추출 자체는 모델 판단입니다. 오류를 완전히 배제할 수 없으므로 화면의 고정 대상과 실제 도구 인자를 함께 확인해야 합니다. 엔진/폴더의 자동 전환 권한을 LLM에 부여하지 않습니다.

시작/대상 전환 시 발견된 프로젝트명(최대 100개)도 분류 문맥에 제공합니다. 다른 프로젝트명이 질의에 있는데 current로 분류하거나 서비스명을 프로젝트 필드에 넣은 모순이 있으면, 프로젝트 참조만 추출하는 **quality LLM** 호출을 한 번 더 수행합니다. 부정·제외와 서비스/프로젝트 이름 중복도 이 모델이 해석합니다. 조건부 JSON Schema는 current/ambiguous의 이름을 null로 제한하고, explicit만 이름을 허용합니다. 서로 다른 POSIX 경로를 대소문자 무시로 합치지 않습니다. 이는 키워드 route fallback이 아니며, 서비스/파일 도구 호출 전에 scope를 다시 확인하는 경계입니다. 통신 실패만 한 번 재시도하고 그래도 실패하면 조회 없이 중단합니다.

## 2. 단순 상태 노드 입력·출력

`status_observe` 입력: 확정된 `routing_target`, 질의. 프로젝트 전체는 `list_containers`, 단일 서비스는 `check_service_health` 한 번입니다. MCP 통신 재시도는 MCPClient 한 계층에서만 최대 2회 추가하며 실패 시 실제 횟수를 기록합니다. Writer·품질 모델·재작성·분석은 호출하지 않습니다.

출력은 실제 구조화된 관측에서 직접 렌더링합니다.

- `observation_status`: complete / partial / failed — 조회가 완료됐는가.
- `subject_status`: present / absent / ambiguous / unknown — 대상 존재 여부.
- `state`/`lifecycle`, `health`: 실행 상태와 healthcheck 상태. 부재는 unhealthy가 아닙니다. 빈 health는 unknown이며, 명시적인 health_configured=false 근거가 있을 때만 not_configured입니다. Compose ps의 빈 문자열은 설정 부재를 증명하지 않습니다.
- 단일 컨테이너를 가정했는데 여러 개면 ambiguous입니다. 통신·파싱 오류는 unknown이지 absent가 아닙니다.
- 완결된 absent도 인용할 수 있는 근거지만 `succeeded`·복구 승인에 쓰는 정상/가동 증거로 승격하지 않습니다. 미배포 상태의 incident에서도 이 차이를 보존합니다.

## 3. 설정 서브그래프

실제 순서: `config_collect → config_review → config_validate → config_propose`.

| 노드 | 입력 | 출력 / 제한 |
|---|---|---|
| collect | 선택된 프로젝트·허용 서비스 | 활성 Compose/override, 정규화 설정, build 참조 Dockerfile·.dockerignore(최대 12경로), 제한된 runtime 관측. 개별 시간·시도 기록 |
| review | 제한·마스킹한 관측(총 문맥 예산 표시), 현재 요청 | fast 모델이 최대 3개 검토 후보·선택적 정확한 edit 제시. **미검증 데이터** |
| validate | 원본 관측 스냅샷 + 후보 | 파일 경로·원본 해시·줄 범위·snippet, supported/contradicted/insufficient 판정, 게시 가능한 **코드 규칙의 정규화된 문장** |
| propose | 확인한 사실 + 검증된 정적 위험 | 답변. change인 경우에만 지원하는 변경의 preview와 파일 승인 생성 가능. 적용은 안 함 |

초기 지원 규칙은 의도적으로 작습니다.

1. 활성 build context의 `.dockerignore` 부재: 전송할 불필요 파일이 포함될 **가능성**. 크기·시간 절감은 미측정.
2. 같은 stage에서 apt update/install이 별도 RUN: 캐시 갱신 불일치 위험. 일반적인 줄 연속(`\\`)을 합쳐 검사합니다. 이미 같은 RUN이면 모순 권고로 제외합니다.
3. 소스 `COPY app/src/.` 뒤 `pip install`: 소스 변경으로 설치 layer 캐시가 무효화될 가능성. 패키징 확인 없이는 자동 재배치하지 않습니다.
4. 이미 `npm ci` 사용: 다시 사용하라는 후보를 모순으로 제외합니다.

heredoc·escape 지시문 등 지원하지 않는 Dockerfile 구문, 마스킹·불완전 파일, 알 수 없는 규칙은 결함으로 단정하지 않습니다. 모델이 쓴 임의 문장이나 요약은 게시하지 않습니다. `runtime success`나 수치형 성능 향상도 정적 파일에서 검증할 수 없으므로 게시·승인 근거로 쓰지 않습니다. 이 검사는 전체 Dockerfile 의미 분석기나 실제 빌드 테스트가 아닙니다.

자동 diff 허용 범위는 **관측상 없는 .dockerignore 추가**부터 시작합니다. 확인한 부재 스냅샷·해시와 검증 claim을 연결하고 기존 ConfigControl의 정확한 snippet·hash·scope·권한 검사를 다시 통과해야 합니다. 다른 수정은 추가 근거가 없으면 승인 생성하지 않습니다. 파일 승인과 배포 승인은 계속 별개이며 해시/컨테이너 identity가 바뀌면 재검토해야 합니다.

검토용 fast 모델을 사용해 왕복과 로드 비용을 줄였습니다. 안전성은 후보 생성 모델의 자기평가가 아니라 마지막 코드 검증·기존 승인 경계에 둡니다. 모델 실패 시에도 확인한 관측·지원 정적 검사 결과는 제한사항과 함께 제공합니다.

## 4. 교육 RAG

벡터 DB는 추가하지 않았습니다. 첫 라우팅 모델이 5개 카드의 제목·요약에서 학습 주제를 정규화하고 카드 ID를 고릅니다(`llm-catalog`). `storage_persistence`는 검토된 K1/K2 안전 묶음, `bind_mount`는 K3, `network`는 K4, `readiness`는 K5, `storage_comparison`은 K1/K2/K3, `unsupported`는 빈 목록으로 서버에서 연결합니다. 이는 키워드 분류가 아니라 **모델의 의미 주제→검토 카드 카탈로그 연결**입니다. 원문에 “저장/데이터/볼륨”이 없어도 인스턴스 교체 후 사진 유실 표현을 분류할 수 있습니다. ID가 실제 카드에 없으면 실패 처리합니다.

기존 `retrieve_lessons(query)` 단독 호출의 keyword-top3는 호환용입니다. 실제 구조화 라우팅에서 `lesson_ids`가 오면 그 선택을 사용하며, Writer에서 중복 모델 호출 없이 검토된 원문을 조합합니다. K1/K2가 검색되면 해당 안전 설명을 임의로 생략하지 않습니다. 존재하지 않는 사실·복구 보장을 생성하지 않습니다.

`known_lifecycle`은 사용자 질의의 정확한 `lifecycle_quote`가 있을 때만 사용합니다. 명시한 삭제/중지/교체 여부는 다시 묻지 않으며, 저장 경로·마운트·백업 여부를 추가로 추측하지 않습니다. 설명 원문·주의사항은 유지하고 아직 모르는 질문만 별도 참고 영역에 표시합니다. 이는 교육용 참고 질문이지 실행을 막는 `human_question`이 아닙니다.

Validator는 이 문맥으로 조합한 문자열 전체·허용 URL·K 인용·위험 명령을 다시 검증합니다. 임의 생성 사실 문장은 허용하지 않습니다.

## 5. 응답·SSE·화면 계약

API 2.5는 기존 필드를 삭제하지 않고 다음을 추가합니다.

- `execution_status`: completed / waiting_for_user / failed (서버 취소가 실제 확인된 경우에만 cancelled).
- `answer_status`: validated / partial / unverified / unavailable.
- `subject_status`, `lifecycle`, `health`: 존재·실행·건강을 분리.
- `config_metrics`: 파일 수집과 검증된 주장 수를 별도로 표시. 전체 경로 제한·마스킹 등도 포함.
- route에 적용되지 않거나 측정하지 않은 coverage/time은 null. 교육에는 장애 coverage=0을 붙이지 않음.

모델 요청 실패도 model_calls와 시간에 포함합니다. 토큰이 보고되지 않은 호출이 있으면 합계 total_tokens는 null이며, 보고된 부분 합계만 reported_tokens로 구분합니다. 실제 모델 호출 0회의 0 토큰과 미측정을 혼동하지 않습니다.

화면은 NodeStarted에서만 노드를 만들고 같은 run_id의 Completed/Failed로 갱신합니다. 단순 상태의 실행 노드는 `route`, `status_observe` 두 개입니다. 진단/교육 Writer 원고는 Validator 통과 후에만 AnswerToken으로 게시하며, 노드 IO의 중간 원고는 명시적으로 미검증 데이터라고 표시합니다. 검증 거절 시 기본 재작성은 없고 진단은 구조화 관측만, 교육은 안전한 제한 안내를 반환합니다.

설정 노드 출력은 그 노드가 생성한 핵심 필드만 보여줍니다(collect의 evidence, review의 미검증 후보, validate의 검증 결과, propose의 최종 답변·승인). 상속받은 전체 상태를 반복 복사해 검증 결과가 잘림 뒤에 숨는 것을 줄였습니다. 크기 제한을 넘으면 truncated 표시가 남으며 원본 관측 근거와 해시를 별도로 대조할 수 있습니다.

브라우저의 중지·연결 종료는 “화면 수신 중단 · 서버 종료 미확인”이며 서버 취소를 뜻하지 않습니다. 이미 서버가 준 최종 결과가 있으면 그 결과의 실행/답변 상태를 표시합니다. 단순 대화는 별도 사실 검증을 수행하지 않으므로 unverified입니다.

## 6. 검증 재실행

```bash
.venv/bin/pytest -q
node --test frontend/tests/*.test.mjs
npm --prefix frontend run build
# 서버가 bizxray를 선택한 live Ollama 모드일 때만, 읽기 전용 평가
.venv/bin/python -m eval.reliability_live --run 새이름 --repeats 3
```

평가 문장은 5개 use case ×10개이며 스크립트에 고정되어 있습니다. 모든 반복의 원시 응답·노드 IO·판정·시간을 별도 JSONL로 보존합니다. 실제 모델 평가는 대역을 쓰는 자동 테스트와 다릅니다. 업무 스택의 파일 수정·기동·승인 실행은 하지 않습니다. 실행 결과와 남은 한계는 별도 구현 결과 보고서를 참조합니다.
