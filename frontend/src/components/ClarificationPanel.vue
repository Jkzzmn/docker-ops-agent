<script setup>
import { ref } from 'vue'

defineProps({ question: { type: Object, default: null }, disabled: Boolean })
const emit = defineEmits(['reply', 'cancel'])
const answer = ref('')
const statuses = { answered: '답변을 전달했습니다', cancelled: '확인을 취소했습니다', superseded: '새 요청으로 대체됐습니다', unknown: '대기 상태를 다시 확인해주세요', expired: '질문이 만료됐습니다. 새 질문을 보내주세요.' }
</script>

<template>
  <section v-if="question" class="clarify" aria-label="추가 확인 질문">
    <h3>진행 전에 한 가지 확인이 필요합니다</h3>
    <p>{{ question.question }}</p>
    <p class="note">질문하는 이유: {{ question.reason }}</p>
    <template v-if="question.status === 'pending'">
      <div class="choices">
        <button v-for="(option, index) in question.options" :key="index" class="btn" type="button" :disabled="disabled || question.busy" @click="emit('reply', { choice: index + 1 })">{{ index + 1 }}. {{ option }}</button>
      </div>
      <form @submit.prevent="emit('reply', { answer: answer.trim() })">
        <label>직접 답변 (번호 또는 자연어)
          <textarea v-model="answer" aria-label="추가 확인 답변" maxlength="1000" :disabled="disabled || question.busy" placeholder="번호를 적거나, 원하는 대상과 작업을 직접 알려주세요"></textarea>
        </label>
        <div class="actions">
          <button class="btn" type="button" :disabled="disabled || question.busy" @click="emit('cancel')">이 요청 취소</button>
          <button class="btn approve" type="submit" :disabled="disabled || question.busy || !answer.trim()">답변하고 계속</button>
        </div>
      </form>
      <p class="note">답변은 대상·의도 확인일 뿐, 파일 변경이나 배포 승인이 아닙니다. 만료 {{ new Date(question.expires_at * 1000).toLocaleTimeString() }}</p>
    </template>
    <p v-else class="note" role="status">{{ statuses[question.status] || question.status }}</p>
  </section>
</template>

<style scoped>
.clarify { margin: 18px 0; padding: 22px; border: 1px solid #dd9a56; border-radius: 14px; background: #fffaf3; }
h3 { margin-top: 0; font-size: 16px; }
.choices { display: grid; gap: 8px; margin: 16px 0; }
.choices button { text-align: left; white-space: normal; line-height: 1.5; }
label { display: block; font-size: 13px; }
textarea { display: block; box-sizing: border-box; width: 100%; min-height: 80px; margin-top: 8px; padding: 12px; border: 1px solid #b9c8c7; border-radius: 8px; font: inherit; }
</style>
