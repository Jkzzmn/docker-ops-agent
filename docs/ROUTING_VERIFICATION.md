# LLM 라우팅 점검·수정 — 2026-10-21

## 판단

수정 전에도 기본 분류는 실제 Ollama `qwen3:4b-instruct`였습니다. 다만 LLM 실패 시 규칙 fallback이 있었고, status를 incident로 바꾸는 키워드 후처리도 있었습니다. 분류 입력에는 최근 assistant 답변까지 포함됐습니다.

제보 문장 자체는 수정 전 새 대화/교육 답변 뒤 각 1회 모두 configuration으로 분류돼, 원래 보고된 응답 전체는 그대로 재현하지 못했습니다. 대신 긴 교육 대화 뒤 “볼륨 개념 설명은 나중에 하고, 지금 Compose 파일들을 읽고 문제를 검토해주세요”에서 실제로 education 오분류를 재현했습니다. 이 사례의 route_method는 llm, route_fallback=false였으므로 **이번에 재현한 문제는 규칙 fallback이 아니라 LLM의 이전 문맥 오분류**였습니다.

첫 보완으로 assistant 답변을 제거하고 짧은 이전 사용자 질문만 제공했지만 위 사례는 여전히 오분류했습니다. 실제 결과 [1차 평가](../eval/results/routing_llm_20261021_attempt1.json)는 18/19였습니다. 실패 기록을 삭제하거나 성공 기록으로 덮어쓰지 않았습니다.

## 현재 구조

```text
실행 안전 정책 (삭제·임의 셸 실행 차단, 의도 분류기와 별개)
  → 명시적 /diagnose-* 명령이면 지정 절차
  → 그 외 현재 질문만 LLM JSON 분류
      needs_context=false → 모델의 route 사용
      needs_context=true  → 최근 사용자 질문 최대 3개/각 500자로 LLM 재분류
      필수 대상·작업 불명확 → clarify → human_question → 답변 대기
      오류/30초 초과/잘못된 JSON → unavailable → 안내 후 종료
```

- 현재 질문이 완결된 새 요청이면 이전 교육 주제를 보내지 않습니다. 이전 assistant의 K1/K2 장문을 분류 입력에 넣지 않습니다.
- 문맥을 쓸지는 키워드가 아니라 LLM의 `needs_context`로 판단합니다. 후속 질문만 추가 호출하므로 보통 1회, 문맥이 필요하면 2회입니다. 전체 분류 제한은 30초입니다.
- 키워드 규칙 fallback과 status→incident 규칙 보정을 삭제했습니다. 이전 결정론적 로직은 모델 double의 테스트 데이터 생성용으로 tests/에만 있으며 앱에서 import하지 않습니다.
- 분류 실패 시 교육 카드·Docker 조회·변경으로 대체하지 않습니다. `route=unavailable`, `route_method=llm-unavailable`, `route_fallback=false` 및 실제 실패 이유를 노출합니다.
- 의도 분류와 실행 권한은 다릅니다. 위험 명령 차단, 도구/서비스/경로 allowlist, 정확한 diff·승인 검증은 계속 코드로 제한합니다. 이 안전 경계까지 LLM에 위임하지 않습니다.
- 추가 확인의 조건·번호/자연어 답변·새 실행·실제 검증은 [Human Question 설계](HUMAN_CLARIFICATION.md)를 참고하세요. 명확한 요청에는 이 노드를 거치지 않습니다.

확인 필드: `metadata.route`, `route_method`, `route_context_used`, `route_reason`, 실제 `route` 노드 출력. `route_fallback`은 기존 응답 호환용이며 현재는 false입니다.

## 실제 모델 평가

[최종 원시 기록](../eval/results/routing_llm_20261021.json), 실행기 `eval/routing_llm_live.py`. 실제 Ollama 호출이며 테스트 double/실제 Docker 조작은 없습니다.

