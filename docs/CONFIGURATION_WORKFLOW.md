# 실제 설정 검토와 2단계 변경 승인

2026-10-21 추가. 기존 교육 RAG·장애 진단·OOM 재시작 흐름은 유지합니다. 자연어 요청이 실행 승인을 대신하지는 않습니다.

## 사용법

Ollama 앱: `./run_ollama.sh`, <http://127.0.0.1:5173>.

- 읽기 전용: “현재 프로젝트의 Dockerfile과 도커 컴포즈 실제 파일을 읽고 설정과 컨테이너 상태를 검토해주세요.”
- 변경 제안: “Dockerfile과 Compose를 점검하고 근거가 확인되는 문제만 수정 제안해주세요. 정확한 diff를 보여주세요.”
- 특정 변경: “ai 빌드 경로에 .dockerignore가 없다면 .git과 .env가 빌드에 들어가지 않도록 추가 제안해주세요.”

LLM 의미 분류에 `configuration`/`change`가 추가됐습니다. 평범한 개념 설명은 여전히 chat/education이며 파일을 읽지 않습니다. 현재 질문만 먼저 분류하고 LLM이 문맥을 요청한 후속 질문에만 이전 사용자 질문을 제공합니다. LLM 실패 때는 규칙/교육 fallback 없이 중단합니다. 실제 변경 권한은 분류 결과와 무관하게 코드의 허용 경로·서비스·서명 승인으로 제한됩니다.

## 실제 실행되는 노드와 입출력

```text
route ─ configuration/change → configuration (Supervisor가 서브 그래프 호출)
                               └ config_collect → config_review → config_propose → END
```

| 노드 | 입력 | 출력 | 조건·실제 실행 |
|---|---|---|---|
| route | query, history | route, reason, method | configuration/change일 때만 아래 그래프 실행 |
| configuration | query, thread_id, route | evidence, final, proposal, approval | Supervisor가 Configuration Agent 감독. 교육 검색·장애 analysis/writer 경로를 억지로 돌리지 않음 |
| config_collect | 실제 선택 프로젝트, 허용 서비스 | E1… 근거 원장, 성공/누락/실패 결과 | 활성 Compose, merged config, ps, 설정 파일 목록, Engine 조회. merged build의 경로에서 Dockerfile/.dockerignore 선택. 각 서비스 inspect·최근 로그 조회 |
| config_review | query, route, 크기 제한·마스킹된 근거 | summary, findings, services, exact-snippet edits | Ollama quality 모델 JSON 검토. 추론은 미검증 가설로 분리. 모델 실패 시 근거만 남기며 수정 없음. 모델이 파일 hash를 생성하지 않고 관측한 hash를 코드가 붙임 |
| config_propose | 검토 결과와 근거 | final, optional exact diff/proposal/file approval | configuration은 승인 없음. change라도 안전한 수정안이 없으면 승인 없음. preview 도구가 경로·서비스·원문 일치·hash·diff를 검증한 경우만 1차 승인 생성 |

NodeStarted/Completed는 실제 노드 호출을 감싼 이벤트입니다. 화면은 NodeStarted가 도착할 때만 노드를 추가합니다. 모든 질문에 미리 그린 그래프를 표시하지 않습니다. 입력·출력 snapshot은 마스킹 후 최대 24,000자이며 초과 시 truncated로 표시합니다.

## 고정 MCP 도구

| 도구 | 입력 | 출력/권한 |
|---|---|---|
| list_project_config_files | 없음 | 설정 파일의 상대 경로·종류·크기·SHA256. .env 원문은 안 읽음. 깊이 6, 항목 수 제한 |
| read_compose_files | 없음 | 실제 활성 base/override의 상대 경로·원문 마스킹·SHA256 |
| read_dockerfile | 실제 build 참조 상대 경로 | build 참조 Dockerfile 또는 .dockerignore만 반환. 외부/원격 context 읽기 불가 |
| validate_compose_config | 없음 | `docker compose config --format json`의 안전한 선택 필드. build, image, healthcheck, depends_on, restart, 리소스, networks, mounts, ports, 권한/user/read_only. 전체 환경 변수는 반환하지 않음 |
| preview_project_config_patch | edits, services | 수정 없이 정확한 diff·해시·대상·위험·고정 검증/배포 명령·복원 방침 |
| apply_project_config_patch | plan, 1차 서명 토큰 | 원본 백업 → 정확한 파일 변경 → `compose config --quiet`. 구문 실패 시 파일 복원. **build/up 없음** |
| deploy_project_config | 1차 성공에서 만든 runtime plan, 새 서명 토큰 | `compose build <services>` → `compose up -d --no-deps --no-build --force-recreate <services>` → inspect/health/logs, 선언된 의존성 network/DNS/TCP 검증. 실패 시 복원 |

