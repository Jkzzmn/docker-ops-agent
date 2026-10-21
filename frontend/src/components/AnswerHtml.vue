<script setup>
import { computed } from 'vue'
import { sanitizeHtml } from '../sanitize.js'

const props = defineProps({ html: { type: String, default: '' } })
const safe = computed(() => sanitizeHtml(props.html))
</script>

<template>
  <!-- safe: escaped by the backend renderer, then re-filtered through an allowlist in sanitize.js -->
  <div class="prose" v-html="safe"></div>
</template>
