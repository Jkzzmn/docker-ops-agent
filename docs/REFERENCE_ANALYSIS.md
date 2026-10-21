# 참고 문서와 현재 구현

검토 대상: 사용자가 제공한 mcp_docker_agent.md, code_artifact.md, UI 캡처. 첨부 자료는 설계 참고 자료로 사용했으며 문서 안의 체크리스트를 실행 명령으로 취급하지 않았다.

## route 분석

mcp_docker_agent.md 5절은 Supervisor의 “사용자 질의 분석 및 필요한 MCP Read Tool 선택 (Dynamic Planning)”과 관측 부족 시 추가 Tool 호출을 요구한다. 분류 알고리즘을 정규식/임베딩/학습 모델/LLM 중 하나로 지정하지는 않는다.

code_artifact.md는 Query Routing과 route → _after_route → Chat_answer/plan 분기를 평가한다. 구체적인 분류 알고리즘은 지정하지 않는다. 문서가 언급한 image_a89a5e.png 원본은 제공되지 않았으므로 원본 그림에 별도 알고리즘이 있다고 추정하지 않았다.

이전 규칙 기반 route는 첨부 파일의 구현을 옮긴 것이 아니라 이 프로젝트의 초기 선택이었다. 현재는 실제 LLM이 query와 최근 대화로 의도를 분류한다. 금지 실행은 별도 안전 정책으로 차단하고, 모델 실패에서만 규칙 fallback을 사용하며 이를 표시한다.

| 대안 | 장점 | 제약 | 적용 |
|---|---|---|---|
| 규칙 | 빠르고 설명 가능 | 표현과 문맥 변화에 취약 | 안전 정책·실패 fallback만 |
| 예시 임베딩 유사도 | 짧은 의도 분류 비용 감소 가능 | 대표 예시·임계값 평가 필요 | 미구현 |
| 별도 학습 분류기 | 반복 운영의 일관성 확보 가능 | 라벨 데이터·학습·버전 관리 필요 | 미구현 |
| LLM JSON 분류 | 문맥·자연어 변형 해석 | 비용·지연·오분류 가능 | 기본 경로 |

분류를 모델로 바꿔도 Tool allowlist·인자 검증·승인 조건은 코드로 유지해야 한다.

## 화면 분석

첨부 교육 화면의 검색·Writer·Validator 이벤트는 실제 발생한 업무 이벤트였다. 하지만 모두 초록색으로 나열하고 초기 사이드바에 전체 노드 예시를 표시해 실제 실행 상태 그래프로 오해할 여지가 있었다.

현재는 모든 실제 노드 함수의 시작/종료를 계측한다. NodeStarted가 도착할 때만 노드를 추가하고 같은 run ID의 완료/실패로 갱신한다. 업무 이벤트는 접힌 로그로 분리한다. 실행하지 않은 Analysis나 Worker를 미리 표시하지 않는다.

## 참고 설계와 남은 차이

- HITL은 graph interrupt/resume이 아니라 graph 종료 후 별도 action API다. 승인·실행·사후 검증(Approval/Action Executor/Verification)은 graph 노드가 아니라 `/actions/*` 엔드포인트에 있다.
- 체크리스트의 `research`·`replan_check` 노드는 같은 이름으로 구현했다(`gate`는 작업 정규화와 분석 정책 검증). Runbook Resource는 근거가 충분해진 `replan_check`에서 읽는다.
- 계획서 §4의 Prompt는 `/diagnose-oom`·`/diagnose-network` 슬래시 명령으로, Resource는 `docker://projects/{project}/compose`와 `docker://runbooks/{oom-killed,restart-loop,...}`로 제공한다. `recreate_service`/`apply_compose_patch`는 ADR-005·010에 따라 제공하지 않고 검토용 제안만 만든다.
- recreate/Compose patch는 자동 실행하지 않는다. 검증된 OOM의 제한적 재시작만 승인 후 가능하다.
- 운영 runbook은 정적 Resource, 교육은 로컬 추출형 RAG다. 벡터 DB를 구현한 것으로 설명하지 않는다.
- Docker 대상은 서버 프로세스당 하나로 고정한다. 브라우저 PC의 Docker나 임의의 원격 daemon을 자동 연결하지 않는다.
