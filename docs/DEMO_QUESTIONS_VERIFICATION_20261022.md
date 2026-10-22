# 시연 질문 10개: 실제 실행·수정 결과 (2026-10-22)

공개용 안내: 아래는 실제 로컬 검증의 요약입니다. 개인 환경 스크린샷, Docker 로그·원시 모델 입출력·대화/승인 DB 및 원시 JSONL은 공개하지 않습니다. 재실행 스크립트와 회귀 테스트는 포함합니다.

## 결론과 평가 범위

원문 10개를 실제 Ollama와 실제 Docker 읽기 도구로 실행하고, 발견한 요청·답변 불일치를 수정한 뒤 다시 실행했다. 최종 **요청·답변 계약 검사 10/10 통과**다. 이는 **장애 10개 재현·자동 복구 10/10 성공이 아니다.**

실행 환경은 기존 선택을 유지한 `buttercast`, Docker context `desktop-linux`, Engine 29.6.2다. 허용 서비스 `api`, `worker` 모두 exited였다. 이 프로젝트에 **backend, DB라는 서비스는 없다.** 없는 역할명을 api/worker로 바꾸거나, 다른 프로젝트를 몰래 선택하거나, 실제 장애가 있는 것처럼 꾸미지 않았다.

- 의미 의도 분류: `qwen3:8b`. 대상 추출: `qwen3:4b-instruct`, 잘못된 인자면 한 번 의미 재확인. 실제 호출 수는 아래 표에 기재했다.
- 서버에서 실행된 LangGraph → MCP → 현재 Docker/파일 관측 → 응답까지 검사했다. 프론트는 별도 실제 api 이미지 질문을 SSE로 확인했다.
- 자동화는 승인 버튼을 누르지 않았다. 자기 실행에서 만들어진 승인은 거절하고, 추가 확인은 취소했다. 사용자 Docker 파일·컨테이너·볼륨은 변경하지 않았다.
- 8번과 9번은 동일 thread로 실행했다. 대상 확인 취소 뒤에도 사용자 대화 이력은 보존되지만, 취소는 실제 DB 진단 근거가 되지 않는다.
- 검사 기준은 HTTP 200이나 모델의 자기 평점이 아니라 응답 경로·필수 필드·근거 도구·대상 일치·잘못된 승인 부재다. 검사 코드는 `eval/demo_questions_live.py`에 있다. 새 자연어 전체의 정확도를 보장하지 않는다.

## 최초 실행에서 확인한 결함

| 질문 | 실제 불일치 |
|---|---|
| 1 상태·CPU/메모리 | 사용량 대신 Memory=0 같은 제한 설정을 표시했다. |
| 2 리소스·보안 | 요청한 항목이 아니라 healthcheck 부재와 일반 검토 문구만 답했다. |
| 3 backend 이미지 | 없는 backend를 버리고 api/worker를 조회했다. 크기·베이스 정보도 없었다. |
| 4 Dockerfile | COPY --chown 반복 자체가 추가 소유권 작업·크기 비효율이라는 잘못된 모델 조언을 검증 모델도 승인했다. |
| 5 재시작 루프 | Backend가 없는데 api를 대상으로 generic 상태 답변을 했다. |
| 6 API OOM | 구조화된 종료 코드·OOMKilled·로그를 수집했지만 최종 관측 답변에서 빠뜨렸다. |
| 7·8 Backend/DB 통신·준비 상태 | 없는 서비스 대신 api를 조회했다. 실제 두 대상의 통신을 확인한 것이 아니었다. |
| 9 healthcheck 수정·재시작 | 현재 환경에서는 대상 확인이 필요했고 최초에도 확인 질문이었다. 자동 수정 기능 성공으로 세지 않는다. |
| 10 8000 포트 | 점유자를 찾지 않고 `docker compose restart api worker` 전체 재시작 승인을 만들었다. 평가 도구가 즉시 거절했다. |

중간 재시험에서도 대상 모델이 유효한 JSON으로 backend를 생략하거나, 포트 조회가 필요하다고 출력하면서 runtime으로 분류하는 실패를 확인했다. 프롬프트만 보완한 단계에서 끝내지 않고 아래의 인자·실행 선행 조건을 코드로 검사했다. 중간 실패 기록도 삭제하지 않았다.

## 수정한 실제 동작

### 1. 사용량·제한값·이미지 세부 정보 분리

`lookup`의 요청 필드에 `usage`, `image_details`를 추가했다. `inventory_observe`는 필요한 경우에만 MCP `inspect_project_configuration`의 `include_usage`, `include_image_details`를 켠다.

