<script setup>
// Renders the small Markdown subset models actually emit (headings, lists, bold, code, links, evidence citations).
// Everything becomes Vue elements; no HTML from the model or the tools is ever injected.
import { computed } from 'vue'

const props = defineProps({ text: { type: String, default: '' } })

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\)|\[(?:E|K)\d+\])/g
function inline(line) {
  return line.split(INLINE).filter(part => part !== '').map(part => {
    if (part.startsWith('**')) return { kind: 'b', text: part.slice(2, -2) }
    if (part.startsWith('`')) return { kind: 'code', text: part.slice(1, -1) }
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/)
    if (link) return { kind: 'a', text: link[1], href: link[2] }
    if (/^\[(?:E|K)\d+\]$/.test(part)) return { kind: 'cite', text: part.slice(1, -1) }
    return { kind: 't', text: part }
  })
}

const blocks = computed(() => {
  const out = []
  let paragraph = [], list = null, fence = null
  const flush = () => {
    if (paragraph.length) out.push({ type: 'p', parts: inline(paragraph.join(' ')) })
    paragraph = []
    if (list) out.push(list)
    list = null
  }
  for (const raw of props.text.split('\n')) {
    if (fence) {
      if (raw.trim().startsWith('```')) { out.push({ type: 'pre', text: fence.join('\n') }); fence = null } else fence.push(raw)
      continue
    }
    const line = raw.trimEnd()
    if (line.trim().startsWith('```')) { flush(); fence = []; continue }
    if (!line.trim()) { flush(); continue }
    const notice = line.match(/^\[((?:교육 답변 )?검증 미통과[^\]]*)\]/)
    if (notice) { flush(); out.push({ type: 'notice', text: notice[1] }); const rest = line.slice(notice[0].length).trim(); if (rest) paragraph.push(rest); continue }
    const heading = line.match(/^#{1,4}\s+(.*)$/)
    if (heading) { flush(); out.push({ type: 'h', parts: inline(heading[1]) }); continue }
    const item = line.match(/^\s*(?:(\d+)\.|[-*])\s+(.*)$/)
    if (item) {
      const ordered = Boolean(item[1])
      if (paragraph.length) { out.push({ type: 'p', parts: inline(paragraph.join(' ')) }); paragraph = [] }
      if (!list || list.ordered !== ordered) { if (list) out.push(list); list = { type: 'list', ordered, items: [] } }
      list.items.push(inline(item[2]))
      continue
    }
    if (list) { out.push(list); list = null }
    paragraph.push(line.trim())
  }
  if (fence) out.push({ type: 'pre', text: fence.join('\n') })
  flush()
  return out
})
</script>

<template>
  <div class="prose">
    <template v-for="(block, i) in blocks" :key="i">
      <p v-if="block.type === 'notice'" class="notice" role="note">{{ block.text }}</p>
      <h4 v-else-if="block.type === 'h'"><template v-for="(p, j) in block.parts" :key="j"><b v-if="p.kind === 'b'">{{ p.text }}</b><code v-else-if="p.kind === 'code'">{{ p.text }}</code><sup v-else-if="p.kind === 'cite'" class="cite">{{ p.text }}</sup><template v-else>{{ p.text }}</template></template></h4>
      <p v-else-if="block.type === 'p'"><template v-for="(p, j) in block.parts" :key="j"><b v-if="p.kind === 'b'">{{ p.text }}</b><code v-else-if="p.kind === 'code'">{{ p.text }}</code><a v-else-if="p.kind === 'a'" :href="p.href" target="_blank" rel="noopener noreferrer">{{ p.text }}</a><sup v-else-if="p.kind === 'cite'" class="cite">{{ p.text }}</sup><template v-else>{{ p.text }}</template></template></p>
      <component :is="block.ordered ? 'ol' : 'ul'" v-else-if="block.type === 'list'"><li v-for="(item, k) in block.items" :key="k"><template v-for="(p, j) in item" :key="j"><b v-if="p.kind === 'b'">{{ p.text }}</b><code v-else-if="p.kind === 'code'">{{ p.text }}</code><a v-else-if="p.kind === 'a'" :href="p.href" target="_blank" rel="noopener noreferrer">{{ p.text }}</a><sup v-else-if="p.kind === 'cite'" class="cite">{{ p.text }}</sup><template v-else>{{ p.text }}</template></template></li></component>
      <pre v-else class="data code-block">{{ block.text }}</pre>
    </template>
  </div>
</template>
