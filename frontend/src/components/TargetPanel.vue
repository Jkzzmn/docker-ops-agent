<script setup>
import { onMounted, ref, watch } from 'vue'

const props = defineProps({ api: String, identity: String, disabled: Boolean, selected: Boolean })
const emit = defineEmits(['selected', 'switching'])
const catalogue = ref(null), error = ref(''), busy = ref(false), project = ref(''), path = ref(''), preview = ref(null), choice = ref(null)

async function json(url, options) {
  const response = await fetch(url, options), body = await response.json()
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '대상 요청을 처리하지 못했습니다.')
  return body
}
async function refresh() {
  busy.value = true; error.value = ''; preview.value = null
  try {
    catalogue.value = await json(`${props.api}/targets`)
    project.value = catalogue.value.projects.find(p => p.name === catalogue.value.current.project)?.name || catalogue.value.projects[0]?.name || ''
  }
  catch (e) { error.value = e.message }
  finally { busy.value = false }
}
async function inspect(value) {
  busy.value = true; error.value = ''; preview.value = null
  try {
    preview.value = await json(`${props.api}/targets/preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
    choice.value = value
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
async function select() {
  if (busy.value || props.disabled || !preview.value) return
  busy.value = true; error.value = ''
  emit('switching', true)
  try {
    const result = await json(`${props.api}/targets/select`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...choice.value, expected_instance_id: preview.value.instance_id }) })
    preview.value = null; emit('selected', result)
    catalogue.value = await json(`${props.api}/targets`)
    project.value = catalogue.value.current.project || catalogue.value.projects[0]?.name || ''
  } catch (e) { error.value = e.message }
  finally { busy.value = false; emit('switching', false) }
}
onMounted(refresh)
watch(() => props.identity, () => { if (!busy.value) refresh() })
</script>

<template>
  <details class="target-picker" :open="!selected">
    <summary><span>프로젝트·컨테이너 관리</span><span class="picker-summary">{{ selected ? '대상 확인 또는 변경' : '진단할 프로젝트를 선택해주세요' }}</span></summary>
    <div class="body">
      <p>백엔드 서버의 Docker 엔진을 조회합니다. 실행·중지 컨테이너를 모두 표시하며, 선택만으로 시작하거나 파일을 수정하지 않습니다.</p>
      <button type="button" class="btn" :disabled="busy || disabled" @click="refresh">Docker 목록 새로고침</button>
      <template v-if="catalogue">
        <p class="current">현재 대상: <b>{{ catalogue.current.project || '선택 필요' }}</b> · {{ catalogue.current.context }}<br>{{ catalogue.current.project_dir }}</p>
        <form @submit.prevent="inspect({ project_name: project })">
          <label>발견된 Compose 프로젝트
            <select v-model="project" aria-label="Compose 프로젝트 선택" :disabled="busy || disabled">
              <option v-for="item in catalogue.projects" :key="item.name" :value="item.name">{{ item.name }} · {{ item.status }}</option>
            </select>
          </label>
          <button class="btn" :disabled="busy || disabled || !project">프로젝트 미리보기</button>
        </form>
        <p v-if="!catalogue.projects.length">발견된 Compose 프로젝트가 없습니다. 아래에서 서버의 프로젝트 폴더를 지정하세요.</p>
        <form @submit.prevent="inspect({ project_dir: path.trim() })">
          <label>서버의 Compose 프로젝트 절대 경로
            <input v-model="path" aria-label="프로젝트 절대 경로" placeholder="/absolute/path/to/project" :disabled="busy || disabled" maxlength="2000">
          </label>
          <button class="btn" :disabled="busy || disabled || !path.trim()">경로 미리보기</button>
        </form>
        <p class="note">브라우저 PC가 아닌 서버에 존재하는 경로입니다. Compose 프로젝트만 지원하며 Dockerfile 하나만 있는 폴더는 선택할 수 없습니다.</p>
        <section v-if="preview" class="preview" aria-label="선택 대상 미리보기">
          <h3>{{ preview.target.project }}</h3>
          <p>{{ preview.target.project_dir }}</p>
          <p>서비스: {{ preview.target.services.join(', ') }}</p>
          <ul><li v-for="file in preview.target.compose_files" :key="file">Compose: {{ file }}</li></ul>
          <ul><li v-for="item in preview.target.build_sources" :key="item.service">{{ item.service }}: {{ item.dockerfile }}{{ item.readable ? '' : ' (읽기 범위 밖 또는 파일 없음)' }}</li></ul>
          <p>서버 전체의 진단 대상이 바뀝니다. 기존 대기 질문·승인은 무효화되며 화면의 이전 실행 기록은 초기화됩니다.</p>
          <button type="button" class="btn approve" :disabled="busy || disabled || preview.instance_id !== identity" @click="select">이 프로젝트를 진단 대상으로 선택</button>
          <p v-if="preview.instance_id !== identity">대상이 변경됐습니다. 미리보기를 다시 실행해주세요.</p>
        </section>
        <details class="containers" open>
          <summary>Docker 컨테이너 {{ catalogue.containers.length }}개 · 실행/중지 포함</summary>
          <p v-if="!catalogue.containers.length">이 엔진에 컨테이너가 없습니다.</p>
          <p v-else-if="!catalogue.containers.some(c => c.state === 'running')">현재 실행 중인 컨테이너는 없습니다.</p>
          <div class="table-wrap"><table v-if="catalogue.containers.length">
            <thead><tr><th>컨테이너</th><th>프로젝트 / 서비스</th><th>상태</th></tr></thead>
            <tbody><tr v-for="item in catalogue.containers" :key="item.id"><td>{{ item.name }}</td><td>{{ item.project || 'Compose 미소속' }} / {{ item.service || '—' }}</td><td>{{ item.state }} · {{ item.status }}</td></tr></tbody>
          </table></div>
          <p v-if="catalogue.truncated">목록 표시 상한에 도달했습니다. 일부 항목은 생략됐습니다.</p>
          <p class="note">Compose 미소속 컨테이너도 목록에는 표시하지만, 진단 대상 전환은 Compose 프로젝트 단위입니다.</p>
        </details>
      </template>
      <p v-if="busy" role="status">Docker 대상을 확인하고 있습니다…</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </div>
  </details>
</template>

<style scoped>
.target-picker { margin-bottom: 24px; border: 1px solid var(--line); border-radius: 14px; font-size: 14px; background: var(--white); }
.target-picker > summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; list-style: none; padding: 16px 18px; }
.target-picker > summary::-webkit-details-marker { display: none; }
.target-picker > summary::after { content: '+'; color: var(--ink-3); font-size: 18px; }
.target-picker[open] > summary::after { content: '−'; }
summary { cursor: pointer; padding: 14px; font-weight: 650; }
.picker-summary { margin-left: auto; font-size: 12px; font-weight: 400; color: var(--ink-3); }
.body { padding: 0 16px 16px; display: grid; gap: 14px; line-height: 1.6; }
form { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; }
label { flex: 1; min-width: 180px; }
input, select { display: block; width: 100%; padding: 10px; border: 1px solid var(--line-strong); border-radius: 8px; font: inherit; color: inherit; background: white; }
.note { color: var(--ink-3); font-size: 13px; }
.preview { padding: 16px; background: var(--orange-50); border-radius: 10px; display: grid; gap: 10px; overflow-wrap: anywhere; }
.preview ul { margin: 0; padding-left: 20px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }
th, td { padding: 9px; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.error { color: var(--orange-700); }
.current { overflow-wrap: anywhere; }
@media (max-width: 480px) { .picker-summary { display: none; } label { min-width: 0; flex-basis: 100%; } .body { padding: 0 14px 14px; } }
</style>