| 질의 집합 | 결과 |
|---|---|
| 제보 문장, 새 대화 3회 | configuration 3/3 |
| 같은 문장, 긴 교육 대화 뒤 3회 | configuration 3/3, 과거 문맥 미사용 |
| 파일 검토 표현 변형·빌드 캐시·교육 주제 전환 | configuration 3/3 |
| 인사·기본 정의 | chat 2/2 |
| 볼륨 설명·일반 유실 상담 | education 2/2 |
| 상태만 조회 | status 1/1 |
| 재시작·DNS 실제 진단 | incident 2/2 |
| 정확한 diff 제안 | change 1/1 |
| “그럼 그걸 고쳐주세요” | LLM이 문맥 요청 → 이전 Dockerfile 질문으로 change 1/1 |
| prune 실행 | 코드 안전 정책 blocked 1/1, 모델 호출 0 |

합계 19/19(LLM 의도 분류 18건 + 독립 안전 차단 1건). 모델 호출 총 19회이며 문맥을 사용한 건은 후속 질문 1건뿐입니다. 확인 질문을 추가한 뒤의 재평가 기록이며 최대 입력 토큰은 1,300이었습니다. 추가 전 기록은 [이전 평가](../eval/results/routing_llm_20261021_before_clarification.json)에 보관했습니다. 제보 문장을 프롬프트 예시로 사용한 소규모 회귀 평가이므로 독립 블라인드 평가나 일반화된 정확도 보장이 아닙니다.

## 검토 요청과 변경 요청의 차이

“오류·비효율을 확인하고 개선점을 알려달라”는 configuration으로 실제 파일을 읽고 근거와 검토 의견을 보고합니다. 실제 diff 생성/파일 변경 요청은 change이며 별도 승인 절차로 갑니다. 이번 수정 범위는 의도 분류이며 설정 검토 모델 자체의 정확도까지 해결한 것은 아닙니다.

## 실제 API 전체 경로

같은 thread_id에서 먼저 “도커 컨테이너에 저장된 내용이 모두 사라졌어요”를 보내 education, Tool 0회를 확인하고, 곧바로 제보 문장을 전송했습니다.

- 최종 route=configuration, route_method=llm, route_context_used=false.
- 실행 노드: route → configuration → config_collect → config_review → config_propose.
- 교육 knowledge 0개, 실제 MCP 조회 19회. 활성 Compose 및 AI/BE/FE Dockerfile·ignore 파일을 조회했습니다.
- approval=null. bizxray 파일 변경·기동/재생성 없음.
- [교육 응답 원시](../eval/results/routing_education_prefix_20261021.json), [설정 검토 응답 원시](../eval/results/routing_pipeline_live_20261021.json).

최종 앱 브라우저에서도 제보 문장을 그대로 입력해 동일 5개 경로 완료를 확인했습니다. route 약 4.0초, 실제 수집 약 474ms, configuration review 약 37.5초였습니다. K1 교육 안내는 실행되지 않았습니다. 다만 최종 검토 의견도 미배포 상태 설명에 치우쳐 비효율 분석·구체적인 개선 제안 품질은 아직 부족했습니다. **라우팅 수정 완료와 검토 모델 품질 미완료를 구분**합니다.

**검토 품질의 별도 한계:** 중간 정적 검토 프롬프트 실험에서 quality 모델이 container-local localhost 헬스체크를 호스트 충돌처럼 잘못 설명하고 정상적인 published/container 포트 매핑을 혼동하는 의견을 생성했습니다. 잘못된 의견을 정확한 개선안으로 취급해서는 안 됩니다. 불필요한 개선안을 강제하는 프롬프트 실험은 철회했고 기존 검토 동작을 유지했습니다. 위 원시 기록은 이 실험 시점의 응답이므로 라우팅·도구 실행의 증거이지 권장 설정의 정확성을 입증하는 자료가 아닙니다. 별도 검토 품질 평가/보완이 필요하며, 현재도 코드로 확인한 사실과 미검증 모델 의견을 분리해 표시합니다.

## 자동 테스트

라우팅 수정 당시 196개 통과, 이후 확인 질문 기능까지 포함하면 backend 222개 통과입니다. 현재 질문 우선, 긴 assistant 교육 본문 제거, 중복 현재 질문 제외, 문맥 크기 제한, 모델이 요청한 2차 분류, 불명확한 후속 질문의 확인 노드 진입, 모델 JSON/연결/timeout 실패 시 RAG·Docker 미실행, API 실패 응답 계약을 확인합니다. 프론트 trace/SSE 테스트 4개 및 production build도 통과했습니다.
