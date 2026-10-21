# 평가 설계 — Supervisor v3

현재 앱은 실제 Ollama/OpenAI만 호출합니다. 과거 모델 대체 실행과 v1/v2 전환은 제거했습니다. 현재 자동 테스트의 모델 double은 tests/ 안에만 있으며 제품 backend가 아닙니다.

## 1. 제어 흐름 회귀 테스트

~~~bash
.venv/bin/pytest -q
npm --prefix frontend run build
~~~

검증 대상:

- 일반 대화·기본 Docker 개념 질문: route→chat_answer, MCP 0회.
- 교육·데이터 유실 상담: Knowledge 검색→Writer→Validator, K 인용과 공식 출처, MCP/변경 0회, 진단 원인·승인 없음.
- 검색 결과가 없으면 LLM 호출 없이 유보. 허구 K/E 인용·URL·위험 명령·검토 원문을 수정한 문장 거부, 교육 검증 한도 실패 시 초안 제거.
- 현재 상태만 요청: 단일 상태 Tool, 계획 모델/분석 미호출, 복구 승인 없음.
- Supervisor의 auto/skip/required 정책과 실제 evidence에 따른 조건부 분석.
- 근거 부족이 skip을 override하고 추가 수집을 2회로 제한.
- 독립 Worker state와 역할별 도구·대상 서비스 범위.
- Worker의 보완 계획, 보고, 부모 evidence ID 병합.
- Validator 재작성 최대 3회, 실패 시 승인 차단.
- HMAC·만료·재사용·중복·거절·부분 도구 실패.
- 실제 stdio MCP와 서브 그래프 custom SSE 전달.

이 테스트는 분기·계약·안전 경계의 검증이며 모델 정확도 측정이 아닙니다.

## 2. 실제 모델과 고정 Docker 관측 평가

동일한 Docker 관측으로 모델·프롬프트를 비교하려면 테스트 서버를 다음처럼 명시적으로 설정합니다.

~~~bash
DOCKER_MODE=fixture DOCKER_FIXTURE_SCENARIO=oom ./run_ollama.sh
.venv/bin/python eval/run_eval.py --scenario oom --repeats 3
~~~

이때도 답변·계획·Worker·Validator에는 실제 모델이 사용됩니다. `eval/scenarios.jsonl`은 8개 상태(oom, oom-exited, readiness, network, healthy, exit137, loopback, host-port)의 43개 질의를 포함하며 단일 실행에서는 서버의 해당 scenario만 선택합니다. server scenario가 다르거나 live Docker이면 고정 정답 평가를 거부합니다. 실행기는 승인 API를 자동 호출하지 않습니다.

| 지표 | 실제 계산 |
|---|---|
| Route accuracy | chat/status/incident/blocked 정답 일치 비율 |
| Root-cause Top-1 | 원인 정답이 있는 질의의 구조화된 cause 일치 |
| Evidence coverage | Supervisor 필수 task 중 성공한 관측 비율 |
| Unsafe block rate | 위험 질의만 분모. PolicyBlocked + mutation 0 + 승인 객체 없음 |
| Invalid citation rate | 실패한/없는 live evidence ID 인용 비율 |
| Analysis usage rate | analysis_used=true 비율 |
| Tool calls | 실행한 관측 호출 수 |
| Latency | 실제 모델을 포함한 p50/p95 및 표준편차 |
| Model calls / Tokens | 요청별 `metadata.llm_usage`(호출 수, fast/quality 호출 수, 입력·출력 토큰). Ollama `prompt_eval_count`/`eval_count`, OpenAI `usage` 값만 합산하며 보고가 없는 호출은 `tokens_reported_for`에 드러남 |
| Tool concurrency | 도구 소요시간 합 ÷ 동시 관측 단계의 실제 소요시간(`tool_concurrency`). 1보다 크면 같은 Worker 안의 도구가 겹쳐 실행된 것. Worker의 모델 계획 시간은 포함하지 않음 |
| Unsupported claim | 현재 미측정(null). ID 존재 검사를 주장 진실성으로 대체하지 않음 |

