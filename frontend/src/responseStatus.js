export function responseStatus(turn) {
  if (turn.serverFailed) return '서버 실행 실패 · 최종 답변 없음'
  if (turn.clarification?.status === 'answered') return '확인 답변 접수됨 · 후속 실행 별도'
  if (['cancelled', 'expired', 'invalidated'].includes(turn.clarification?.status)) return '질문 대기 종료 · 취소 또는 만료'
  if (turn.clarification?.status === 'pending' || turn.metadata?.execution_status === 'waiting_for_user') return '사용자 선택 대기'
  if (turn.nodes.some(n => n.status === 'running')) return '실행 중'
  if (!turn.metadata) return '화면 수신 중단 · 서버 종료 미확인'
  if (turn.metadata.execution_status === 'failed') return '실행 실패 · 상태 확인 불가'
  if (turn.metadata.execution_status === 'cancelled') return '서버 실행 취소 확인됨'
  const quality = { validated: '근거 확인됨', partial: '확인된 부분만 표시', unverified: '검증 미수행', unavailable: '답변 불가' }[turn.metadata.answer_status] || '검증 상태 미제공'
  const subject = { absent: ' · 대상 없음', ambiguous: ' · 대상 여러 개' }[turn.metadata.subject_status] || ''
  return `실행 종료${subject} · ${quality}`
}
