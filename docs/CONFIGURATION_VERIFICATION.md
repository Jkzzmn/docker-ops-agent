# 설정 검토·변경 검증 — 2026-10-21

사용자 흐름: 자연어 질문 → LLM route → 실제 Docker/파일 MCP 수집 → 모델 검토 → 사실/가설·diff → 파일 승인 → 별도 배포 승인 → 검증·필요 시 복원.

## 실제 bizxray 읽기 전용 실행

질문: “현재 bizxray의 Dockerfile과 도커 컴포즈 실제 파일을 읽고 컨테이너 상태와 설정 문제를 검토해주세요. 파일 변경이나 배포는 하지 마세요.”

실행 경로: `route → configuration → config_collect → config_review → config_propose`.

| 실제 입력 | 실제 출력 |
|---|---|
| route: 위 질문·최근 대화 | `configuration`, LLM 분류 |
| config_collect: desktop-linux / bizxray | E1 활성 docker-compose.yml, E2 merged config, E3 실제 ps, E4 설정 파일 목록, E5 Engine, E6… Dockerfile/ignore와 각 서비스 inspect/logs |
| config_review: 마스킹·크기 제한된 실제 E 근거 | Ollama JSON 검토. 모델 의견은 확정 사실이 아님 |
| config_propose: 읽기 전용 route | 근거 포함 답변, `approval=null`, `proposal=null`. 파일·컨테이너 변경 0 |

코드로 확인한 실제 사실:

- 현재 Compose 정의: ai, postgres, backend, frontend.
- 이 네 서비스에 대응하는 컨테이너 없음 → **미배포**. health 정상/장애 판정 불가.
- 같은 프로젝트에 예전 ollama, ollama-pull 컨테이너가 exited 상태로 존재. allowlist 밖이므로 자동 변경하지 않음.
- Dockerfile: AI, BE/business, FE/business에서 실제 읽기 성공.
- AI/.dockerignore 누락. 누락 자체가 장애나 정보 유출의 증명은 아님.
- frontend는 Compose healthcheck 정의 없음. 실제 이미지 내장 healthcheck는 컨테이너가 없어 확인 불가.

초기 모델 schema 실패와 마스킹/포트 오해가 재현돼 JSON context 크기를 명시하고 모델 검토 입력을 제한했습니다. 최종 읽기 요청은 schema 오류 없이 종료했으나 모델의 “설정 문제 없음” 의견은 코드로 검증된 결론이 아니므로 별도 미검증 요약으로 표시합니다.

### 브라우저에서 실제 변경 제안까지 확인

질문: “ai 서비스 빌드 경로의 AI/.dockerignore가 실제로 없는지 확인하고, 없다면 .git, .env, __pycache__/를 제외하도록 새 파일을 추가하는 정확한 diff를 제안해주세요. 파일 쓰기와 배포는 승인 전 실행하지 마세요.”

보완 후 실제 route 노드 출력은 `change`, `route_method=llm`, `route_fallback=false`였습니다. 같은 5개 노드 실행 뒤 아래 실제 diff와 대상 desktop-linux/bizxray/ai, SHA256, 검증·배포 명령, 위험·복원 설명 및 “1차: 파일 변경 승인” 버튼이 표시됐습니다. **승인하지 않았고 실제 파일은 생성하지 않았습니다.**

```diff
--- a/AI/.dockerignore
+++ b/AI/.dockerignore
@@ -0,0 +1,4 @@
+.git
+.env
+.env.*
+__pycache__/
```

diff SHA256: `22184d03d41783699749cf29c5c19ef3db4e00547eaa85edefd814dce43bbe77`. 이 제안은 파일 변경만 대상으로 하며, 현재 ai 컨테이너가 없으므로 파일 승인 이후에도 자동 신규 배포는 지원하지 않습니다. 승인 만료 시 같은 질문으로 새 제안을 받아야 합니다.

## 분리된 실제 Docker 변경·복원

실행기: `eval/config_transaction_live.py`. 외부 다운로드 없이 기존 로컬 `docker-ops-example-web` 이미지를 사용했습니다. 테스트 전용 임시 경로, 고유 Compose 프로젝트, 서비스 probe 하나, 영구 볼륨/공개 포트 없음. bizxray에는 어떤 변경 승인도 실행하지 않았습니다.

| 단계 | 실제 결과 |
|---|---|
| 기준 컨테이너 build/up | Docker health 정상 |
| 1차 Dockerfile LABEL 패치 승인 | 파일·Compose 검증 성공, 컨테이너 ID 바뀌지 않음 |
| 별도 2차 배포 승인 | 이전 이미지 태그 보존 → build → no-deps 재생성 → health 정상 |
| 의도적 실패 CMD 패치/배포 | 컨테이너 exit 1, health 실패 감지 |
| 자동 복원 | 파일 원본 및 이전 이미지 복원, running 상태 복원, health 재정상화 확인 |
| 정리 | 테스트 프로젝트 컨테이너·네트워크만 down. 볼륨 삭제 없음. 복원 태그·테스트 이미지는 보존 |

테스트 프로젝트: `docker-ops-config-check-9993c6effa`. 최초 복원 시 이전 이미지 참조가 사라지는 실제 문제를 잡아 복원용 태그 보존을 추가했습니다. 수정 후 위 전체 경로가 통과했습니다. 별도의 중지 상태 복원·scope/해시 공격·만료·재사용·2차 거절은 자동 테스트로 확인했으며, 실제 중지 서비스 재생성은 이번 실기에서 추가 실행하지 않았습니다.

## 범위와 한계

기존 155개 + 신규 설정 제어/그래프/API 34개, 합계 189개가 통과했고 프론트 trace 테스트 2개와 production build도 통과했습니다. 브라우저에서 질문 직후 실제 노드 1개만 표시되고, 완료 시 해당 경로 5개 노드가 나타나는 것과 사실/미검증 의견 분리·근거 표시를 확인했습니다. “승인 전 실행하지 말고 diff 제안” 요청이 configuration으로 오분류되어 분류 지침을 보완했고, 실제 재실행의 route 노드 출력에서 `change`, `route_method=llm`, `route_fallback=false`를 확인했습니다. 모델 정확도 보증, 신규 서비스 생성, 업무 트랜잭션/DB 복구, 분산 잠금, 강제 종료 후 자동 복원은 미검증/미지원입니다. 운영 배포 전에 인증과 백업 보존 정책이 필요합니다.

원시 기록: [실제 배포·실패 복원](../eval/results/config_transaction_live_20261021.json), [실제 읽기 전용 검토](../eval/results/config_review_live_20261021.json). 읽기 기록에는 마스킹된 프로젝트 설정과 로컬 경로가 있으므로 외부 공유 전 확인하세요. 읽기 기록은 모델 요약에 미검증 표시를 보강하기 직전 API 응답이며, 최종 UI의 표시는 본 문서의 브라우저 확인 내용과 같습니다.
