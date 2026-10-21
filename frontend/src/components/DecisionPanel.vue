<script setup>
import { computed } from 'vue'

const props = defineProps({
  approval: { type: Object, default: null },
  result: { type: Object, default: null }
})
const emit = defineEmits(['decide'])

const rows = [
  ['상태', 'state'], ['health', 'health'], ['종료 코드', 'exit_code'], ['OOM 종료', 'oom_killed'], ['재시작 횟수', 'restarts']
]
const show = value => value === undefined || value === null || value === '' ? '—' : value === true ? '예' : value === false ? '아니오' : value
const executed = computed(() => props.result?.type === 'RecoveryVerified')
const rejected = computed(() => props.result?.type === 'ActionRejected')
const plan = computed(() => props.approval?.args?.plan)
const configResult = computed(() => props.result?.type === 'ConfigurationAction')
const approveLabel = computed(() => plan.value?.stage === 'files' ? '1차: 파일 변경 승인' : plan.value?.stage === 'runtime' ? '2차: 빌드·재생성 승인' : '승인하고 재시작')
</script>

<template>
  <section v-if="approval" class="decision" aria-label="사람 승인">
    <div class="decision-inner">
      <h3>{{ rejected ? '승인을 거절했습니다' : executed ? '승인한 조치를 실행했습니다' : '이 조치를 실행해도 될까요?' }}</h3>
      <div class="what"><span>{{ approval.tool }}</span><code>{{ plan ? plan.services.join(', ') : JSON.stringify(approval.args) }}</code></div>
      <p class="why">{{ approval.reason }}</p>
      <template v-if="plan">
        <p class="note">{{ plan.scope.context }} · {{ plan.scope.project }} · {{ plan.scope.project_dir }}</p>
        <pre class="data">{{ plan.diff }}</pre>
        <pre class="data">{{ (plan.commands || plan.validation_commands || []).map(cmd => cmd.join(' ')).join('\n') }}</pre>
        <p class="note">{{ plan.risk }}</p><p class="note">복원: {{ plan.rollback }}</p>
        <p class="note">승인 만료: {{ new Date(approval.expires_at * 1000).toLocaleString() }}</p>
      </template>
      <div v-if="!result" class="actions">
        <button class="btn" type="button" :disabled="approval.busy || approval.status !== 'pending'" @click="emit('decide', false)">거절</button>
        <button class="btn approve" type="button" :disabled="approval.busy || approval.status !== 'pending'" @click="emit('decide', true)">{{ approval.busy ? '실행하고 확인하는 중…' : approveLabel }}</button>
      </div>
      <p v-else-if="rejected" class="why">{{ plan?.stage === 'runtime' ? '배포하지 않았습니다. 1차 승인한 파일 변경은 유지됩니다.' : '아무것도 변경하지 않았습니다.' }}</p>
      <div v-if="configResult">
        <h4>{{ result.result.ok ? '승인한 단계 완료' : '승인한 단계 실패 또는 실행 전 중단' }}</h4>
        <p v-if="result.result.error" class="note">{{ result.result.error }}</p>
        <p v-if="result.result.runtime_blocked" class="note">{{ result.result.runtime_blocked }}</p>
        <p v-if="result.result.rollback" class="note">복원 {{ result.result.rollback.ok ? '검증 완료' : '미완료 — 직접 확인 필요' }}</p>
        <details><summary>실제 실행·검증 결과</summary><pre class="data">{{ JSON.stringify(result.result, null, 2) }}</pre></details>
      </div>
    </div>

    <div v-if="executed" class="recovery" :class="{ bad: !result.recovery_ok }">
      <h4>{{ result.recovery_ok ? 'Docker health가 정상으로 돌아왔습니다' : '명령은 실행됐지만 정상화를 확인하지 못했습니다' }}</h4>
      <table class="compare num">
        <thead><tr><th>항목</th><th>조치 전</th><th>조치 후</th></tr></thead>
        <tbody>
          <tr v-for="[label, key] in rows" :key="key"><td>{{ label }}</td><td>{{ show(result.inspect_before?.[key]) }}</td><td class="after">{{ show(result.inspect_after?.[key]) }}</td></tr>
        </tbody>
      </table>
      <p class="note num">health 확인에 {{ result.waited_seconds }}초 기다렸습니다. 재시작은 임시 복구입니다. 같은 원인이 남아 있으면 다시 종료될 수 있으니 메모리 사용 패턴과 누수 가능성을 따로 확인하세요.</p>
    </div>
  </section>
</template>
