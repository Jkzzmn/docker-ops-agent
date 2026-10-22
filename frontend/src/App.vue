<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { applyTraceEvent } from './trace.js'
import { consumeSse } from './sse.js'
import { responseStatus } from './responseStatus.js'
import { nodeTitle, causeTitle, isSubmitKey, nearBottom } from './presentation.js'
import TraceRail from './components/TraceRail.vue'
import DecisionPanel from './components/DecisionPanel.vue'
import ClarificationPanel from './components/ClarificationPanel.vue'
import TargetPanel from './components/TargetPanel.vue'
import ProposalPanel from './components/ProposalPanel.vue'
import AnswerText from './components/AnswerText.vue'
import AnswerHtml from './components/AnswerHtml.vue'

const API = import.meta.env.VITE_API_URL || '/api'
const message = ref('')
const turns = ref([])
const sending = ref(false)
const health = ref(null)
const actionBusy = ref(false), targetNotice = ref('')
const selectingTarget = ref(false)
const retryOriginal = ref('')
const logEl = ref(null)
const following = ref(true)
const threadId = localStorage.getItem('docker-ops-thread') || `docker-${crypto.randomUUID()}`
localStorage.setItem('docker-ops-thread', threadId)
let source = null
let replyController = null
let healthTimer = null
let checkingHealth = false
let restoredForIdentity = null
let targetRevision = 0
const targetHeaders = () => ({ 'X-Docker-Target': health.value?.instance_id || '' })

const labels = {
  RouteDecided: '질의 라우팅', PlanCreated: '진단 계획 생성', GateApplied: '안전 정책 게이트',
  TaskCompleted: 'MCP 증거 수집', ReplanCheck: '증거 충족도·재계획', AnalysisCompleted: '교차 분석',
  DraftWritten: '진단 보고서 작성', Validation: '근거·안전성 검증', ApprovalRequired: '사람 승인 대기',
  ActionNotRequired: '복구 불필요', Finalize: '그래프 완료', RecoveryVerified: '복구 후 재검증',
  TaskFailed: '일부 증거 조회 실패', PolicyBlocked: '허용 범위 밖 요청 차단',
  AgentsDispatched: 'Supervisor 작업 위임', AgentStarted: '서브 에이전트 시작',
  AgentPlanCreated: '서브 에이전트 관측 계획', AgentCompleted: '서브 에이전트 보고',
  SupervisorDecision: 'Supervisor 다음 단계 결정', AnalysisSkipped: '분석 단계 생략',
  KnowledgeRetrieved: '교육 자료 검색', KnowledgeUnavailable: '교육 근거 부족',
  ProposalCreated: '설정 변경 제안', HumanInputRequired: '필수 정보 확인 · 답변 대기', ModelError: '모델 호출 실패', RoutingUnavailable: 'LLM 분류 실패 · 대체 분류 없음', PromptUnavailable: 'MCP 표준 절차 조회 실패'
}

const activeNodes = computed(() => turns.value.at(-1)?.nodes || [])
const waitingTurn = computed(() => turns.value.findLast(turn => turn.clarification?.status === 'pending'))
const services = computed(() => health.value?.docker_target?.services || [])
const primaryService = computed(() => services.value[0])
const peerService = computed(() => services.value[1])
const backendLabel = computed(() => health.value ? `${health.value.fast_model} / ${health.value.quality_model}` : '백엔드 연결 확인 중')
const starters = computed(() => {
  const a = primaryService.value || 'api', b = peerService.value || 'db'
  return {
    live: [
      { title: '컨테이너 상태', icon: '01', cmd: `${a} 상태만 알려주세요`, why: `${a}의 현재 상태만 빠르게 확인` },
      { title: '설정 파일 검토', icon: '02', cmd: '현재 프로젝트의 Dockerfile과 도커 컴포즈 실제 파일을 읽고 설정 문제를 검토해주세요. 파일 변경은 하지 마세요.', why: 'Compose·Dockerfile의 오류와 개선점' },
      { title: '네트워크 점검', icon: '03', cmd: `/diagnose-network ${a} ${b}`, why: `${a} → ${b} 연결을 순서대로 확인` },
      { title: '메모리 문제 진단', icon: '04', cmd: `/diagnose-oom ${a}`, why: `${a}가 메모리 부족으로 종료됐는지 확인` }
    ],
    learn: [
      { title: '컨테이너 데이터는 어디로?', icon: '↳', cmd: '도커 컨테이너에 저장된 내용이 모두 사라졌어요', why: '데이터 수명과 보존 원리 배우기' },
      { title: '볼륨 vs 바인드 마운트', icon: '↳', cmd: '볼륨과 바인드 마운트의 차이를 설명해주세요', why: '검토된 자료로 차이 이해하기' }
    ]
  }
})
const stateLabel = turn => sending.value && turn === turns.value.at(-1) && !turn.nodes.length ? '연결 중 · 첫 실행 대기' : responseStatus(turn)
const stateClass = turn => turn.nodes.some(n => n.status === 'running') ? 'running' : (turn.metadata ? '' : 'stopped')

