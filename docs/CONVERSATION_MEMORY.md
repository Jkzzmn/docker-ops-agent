# 대화 기억과 SQLite 체크포인터

## 확인한 원인과 변경

기존에는 SessionStore가 SQLite에 메시지만 저장했습니다. `graph.compile()`에 checkpointer가 없었고 요청 config에도 checkpoint thread_id가 없었습니다. 라우터는 처음에 이전 대화를 보지 않고, 모델이 `needs_context=true`를 선택했을 때만 사용자 발언 3개를 다시 읽었습니다. 따라서 저장은 돼도 맥락이 전달되지 않는 경우가 있었습니다.

현재 서버는 공식 `langgraph-checkpoint-sqlite`의 `AsyncSqliteSaver`를 생성해 `build_graph(..., checkpointer=saver)`로 연결합니다. 연결은 서버/대상 bundle 생명주기에 맞춰 열고 닫습니다. 테스트용 순수 `build_graph(deps)`는 선택적으로 checkpointer 없이도 실행할 수 있지만 실제 서버는 SQLite saver를 사용합니다.

## 저장 위치와 대화 키

- 메시지·승인: 기본 `data/sessions.db`.
- 그래프 checkpoint: 기본 `data/sessions.checkpoints.sqlite`.
- `SESSION_DB=/path/name.db`이면 checkpoint 파일은 `/path/name.checkpoints.sqlite`입니다.
- 실제 checkpoint 키: `history_scope_hash:browser_thread_id`. 브라우저 thread_id는 기존 localStorage의 `docker-ops-thread`입니다.
- Docker engine/context/project/path와 Compose 구성으로 scope를 나눕니다. 다른 프로젝트로 전환하면 이전 프로젝트의 맥락·체크포인트를 공유하지 않습니다. 같은 대상·thread_id·DB 파일이면 프로세스를 재시작해도 이어집니다.

서비스 집합은 정렬해 저장 키를 만듭니다. Docker가 서비스 순서를 바꿔 반환해도 같은 키입니다. 기존 순서 의존 메시지는 서비스 6개 이하인 프로젝트에서 동일 대상의 이전 순열 키를 읽는 방식으로 호환하며 원본을 이동·삭제하지 않습니다. 그보다 큰 프로젝트는 현재 이전 순서 키만 호환합니다. 과거 체크포인트 namespace는 수정하지 않고 새 정규화 namespace부터 영속 저장합니다.

## 기억을 모델에 전달하는 방식

최근 메시지 12개를 SessionStore에서 읽고, 라우터에는 최근 사용자 발언 3개(각 500자)와 직전 답변 800자까지 처음부터 제공합니다. 선택한 서비스명 출처는 읽어온 실제 사용자 발언에서 검사하며 모델의 needs_context 표시가 잘못 false여도 맥락을 무시하지 않습니다. 현재 질문이 최우선이며, 과거 답변은 참고 문맥일 뿐 실제 관측이나 승인으로 취급하지 않습니다. 추가적인 “맥락 가져오기” LLM 라운드는 없습니다. 일반 답변 모델은 최근 8개 메시지를 각각 1500자로 제한해 전달합니다. 데이터베이스에는 과거 메시지가 남지만 모델에 무제한 과거 전체를 넣는 장기 기억/요약 기능은 아닙니다.

확인 위치: `/health`의 `checkpoint_enabled`, `checkpoint_backend`, 답변 metadata의 `conversation_memory`와 `route_context_used`. 후자는 모델이 맥락 의존 질문이라고 판단했을 때 true이며, 과거 메시지 저장 여부 자체와는 다릅니다.

## 새 질문의 안전 경계

체크포인트가 있어도 새 사용자 질문은 반드시 `fresh_state()`로 명령·관측·노드 이벤트·진단·승인·재시도 횟수를 초기화합니다. 과거 관측을 현재 관측처럼 재사용하거나 이전 승인/명령을 재실행하지 않습니다. 이전 결과에 대한 설명은 대화 이력으로 하고, “현재 상태”를 요청하면 새로 조회합니다. 같은 대화에서 응답이 진행 중이면 두 번째 실행은 409로 거절해 checkpoint 충돌을 막습니다.

체크포인터를 연결한 것과 운영 중단 작업을 자동 재개하는 것은 다릅니다. 이 서비스는 `ainvoke(None)`/`Command(resume=...)` API를 노출하지 않으며, 재시작으로 미완료 변경 작업을 자동 재실행하지 않습니다. HITL은 기존 질문/승인 API와 SQLite 레코드를 계속 사용합니다.

## 한계

이 기능은 같은 대화 식별자의 백엔드 기억입니다. 브라우저 새로고침 때 과거 메시지를 화면에 다시 그리는 대화 목록 UI를 새로 만든 것은 아닙니다. DB 삭제, localStorage 삭제/다른 브라우저, 다른 thread_id, 다른 Docker 대상에서는 같은 대화가 아닙니다. SQLite는 과제/로컬 단일 서버 용도이며 DB 암호화·보존 기간·자동 정리는 추가하지 않았습니다. 로컬 DB에는 질의·답변·실행 상태가 남으므로 제출물에 data 폴더를 포함하지 마세요.

## 실제 확인 결과

최종 전체 자동 테스트 **322개 통과(55.92초)**. 실제 SQLite saver의 체크포인트 복원, 프로세스 생명주기 재생성, 서비스 순서 변경, 다른 대화·프로젝트 격리, SSE→JSON 후속 실행, 과거 명령/승인/관측 미누적, 동일 대화 동시 실행 거절을 포함합니다.

실제 Ollama + live Docker에서는 다음을 실행했습니다. 원본은 `eval/results/memory_sqlite_before_20261021_stable01.json`, `memory_sqlite_after_20261021_stable01.json`입니다.

1. `현재 프로젝트의 ai 서비스 상태만 확인해주세요.` → 실제 ai 컨테이너 부재 관측, SQLite checkpoint 생성.
2. 서버 프로세스를 실제 종료·재시작. instance_id가 바뀐 것을 확인.
3. `아까 제가 점검해달라고 한 서비스 이름이 무엇이었나요? Docker를 새로 조회하지 말고 이름만 답해주세요.` → **“아까 점검해달라고 한 서비스 이름은 'ai'입니다.”**, 도구 호출 0회.
4. `그 서비스의 현재 상태만 다시 확인해주세요.` → **ai** 대상 상태를 새로 1회 조회. 이전 근거는 재사용하지 않고 근거 1개·노드 2개로 종료.

동일 checkpoint thread 키, 새 checkpoint 증가, 도구 없는 회상, 올바른 서비스 선택, 근거 미누적의 5개 실기 검사가 모두 true입니다. 중간 실패 기록도 보존했습니다. 첫 실기에서는 모델의 잘못된 needs_context=false로 후속 서비스 확인이 막혔고, 다음 재시작에서는 서비스 목록 순서가 대화 키를 바꾸는 문제가 드러났습니다. 최종 버전은 두 원인을 보완했으며 중간 실패를 성공으로 다시 표기하지 않았습니다. 프론트의 기존 localStorage thread_id·GET SSE 전달 코드는 확인했지만 이번 검증에서 브라우저 화면을 자동 조작하지는 않았습니다.