범용 shell, exec 문자열, write_file은 없습니다. Dockerfile의 빌드 명령은 별도 배포 승인을 통해 실제 Docker build에서 실행되므로 diff를 반드시 검토하세요. 파일 읽기만으로 빌드를 실행하지 않습니다.

## 승인 계약

1. **1차: 파일 변경 승인**. 화면에서 실제 diff, 서비스, context/project/root, 위험, 명령, diff SHA256, 만료를 확인합니다. 서명은 정확한 plan 전체와 action ID/도구/만료에 결합합니다.
2. MCP는 Engine ID/context/DOCKER_HOST/프로젝트 root/Compose 파일 순서/허용 서비스/설정 파일 hash/merged config hash를 다시 확인합니다. .env의 해결된 설정이 달라져도 전체 merged config hash가 바뀌어 거부합니다. 모델에 .env 내용을 보내지는 않습니다.
3. POSIX directory FD + O_NOFOLLOW로 경로를 고정합니다. 절대·상위 이동·symlink·hardlink·64KiB 초과 파일·숨김 디렉터리를 거부합니다. 정확한 public snippet이 한 번 일치해야 하고 마스킹된 값을 덮어쓰지 않습니다.
4. 프로젝트별 고정 설정 파일만 수정합니다. Compose 서비스 추가/삭제·비승인 서비스·전역 volumes/networks 변경은 거부합니다. mount/devices/privileged/cap_add/network_mode/pid/ipc/env_file/secrets 변경은 수동 검토로 넘깁니다. 누락 .dockerignore 외 새 파일·파일 삭제는 불가합니다.
5. `data/config_backups/<transaction>/`에 원본·manifest를 보관합니다(디렉터리 0700, 파일 0600). 파일 교체는 원자적이며 구문 실패 시 복원합니다. 다중 파일 전체를 단일 OS 원자 연산으로 바꾸지는 않습니다.
6. **2차: 빌드·재생성 승인**은 1차 파일 검증 성공 후 별도로 만듭니다. 기존 컨테이너/이미지/실행 상태를 확보하지 못하면 배포 승인을 만들지 않습니다. 2차 거절·만료는 배포하지 않으며 **이미 승인된 파일 변경은 유지**됩니다.
7. 2차 실행 직전 파일·Engine·기존 컨테이너 ID/이미지/상태를 재확인합니다. 현재 단일 기존 컨테이너 및 healthcheck가 있는 서비스만 자동 배포합니다. 없으면 실행 전 중단합니다. 신규 서비스 프로비저닝은 미지원입니다.
8. 변경 도구는 한 번만 호출하며 timeout 때 자동 재시도하지 않습니다. 승인 API는 프로세스 내 직렬화, MCP 변경 도구도 잠금으로 직렬화합니다. 여러 API 프로세스/다중 호스트 간 분산 잠금은 미지원입니다.

## 실패와 복원