function scroll() { nextTick(() => { if (following.value) logEl.value?.scrollTo({ top: logEl.value.scrollHeight }) }) }
function followLatest() { following.value = true; scroll() }
function onComposerKeydown(event) {
  if (isSubmitKey(event)) { event.preventDefault(); submit() }
}
function stop() {
  source?.close(); source = null; sending.value = false
  replyController?.abort(); replyController = null
  for (const node of activeNodes.value) if (node.status === 'running') node.status = 'interrupted'
}
onBeforeUnmount(() => { stop(); clearInterval(healthTimer) })
async function checkHealth() {
  if (checkingHealth) return
  checkingHealth = true
  const revision = targetRevision
  try {
    const response = await fetch(`${API}/health`)
    if (response.ok) {
      const current = await response.json()
      if (revision !== targetRevision) return
      if (health.value && health.value.instance_id !== current.instance_id) {
        stop(); turns.value = []; message.value = ''
        targetNotice.value = 'Docker 대상 연결이 변경돼 이전 실행 화면을 초기화했습니다.'
      }
      health.value = current
      if (current.target_selected && !turns.value.length && !sending.value && restoredForIdentity !== current.instance_id) {
        const pending = await fetch(`${API}/clarifications/pending?thread_id=${encodeURIComponent(threadId)}`, { headers: targetHeaders() })
        if (pending.ok) {
          restoredForIdentity = current.instance_id
          const { clarification } = await pending.json()
          if (revision === targetRevision && clarification && !turns.value.length) {
            const turn = createTurn(clarification.original_query)
            Object.assign(turn, { clarification, answer: clarification.question, metadata: { route: 'clarify' }, restored: true })
          }
        }
      }
    }
  } catch { /* the backend may still be starting; the next poll retries */ }
  finally { checkingHealth = false }
}
onMounted(() => { checkHealth(); healthTimer = setInterval(checkHealth, 4000) })
async function targetSelected(result) {
  retryOriginal.value = turns.value.findLast(turn => turn.metadata?.target_selection_required)?.question || ''
  targetRevision += 1
  stop(); turns.value = []; message.value = ''; restoredForIdentity = null
  health.value = { ...health.value, instance_id: result.instance_id, docker_target: result.target, target_selected: true, target_switching: false }
  targetNotice.value = result.note; await checkHealth()
}
function prepareRetry() { message.value = retryOriginal.value; retryOriginal.value = '' }

function addEvent(turn, event) {
  if (['NodeStarted', 'NodeCompleted', 'NodeFailed'].includes(event.type)) { applyTraceEvent(turn.nodes, event); return }
  if (event.type === 'AnswerToken') {
    if (turn.attempt !== event.attempt) { turn.attempt = event.attempt; turn.answer = ''; if (event.attempt > 1) turn.rewritten = true }
    turn.answer += event.delta; return
  }
  if (event.type === 'FinalAnswer') {
    Object.assign(turn, { answer: event.reply, answerHtml: event.reply_html || '', prompt: event.prompt, clarification: event.clarification, approval: event.approval, proposal: event.proposal, metadata: event.metadata, evidence: event.evidence || [], knowledge: event.knowledge || [], diagnosis: event.diagnosis })
    return
  }
  if (event.type === 'Error') { turn.error = event.message; turn.serverFailed = true; return }
  turn.events.push(event)
}

function createTurn(text) {
  const turn = reactive({ id: crypto.randomUUID(), question: text, answer: '', events: [], nodes: [], attempt: 0, clarification: null, approval: null, proposal: null, actionResult: null, actionHistory: [], answerHtml: '', prompt: null, metadata: null, evidence: [], knowledge: [], diagnosis: null, error: null, rewritten: false })
  following.value = true
  turns.value.push(turn); scroll()
  return turn
}