실제 모델의 비결정성을 고려해 같은 관측/질문/모델/장비에서 3~5회 이상 반복하고 cold/warm 구분과 실패율도 기록해야 합니다.

## 3. 실제 Ollama smoke

~~~bash
.venv/bin/python eval/check_supervisor.py --model qwen3:4b-instruct
~~~

설치된 모델로 개념 질문·상태-only·OOM incident 세 경로를 API→실제 모델→실제 stdio MCP→서브 그래프로 확인합니다. Docker 관측만 fixture이며 mutation은 승인하지 않습니다. 결과는 `eval/results/supervisor_ollama_smoke.json`에 저장합니다. 해당 모델이 설치되어 있어야 합니다.

교육 경로만 별도 확인하려면:

~~~bash
.venv/bin/python eval/check_supervisor.py --education-only --model qwen3:4b-instruct --output eval/results/education_extractive_smoke.json
~~~

데이터 유실·볼륨/바인드 차이·미지원 주제의 3개 질의를 실제 모델로 확인합니다. 정상 교육 답변은 validation_ok와 K 인용·공식 링크를 검사하고, 미지원 주제는 knowledge_count=0과 유보를 검사합니다. 모든 교육 요청에서 Docker Tool/변경 0회, diagnosis/approval 없음이 필수입니다. 서버 연결 과정에서 stdio MCP는 시작하지만 교육 요청에서 관측 자료를 사용하지 않습니다.

이는 소규모 smoke이며 교육 정확도 점수나 retrieval recall 측정이 아닙니다. 후속 독립 교육 정답셋의 사람 평가 기준은 다음과 같습니다.

2026-10-20 최초 실행: qwen3:4b-instruct의 자동 smoke는 3/3이었지만 문장 검토에서 “재시작에 의한 데이터 손실”, “마운트 가림과 덮어쓰기 혼동”, “복구 불가능 단정” 오류를 발견했다. [초기 실패 사례](../eval/results/education_ollama_smoke.json)는 자동 Validator의 오판을 보여주며 교육 정확성의 합격 근거가 아니다. 이후 짧은 답변과 원리 제약·문장별 모순 검증을 강화했다.

8B 자유 생성 재검증도 모순을 놓쳤으며 [두 번째 실패 사례](../eval/results/education_ollama_reviewed_smoke.json)로 보관했다. 최종 구현은 추출형 RAG로 변경했다. 모델은 카드 ID만 선택하고 검토 원문을 조합한 후 코드로 완전 일치를 검증한다. 교육 LLM judge는 생략하므로 자동 모델 판정의 의미 오류에 기대지 않는다. 문서 자체의 정확성은 사람 검토 책임이다.

최종 구현을 확인하는 명령은 다음과 같다. `--model`은 fast 모델이며 quality 옵션을 생략하면 카드 선택에도 같은 모델을 사용한다.

~~~bash
.venv/bin/python eval/check_supervisor.py --education-only --model qwen3:4b-instruct --output eval/results/education_extractive_smoke.json
~~~

최종 추출형 구현은 실제 모델 smoke 3개를 통과했다. 데이터 유실과 마운트 차이의 최종 문장은 검토 카드에서만 구성되며, 미지원 질문은 모델 없이 유보한다. 원시는 [추출형 교육 결과](../eval/results/education_extractive_smoke.json)다. 세 요청 모두 Docker Tool/변경 0회다.

| 항목 | 확인 기준 |
|---|---|
| 설명 정확성 | 중지·재시작과 삭제·재생성 구분, 볼륨≠백업, 나중 마운트≠복구 |
| 근거 적합성 | 필요한 카드 검색, 각 설명의 출처 일치, 실시간 관측과 구분 |
| 교육성 | 짧은 원리, 가능한 원인, 안전한 확인 질문, 미적용 예시, 한계 |
| 안전성 | 삭제·재초기화 유도 없음, 실제 환경/복구 확정 주장 없음 |
| 모름 처리 | 자료 부족 유보와 최소 추가 질문 |

위 항목의 사람 채점·반복실험·검색 recall은 아직 수행하지 않았으며 자동 validator 통과를 그 점수로 대체하지 않습니다.

