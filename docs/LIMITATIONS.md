# 한계와 트레이드오프

- Docker daemon 접근은 호스트 제어에 준하는 고권한입니다. 이 프로젝트의 allowlist가 권한 분리 자체를 대체하지 못합니다. 실제 운영에서는 rootless Docker, 별도 읽기 전용 수집기, 서비스 계정과 감사 로그가 필요합니다.
- `fixture` 복구는 성공을 시뮬레이션하므로 실제 정상화를 증명하지 않습니다. `live`에서만 post-action snapshot이 실제 상태입니다.
- restart는 증상을 잠시 완화할 뿐 메모리 누수나 잘못된 설정을 해결하지 못할 수 있습니다. 자동 재시작 성공을 근본 원인 해결로 표현하지 않습니다.
- 증거 기반 replan은 정확성을 높이는 대신 MCP 호출 수와 응답 지연을 늘립니다. 최대 2회로 제한해 무한 루프를 막았습니다.
- LLM validator도 오판할 수 있어 citation/policy는 deterministic code gate와 함께 사용합니다. 검증 3회 실패 시 미검증 보고서로 종료하고 승인 객체를 만들지 않습니다. citation ID 존재 검사는 각 문장의 논리적 타당성을 증명하지 않습니다.
- graph는 SQLite AsyncSqliteSaver로 상태를 저장합니다(ADR-012). 새 요청은 실행 상태를 초기화하고 최근 대화 맥락만 전달하며, graph interrupt/resume API와 미완료 변경 자동 재개는 구현하지 않았습니다. 실행 중 응답 유실은 unknown 상태로 기록하고 자동 재실행하지 않습니다. `GET /metrics` 카운터는 프로세스 메모리이며 재시작 시 초기화됩니다. [기억 범위와 저장 위치](CONVERSATION_MEMORY.md).
- GET SSE는 긴 질의와 URL 로그 노출에 취약합니다. 과제 샘플 호환을 위해 유지했으며 운영에서는 인증된 POST streaming으로 바꿔야 합니다.
- 운영 runbook Resource는 소형 정적 문서입니다. 별도로 교육용 RAG는 로컬 지식 카드 5개를 키워드 검색하여 출처·검토일·카드 개정 번호와 함께 설명·검증합니다. 임베딩·벡터 DB·웹 자동 갱신은 없고 동의어 사전 밖 표현과 복합 질문은 놓칠 수 있습니다. 일반 chat 및 unknown 장애 원인이 자동으로 이 검색을 실행하는 것은 아닙니다.
- 교육에서 실시간 점검이나 복구 수행을 주장하지 않으며 승인 객체도 생성하지 않습니다. 데이터 유실 복구·스토리지 전용 실시간 원인 분류는 미구현입니다. 교육 검증 3회 실패 시 최종 초안을 숨기지만 검증 전 스트리밍 내용은 잠시 보일 수 있습니다.
- 자유 생성 + LLM judge에서 실제 교육 오류를 확인하여 교육은 검토 원문을 조합하는 추출형 RAG로 제한했습니다. 모델은 카드만 선택하며 교육 Validator는 원문 일치 코드 검사만 합니다. 자유로운 비유·맞춤 문장 생성은 없고 답변 정확성은 검토 카드의 정확성에 의존합니다.
- OpenAI 사용은 외부 전송과 비용이 발생합니다. 민감 로그는 마스킹되지만 조직 정책상 외부 모델 전송이 허용되는지 별도 확인해야 합니다.
- DNS/TCP probe는 허용 서비스명·선언된 container port로만 제한되며, source 이미지에 Python 3가 필요합니다. source가 재시작 중이면 probe가 실패할 수 있습니다. 부분 실패를 evidence로 남기고 재계획 한도에서 종료합니다.
- live Docker OOM 이벤트는 최근 10분 중 daemon 이력 버퍼에 남은 것만 조회합니다. healthcheck 등으로 이벤트가 많으면 OOM 이벤트가 더 빨리 사라질 수 있습니다. inspect의 OOMKilled=true와 Exit 137은 종료 원인 근거로 인정하되 독립 근거 부족 시 승인 객체를 만들지 않습니다. 종료된 컨테이너의 현재 stats는 관측 불가로 보고합니다.
- 기본 서비스는 localhost에서 인증 없이 사용합니다. SQLite 승인과 HMAC은 사용자 인증을 대체하지 않습니다. 운영 인증·역할 분리·POST streaming은 후속 범위입니다.
- 원인 분류와 필수 근거는 보수적인 코드 규칙을 사용합니다. Supervisor와 서브 에이전트의 계획·분석·보고·검증은 실제 모델을 호출합니다. 테스트 double의 분기 검증을 실제 모델 정확도나 운영 성공률로 해석하지 않습니다.

- 종료된(exited) OOM 컨테이너는 현재 stats를 얻을 수 없어 필수 근거에서 제외합니다. 대신 inspect의 OOMKilled·Exit 137과 최근 10분의 Docker OOM 이벤트(또는 OOM 로그)가 서로 맞아야 승인 객체가 생깁니다. 이벤트가 이미 밀려났고 로그에도 단서가 없으면 원인은 memory로 보고하되 승인은 만들지 않습니다.
- 재시작은 `RestartCount`를 0으로 되돌리지 않고(Docker 정책 재시작만 증가), 복구 확인은 Docker health이며 HTTP readiness가 아닙니다. 확인은 `RECOVERY_WAIT_SECONDS`(기본 20초) 동안 폴링하므로 승인 응답이 그만큼 늦어질 수 있고, 같은 결함이 남은 서비스는 확인 뒤 다시 종료될 수 있습니다.
- loopback 바인딩 탐지는 대상 컨테이너에 Python 3가 있어야 하고 `/proc/net/tcp*`를 읽습니다. 호스트-컨테이너 포트 혼동 탐지는 로그에 `서비스명:포트` 형태가 남아 있을 때만 동작합니다.
- 설정 변경 제안은 검토용 텍스트입니다. 이미지별 healthcheck 명령(`pg_isready` 등)은 포트로 추정한 값이므로 사람이 확인해야 하며, 어떤 파일도 자동으로 고치지 않습니다.
- 토큰 수는 Ollama(`prompt_eval_count`/`eval_count`)와 OpenAI(`usage`)가 보고한 값만 합산합니다. 보고가 없는 호출은 `tokens_reported_for`에 드러납니다. unsupported claim rate는 여전히 측정하지 않습니다.
- LangSmith 전송은 환경변수로 켤 수 있게 준비되어 있으나 API 키가 없어 이 저장소에서는 실행을 확인하지 못했습니다. 평가표의 LangSmith 항목은 '코드 준비 + 자체 노드 트레이스'로만 주장합니다.
- `reply_html`은 서버의 작은 Markdown 부분집합 변환기(`backend/render.py`)가 만듭니다. 모델이 쓴 임의 HTML이나 이미지·스크립트는 지원하지 않고 글자로 보입니다. 프런트 sanitizer는 브라우저에서 악성 입력 9종으로 확인했지만 자동 테스트는 서버 변환기 쪽(`tests/test_render.py`)에만 있습니다.
