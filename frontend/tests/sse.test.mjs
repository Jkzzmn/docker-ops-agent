import test from 'node:test'
import assert from 'node:assert/strict'
import { consumeSse } from '../src/sse.js'

test('POST reply SSE preserves UTF-8 split bytes and actual node event order', async () => {
  const frames = [{ type: 'NodeStarted', node: 'route' }, { type: 'FinalAnswer', reply: '확인한 답변입니다' }]
  const bytes = new TextEncoder().encode(frames.map(x => `data: ${JSON.stringify(x)}\n\n`).join('') + 'data: [DONE]\n\n')
  const body = new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close() } })
  const actual = []
  await consumeSse(new Response(body), event => actual.push(event))
  assert.deepEqual(actual, frames)
})

test('incomplete or error-only SSE never reports a finished answer', async () => {
  await assert.rejects(() => consumeSse(new Response('data: {"type":"Error","message":"failure"}\n\ndata: [DONE]\n\n'), () => {}))
  await assert.rejects(() => consumeSse(new Response('data: {"type":"NodeStarted"}\n\n'), () => {}))
})