- usage: running 컨테이너의 실제 ID 목록에 대해서만 한 번 `docker stats --no-stream`을 호출하고 ID가 일치하는 수치를 붙인다. 중지 컨테이너는 측정 불가이며 0%라고 채우지 않는다. 현재 stats와 과거 종료 당시 시계열은 다르다.
- resources: CPU/메모리 제한 설정이다. 사용량과 혼합하지 않는다.
- image_details: 현재 태그를 새로 해석하지 않고 컨테이너의 immutable image ID를 inspect한다. 크기·OS/아키텍처·OCI base metadata를 안전 필드로 반환한다. 현재 파일의 FROM 선언은 별도 표시하며, 실제 빌드 당시 베이스 digest가 확인됐다는 뜻이 아니다.
- 추가 측정이 실패하면 답변은 partial이며 validated로 과장하지 않는다. 일반 이미지 이름·마운트 질문에 stats/image inspect를 불필요하게 붙이지 않는다.

### 2. LLM 라우팅과 인자 검증의 역할 구분

분류는 여전히 LLM이다. 단어를 찾아 교육/진단 경로로 강제하는 키워드 라우터를 넣지 않았다.

대신 대상 모델에 `requested_services`를 요구하고, 원문에 실제 나온 이름인지를 검증한다. 모델이 명시된 `backend 컨테이너`, `Backend에서 DB`, `DB 서비스` 같은 이름 인자를 생략하더라도 문자 출처 검사로 보존한다. 이것은 포트 번호 검사와 같은 **인자 바인딩 안전 경계**다. 역할명 Backend/DB를 api/worker/postgres로 변환하지 않는다. 허용 서비스 밖 이름은 조회·변경 대신 프로젝트/서비스 확인 질문으로 보낸다.

일반 프로젝트 질문에 available_services를 그대로 복사한 잘못된 인자도 거절하며 한 번 의미 재확인한다. LLM 입력에서 불필요한 Engine metadata를 제거했다. 분류 실패를 교육 템플릿으로 감추지 않는다.

### 3. 모델이 선택한 포트 조회를 실행 선행 조건으로 사용

```text
route: LLM이 port_owners 주제를 선택
  → port_diagnose
      포트 번호 한 개 검증
      MCP inspect_port_owners(port=8000)
      현재 Engine 전체의 ps --all + identity 확인 inspect
      running의 실제 게시 바인딩 / 생성 시 바인딩 선언 분리
  → END (승인·변경 없음)
```

모델이 `runtime`이라고 하면서도 `port_owners`를 요구하면 선택된 읽기 선행 조건을 먼저 실행한다. 아직 특정 데모 이름을 모르더라도 호스트 포트 점유 조회 자체에는 서비스 추측이 필요 없다. runtime 승인 경로에도 포트 인자가 있으면 먼저 점유를 읽는 독립 안전 경계를 뒀다.

이 도구는 **현재 선택 프로젝트만이 아니라 현재 Engine의 게시 포트**를 조회한다고 답변에 명시한다. 소유자가 다른 프로젝트면 범위 밖 표시를 하며 중지하지 않는다. 중지 상태의 포트 선언을 현재 점유로 간주하지 않고, 점유가 없다고 호스트 프로세스·host-network까지 비었다고 말하지 않는다. ‘좀비’라는 사용자 표현만으로 종료 대상을 확정하지 않는다.

### 4. 파일 검토·장애 근거 보완