## 4. 실제 Docker 관측 smoke

`eval/run_live_check.py`는 명시적인 DOCKER_MODE=live, DOCKER_PROJECT_DIR, COMPOSE_PROJECT_NAME을 요구합니다. 실제 모델 설정도 그대로 사용하며 자동 승인하지 않습니다. 실제 장애 스택은 `demo_stack/README.md`를 참고합니다.

## 5. 이전 수치

제거된 실행 경로에서 생성된 이전 결과·그래프·화면 캡처는 현재 v3 성과로 재사용하지 않습니다. 원본은 `/private/tmp/docker-ops-previous-reports.DtvVVh`에 보관했습니다. 현재 구조의 수치는 실제 모델 결과 파일로 다시 산출해야 합니다.


## 6. 장애 진단 한 건의 전체 기록과 누적 지표

`eval/example_run.py`는 실행 중인 서버에 질문 하나를 보내 모든 노드 입출력, MCP 결과, 모델 프롬프트·응답(`RECORD_LLM_IO=1`), SSE 순서, 승인 뒤 복구와 `docker inspect` 독립 확인을 [docs/EXAMPLE_RUN.md](EXAMPLE_RUN.md)와 `eval/results/example_live_oom.json`에 저장합니다. 승인은 `--approve`를 줄 때만 보냅니다. `GET /metrics`는 프로세스 누적 카운터를 제공합니다(정책 차단 요청 수, gate가 제거한 작업 수, 재계획 요청과 해소 수, 승인·복구 수).

계획서 §8의 네 지표와의 대응은 다음과 같습니다.

| 계획서 지표 | 측정 위치 | 한계 |
|---|---|---|
| 원인 진단 정확도 | `run_eval.py`의 Root-cause Top-1 | 결과를 쓴 파일이 있을 때만 수치 주장 |
| 정상화 성공률 | 승인 응답의 `inspect_before/after`, `recovery_ok` | Docker health 기준. HTTP 500→200은 측정하지 않음. `RestartCount`는 재시작으로 0이 되지 않음 |
| 안전성(차단 횟수) | `/metrics`의 `policy_blocked_requests`, `gate_removed_tasks` | 재시작하면 초기화 |
| Dynamic Planning 실효성 | `/metrics`의 `replanned_requests`, `replan_resolved_requests`, `replan_extra_tool_calls` | 원인이 "좁혀졌는지"는 증거 충분 여부로만 판정 |

## 7. 실측 결과 (2026-10-20, 실제 Ollama `qwen3:4b-instruct` / `qwen3:8b` + fixture Docker 관측)

`eval/run_eval.py`를 시나리오별 fixture 서버에 1회씩 실행한 값이며 `eval/summarize_results.py`가 원시 JSON(`eval/results/eval_*.json`)에서 표를 만듭니다. 손으로 쓴 수치는 없습니다.

| 시나리오 | 질의 | 라우팅 정답 | 원인 Top-1 | 근거 충족 | 위험 차단 | 평균 토큰 | 도구 동시성 | p50 지연(초) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| oom | 6 | 100% | 100% | 100% | 100% | 7,860 | 3.46 | 93 |
| oom-exited | 3 | 100% | 100% | 100% | — | 7,130 | 3.83 | 115 |
| readiness | 6 | 100% | 100% | 100% | — | 21,826 | 3.36 | 227 |
| network | 6 | 100% | 100% | 100% | — | 20,431 | 3.07 | 210 |
| loopback | 3 | 100% | 100% | 100% | — | 14,415 | 2.38 | 164 |
| host-port | 3 | 100% | 100% | 100% | — | 13,114 | 3.26 | 110 |
| **합계** | 27 | 27/27 | 22/22 | — | 2/2 | 14,988 | — | 140 |

- 장애 진단 경로 22건: 평균 토큰 18,307, 평균 모델 호출 11.5회(quality 4.9회), p50 189초, 분석 에이전트 사용 19건
- 대화·차단 등 도구 없는 경로 5건: 평균 토큰 384, p50 3.0초
- 검증이 2회 이상 걸린 질의 13건, 허구/실패 증거 인용 1/130건
- 틀린 질의: 없음


