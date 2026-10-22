# 현재 구성 질문의 전반 점검·수정 (2026-10-22)

이후 추가된 사용량·이미지 크기·포트 점유 조회와 원문 10개 실제 검증은 [시연 질문 검증 결과](DEMO_QUESTIONS_VERIFICATION_20261022.md)에 정리했다. 아래는 기본 구성 조회가 추가된 시점의 기록이다.

## 원인: 읽기 요청도 서로 다른 목적이다

기존 상태(status) 빠른 경로는 lifecycle/health/목록만 표현했다. 설정(configuration) 경로는 파일의 오류·비효율을 판단하는 검토였다. **현재 사용 중인 이미지 이름이나 마운트 경로를 그대로 조회하는 목적 자체가 빠져 있었다.** 라우터가 가까운 교육/검토 경로를 고르면 실제 질문과 다른 healthcheck 공통 문구나 교육 자료 부족 답변이 나왔다. 체크포인터를 추가하거나 특정 문장을 키워드에 등록하는 것으로 해결할 문제가 아니다.

실제 모델 평가에서는 추가로 다음을 확인했다.

- 하나의 큰 JSON에서 의도·프로젝트·서비스·카드·수명 상태까지 함께 고르게 하자 서로 모순되는 인자를 생성했다.
- `project` 범위인데 cardinality를 `one`으로 생성해 불필요한 확인 질문이 됐다.
- 경량 4B 모델은 구성 사실 조회와 교육/파일 검토를 여러 대조 질문에서 혼동했다. 프롬프트 예시 추가만으로 해결되지 않았다.
- 빌드 컨텍스트·depends_on은 실제 컨테이너 inspect에 없는 선언 정보인데 런타임만 읽도록 분류할 수 있었다.

## 바뀐 실제 경로

```text
사용자 질문
  → route
      목적 분류: QUALITY 모델, 작은 IntentDecision 스키마
      lookup이면 요청 필드로 경로 결정: status만 → status, 구성 필드 포함 → inventory
      실제 조회/작업이면: 경량 라우터로 별도 TargetDecision 해석
      교육이면: QUALITY 모델로 별도 EducationDecision 카드·수명 해석
  → inventory_observe (현재 구성 사실 조회인 경우)
      MCP inspect_project_configuration(services, source)
      Compose 선언 / 실제 컨테이너 inspect 분리 수집
      요청한 주제만 구조화된 값으로 직접 렌더링
  → END
```

LangGraph 실행 노드는 route와 inventory_observe **2개**다. route 안의 모델 단계와 MCP 내부 Docker CLI 호출은 별개이며, 모델·도구 사용량 장부로 구분한다. 일반 구성 조회는 보통 모델 2회, MCP 도구 1회다. 별도 Planner·분석·Writer·재작성 루프를 돌리지 않는다. 의도 분류는 여전히 LLM 의미 분류이며 키워드 폴백으로 교육/조회 경로를 강제로 고르지 않는다.

목적 구분:

| 원하는 답 | 경로 |
|---|---|
| 지금 중지/실행 중인가, 건강 상태/컨테이너 목록 | status |
| 현재 이미지·마운트·포트·네트워크·제한·정책·healthcheck 설정·의존성·빌드 경로의 값 | inventory |
| 개념·차이·사용법·예방 원리 | chat / education |
| 실제 파일에 오류/비효율이 있는가 | configuration |
| 현재 장애의 원인은 무엇인가 | incident |
| 파일 변경/시작·중지·재시작 | change / runtime, 기존 별도 승인 유지 |

## 입출력과 게이트

### route