- 파일 구문 검증 실패: 변경한 파일만 원본으로 복원. 제3자가 수정한 파일은 덮어쓰지 않고 복원 미완료로 표시합니다.
- 배포 직전 이전 이미지에 `docker-ops-rollback-<transaction>:<service>` 태그를 붙여 빌드가 원래 태그를 바꿔도 복원 참조가 사라지지 않게 합니다. 이 명령도 2차 승인 화면에 표시됩니다. 복원 태그는 자동 삭제하지 않습니다.
- build/up/health/network 검증 실패: 승인된 변경 파일 복원 → 원래 Compose 검증 → 이전 **불변 image ID에 고정한 복원 태그** override로 대상 서비스 재생성 → 이전 running 서비스는 기동, exited/created 서비스는 `--no-start`로 중지 상태 유지 → 이미지/상태 및 실행 중 서비스의 health 재확인.
- 복원 실패는 성공으로 포장하지 않고 `manual_intervention` 및 실제 오류를 반환합니다. 이전 파일과 이미지에 대한 복원이지 DB 데이터·마이그레이션·외부 API 부작용 롤백은 아닙니다. 컨테이너 ID는 복원 시에도 재생성됩니다.
- `down -v`, prune, 볼륨 삭제는 실행하지 않습니다. 검증 전용 스크립트의 `down`은 스크립트가 만든 고유·볼륨 없는 테스트 프로젝트에만 적용합니다.
- healthcheck가 없거나 Python3 DNS/TCP probe를 실행할 수 없는 이미지는 자동 성공 판정을 하지 않습니다. Docker health 통과도 업무 데이터 무결성이나 장기 안정성의 증명은 아닙니다.
- 백업에는 비밀 원문이 있을 수 있습니다. API/모델로 반환하지 않으며 로컬 접근 권한을 보호하세요. 암호화·자동 보존기한 삭제·프로세스 강제 종료 후 자동 복원은 아직 없습니다. 강제 종료 시 백업과 실제 파일을 직접 확인해야 합니다.
- 승인 hash는 Docker 설정 파일 및 해결된 Compose 설정 범위입니다. 애플리케이션 소스 전체를 스냅샷/잠금하지는 않습니다. 승인 대기·배포 중 다른 작업이 소스나 Docker 대상을 바꾸지 않게 운영하세요. 마스킹도 알려진 비밀 패턴 기반이며 모든 임의 비밀 문자열 탐지를 보장하지 않습니다.

## 실제 점검 기록 및 검증 범위

2026-10-21의 bizxray 읽기 전용 점검에서 현재 Compose 정의(ai/postgres/backend/frontend)와 남아 있는 예전 컨테이너(ollama/ollama-pull)가 다른 것을 확인했습니다. 정의된 서비스에 컨테이너가 없으면 미배포라고 보고하며 정상/장애를 추정하지 않습니다. Dockerfile 3개와 실제 .dockerignore들을 읽었고 AI/.dockerignore는 누락으로 관측했습니다. 파일/컨테이너는 변경하지 않았습니다.

모델의 첫 검토 응답은 JSON schema 실패, 다음 시도는 마스킹과 포트를 오해했습니다. 이를 숨기지 않고 context 크기 옵션(`OLLAMA_NUM_CTX`, 기본 8192), 제한된 검토 입력, 코드로 확인한 사실/미검증 모델 의견 분리로 보완했습니다. 모델 의견은 여전히 정확성을 보장하지 않습니다.

별도 실제 Docker 프로젝트 `docker-ops-config-check-9993c6effa`에서 파일 승인 시 컨테이너 ID 유지, 2차 승인 후 build/up/health 성공, 이후 의도적 exit 1 실패 → 원본 파일/이전 이미지 복원 → health 재정상화를 확인했습니다. 테스트 컨테이너와 네트워크는 종료 후 정리했습니다. 중간 검증에서 containerd의 이전 이미지 참조 유실을 발견해 배포 전 복원용 태그를 고정한 뒤 재검증했습니다. 실제 결과 요약은 [검증 보고서](CONFIGURATION_VERIFICATION.md)입니다.

```bash
.venv/bin/python -m pytest -q
node --test frontend/tests/trace.test.mjs
npm --prefix frontend run build
# 선택 실행: 기존 로컬 docker-ops-example-web 이미지를 쓰는 분리된 실제 Docker 검증
.venv/bin/python -m eval.config_transaction_live
```

단위·통합 테스트는 경로 공격, 파일 크기·마스킹, scope/hash/승인 만료·재사용, 설정 구문 실패 복원, 외부 수정 보호, 2차 거절, 배포 실패 복원, 실제 LangGraph 분기/노드 입출력 및 API 계약을 확인합니다. 테스트 double 결과를 실제 Docker 배포 결과와 혼동하지 않습니다. 운영용 인증·분산 조정·다중 replica 배포·신규 프로젝트/서비스 생성은 범위 밖입니다.
