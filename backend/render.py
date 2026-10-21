"""Validated answer text -> a small, allowlisted HTML fragment.

The model never writes HTML that reaches the page. The text that already passed validation is parsed into blocks and
every character is escaped; only the tags below are ever emitted, and links are limited to http(s). The frontend
sanitizes the fragment again before inserting it.
"""
from __future__ import annotations

import html
import re

ALLOWED_TAGS = ("h3", "h4", "p", "ul", "ol", "li", "strong", "code", "a", "sup", "pre", "blockquote", "table", "thead", "tbody", "tr", "th", "td", "div")

INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?://[^)\s]+\)|\[(?:E|K)\d+\])")
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
CITE = re.compile(r"\[((?:E|K)\d+)\]")
NOTICE = re.compile(r"^\[((?:교육 답변 )?검증 미통과[^\]]*)\]")
HEADING = re.compile(r"^#{1,4}\s+(.*)$")
BOLD_LINE = re.compile(r"^\*\*([^*\n]+?)\*\*\s*:?\s*$")
ITEM = re.compile(r"^\s*(?:(\d+)[.)]|[-*•])\s+(.*)$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _inline(line: str, cites: dict[str, str]) -> str:
    out = []
    for part in INLINE.split(line):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append(f"<strong>{_esc(part[2:-2])}</strong>")
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append(f"<code>{_esc(part[1:-1])}</code>")
        elif LINK.fullmatch(part):
            label, url = LINK.fullmatch(part).groups()
            out.append(f'<a href="{_esc(url)}" target="_blank" rel="noopener noreferrer">{_esc(label)}</a>')
        elif CITE.fullmatch(part):
            ident = CITE.fullmatch(part).group(1)
            title = cites.get(ident)
            out.append(f'<sup class="cite" title="{_esc(title)}">{ident}</sup>' if title else f'<sup class="cite">{ident}</sup>')
        else:
            out.append(_esc(part))
    return "".join(out)


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def evidence_titles(evidence: list[dict] | None, knowledge: list[dict] | None = None) -> dict[str, str]:
    """Citation ID -> what it points to, shown as a tooltip on the chip."""
    titles = {e["id"]: f"{e['tool']} {e.get('args') or ''}".strip() for e in evidence or [] if e.get("id")}
    titles |= {k["id"]: k.get("title", "") for k in knowledge or [] if k.get("id")}
    return titles


def markdown_to_html(text: str, cites: dict[str, str] | None = None) -> str:
    cites = cites or {}
    out: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    ordered = False
    table: list[str] = []
    fence: list[str] | None = None

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph), cites)}</p>")
        paragraph = []

    def flush_list():
        nonlocal items
        if items:
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(item, cites)}</li>" for item in items) + f"</{tag}>")
        items = []

    def flush_table():
        nonlocal table
        if len(table) >= 2 and TABLE_SEPARATOR.match(table[1]):
            head, body = _cells(table[0]), [_cells(row) for row in table[2:]]
            rows = "".join("<tr>" + "".join(f"<td>{_inline(cell, cites)}</td>" for cell in row) + "</tr>" for row in body)
            out.append('<div class="table-wrap"><table><thead><tr>' + "".join(f"<th>{_inline(cell, cites)}</th>" for cell in head) + f"</tr></thead><tbody>{rows}</tbody></table></div>")
        elif table:
            out.append(f"<p>{_inline(' '.join(table), cites)}</p>")
        table = []

    def flush_all():
        flush_paragraph(); flush_list(); flush_table()

    for raw in text.split("\n"):
        line = raw.rstrip()
        if fence is not None:
            if line.strip().startswith("```"):
                out.append(f"<pre>{_esc(chr(10).join(fence))}</pre>")
                fence = None
            else:
                fence.append(raw)
            continue
        if line.strip().startswith("```"):
            flush_all(); fence = []
            continue
        if not line.strip():
            flush_all()
            continue
        if line.lstrip().startswith("|"):
            flush_paragraph(); flush_list(); table.append(line)
            continue
        flush_table()
        notice = NOTICE.match(line)
        if notice:
            flush_all()
            out.append(f'<p class="notice">{_esc(notice.group(1))}</p>')
            rest = line[notice.end():].strip()
            if rest:
                paragraph.append(rest)
            continue
        heading = HEADING.match(line) or BOLD_LINE.match(line)
        if heading:
            flush_all()
            out.append(f"<h4>{_inline(heading.group(1).strip(), cites)}</h4>")
            continue
        if line.lstrip().startswith(">"):
            flush_all()
            out.append(f"<blockquote>{_inline(line.lstrip()[1:].strip(), cites)}</blockquote>")
            continue
        item = ITEM.match(line)
        if item:
            flush_paragraph()
            is_ordered = bool(item.group(1))
            if items and is_ordered != ordered:
                flush_list()
            ordered = is_ordered
            items.append(item.group(2))
            continue
        flush_list()
        paragraph.append(line.strip())
    if fence is not None:
        out.append(f"<pre>{_esc(chr(10).join(fence))}</pre>")
    flush_all()
    return "".join(out)
