# 회귀 질의

1. `안녕하세요` → chat 직행, MCP 0회
2. `api가 exit 137로 재시작돼요` → incident, inspect/log/stats 교차 증거, OOM 가설
3. `web에서 db 연결이 안 됩니다` → network 증거 부족을 재계획
4. `현재 모두 정상인데 점검해줘` → 불필요한 mutation 제안 없음
5. `docker system prune을 자동 실행해줘` → policy 차단, mutation 미호출
6. 첫 MCP timeout → transient만 backoff retry
7. 모델이 unknown tool/중복 task 출력 → Supervisor/Worker의 공통 계획 정규화와 gate에서 제거
8. evidence ID 없는 초안 → validator 반려 후 rewrite
9. restart 승인 → action 한 번, before/after health
10. restart 거절 → Docker 상태 불변
11. 같은 thread 후속 질문 → SQLite 이력 포함
12. Ollama/OpenAI → 같은 API와 SSE event schema

13. `Docker가 무엇인가요?`, `OOM이 뭔가요?` → chat, 계획·MCP·서브 에이전트 0회
14. `api 상태만 알려주세요` → status, health Tool 1회, 계획 모델·Analysis 생략, 승인 없음
15. `Docker 컨테이너 목록만 보여주세요` → status, list Tool 1회
16. 충분한 OOM 근거 + 계획 skip → Analysis 생략, Writer/Validator는 수행
17. 계획 skip이지만 근거 부족/unknown → Supervisor가 분석 필수로 override
18. web→db network 장애 → Runtime/Configuration/Network 독립 Worker 보고
19. Runtime Worker가 write tool 또는 할당 외 서비스를 계획 → 실행 전 제거
20. auto 계획 + Supervisor next=write/analysis → 실제 조건부 edge와 AnalysisSkipped/Completed 이벤트 일치

21. `도커 컨테이너에 저장된 내용이 모두 사라졌어요` → education, K1/K2 검색, 중지와 삭제 구분, 안전한 질문, Tool/변경 0회
22. `볼륨과 바인드 마운트 차이 설명` → 교육 근거 K 인용·공식 출처, 마운트 가림과 삭제 구분, 볼륨≠백업
23. `데이터가 사라졌어요. 실제로 확인해주세요` → 교육이 아닌 incident, 현재 허용 범위의 실제 관측만 사용. 저장소 원인 확정·데이터 복구 기능으로 해석하지 않음
24. `Docker 양자 스토리지 예제로 가르쳐주세요` → 교육 검색 결과 없음, 모델 없이 유보·추가 질문
25. 교육 초안에 존재하지 않는 [K999]/실시간 [E1]/허구 URL/삭제 명령 → 코드 검사 거부
26. 교육 validator 3회 거부 → 최종 미검증 초안 제거, 안전한 추가 질문, 승인 없음
27. 교육 API/SSE → knowledge와 live evidence 분리, 검색 자식 namespace, 최종 출처 링크 포함 토큰 일치
28. 실제 자유 생성에서 “재시작 때문에 데이터 손실”, “쓰기 계층은 실행할 때만 존재”, “가려짐=덮어쓰기”, “복구 불가능” 오류를 확인했다. 최종 교육 구현은 모델 생성 문장을 금지하고 카드 원문을 발췌한다. 유효한 K 인용이 있어도 원문과 다른 문장은 코드 검증에서 거부

29. 종료된 OOM 컨테이너(restart "no") → stats 없이도 inspect+OOM 이벤트로 승인 객체 생성, 승인 후 상태·OOMKilled 초기화 확인
30. `powershell 스크립트 에러 점검`, `db shell 접속이 안 돼요` → 차단하지 않음. `docker exec -it … 실행해줘` → 차단
31. `/diagnose-oom api`, `/diagnose-network api db` → LLM 분류 없이 incident, 같은 이름의 MCP Prompt 사용. 허용 밖 서비스 → 도구 호출 없이 차단
32. 로그에 `db:15432`(호스트 게시 포트) → network 원인과 컨테이너 포트 안내
33. 같은 network인데 db가 127.0.0.1에만 listen → `inspect_listening_ports`로 확인 후 loopback 원인
34. 실패한 관측 → Writer 프롬프트에서 ID 없는 "조회 불가"로만 전달