function submit() {
  const text = message.value.trim()
  if (!text || sending.value || waitingTurn.value || selectingTarget.value || !health.value?.target_selected) return
  sending.value = true
  const turn = createTurn(text)
  message.value = ''
  source = new EventSource(`${API}/chat/stream?message=${encodeURIComponent(text)}&thread_id=${encodeURIComponent(threadId)}&target_id=${encodeURIComponent(health.value.instance_id)}`)
  source.onmessage = e => {
    if (e.data === '[DONE]') { stop(); scroll(); return }
    try { addEvent(turn, JSON.parse(e.data)); scroll() } catch { /* ignore a malformed frame */ }
  }
  source.onerror = () => { if (sending.value) turn.error = '스트림 연결이 끊겼습니다. 백엔드 상태를 확인하고 다시 시도하세요.'; stop() }
}

function useStarter(text) { message.value = text; submit() }

async function replyToQuestion(parent, response) {
  if (sending.value || parent.clarification.status !== 'pending') return
  const question = parent.clarification
  const text = response.choice ? `${response.choice}. ${question.options[response.choice - 1]}` : response.answer
  const turn = createTurn(`확인 답변: ${text}`)
  sending.value = true; question.busy = true; replyController = new AbortController()
  try {
    const result = await fetch(`${API}/clarifications/${question.id}/reply?stream=true`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...targetHeaders() }, body: JSON.stringify({ thread_id: threadId, ...response }), signal: replyController.signal
    })
    if (!result.ok) { const body = await result.json(); throw new Error(typeof body.detail === 'string' ? body.detail : '답변을 접수하지 못했습니다.') }
    question.status = 'answered'
    await consumeSse(result, event => { addEvent(turn, event); scroll() })
  } catch (error) {
    if (question.status === 'pending') question.status = 'unknown'
    if (error.name !== 'AbortError') turn.error ||= error.message
  } finally { question.busy = false; stop(); scroll() }
}

async function cancelQuestion(turn) {
  if (sending.value) return
  turn.clarification.busy = true
  try {
    const result = await fetch(`${API}/clarifications/${turn.clarification.id}/cancel`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...targetHeaders() }, body: JSON.stringify({ thread_id: threadId }) })
    turn.clarification.status = result.ok ? 'cancelled' : 'expired'
  } catch (error) { turn.error = error.message }
  finally { turn.clarification.busy = false }
}

async function decide(turn, approve) {
  const id = turn.approval.id
  turn.approval.busy = true; actionBusy.value = true
  try {
    const r = await fetch(`${API}/actions/${id}/${approve ? 'approve' : 'reject'}`, { method: 'POST', headers: targetHeaders() })
    const data = await r.json()
    if (!r.ok) throw new Error(typeof data.detail === 'object' ? data.detail.message : data.detail || 'request failed')
    turn.approval.status = data.status; turn.events.push({ ...data, node: 'verify' }); turn.actionResult = data
    if (data.type === 'RuntimeAction' && turn.metadata) turn.metadata.execution_status = data.status === 'executed' ? 'completed' : 'failed'
    if (data.next_approval) {
      turn.actionHistory.push(data)
      turn.approval = data.next_approval
      turn.actionResult = null
    }
  } catch (e) { turn.error = e.message; turn.approval.status = 'unknown'; if (turn.metadata) turn.metadata.execution_status = 'failed' } finally { turn.approval.busy = false; actionBusy.value = false; scroll() }
}

