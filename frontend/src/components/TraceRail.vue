<script setup>
// Stations appear only when the backend reports a node start; nothing is pre-drawn.
defineProps({ nodes: { type: Array, default: () => [] }, target: { type: Object, default: null } })

const ms = value => value >= 1000 ? `${(value / 1000).toFixed(1)}초` : `${Math.round(value)}ms`
const statusLabel = status => ({ running: '실행 중', completed: '완료', failed: '실패', interrupted: '연결 종료 · 완료 미확인' }[status] || status)
</script>

<template>
  <aside class="rail" aria-label="실행 경로">
    <h2>실행 경로</h2>
    <p v-if="!nodes.length" class="hint">질문하면 실제로 실행된 단계만 이 선 위에 나타납니다.</p>
    <ol v-else class="stations">
      <li v-for="node in nodes" :key="node.run_id" class="station" :class="[node.status, { child: node.parent_run_id }]">
        <span class="marker" aria-hidden="true"></span>
        <div>
          <b class="mono">{{ node.scope }}.{{ node.node }}</b>
          <small class="num">{{ statusLabel(node.status) }}<template v-if="node.elapsed_ms != null"> · {{ ms(node.elapsed_ms) }}</template></small>
        </div>
      </li>
    </ol>

    <template v-if="target">
      <div class="divider"></div>
      <dl class="facts">
        <dt>Context</dt><dd>{{ target.context }}</dd>
        <dt>Engine</dt><dd>{{ target.engine_name || 'fixture' }}</dd>
        <dt>Project</dt><dd>{{ target.project }}</dd>
        <dt>Services</dt><dd>{{ target.services?.join(', ') }}</dd>
      </dl>
    </template>
    <p class="rule-note">읽기 도구는 자동 실행합니다. 파일 변경과 빌드·재생성은 별도로 승인받으며, 승인한 범위만 한 번 실행합니다.</p>
  </aside>
</template>
