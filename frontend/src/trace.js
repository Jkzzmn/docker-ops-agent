// Only real lifecycle events create graph nodes. No prepopulated topology or timers.
export function applyTraceEvent(nodes, event) {
  if (!['NodeStarted', 'NodeCompleted', 'NodeFailed'].includes(event.type)) return
  let node = nodes.find(item => item.run_id === event.run_id)
  if (!node) {
    node = { run_id: event.run_id, parent_run_id: event.parent_run_id, scope: event.scope, node: event.node, agent: event.agent }
    nodes.push(node)
  }
  if (event.type === 'NodeStarted') Object.assign(node, { status: 'running', input: event.input, started_at: event.timestamp })
  else Object.assign(node, { status: event.type === 'NodeCompleted' ? 'completed' : 'failed', output: event.output, error: event.error, elapsed_ms: event.elapsed_ms, finished_at: event.timestamp })
}