function details(e) {
  if (e.type === 'RouteDecided') return `경로 ${e.route} · ${e.method} · ${e.reason}`
  if (e.type === 'KnowledgeRetrieved') return `${e.count}개 교육 자료 · ${e.ids.join(', ')} · ${e.method}`
  if (e.type === 'AgentsDispatched') return e.agents.join(', ')
  if (e.type === 'AgentStarted' || e.type === 'AgentPlanCreated') return `${e.agent} · ${e.task_count ?? ''}`
  if (e.type === 'AgentCompleted') return `${e.agent} · 완료${e.failed ? ` · 실패 ${e.failed}건` : ''}`
  if (e.type === 'SupervisorDecision') return `${e.next} · ${e.reason}`
  if (e.type === 'AnalysisSkipped') return e.reason
  if (e.type === 'PlanCreated') return `${e.task_count}개 작업 · ${e.targets.join(', ')}`
  if (e.type === 'GateApplied') return `${e.removed_tasks}개 위험/중복 작업 제거 · 분석 정책 ${e.analysis_policy}`
  if (e.type === 'TaskCompleted' || e.type === 'TaskFailed') return `${e.id} · ${e.target} · 재시도 ${e.retries}`
  if (e.type === 'ReplanCheck') return e.is_sufficient ? `${e.round}회차 · 증거 충분` : `${e.round}회차 · ${e.exhausted ? '조회 한도 도달 · ' : ''}부족: ${e.missing.join(', ')}`
  if (e.type === 'Validation') return `${e.verdict} · 시도 ${e.attempt}`
  if (e.type === 'ApprovalRequired') return '변경 작업은 자동 실행하지 않음'
  if (e.type === 'ProposalCreated') return `${e.title} · 적용 안 함`
  if (e.type === 'RecoveryVerified') return `${e.status} · ${e.recovery_ok ? 'Docker health 정상 확인' : '정상화 미확인 · 추가 점검 필요'}`
  if (e.type === 'ModelError' || e.type === 'PromptUnavailable') return e.message
  return e.pipeline || (e.length ? `${e.length}자` : '')
}
const ms = value => value >= 1000 ? `${(value / 1000).toFixed(1)}초` : `${Math.round(value)}ms`
const nodeStatus = status => ({ running: '실행 중', completed: '완료', failed: '실패', interrupted: '완료 미확인' }[status] || status)
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="mark" aria-hidden="true"></span>
        <div><h1>Docker Ops <span>Agent</span></h1><p>관측한 사실로, 더 명확하게</p></div>
      </div>
      <div class="status" role="status">
        <span class="pulse" :class="{ on: health?.ok }" aria-hidden="true"></span>
        <details class="connection"><summary>{{ health?.ok ? '서버 연결됨' : '연결 확인 중' }}</summary><p>{{ backendLabel }}</p></details>
        <span v-if="health" class="badge" :class="{ sim: health.docker_mode !== 'live' }">{{ health.docker_mode === 'live' ? '실제 Docker' : '시뮬레이션' }}</span>
      </div>
    </header>

    <div class="layout">
      <TraceRail :nodes="activeNodes" :target="health?.docker_target" />

      <main class="stage">
        <p v-if="health?.docker_target" class="target-note" :class="{ warn: health.docker_target.warning }">
          <span class="target-caption">현재 진단 대상</span><b>{{ health.docker_target.project }}</b><span class="target-context">{{ health.docker_target.context }} · 서비스 {{ services.length }}개</span><span class="target-scope">백엔드 서버의 Docker 기준</span><template v-if="health.docker_target.warning"><span class="target-warning">{{ health.docker_target.warning }}</span></template>
        </p>

        <div class="feed" ref="logEl" @scroll="following = nearBottom($event.target)">
          <div id="target-selection"><TargetPanel v-if="health?.docker_mode === 'live'" :api="API" :identity="health.instance_id" :selected="health.target_selected" :disabled="sending || actionBusy || health.target_switching" @selected="targetSelected" @switching="value => selectingTarget = value" /></div>
          <p v-if="targetNotice" class="target-note" role="status">{{ targetNotice }}</p>
          <button v-if="retryOriginal" type="button" class="btn" @click="prepareRetry">원래 질문 불러오기 (전송 전 확인)</button>
          <section v-if="!turns.length" class="start">
            <p class="eyebrow">YOUR DOCKER WORKSPACE</p>
            <h2>무엇을 확인할까요?</h2>
            <p>컨테이너 상태부터 설정 파일까지.<br>필요한 것만 살펴보고, 확인한 근거와 함께 알려드려요.</p>
            <h3 class="starter-heading">실제 환경 점검 <span>읽기 전용으로 시작합니다</span></h3>
            <ul class="starters">
              <li v-for="item in starters.live" :key="item.cmd"><button type="button" :disabled="!health?.target_selected || selectingTarget" :title="item.cmd" @click="useStarter(item.cmd)"><span class="starter-icon" aria-hidden="true">{{ item.icon }}</span><span class="starter-copy"><span class="cmd">{{ item.title }}</span><span class="why">{{ item.why }}</span></span><span class="starter-arrow" aria-hidden="true">↗</span></button></li>
            </ul>
            <h3 class="starter-heading">개념부터 차근차근 <span>실제 환경을 조회하지 않아요</span></h3>
            <ul class="starters learn">
              <li v-for="item in starters.learn" :key="item.cmd"><button type="button" :disabled="!health?.target_selected || selectingTarget" :title="item.cmd" @click="useStarter(item.cmd)"><span class="starter-icon" aria-hidden="true">{{ item.icon }}</span><span class="starter-copy"><span class="cmd">{{ item.title }}</span><span class="why">{{ item.why }}</span></span><span class="starter-arrow" aria-hidden="true">↗</span></button></li>
            </ul>
            <p class="start-safety">파일 변경과 빌드·재생성은 내용을 보여드린 뒤 각각 승인받습니다.</p>
          </section>

          <article v-for="turn in turns" :key="turn.id" class="turn">
            <p class="ask">{{ turn.question }}</p>

            <div v-if="turn.answer" class="answer" :class="{ education: turn.metadata?.route === 'education' }">
              <div class="answer-head"><span>{{ turn.metadata?.route === 'runtime' ? (turn.approval ? '실행 승인 준비' : '실행 요청 확인') : turn.metadata?.route === 'inventory' ? '현재 구성 조회' : turn.metadata?.route === 'education' ? '학습 안내' : '점검 결과' }}</span><span v-if="turn.rewritten" class="tag">검증 뒤 다시 작성됨</span><span v-else class="answer-origin">Docker Ops Agent</span></div>
              <AnswerHtml v-if="turn.answerHtml" :html="turn.answerHtml" />
              <AnswerText v-else :text="turn.answer" />
              <p v-if="turn.metadata" class="summary-line">{{ stateLabel(turn) }}<template v-if="turn.metadata.config_metrics"> · 설정 수집 {{ turn.metadata.config_metrics.files_collected }}/{{ turn.metadata.config_metrics.files_requested }} · 지원 주장 {{ turn.metadata.config_metrics.claims_supported }}건</template><template v-if="turn.metadata.retrieval_method"> · 검색 {{ turn.metadata.retrieval_method }}</template></p>
              <a v-if="turn.metadata?.target_selection_required" href="#target-selection">프로젝트 선택 패널로 이동</a>
              <dl v-if="turn.diagnosis" class="result-metrics"><div><dt>관측 판단</dt><dd :title="turn.diagnosis.cause">{{ causeTitle(turn.diagnosis.cause) }}</dd></div><div><dt>근거 충족</dt><dd>{{ turn.metadata?.evidence_coverage == null ? '미측정' : `${Math.round(turn.metadata.evidence_coverage * 100)}%` }}</dd></div><div><dt>도구 호출</dt><dd>{{ turn.metadata?.tool_calls ?? 0 }}회</dd></div><div v-if="turn.metadata?.llm_usage?.total_tokens != null"><dt>사용 토큰</dt><dd>{{ turn.metadata.llm_usage.total_tokens.toLocaleString() }}</dd></div></dl>
              <p v-if="turn.metadata?.execution_budget?.stop_reason" class="hint">실행 한도에 도달해 추가 호출을 중단했습니다. 확인된 관측만 표시하며 변경은 수행하지 않았습니다.</p>
            </div>

            <ProposalPanel :proposal="turn.proposal" />
            <ClarificationPanel :question="turn.clarification" :disabled="sending" @reply="response => replyToQuestion(turn, response)" @cancel="cancelQuestion(turn)" />
            <p v-for="action in turn.actionHistory" :key="action.action_id" class="note">1차 파일 변경·Compose 구문 검증 완료. 컨테이너는 아직 변경하지 않았습니다. 2차 승인을 거절하거나 만료되면 파일 변경은 유지되며 자동 배포되지 않습니다.</p>
            <DecisionPanel :approval="turn.approval" :result="turn.actionResult" @decide="ok => decide(turn, ok)" />

            <details class="trace" :open="sending && turn === turns.at(-1)">
              <summary><span>{{ turn.restored ? '복원된 확인 질문' : `실제 실행 단계 · ${turn.nodes.length}개` }}</span><span class="state" :class="stateClass(turn)">{{ stateLabel(turn) }}</span></summary>
              <p v-if="!turn.nodes.length" class="wait">{{ turn.restored ? '저장된 질문만 복원했습니다. 답변 후 새 실행의 실제 이벤트를 표시합니다.' : '첫 실행 이벤트를 기다리는 중입니다.' }}</p>
              <div class="trace-body">
                <details v-for="node in turn.nodes" :key="node.run_id" class="node-row" :class="[node.status, { child: node.parent_run_id }]" :data-node="`${node.scope}.${node.node}`" :data-status="node.status">
                  <summary><span class="dot" aria-hidden="true"></span><span class="name">{{ nodeTitle(node) }}<small>{{ node.scope }}.{{ node.node }}</small></span><span class="time num">{{ nodeStatus(node.status) }}<template v-if="node.elapsed_ms != null"> · {{ ms(node.elapsed_ms) }}</template></span></summary>
                  <div class="io">
                    <p class="mono">run {{ node.run_id }} · parent {{ node.parent_run_id || 'root' }}</p>
                    <pre class="data">입력 {{ JSON.stringify(node.input, null, 2) }}</pre>
                    <pre v-if="node.output" class="data">노드 출력 (중간 원고·후보는 최종 검증 전 데이터) {{ JSON.stringify(node.output, null, 2) }}</pre>
                    <p v-else>{{ node.error || '아직 출력이 없습니다.' }}</p>
                  </div>
                </details>
              </div>
            </details>

            <details class="fold"><summary>업무 이벤트 {{ turn.events.length }}개</summary><div class="body"><p v-for="(e, i) in turn.events" :key="i">{{ labels[e.type] || e.type }} · {{ details(e) }}</p></div></details>
            <details v-if="turn.evidence.length" class="fold"><summary>수집한 관측 근거 {{ turn.evidence.length }}개</summary><div class="body"><template v-for="item in turn.evidence" :key="item.id"><p class="mono">{{ item.id }} · {{ item.tool }}<template v-if="item.elapsed_ms != null"> · {{ ms(item.elapsed_ms) }}</template></p><pre class="data">{{ JSON.stringify(item.observation, null, 2) }}</pre></template></div></details>
            <details v-if="turn.knowledge.length" class="fold"><summary>검색한 교육 자료 {{ turn.knowledge.length }}개 (실시간 관측 아님)</summary><div class="body"><template v-for="item in turn.knowledge" :key="item.id"><p>{{ item.id }} · {{ item.title }} · v{{ item.revision }} · 검토 {{ item.reviewed_at }} · <a :href="item.source_url" target="_blank" rel="noopener noreferrer">Docker 공식 문서</a></p><pre class="data">{{ item.content }}</pre></template></div></details>
            <details v-if="turn.prompt || turn.metadata" class="fold"><summary>프롬프트와 실행 메타데이터</summary><div class="body"><pre class="data">{{ turn.prompt }}

