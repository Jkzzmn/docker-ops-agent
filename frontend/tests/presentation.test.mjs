import { test } from 'node:test'
import assert from 'node:assert/strict'
import { nodeTitle, causeTitle, isSubmitKey, nearBottom } from '../src/presentation.js'

test('readable labels preserve unknown backend values and do not change their meaning', () => {
  assert.equal(nodeTitle({ node: 'status_observe' }), '컨테이너 상태 확인')
  assert.equal(nodeTitle({ node: 'future_node' }), 'future_node')
  assert.equal(causeTitle('not_deployed'), '대응 컨테이너 없음')
  assert.equal(causeTitle('unknown'), '원인 미확정')
  assert.equal(causeTitle('future_cause'), 'future_cause')
})

test('Enter submits, but Korean composition and modified Enter never submit', () => {
  assert(isSubmitKey({ key: 'Enter' }))
  for (const modifier of ['isComposing', 'shiftKey', 'ctrlKey', 'altKey', 'metaKey']) {
    assert(!isSubmitKey({ key: 'Enter', [modifier]: true }))
  }
  assert(!isSubmitKey({ key: 'Enter', keyCode: 229 }))
  assert(!isSubmitKey({ key: 'a' }))
})

test('reading older output pauses automatic following; bottom resumes it', () => {
  assert(nearBottom({ scrollHeight: 1000, scrollTop: 510, clientHeight: 400 }))
  assert(!nearBottom({ scrollHeight: 1000, scrollTop: 300, clientHeight: 400 }))
})
