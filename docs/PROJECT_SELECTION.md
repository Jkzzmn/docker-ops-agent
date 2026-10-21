# Docker 프로젝트·경로 선택

API 2.4.0, 2026-10-21. 기존 FastAPI·Vue·SQLite·MCP를 확장했으며 새 의존성은 없습니다.

## 사용 방법

1. 대화 영역 위의 **프로젝트 선택 · Docker 컨테이너 보기**를 펼칩니다.
2. 발견된 Compose 프로젝트를 고르거나, 백엔드 서버에 있는 Compose 폴더의 절대 경로를 입력합니다.
3. 미리보기에서 프로젝트 이름·루트 경로·서비스·활성 Compose 파일·build 참조 Dockerfile을 확인합니다.
4. **이 프로젝트를 진단 대상으로 선택**을 누릅니다. 이 단계는 컨테이너 시작·중지나 파일 변경 승인이 아닙니다.
5. 새 대상이 상단에 표시된 뒤 질문합니다. 파일 검토는 선택된 Compose가 참조하는 프로젝트 내부 Dockerfile/.dockerignore를 실제로 읽습니다.

브라우저의 파일 업로드/폴더 선택이 아닙니다. 다른 PC의 브라우저로 접속해도 **백엔드가 연결한 Docker 엔진과 백엔드 파일 경로**를 대상으로 합니다. 원격 엔진의 Compose 경로가 서버에서 읽히지 않으면 서버의 읽을 수 있는 로컬 Compose 사본을 지정해야 합니다.

## 보이는 목록과 실제 진단 범위

| 구분 | 범위 |
|---|---|
| 프로젝트 목록 | 고정된 서버 Docker 엔진의 `docker compose ls --all`, 최대 100개 |
| 컨테이너 목록 | 같은 엔진의 `docker ps --all`, 최대 500개. 실행/중지 상태·프로젝트·서비스 표시 |
| Compose 미소속 컨테이너 | 목록에는 표시. 현재 버전의 진단 대상으로 직접 선택하는 기능은 없음 |
| 선택 가능한 경로 | 서버에 존재하며 지원되는 Compose 파일을 포함한 절대 폴더. Dockerfile 단독 폴더는 미지원 |
| 파일 읽기 | 선택한 프로젝트 내부의 활성 Compose 및 build 참조 Dockerfile/.dockerignore. 프로젝트 외부·원격 build 파일은 읽기 대상 아님 |

목록 새로고침/선택은 컨테이너를 기동하거나 삭제하지 않습니다. Docker Desktop에서 클릭한 화면이나 열려 있는 IDE 폴더를 자동으로 추적하지 않습니다. 목록은 새로고침 시점의 스냅샷이며, 실시간 Docker 이벤트 구독은 아닙니다.

## 전환 흐름·보호 경계

```text
프로젝트명 또는 서버 폴더
  → 읽기 전용 미리보기: 엔진·Compose·서비스·Dockerfile 경로 확인
  → 명시적 선택 (expected_instance_id 검증)
  → 해당 프로젝트 전용 환경의 새 MCP 연결·그래프 생성
  → 이전 대기 질문·미실행 승인 superseded
  → 현재 bundle 교체, 이전 MCP 종료
  → UI 이전 실행 기록 초기화, 새 대상으로 질문
```

- 전환 중 새 진단/승인/확인 답변은 409로 거부합니다. 기존 진단·SSE·승인 실행이 끝나지 않았으면 전환도 409입니다. 브라우저에서 “중지”해도 서버 응답이 아직 진행 중이면 기다려야 합니다.
- 새 대상 연결에 실패하면 기존 대상을 유지합니다. MCP 연결은 소유 task 안에서 시작·종료해 cancel-scope 오류를 피합니다.
- 프로젝트 변경 시 이전 COMPOSE_FILE·이름·서비스 allowlist·profiles/env-file 선택 환경을 지우고 새 Compose에서 서비스를 다시 도출합니다. Docker context/host는 그대로 유지합니다.
- 이전 대상의 미실행 승인과 대기 질문은 무효화합니다. 이전 대상으로 돌아와도 되살리지 않습니다. 이미 적용된 변경을 되돌리는 동작은 아닙니다.
- 대화 문맥은 thread_id뿐 아니라 Docker scope 해시로 분리합니다. 기존 버전의 scope 없는 과거 문맥은 자동으로 섞지 않습니다. 화면 기록 초기화는 SQLite 기록 삭제가 아닙니다.
- 브라우저 요청은 대상 instance ID를 보내 오래된 화면의 요청을 거부합니다. 다른 열린 탭도 4초 health 확인 후 전환을 감지하고 화면을 초기화합니다. 직접 API 클라이언트가 대상 ID를 생략하면 현재 서버 대상에 대한 요청으로 처리되므로, 클라이언트에서도 ID를 보내는 것이 안전합니다.
- **대상은 서버 전체에서 하나**입니다. 사용자/탭마다 독립된 프로젝트를 동시에 쓰는 다중 사용자 설계는 아닙니다. 한 프로세스/uvicorn worker를 전제로 하며 여러 worker나 공개 운영용 인증·권한 관리는 지원하지 않습니다.
- 선택은 실행 중 메모리에만 유지합니다. .env를 수정하지 않습니다. 서버 재시작 시 환경 설정/시작 자동 탐색을 다시 적용합니다.
- Compose를 해석할 때 Docker Compose가 프로젝트 .env/env_file 등을 참조할 수 있지만 원문 환경 값은 미리보기 응답에 반환하지 않습니다. 기존 파일·로그 마스킹/경로 제한/별도 실행 승인도 유지합니다.

