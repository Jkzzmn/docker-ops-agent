// POST replies stream the same real node lifecycle as normal chat (EventSource only supports GET).
export async function consumeSse(response, onEvent) {
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = '', finalized = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop()
      for (const frame of frames) {
        for (const line of frame.split(/\r?\n/)) {
          if (!line.startsWith('data: ')) continue
          const data = line.slice(6)
          if (data === '[DONE]') {
            if (!finalized) throw new Error('완료 응답을 받지 못했습니다. 요청 상태를 확인해주세요.')
            return
          }
          const event = JSON.parse(data)
          if (event.type === 'FinalAnswer') finalized = true
          onEvent(event)
        }
      }
      if (done) break
    }
    if (!finalized) throw new Error('응답 연결이 완료 전에 종료됐습니다.')
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}