{{ JSON.stringify(turn.metadata, null, 2) }}</pre></div></details>
            <p v-if="turn.error" class="error" role="alert">{{ turn.error }}</p>
          </article>
        </div>

        <div class="composer-dock">
          <button v-if="!following && turns.length" type="button" class="jump-latest" @click="followLatest">↓ 최신 답변 보기</button>
          <form class="composer" @submit.prevent="submit">
            <div class="composer-box">
              <label class="sr-only" for="docker-question">Docker 교육 또는 진단 질문</label>
              <textarea id="docker-question" v-model="message" rows="2" @keydown="onComposerKeydown" :disabled="sending || selectingTarget || !!waitingTurn || !health?.target_selected" maxlength="4000" aria-describedby="composer-help" :placeholder="!health?.target_selected ? '먼저 위에서 프로젝트를 선택해주세요' : waitingTurn ? '추가 확인 카드에 답변하거나 요청을 취소해주세요' : '증상이나 확인할 내용을 적어주세요.'"></textarea>
              <button v-if="sending" type="button" class="btn stop" @click="stop">수신 중지</button>
              <button v-else type="submit" class="btn send" :disabled="selectingTarget || !message.trim() || !!waitingTurn || !health?.target_selected">질문하기 <span aria-hidden="true">↑</span></button>
            </div>
            <div class="composer-footer"><p id="composer-help"><template v-if="sending">실제 실행 과정을 받아오는 중입니다. 수신 중지는 서버 작업 취소를 보장하지 않습니다.</template><template v-else>Enter로 전송 <span>· Shift + Enter로 줄바꿈</span></template></p><span v-if="message.length" class="character-count">{{ message.length.toLocaleString() }} / 4,000</span><span v-else class="composer-safety">변경 전에는 반드시 승인</span></div>
          </form>
        </div>
      </main>
    </div>
  </div>
</template>
