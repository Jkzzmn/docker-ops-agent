<script setup>
import { nodeTitle } from '../presentation.js'
// Stations appear only when the backend reports a node start; nothing is pre-drawn.
defineProps({ nodes: { type: Array, default: () => [] }, target: { type: Object, default: null } })

const ms = value => value >= 1000 ? `${(value / 1000).toFixed(1)}초` : `${Math.round(value)}ms`
const statusLabel = status => ({ running: '실행 중', completed: '완료', failed: '실패', interrupted: '연결 종료 · 완료 미확인' }[status] || status)
</script>

<template>
  <aside class="rail" aria-label="실행 경로">
    <div class="rail-heading"><p class="eyebrow">LIVE EXECUTION</p><h2>실행 흐름 <span v-if="nodes.length" class="rail-count">{{ nodes.length }}</span></h2></div>
    <div v-if="!nodes.length" class="rail-empty"><span aria-hidden="true">↳</span><b>아직 실행된 단계가 없어요</b><p>질문하면 사용된 노드만<br>실시간으로 나타납니다.</p></div>
    <ol v-else class="stations">
      <li v-for="node in nodes" :key="node.run_id" class="station" :class="[node.status, { child: node.parent_run_id }]">
        <span class="marker" aria-hidden="true"></span>
        <div>
          <b>{{ nodeTitle(node) }}</b>
          <span class="node-id mono">{{ node.scope }}.{{ node.node }}</span>
          <small class="num">{{ statusLabel(node.status) }}<template v-if="node.elapsed_ms != null"> · {{ ms(node.elapsed_ms) }}</template></small>
        </div>
      </li>
    </ol>

    <template v-if="target">
      <div class="divider"></div>
      <p class="rail-section-label">연결된 작업 환경</p>
      <dl class="facts">
        <dt>프로젝트</dt><dd>{{ target.project }}</dd>
        <dt>컨텍스트</dt><dd>{{ target.context }}</dd>
        <dt>엔진</dt><dd>{{ target.engine_name || 'fixture' }}</dd>
        <dt>서비스</dt><dd class="service-tags"><span v-for="service in target.services" :key="service">{{ service }}</span></dd>
      </dl>
    </template>
    <p class="rule-note"><b>안전하게, 읽기부터</b><br>파일 변경과 빌드·재생성은 각각 승인한 범위에서만 실행합니다.</p>
  </aside>
</template>