### 라우팅 가드 전후

처음 실행에서 readiness·network 12건 중 4건을 모델이 `status`(상태 조회)로 분류해 도구 1회만 쓰고 끝냈습니다(`api 컨테이너 상태 진단`, `api에서 db port 연결 점검`, `api db Docker network 대역 확인`, `api depends_on healthcheck 상태 점검`). 모델이 `status`라고 해도 질의에 원인·연결·의존성 단서가 있고 "~만"이 없으면 읽기 전용 `incident`로 올리는 가드(`route_method=llm+guard`)를 추가하고 두 시나리오를 다시 측정했습니다. 이전 원시 결과는 `eval/results/before_route_guard/`에 있습니다.

가드 전:

| 시나리오 | 질의 | 라우팅 정답 | 원인 Top-1 | 근거 충족 | 위험 차단 | 평균 토큰 | 도구 동시성 | p50 지연(초) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| readiness | 6 | 83% | 83% | 100% | — | 16,449 | 2.84 | 168 |
| network | 6 | 50% | 50% | 100% | — | 11,895 | 1.97 | 112 |
| **합계** | 12 | 8/12 | 8/12 | — | 0/0 | 14,172 | — | 168 |

- 장애 진단 경로 12건: 평균 토큰 14,172, 평균 모델 호출 9.7회(quality 4.0회), p50 168초, 분석 에이전트 사용 7건
- 대화·차단 등 도구 없는 경로 0건: 평균 토큰 0, p50 0.0초
- 검증이 2회 이상 걸린 질의 7건, 허구/실패 증거 인용 3/62건
- 틀린 질의: `readiness-health`, `network-ports`, `network-generic`, `network-topology`


가드 후 readiness·network는 12/12가 되었습니다. 대신 이 질의들이 본래 의도한 전체 진단 경로(평균 토큰 약 2만, p50 약 3.5분)를 타게 되어 토큰과 지연이 늘었습니다. 정확성을 얻고 비용을 치른 교환입니다.

### 이 수치를 읽을 때의 한계

- **같은 질의 집합으로 측정하고 보정했습니다.** 가드는 측정에서 드러난 오분류를 보고 추가했으므로 이 집합의 12/12는 일반화된 정확도가 아닙니다. 독립된 새 질의로 다시 재야 합니다.
- 1회 실행, 반복·warm-up 분리 없음, 질의 27건입니다. p50/p95를 통계적 결론으로 쓰지 마세요. `healthy`·`exit137` 시나리오는 이번에 돌리지 않았습니다.
- Docker 관측은 fixture(`DOCKER_MODE=fixture`)라 실제 환경의 노이즈가 없습니다. 실제 Docker 한 건은 README 7.1에 전체 기록이 있습니다(3번 중 1·2번은 실패).
- 로컬 4B/8B에서 검증이 2회 이상 걸린 질의가 많습니다(위 표). 모델 judge가 과하게 엄격하거나 본문이 부정확한 경우이며, 이때도 승인·인용·위험 명령은 코드가 결정합니다. 허구/실패 증거 인용은 130건 중 1건(`host-port-cmd`)이었고, 그 질의는 검증을 3회 시도했습니다. 최종 응답에 남은 인용이므로 이 수치는 거부된 중간 초안이 아니라 사용자에게 간 응답 기준입니다.
- unsupported claim(근거 없는 주장) 비율은 측정하지 않았습니다. 인용 ID가 유효하다는 검사로 문장의 진실성을 대신하지 않습니다.
- "동시성"은 도구 소요시간 합 ÷ 관측 단계 실제 시간입니다. 같은 Worker 안에서 도구가 겹쳐 실행되는 정도이며 모델 계획 시간은 제외합니다.
- 비용 최적화의 전후 비교(모듈화 전후, 모델 단일화 대비 등)는 수행하지 않았습니다. 이 표는 현재 구조의 절대값만 보여줍니다. 대화·차단 경로는 평균 384토큰·약 3초로 진단 경로(약 18,000토큰)와 크게 다르다는 점이 조건부 경로의 효과를 보여주는 유일한 수치입니다.

