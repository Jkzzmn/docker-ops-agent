import re

import pytest
from fastapi.testclient import TestClient

from backend.render import ALLOWED_TAGS, evidence_titles, markdown_to_html
from backend.server import app


def tags(fragment: str) -> set[str]:
    return set(re.findall(r"</?([a-z0-9]+)", fragment))


def test_model_html_and_script_are_escaped_not_rendered():
    out = markdown_to_html('<script>alert(1)</script> <img src=x onerror=alert(1)> **굵게** `<b>x</b>`')
    assert "<script" not in out and "<img" not in out and "&lt;script&gt;" in out
    assert "<strong>굵게</strong>" in out and "<code>&lt;b&gt;x&lt;/b&gt;</code>" in out


@pytest.mark.parametrize("link", ["javascript:alert(1)", "data:text/html,x", "vbscript:x", "//evil.example/x"])
def test_only_http_links_become_anchors(link):
    out = markdown_to_html(f"[눌러보세요]({link})")
    assert "<a " not in out and 'href="' not in out


def test_https_link_is_hardened_and_attribute_injection_is_escaped():
    out = markdown_to_html('[문서](https://docs.docker.com/a?x="onmouseover=alert(1))')
    assert out.count("<a ") == 1 and 'rel="noopener noreferrer"' in out and 'target="_blank"' in out
    assert '"onmouseover' not in out and "&quot;onmouseover" in out


def test_every_emitted_tag_is_on_the_allowlist():
    text = "### 제목\n\n**소제목:**\n1. 하나 [E1]\n2. 둘 `x`\n- 점\n\n> 인용\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```yaml\nservices: {}\n```\n[검증 미통과: 확인 필요]\n끝 [문서](https://example.com)"
    assert tags(markdown_to_html(text)) <= set(ALLOWED_TAGS)


def test_citations_get_tooltips_from_the_evidence_ledger():
    titles = evidence_titles([{"id": "E4", "tool": "inspect_container", "args": {"service": "api"}}], [{"id": "K1", "title": "쓰기 계층"}])
    out = markdown_to_html("OOM입니다 [E4] 그리고 [K1] 와 [E9]", titles)
    assert '<sup class="cite" title="inspect_container {&#x27;service&#x27;: &#x27;api&#x27;}">E4</sup>' in out
    assert '<sup class="cite" title="쓰기 계층">K1</sup>' in out and '<sup class="cite">E9</sup>' in out


def test_blocks_notice_lists_tables_and_code():
    out = markdown_to_html("[검증 미통과: 자동 조치를 실행하지 마세요]\n\n**관찰된 사실:**\n1. 첫째\n2. 둘째\n\n| 항목 | 값 |\n| --- | --- |\n| 상태 | exited |\n\n```\nservices:\n  api: {}\n```")
    assert '<p class="notice">검증 미통과: 자동 조치를 실행하지 마세요</p>' in out
    assert "<h4>관찰된 사실:</h4>" in out and "<ol><li>첫째</li><li>둘째</li></ol>" in out
    assert "<th>항목</th>" in out and "<td>exited</td>" in out and "<pre>services:\n  api: {}</pre>" in out


def test_unterminated_fence_and_pipes_without_separator_are_safe():
    assert markdown_to_html("```\n<b>x").endswith("</pre>")
    assert "<table" not in markdown_to_html("| 그냥 | 파이프 |")


def test_api_returns_reply_and_a_matching_html_fragment():
    with TestClient(app) as client:
        body = client.post("/chat", json={"message": "api 컨테이너 장애", "thread_id": "html"}).json()
        text = client.get("/chat/stream", params={"message": "api 컨테이너 장애", "thread_id": "html2"}).text
    assert body["reply_html"].startswith("<") and "<script" not in body["reply_html"]
    assert 'class="cite"' in body["reply_html"] and "inspect_container" in body["reply_html"]
    assert '"reply_html": "<' in text