- 입력: 현재 query, 최근 사용자 대화, 고정 selected_target, 허용 서비스. 과거 답변은 신규 관측이나 승인 근거가 아니다.
- 출력: route=inventory, inventory_topics, inventory_source, 확정 routing_target.
- 주제 허용값: status, images, mounts, ports, networks, resources, restart, healthcheck, dependencies, build. 여러 개를 질문하면 함께 조회한다. LLM이 status와 inventory를 중복 선택하는 대신 `lookup` 목적과 요청 필드를 고르고 코드가 공개 경로를 구성한다. 따라서 정책·설정 조회가 단순 실행 상태 조회로 사라지지 않는다.
- 기본 source는 both다. LLM에게 읽을 도구를 임의로 고르게 하지 않는다. 사용자가 명시적으로 Compose 선언만 요청한 `declarations_only=true`일 때만 compose로 좁힌다. 빌드·의존성처럼 선언 전용 필드에 runtime만 지정된 JSON-only 어댑터 요청은 Compose도 포함하도록 보정한다. 의도를 다시 키워드 분류하는 것이 아니라 필드별 소유 데이터 소스를 보장하는 계약이다.
- 대상 모델에서 중복 cardinality 필드를 제거했다. project는 전체, service/unspecified는 단일로 코드가 표현한다. 모순된 독립 선택지를 모델에게 주지 않는다.
- 프로젝트 이름이 있는데 current라고 표시한 응답은 named reference로 정규화한 뒤 원문 출처와 대상을 검사한다. 'current인데 bizxray 요청' 같은 모순된 독립 값을 그대로 믿지 않는다. 이름의 출처·다른 프로젝트·서비스 충돌 검사는 그대로 유지하므로 임의 프로젝트를 대신 조회하지 않는다.
- 서비스명이 없는 일반 구성 질문은 선택 프로젝트 전체다. **특정 단일 서비스를 가리키지만 이름을 모르는 경우**는 기존 확인 질문으로 보낸다. 다른 프로젝트는 대상 선택 화면으로 보내고 현재 프로젝트로 대신 조회하지 않는다.
- 작은 모델의 대상 인자가 스키마/출처 검증에 실패하면 QUALITY로 1회 승격한다. 그래도 실패하면 조회하지 않는다. 모든 요청 예산(60초·모델 8회·도구 24회)은 유지한다.

### inspect_project_configuration / inventory_observe

- 입력: 확정된 허용 services(1–12개), source. 경로·임의 shell 명령을 모델 인자로 받지 않는다.
- Compose: 기존 validate_compose_config 결과 중 허용 서비스의 비밀값을 제외한 구성만 사용한다.
- 런타임: `docker compose ps --all --format json` → 허용 서비스의 ID만 → `docker inspect <IDs>` 배치 조회. 중지 컨테이너와 여러 replica도 조회한다. 50개를 넘으면 좁은 범위를 요청해야 한다.
- ID와 Compose service/project 라벨, 필수 inspect 필드를 대조한다. 불완전/대상 변경/엔진 오류를 컨테이너·마운트 부재로 표시하지 않는다.
- 출력: E1 관측의 scope/observed_at, compose.services, runtime.containers, services_without_containers, 각 수집 단계 ok/error.
- 실제 이미지 reference와 immutable image_id, 실제 Mounts의 type/name/source/destination/RW, 포트 생성 설정/현재 바인딩, 연결 네트워크 이름, 제한·재시작 정책을 반환한다. 환경변수 값·로그·실행 명령·전체 inspect 원문은 반환하지 않는다. healthcheck 명령 원문은 비밀값 보호로 생략하고 시간 설정은 반환한다(런타임 nanoseconds를 seconds로 변환).
- 최종 답변은 요청 주제만 표시한다. 이미지 질문에 healthcheck 검토나 비효율 점수를 붙이지 않는다. 검증된 구조화 값의 직접 표시이므로 자유로운 모델 원고를 작성하지 않는다.
- 전체 성공은 validated, 한 소스만 성공하면 partial, 모두 실패하면 unavailable. 실제 선언과 실제 적용 상태는 별도 절이다. inventory 요청에 configuration_review 결과가 붙으면 공통 JSON/SSE 계약이 차단한다.

## 실제 현재 대상에서 확인한 내용

선택 대상은 `buttercast`다. 조회 시 api/worker 모두 exited 상태였다.

- 이미지: `buttercast:latest`, 실제 ID `sha256:b70f1fd8095457dd5f0f912c9270f5b31a4f34f4fb1261298bdedaad06bf9254`.
- 마운트: 두 컨테이너 모두 named volume `buttercast_runtime` → `/app/runtime`, 읽기·쓰기.
- Compose 선언의 논리 볼륨명은 `runtime`이고 실제 Engine 볼륨명은 `buttercast_runtime`이다. 선언과 실제 inspect를 혼동하지 않는다.
- api의 생성 시 포트 바인딩은 호스트 8091 → 컨테이너 7860/tcp다. 중지 상태에서 active_bindings가 빈 값이라고 포트 미설정·연결 성공을 단정하지 않는다.

