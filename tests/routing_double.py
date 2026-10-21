"""Deterministic TEST-ONLY responses. Never imported by the app; not an LLM accuracy evaluation."""
import re
from backend.knowledge import retrieve_lessons
from backend.planning import unsafe_execution_request


def education_request(query: str, concept: bool) -> bool:
    text = query.lower()
    if re.search(r"점검|진단|분석|조회|조사|관측|상태|로그|inspect|diagnos|investigat|check|실제로.{0,8}(확인|봐)|(?:현재|지금|실제).{0,25}(확인|봐|살펴)", text): return False
    if re.search(r"(?:교육|학습|가르|배우|예제).*(?:도커|docker|컨테이너|compose)|(?:도커|docker|컨테이너|compose).*(?:교육|학습|가르|배우|예제)", text): return True
    if not retrieve_lessons(query): return False
    return concept or bool(re.search(r"사라|날아|없어|유실|초기화|lost|disappear|볼륨|volume|마운트|mount|영속", text))


def route_request(query: str) -> tuple[str, str]:
    text = query.lower()
    concept = bool(re.search(r"뭐|뭔|무엇|정의|차이|개념|설명|what is|what are|explain|difference", text))
    live = bool(re.search(r"현재|지금|실제|발생|안\s*(돼|됩|되)|계속|재시작|종료|장애|연결이|unhealthy|restarting|exit\s*\d", text))
    if unsafe_execution_request(query): return "blocked", "test unsafe request"
    if re.search(r"dockerfile|도커파일|dockerignore|컴포즈|compose|설정\s*파일", text) and not concept:
        if re.search(r"수정|고쳐|개선|반영|적용|추가|패치|변경안|diff|fix|change|edit", text): return "change", "test change intent"
        if re.search(r"검토|읽어|점검|내용|review|inspect", text): return "configuration", "test config review"
    if education_request(query, concept): return "education", "test education intent"
    if concept and not live: return "chat", "test concept intent"
    operations = ("docker", "도커", "컨테이너", "compose", "배포", "서비스", "장애", "로그", "restart", "재시작", "메모리", "network", "네트워크", "readiness", "healthcheck", "연결", "점검", "데이터", "볼륨", "마운트", "volume", "mount")
    short_term = re.search(r"(?<![a-z0-9_-])(?:api|db|dns|cpu|oom|tcp)(?![a-z0-9_-])", text)
    if not any(word in text for word in operations) and not short_term: return "chat", "test general intent"
    status = bool(re.search(r"상태만|헬스만|health만|목록만|살아있|켜져|running인지|실행 중인지|상태.{0,3}(알려|확인)|status only|health only", text))
    diagnosis = bool(re.search(r"원인|분석|진단|장애|안\s*(돼|됩|되)|왜", text))
    return ("status", "test status intent") if status and not diagnosis else ("incident", "test incident intent")
