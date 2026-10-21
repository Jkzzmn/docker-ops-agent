import test from 'node:test'
import assert from 'node:assert/strict'
import { responseStatus } from '../src/responseStatus.js'

test('execution completion is not answer validation and absence is not a failure', () => {
  const turn = { nodes: [], metadata: { execution_status: 'completed', answer_status: 'validated', subject_status: 'absent' } }
  assert.equal(responseStatus(turn), '실행 종료 · 대상 없음 · 근거 확인됨')
  turn.metadata.answer_status = 'partial'
  assert.match(responseStatus(turn), /확인된 부분만/)
  turn.metadata.answer_status = 'unverified'
  assert.match(responseStatus(turn), /검증 미수행/)
  turn.metadata.answer_status = 'unavailable'
  assert.match(responseStatus(turn), /답변 불가/)
})

test('client disconnect never asserts server cancellation', () => {
  assert.match(responseStatus({ nodes: [] }), /화면 수신 중단 · 서버 종료 미확인/)
  assert.equal(responseStatus({ nodes: [], metadata: { execution_status: 'waiting_for_user' } }), '사용자 선택 대기')
  assert.equal(responseStatus({ nodes: [{ status: 'running' }] }), '실행 중')
})

test('answered/cancelled question and reported server error do not remain waiting', () => {
  assert.match(responseStatus({ nodes: [], clarification: { status: 'answered' }, metadata: { execution_status: 'waiting_for_user' } }), /접수됨/)
  assert.match(responseStatus({ nodes: [], clarification: { status: 'cancelled' }, metadata: { execution_status: 'waiting_for_user' } }), /대기 종료/)
  assert.match(responseStatus({ nodes: [], serverFailed: true }), /서버 실행 실패/)
})
