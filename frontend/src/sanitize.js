// Second line of defence for the server's reply_html: the backend already escapes everything, and this
// removes anything outside a small allowlist before the fragment is inserted into the page.
const TAGS = new Set(['h3', 'h4', 'p', 'ul', 'ol', 'li', 'strong', 'code', 'a', 'sup', 'pre', 'blockquote', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'div', 'br'])
const DROP_WITH_CONTENT = new Set(['script', 'style', 'iframe', 'object', 'embed', 'template', 'noscript', 'svg', 'math', 'form', 'textarea', 'select', 'option', 'link', 'meta', 'base', 'title'])
const CLASSES = new Set(['cite', 'notice', 'table-wrap'])

function clean(parent) {
  for (const node of [...parent.childNodes]) {
    if (node.nodeType === 3) continue
    if (node.nodeType !== 1) { node.remove(); continue }
    const tag = node.tagName.toLowerCase()
    if (DROP_WITH_CONTENT.has(tag)) { node.remove(); continue }
    if (!TAGS.has(tag)) {
      node.replaceWith(parent.ownerDocument.createTextNode(node.textContent))
      continue
    }
    for (const attr of [...node.attributes]) {
      const name = attr.name.toLowerCase()
      const keep =
        (name === 'class' && attr.value.split(/\s+/).every(token => CLASSES.has(token))) ||
        (name === 'title' && ['sup'].includes(tag)) ||
        (name === 'href' && tag === 'a' && /^https?:\/\//i.test(attr.value.trim())) ||
        (['target', 'rel'].includes(name) && tag === 'a')
      if (!keep) node.removeAttribute(attr.name)
    }
    if (tag === 'a') {
      if (!node.hasAttribute('href')) { node.replaceWith(parent.ownerDocument.createTextNode(node.textContent)); continue }
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer')
    }
    clean(node)
  }
}

export function sanitizeHtml(input) {
  const doc = new DOMParser().parseFromString(`<body>${input || ''}</body>`, 'text/html')
  clean(doc.body)
  return doc.body.innerHTML
}
