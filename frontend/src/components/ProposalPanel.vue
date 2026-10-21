<script setup>
import { ref } from 'vue'

defineProps({ proposal: { type: Object, default: null } })
const copied = ref(false)

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => { copied.value = false }, 1600)
  } catch { /* clipboard may be blocked; the text stays selectable */ }
}
</script>

<template>
  <section v-if="proposal" class="proposal" aria-label="설정 변경 제안">
    <div class="proposal-inner">
      <h3>
        {{ proposal.title }}
        <span class="chip">적용되지 않음 · 검토용</span>
      </h3>
      <p class="note">{{ proposal.note }}</p>
      <template v-if="proposal.diff">
        <p class="note">대상 {{ proposal.scope?.context }} / {{ proposal.scope?.project }} · 서비스 {{ proposal.services?.join(', ') }}</p>
        <pre class="data">{{ proposal.diff }}</pre>
        <p class="note num">diff SHA256: {{ proposal.diff_sha256 }}</p>
        <p class="note">{{ proposal.risk }}</p>
        <p class="note">복원: {{ proposal.rollback }}</p>
        <pre class="data">{{ [...(proposal.validation_commands || []), ...(proposal.runtime_commands || [])].map(cmd => cmd.join(' ')).join('\n') }}</pre>
      </template>
      <template v-if="proposal.yaml">
        <pre class="data">{{ proposal.yaml }}</pre>
        <div class="actions"><button class="btn quiet" type="button" @click="copy(proposal.yaml)">{{ copied ? '복사했습니다' : 'YAML 복사' }}</button></div>
      </template>
    </div>
  </section>
</template>