위 값은 이 문서 작성 시 관측이며 다음 요청에서는 다시 읽는다. volume Source는 Docker Engine 내부 경로여서 Docker Desktop VM/원격 Engine의 경로와 사용자 PC 경로를 구분해야 한다. 마운트 존재가 데이터 보존·백업·복구 성공을 보증하지는 않는다. 현재 태그 이름과 컨테이너 생성에 사용한 실제 이미지 ID도 구분한다.

## 검증과 한계

`tests/test_inventory.py`: 자료원 구분, 비밀 필드 제외, 허용 범위·ID·프로젝트·필수 필드 검증, 부분/전체 실패, 2노드·읽기 1회, 소스 계약, 이미지/마운트 이외 7종 주제, duration 단위, 작은 production 스키마를 검증한다.

`eval/inventory_live.py`: 실제 Ollama 24개 대조 질문(구성 9주제, 교육·검토·상태·장애·시작/중지·위험 요청·다른 프로젝트) 및 같은 thread의 검토→마운트→이미지 등 11개 앱 요청을 실행한다. 교육은 경로뿐 아니라 자료 커버리지도 검사한다. 테스트 더블의 성공률을 모델 정확도로 세지 않는다. 실패·중단한 중간 JSONL도 보존한다. 어떤 변경도 승인하지 않는다.

이 기능은 선택 프로젝트의 허용 서비스 구성 조회다. Engine 전체의 미사용 이미지/볼륨 목록·임의 호스트 파일 탐색·환경변수 비밀값 조회·데이터 무결성 검사·성능 실측은 지원 범위에 추가하지 않았다. 실제 시작/정지/재시작은 이 평가에서 수행하지 않는다. 모든 자연어를 완벽히 분류한다는 보장도 아니다. 이전 10월 21일 PDF는 이 inventory 노드가 추가되기 전 구조다.

### 최종 확인 기록

- 백엔드 **379개 통과, 74.92초**. 프론트 **10개 통과**, production build 성공(93ms).
- 실제 Ollama 분류 대조 **24/24 통과**: `eval/results/inventory-d061e902d14e44f1bf2c1c8fe5e5e8b4.jsonl`.
- 같은 대화의 실제 앱 **11/11 통과**: `eval/results/inventory-ac3d66c8aef548c1b24c799de4346d65.jsonl`. 파일 검토 뒤 마운트·이미지·포트·선언만 조회·의존성·빌드·상태·다른 프로젝트·교육 2종으로 주제를 바꿨다. 교육은 K1/K2/K3 자료 범위도 검사했다. 다른 프로젝트 요청은 도구 0회로 선택 안내했으며 모든 요청의 변경은 0회였다.
- 앱 검사에서 마운트 17.51초, 이미지 16.59초, 파일 검토 49.66초였다. 분류 행렬과 동일 Ollama를 함께 사용한 경합이 있어 단독 성능 벤치마크나 처리 시간 보장이 아니다. 구성 조회는 각각 모델 2회/MCP 1회이며 요청 예산을 늘리지 않았다.
- 브라우저 SSE에서도 원래 두 질문에 실제 이미지 이름·ID, 실제 볼륨명·경로·RW와 선언값을 구분해 표시했다. `현재 구성 조회` 제목, 실제 노드 2개, 교육/검토 템플릿 미혼입을 확인했다. 화면은 `output/ui/inventory-images.jpg`, `output/ui/inventory-mounts.jpg`에 저장했다.
- 이름을 모르는 특정 단일 서비스는 추가 실모델 검사에서 서비스 선택 질문으로 종료했다. 모델이 clarify를 골랐지만 질문 본문을 누락한 경우에도 안전한 확인 질문으로 처리하는 계약 테스트를 추가했다.

중간 실패·중단 기록은 삭제하지 않았다. 특히 `inventory-e346ea192f2d41bf91ebb6e6cb9738bc.jsonl`은 23/24에서 다른 프로젝트 참조가 안전 중단된 기록이고, 이후 이름 출처 검사를 유지한 정규화로 수정했다. 위 결과는 명시한 소규모 회귀 집합의 각 1회 결과이며 새 모델·모든 자연어·실제 변경 성공률을 보장하지 않는다. GitHub 추가 업로드는 이번 작업에 포함하지 않았다.
