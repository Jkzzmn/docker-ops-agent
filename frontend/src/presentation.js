const nodeNames = {
  route: '질문 이해·경로 선택', port_diagnose: '호스트 포트 점유 확인', inventory_observe: '실제 구성 조회', runtime_action: '컨테이너 실행 요청 확인', status_observe: '컨테이너 상태 확인', network_diagnose: '네트워크 연결 점검',
  target_selection: '프로젝트 선택 안내', human_question: '필요한 정보 확인', routing_unavailable: '분류 결과 확인',
  policy_block: '허용 범위 확인', chat_answer: '답변 작성', education_retrieve: '교육 자료 검색',
  education_unknown: '교육 자료 범위 확인', configuration: '설정 파일 검토', plan: '관측 계획', gate: '안전·중복 검사',
  research: '담당 에이전트 관측', replan_check: '수집 근거 검토', analysis: '원인 교차 분석',
  write: '답변 구성', validate: '근거·안전성 검증', finalize: '최종 답변 정리', facts_finish: '확인된 사실 정리',
  observe: '도구로 상태 조회', report: '관측 결과 보고', cross_check: '근거 교차 분석', retrieve: '검토 자료 검색',
  compose: '답변 구성', code_check: '코드 기반 검증', judge: '독립 검증',
  config_collect: '파일·컨테이너 수집', config_review: '설정 개선점 검토', config_validate: '주장·파일 대조', config_propose: '검토 결과·제안 정리'
}

export const nodeTitle = node => nodeNames[node.node] || node.node
export const causeTitle = cause => ({
  not_deployed: '대응 컨테이너 없음', not_running: '실행 중이 아님', healthy: '정상 상태 관측',
  unknown: '원인 미확정', memory: '메모리 관련', network: '네트워크 관련', readiness: '준비 상태 관련'
}[cause] || cause)

export function isSubmitKey(event) {
  return event.key === 'Enter' && !event.isComposing && event.keyCode !== 229 &&
    !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey
}

export const nearBottom = element => element.scrollHeight - element.scrollTop - element.clientHeight < 100
