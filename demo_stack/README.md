# 격리된 Compose 장애 재현

이 스택의 `db`는 실제 데이터베이스가 아닌 Python TCP readiness 대역입니다. 데이터 볼륨을 사용하지 않고 세 컨테이너에서 관측·연결 실패를 재현합니다. Docker Compose v2와 Python을 포함한 이미지가 필요합니다.

프로젝트 루트에서 다음 중 하나를 선택합니다. `COMPOSE_FILE`은 장애 재현과 Agent 실행 양쪽에서 같아야 진단이 같은 구성을 봅니다.

```bash
export COMPOSE_FILE="$PWD/demo_stack/compose.yaml:$PWD/demo_stack/compose.oom.yaml"
# 또는 두 번째 파일을 compose.readiness.yaml / compose.network.yaml로 변경
docker compose --project-directory ./demo_stack up -d --build

DOCKER_MODE=live DOCKER_PROJECT_DIR="$PWD/demo_stack" \
DOCKER_ALLOWED_SERVICES=api,db,web ./run_ollama.sh
```

정상 기준선은 `COMPOSE_FILE="$PWD/demo_stack/compose.yaml"`로 실행합니다. API health endpoint는 `http://127.0.0.1:18000/health`입니다.

- OOM: 약 5초 뒤 API 메모리를 반복 할당하고 종료된 상태를 유지합니다. inspect의 OOMKilled/ExitCode와 최근 10분의 Docker OOM 이벤트를 교차 확인합니다. 종료된 컨테이너의 현재 stats는 확보하지 못할 수 있으며 이때는 coverage 부족을 보고하고 복구 승인을 생성하지 않습니다. 재시작해도 결함은 다시 발생합니다.
- Readiness: DB listener 시작을 120초 지연하고 healthy 의존성 조건을 제거합니다. API 부팅 시 연결이 거부되며 DB가 준비된 뒤 재시작하면 회복됩니다.
- Network: API/web와 DB를 서로 다른 network에 배치해 DNS/TCP 실패를 재현합니다. Agent는 network 수정을 제안하며 자동 patch는 수행하지 않습니다.

정리는 같은 `COMPOSE_FILE` 환경에서 `docker compose --project-directory ./demo_stack down`으로 수행합니다.