로컬 무인증 API이므로 localhost 바인딩을 유지해야 합니다. 이 기능은 서버 계정이 접근 가능한 다른 Compose 폴더를 명시적으로 선택하게 하므로, 인터넷/LAN 공개 전에 인증·프로젝트 루트 allowlist·사용자별 세션 격리를 추가해야 합니다. 셸 명령 문자열을 실행하거나 임의 파일 읽기 API를 추가한 것은 아닙니다.

## API

| API | 입력·출력 |
|---|---|
| GET /targets | current, instance_id, projects, containers, truncated |
| POST /targets/preview | `{project_name}` 또는 `{project_dir}` → target·instance_id, 연결 대상은 변경하지 않음 |
| POST /targets/select | 위 선택 + `{expected_instance_id}` → 새 target·instance_id. 진단·컨테이너 조작 없이 대상 전환 |
| GET /health | target_selected, target_switching, instance_id, docker_target |

일반 JSON/승인/확인 답변 요청은 `X-Docker-Target: <instance_id>`, GET SSE는 `target_id=<instance_id>`로 오래된 대상 요청을 막습니다. 전환 충돌/오래된 선택은 409, 잘못된 경로·프로젝트는 400, 새 MCP 연결 실패는 502입니다. 동일 화면에서 경로 미리보기를 실행했다고 자동 전환되지는 않습니다.

## 실제 검증

[원시 API·파일 관측 기록](../eval/results/target_selection_live_20261021.json), 실행기 `python -m eval.target_selection_live`.

- 실제 Ollama/live Docker에서 bizxray → buttercast 선택.
- buttercast의 서비스 api/worker 및 실제 루트 경로가 MCP에 반영됨.
- configuration route로 실제 buttercast `docker-compose.yml`, `Dockerfile`, `.dockerignore` 조회 성공. 이 평가는 파일 선택/수집 경계 검증이며 모델 검토 의견의 정확도 보장은 아님.
- 이전 대상 ID의 요청은 409.
- 서버 절대 경로 입력으로 BizXray 미리보기 → bizxray 복귀 성공.
- 전후 12개 컨테이너 ID·상태 동일, 변경 승인 객체 없음. 컨테이너 시작/중지/재생성·파일 수정 없음.

브라우저에서도 목록에서 buttercast 선택 → 경로 미리보기 → 진단 대상 전환을 확인했고, BizXray 절대 경로를 직접 입력해 Dockerfile 3개의 미리보기와 bizxray 복귀까지 확인했습니다. 실제 엔진 목록은 프로젝트 3개·컨테이너 12개, 실행 중 컨테이너는 0개였습니다.

자동 테스트: backend 전체 **233개**, 프론트 trace/SSE 4개 및 production build 통과. 추가 테스트는 목록의 중지/미소속 컨테이너, 경로 검증, 이전 환경 제거, MCP 재연결·종료, 대화 격리, 이전 승인/질문의 복귀 후 무효 상태, 전환 실패·실행 중 충돌, SSE 전체 수명 lease를 포함합니다.

![프로젝트 선택·컨테이너 목록 실제 화면](../eval/results/project_selection_ui_20261021.jpg)

Windows/원격 Docker 엔진 실기, 다중 사용자/다중 worker, Docker 데몬 종료 중 전환·부하 테스트는 수행하지 않았습니다.
