"""Reviewed, local educational retrieval; no network, embeddings or live observations."""
import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def lessons() -> list[dict]:
    return json.loads(Path(__file__).with_name("knowledge").joinpath("docker_lessons.json").read_text(encoding="utf-8"))


def retrieve_lessons(query: str, semantic_ids: list | None = None) -> list[dict]:
    if semantic_ids is not None:
        return [dict(item, retrieval_method="llm-catalog", matched_keywords=[]) for item in lessons() if item["id"] in semantic_ids][:3]
    text = query.lower()
    ranked = []
    for lesson in lessons():
        matches = [term for term in lesson["keywords"] if (re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) if term.isascii() else term in text)]
        if matches:
            ranked.append((len(matches), dict(lesson, matched_keywords=matches)))
    return [dict(lesson, score=score, retrieval_method="keyword-top3") for score, lesson in sorted(ranked, key=lambda pair: -pair[0])[:3]]


def teaching_guide(knowledge: list[dict], context: dict | None = None) -> str:
    """Only reviewed text is published; model-generated factual sentences are excluded."""
    sections = ["교육 안내입니다. 실제 환경을 조회하거나 변경하지 않았습니다."]
    for item in knowledge:
        content = item['content']
        if item['id'] == 'K1':
            before, rest = content.split("\n\n확인 질문: ", 1)
            questions, warning = rest.split("\n\n", 1)
            if (context or {}).get("lifecycle") in {"stopped", "deleted", "recreated"}:
                questions = questions.split("? ", 1)[1]
            content = before + "\n\n" + warning
            sections.append(f"{item['title']} [{item['id']}]\n\n{content}")
            sections.append("아직 확인되지 않은 정보 (학습용 참고이며 답변 의무 없음):\n" + questions)
        else:
            sections.append(f"{item['title']} [{item['id']}]\n\n{content}")
        if item.get("example"):
            sections.append("미적용 학습용 예시 (/data는 실제 앱 저장 경로로 변경):\n\n```yaml\n" + item["example"] + "\n```")
    sections.append("참고 자료 (실시간 관측 아님):\n\n" + "\n\n".join(
        f"[{item['id']}] [{item['title']}]({item['source_url']}) (교육 자료 v{item['revision']}, 검토 {item['reviewed_at']})" for item in knowledge))
    return "\n\n".join(sections)