- Compose의 정규화 `service_config`에서 CPU/메모리 limit, read_only, user, cap_add/cap_drop, security_opt, privileged, network_mode 선언을 점검한다. 미지정은 개선 검토 대상일 수 있지만 취약점·root 실행·호스트 무제한의 증명이 아니다. 실제 앱 쓰기 경로·capability 요구 확인이 선행되어야 한다.
- 파일 검토에서 모든 서비스의 inspect·로그 수집을 제거했다. 2·4번은 MCP 11회에서 7회로 줄었다. 실제 장애 경로가 로그·inspect 수집을 담당한다.
- 파일 줄을 인용했다는 이유만으로 잘못된 성능 조언을 통과시키지 않는다. COPY --chown만 있는 span은 반복 재귀 chown/추가 소유권 layer 비용을 입증하지 못한다. 자유로운 fact 원고 대신 코드 검사와 의존성 순서 관측을 사용한다. 이 보수적 필터 때문에 타당한 새 조언도 추가 근거 없이는 보류될 수 있다.
- COPY --chown은 복사 중 소유권을 설정하는 기능이다. 단순 반복을 별도 RUN chown과 동일시하지 않는다. [Dockerfile 공식 참조](https://docs.docker.com/reference/dockerfile/#copy---chown---chmod)
- 장애 분석이 조기 종료되어도 ExitCode, OOMKilled, RestartCount, 최근 로그 끝부분과 조회 기간 OOM 이벤트를 최종 관측에 유지한다. 이를 원인 확정이나 복구 성공으로 바꾸지 않는다.

## 최종 원문 10개 결과

아래는 최종 JSONL의 실제 수치다. 노드는 서브그래프를 포함해 실제 NodeStarted 이벤트 수다. 도구는 MCP 호출 수이며 그 내부 Docker CLI 호출 수와 같지 않다. 시간은 이번 로컬 1회 실행이며 성능 보장이 아니다.

| 번호·질문 요지 | 실제 답변·처리 | 경로 | 초 | 모델/MCP/노드 | 판정 |
|---|---|---|---:|---|---|
| 1 상태와 CPU/메모리 | api/worker exited, 실시간 사용량 측정 불가. 제한값을 사용량으로 답하지 않음 | inventory | 6.77 | 2/1/2 | 계약 통과·측정 부분 결과 |
| 2 Compose limit/보안 | 두 서비스 CPU·메모리 제한 명시 없음, 보안 선언 및 호환성 확인 개선점 | configuration | 31.56 | 3/7/6 | 계약 통과·정적 검토 |
| 3 backend 이미지 크기/베이스 | backend 없음. 실제 프로젝트/서비스 확인 요청, api 대체 조회 없음 | clarify | 14.26 | 3/0/2 | 대상 확인 통과 |
| 4 Dockerfile 캐시/크기 | 22줄 수집. pip 설치가 나머지 COPY 앞에 이미 분리됨. 확정 비효율 없음/미측정 명시 | configuration | 28.66 | 3/7/6 | 계약 통과·정적 검토 |
| 5 Backend restart loop | Backend 없음. 재시작 루프를 꾸미거나 api로 대체하지 않음 | clarify | 13.95 | 3/0/2 | 대상 확인 통과 |
| 6 API OOM/당시 메모리 | ExitCode=0, OOMKilled=False, RestartCount=0, 최근 10분 OOM 0건, 로그 종료 내용 표시. 과거 메모리 복원 불가 | incident | 24.54 | 4/8/18 | 계약 통과·원인 미확정 |
| 7 Backend→DB 네트워크/DNS | Backend/DB 없음. 다른 서비스로 통신 테스트하지 않음 | clarify | 16.36 | 3/0/2 | 대상 확인 통과 |
| 8 DB running/Backend 거부/readiness | DB/Backend 없음. 현재 프로젝트의 그런 running DB를 확인한 것이 아님 | clarify | 13.92 | 3/0/2 | 대상 확인 통과 |
| 9 DB healthcheck 수정+Backend 재시작 | DB/Backend 없음. 대상 확인 요청, 변경·재시작 승인 없음 | clarify | 13.31 | 3/0/2 | 대상 확인 통과·자동 수정 미시연 |
| 10 8000 점유 좀비 종료+데모 시작 | 현재 Engine에서 running 게시 점유 미관측. 호스트 점유 미확인 명시, 중지/전체 재시작 승인 없음 | inventory | 5.16 | 1/1/2 | 계약 통과·자동 복구 미시연 |

총 요청 시간은 최초 198.28초 → 최종 168.49초, MCP 57회 → 24회였다. 모델 호출은 각각 28회다. **없는 대상에서 불필요한 진단을 중단한 영향이 크므로 동일 장애 처리 속도 향상률로 해석하면 안 된다.**

## 실제 답변 발췌

### 1번

> api / buttercast-api-1 · exited · 현재 CPU/메모리 사용량: 현재 중지 상태여서 실시간 사용량을 수집할 수 없습니다. 사용량을 0으로 추정하지 않습니다. [E1]

### 3번

> 현재 프로젝트에는 backend 서비스가 없습니다. 대상 프로젝트를 바꾸거나 실제 서비스명을 알려주세요.
>
> 현재 허용 서비스는 api, worker입니다. 다른 서비스를 대신 조회·변경하지 않습니다.

### 6번

> api: ExitCode=0 · OOMKilled=False · RestartCount=0 [E7]
>
> 현재 stats는 종료 당시 사용량이 아니며, 과거 메모리 시계열은 저장하지 않아 당시 메모리 사용량을 복원할 수 없습니다.

### 10번

> 실행 중인 Docker 게시 포트 점유 컨테이너가 관측되지 않았습니다. 포트가 비었다는 증명은 아닙니다. [E1]
>
> 아직 중지·재시작·데모 배포를 수행하지 않았습니다. 정확한 프로젝트·서비스와 데모 시작 대상을 확인해야 합니다.

전체 질문·전체 답변·근거·노드 입출력은 아래 JSONL에 보존했다. 위 발췌가 전체 답변을 대신하지 않는다.

## 화면 검증: 실제 서비스명으로 이미지 질문

원문 3번은 backend가 없기 때문에 추가로 **서비스명을 api로 명시한 이미지 질문**을 브라우저에서 제출했다. api의 immutable image ID 조회 결과:

- 로컬 이미지 Size: 622,815,241 bytes = 593.96 MiB.
- 플랫폼: linux/arm64.
- OCI base metadata 없음 → 실제 빌드 베이스 digest 확정 불가.
- 현재 Dockerfile 3줄: `FROM python:3.11-slim`. 선언과 실제 빌드 provenance 구분.
- SSE 화면: 실제 실행 노드 route → inventory_observe, 총 2개, 수집 근거 1개. Planner/Writer/Analysis는 실행되지 않았다. 초기에는 실행 노드 0개였고 NodeStarted 이후 노출됐다. 브라우저 warning/error 로그는 빈 배열이었다.

개인 환경 화면 `demo-image-details-ui.png`는 로컬 기록으로만 보존하고 공개 사본에서는 제외했다.

## 기록·재실행

최종 자동 회귀: 백엔드 **397개 통과 (60.83초)**, 프론트 **10개 통과**, production build 성공. 최종 실모델 10건은 승인 생성 0건, 예산 초과 0건이었다. 모의 관측 단위 테스트 수와 실모델 평가 수를 합쳐 정확도로 표현하지 않는다.

- 최초 원문 10개 로컬 기록: `demo-questions-1dde2d0af6f44a9baf4cf30f03c147d8.jsonl`.
- 중간 실패 포함 로컬 기록: `demo-questions-06fcfa7308854a7e935ef1a0f13e4731.jsonl`, `demo-questions-d4854f0620f84c608446d6256e750f3d.jsonl`.
- 최종 10개 로컬 기록: `demo-questions-050276ec308f4cca97b978e35550cea4.jsonl`. 3·10번을 먼저 실행하고 나머지를 실행했다. 원시 파일은 공개 사본에 없으며 이 문서의 결과 요약으로 제공한다.
- 회귀 계약: `tests/test_demo_questions.py` (실제 모델 정확도 평가가 아닌 모의 관측 테스트).

```bash
.venv/bin/python -m eval.demo_questions_live
.venv/bin/python -m pytest -q
node --test frontend/tests/*.test.mjs
npm --prefix frontend run build
```

평가 실행이 만드는 자기 승인·확인 질문만 거절/취소한다. 이미 대기 중인 사용자의 다른 승인까지 건드리지 않는다. eval의 failures는 프로세스 실패 코드로 반환한다.

## 남은 한계: 과제 발표에서 과장하면 안 되는 부분

1. 현재 프로젝트에는 Backend/DB가 없다. 5·7·8번은 실제 restart loop, 브릿지/DNS 장애, readiness 장애를 주입·재현하지 않았다. 올바른 대상 확인만 검증했다. 이 질문으로 장애 시연하려면 실제 서비스명이 있는 프로젝트를 사용자가 선택해야 한다.
2. 6번은 API를 실제 OOM으로 죽인 실험이 아니다. 현재 관측에서 OOM 원인 확정 근거가 없음을 설명했다. 과거 메모리 사용량 저장소는 없다.
3. 파일 자동 수정은 관측상 누락된 `.dockerignore` 추가만 지원한다. 임의 Health Check/depends_on 패치·후속 재시작은 아직 지원하지 않는다. 9번의 자동 조치가 완성됐다고 발표하면 안 된다.
4. 기존 컨테이너의 start/stop/restart는 정확한 대상의 개별 1회 승인 기능이다. 10번의 범위 밖 점유자 자동 중지와 새 데모 생성/연쇄 배포는 지원하지 않으며 이번 평가에서 실행하지 않았다. 잘못된 전체 재시작 승인 제거와 읽기 선행 조건만 검증했다.
5. 중지 상태의 실제 CPU/메모리 측정, 장기 메모리 추이, 빌드 성능/압축 전송 크기/캐시 적중률 실측, 다른 물리 PC는 이번에 검증하지 않았다. LLM은 여전히 오분류할 수 있다. 회귀 10/10은 일반 정확도 100%가 아니다.
