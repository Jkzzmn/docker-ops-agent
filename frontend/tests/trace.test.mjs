import { test } from 'node:test'
import assert from 'node:assert/strict'
import { applyTraceEvent } from '../src/trace.js'

test('nodes appear on real start only; completion updates the same execution', () => {
  const nodes = []
  applyTraceEvent(nodes, {type:'RouteDecided', route:'education'})
  assert.equal(nodes.length, 0)
  applyTraceEvent(nodes, {type:'NodeStarted', run_id:'one', scope:'supervisor', node:'route', input:{query:'hello'}, timestamp:1})
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].status, 'running')
  assert.equal(nodes[0].output, undefined)
  applyTraceEvent(nodes, {type:'NodeCompleted', run_id:'one', scope:'supervisor', node:'route', output:{route:'chat'}, elapsed_ms:3})
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].status, 'completed')
  assert.deepEqual(nodes[0].output, {route:'chat'})
  applyTraceEvent(nodes, {type:'NodeStarted', run_id:'retry', scope:'supervisor', node:'route'})
  assert.equal(nodes.length, 2)
  assert(!nodes.some(n => n.node === 'analysis'))
})

test('different turns have no shared nodes and errors do not become completed', () => {
  const first = [], second = []
  applyTraceEvent(first, {type:'NodeStarted', run_id:'x', node:'education_retrieve'})
  applyTraceEvent(first, {type:'NodeFailed', run_id:'x', error:'timeout'})
  applyTraceEvent(second, {type:'NodeStarted', run_id:'y', node:'chat_answer'})
  assert.equal(first[0].status, 'failed')
  assert.deepEqual(second.map(n=>n.node), ['chat_answer'])
})
