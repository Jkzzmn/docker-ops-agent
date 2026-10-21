"""Render the documented current topology, without starting Docker or an LLM.

Run with the bundled Python (reportlab); render PDF pages with pdftoppm for QA.
Coordinates use a top-left origin to keep the diagrams straightforward to edit.
"""
from pathlib import Path
import math

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf/current_langgraph.pdf"
FONT = Path("/Library/Fonts/Arial Unicode.ttf")
W, H = 1200, 900
INK, MUTED = "#163047", "#536579"
BLUE, GREEN, AMBER, PURPLE = "#E7F1FF", "#E6F5ED", "#FFF2D8", "#EFEAFF"


def run():
    pdfmetrics.registerFont(TTFont("Korean", str(FONT)))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(OUT), pagesize=(W, H))
    c.setTitle("Docker Ops Agent - Current LangGraph / 2026-10-21")
    c.setAuthor("Docker Ops Agent project documentation")

    def text(x, y, value, size=14, color=INK, center=False):
        c.setFillColor(HexColor(color)); c.setFont("Korean", size)
        (c.drawCentredString if center else c.drawString)(x, H-y, value)

    def rect(x, y, w, h, fill="#FFFFFF", stroke="#C7D4DF", dash=False):
        c.setFillColor(HexColor(fill)); c.setStrokeColor(HexColor(stroke))
        c.setLineWidth(1); c.setDash(4, 3) if dash else c.setDash()
        c.roundRect(x, H-y-h, w, h, 10, fill=1, stroke=1); c.setDash()

    def box(x, y, w, h, lines, fill=BLUE, size=14):
        rect(x, y, w, h, fill)
        if isinstance(lines, str): lines = [lines]
        for i, line in enumerate(lines):
            text(x+w/2, y+h/2+(i-(len(lines)-1)/2)*21+5, line, size, center=True)

    def arrow(points, label=None, lx=None, ly=None, dash=False, color="#698299"):
        c.setStrokeColor(HexColor(color)); c.setFillColor(HexColor(color)); c.setLineWidth(1.5)
        c.setDash(5, 3) if dash else c.setDash()
        p = c.beginPath(); p.moveTo(points[0][0], H-points[0][1])
        for x, y in points[1:]: p.lineTo(x, H-y)
        c.drawPath(p); c.setDash()
        x, y = points[-1]; px, py = points[-2]; angle = math.atan2(y-py, x-px)
        p = c.beginPath(); p.moveTo(x, H-y)
        for off in (-0.45, 0.45):
            p.lineTo(x-9*math.cos(angle+off), H-(y-9*math.sin(angle+off)))
        p.close(); c.drawPath(p, fill=1, stroke=0)
        if label: text(lx, ly, label, 12, color)

    def base(page, title, subtitle):
        c.setFillColor(HexColor("#F7FAFC")); c.rect(0, 0, W, H, fill=1, stroke=0)
        text(40, 42, "DOCKER OPS AGENT  /  CURRENT IMPLEMENTATION", 12, MUTED)
        text(40, 83, title, 28)
        text(40, 112, subtitle, 14, MUTED)
        text(40, 876, "2026-10-21 | 코드 기준 정적 구조도: 특정 요청의 실행 기록이나 모든 노드의 실행을 의미하지 않습니다.", 11, MUTED)
        text(1150, 876, f"{page} / 3", 11, MUTED, center=True)

    base(1, "01  질문에 따라 짧게 끝나는 전체 경로", "경로에는 실제 노드 ID를 표시합니다. after_route와 행 제목은 분기 조건이며 별도 노드가 아닙니다.")
    box(40, 140, 110, 45, "START", GREEN)
    box(185, 140, 180, 45, "route", BLUE)
    box(405, 140, 710, 45, "after_route: 예산 확인 > 명령 확인 > route 값에 따라 아래 중 하나 선택", AMBER, 15)
    arrow([(150, 162), (185, 162)]); arrow([(365, 162), (405, 162)])
    rows = [
        ("chat", "chat_answer  →  END", "일반 대화: Docker 도구·분석·검증 생략", BLUE),
        ("status", "status_observe  →  END", "서비스 health 또는 프로젝트 목록 1회; 모델 보고서 생략", GREEN),
        ("/diagnose-network a b", "network_diagnose  →  END", "최대 5개 읽기 체크; 부재·중지·오류에서 즉시 종료", GREEN),
        ("education", "education_retrieve  →  write  →  validate  →  finalize  →  END", "자료가 없으면 education_unknown → END / 상세: 3쪽", PURPLE),
        ("configuration / change", "configuration  →  END", "내부 4노드로 파일 수집·검토·검증·제안 / 상세: 3쪽", PURPLE),
        ("incident (일반 진단)", "plan  →  gate  →  research  →  replan_check  →  ...", "필요한 Worker만 실행; 근거에 따라 종료·추가 수집·분석 / 2쪽", BLUE),
        ("clarify", "human_question  →  END", "필수 대상이 모호할 때 번호 선택 또는 자연어 답변 요청", AMBER),
        ("target_selection", "target_selection  →  END", "다른 프로젝트 요청: 자동 전환·현재 대상 대리 조회 금지", AMBER),
        ("unavailable / blocked", "routing_unavailable 또는 policy_block  →  END", "분류 실패 / 허용 범위 밖 실행은 Docker 도구 없이 종료", AMBER),
        ("예산 소진 (route 직후)", "facts_finish  →  END", "실행 중 기한 초과는 공통 실행기도 부분 관측으로 종료 처리", AMBER),
    ]
    for i, (condition, path, desc, fill) in enumerate(rows):
        y = 211 + i*61
        rect(40, y, 1115, 54, "#FFFFFF")
        text(53, y+23, condition, 14)
        rect(280, y+5, 862, 44, fill)
        text(294, y+23, path, 15)
        text(294, y+41, desc, 11, MUTED)
    text(42, 846, "핵심: route는 LLM 의미 분류 + 명시적 명령 파싱, 이후 안전 게이트는 결정적 코드입니다.", 15)
    c.showPage()

    base(2, "02  일반 장애 진단: 수집은 병렬, 결정은 근거 기반", "부모 그래프의 실제 노드와 조건부 간선. Worker 내부와 기본 한도는 아래 보조 패널에 표시합니다.")
    for x, name in [(40, "plan"), (235, "gate"), (430, "research"), (655, "replan_check")]:
        box(x, 180, 170 if x != 655 else 210, 70, [name, {"plan":"최소 관측 계획", "gate":"허용·중복 검사", "research":"필요한 Worker 위임", "replan_check":"필수 근거·원인 판정"}[name]], size=15)
    arrow([(210,215),(235,215)]); arrow([(405,215),(430,215)]); arrow([(600,215),(655,215)])
    arrow([(760,180),(760,144),(320,144),(320,180)], "collect: 미시도 필수 관측 + 재수집 한도 이내", 370, 136)
    box(930, 180, 220, 70, ["END", "not_deployed: 상태 반환"], GREEN)
    arrow([(865,215),(930,215)], "done", 879, 202)
    box(660, 322, 200, 65, ["analysis", "cross_check (QUALITY)"], PURPLE)
    arrow([(760,250),(760,322)])
    text(645, 286, "근거 충분", 12, "#698299")
    text(645, 307, "+ required", 12, "#698299")
    box(660, 452, 200, 65, ["write", "compose"], BLUE)
    arrow([(760,387),(760,452)])
    arrow([(690,250),(624,250),(624,484),(660,484)], "write: 분석 생략", 460, 282)
    box(935, 322, 215, 65, ["facts_finish", "확인된 관측만 → END"], AMBER)
    arrow([(865,235),(901,235),(901,354),(935,354)], "facts: 부족·불명·예산", 935, 296)
    box(935, 452, 215, 65, ["validate", "code_check → 조건부 judge"], PURPLE, 14)
    arrow([(860,484),(935,484)])
    box(935, 588, 215, 65, ["finalize", "최종 답변·승인 제안 → END"], GREEN, 14)
    arrow([(1100,517),(1100,588)], "합격 또는 검증 횟수 소진", 930, 574)
    arrow([(995,517),(995,540),(800,540),(800,517)], "거절 + 남은 횟수: 재작성 (기본 없음)", 665, 566, dash=True)
    rect(40, 300, 550, 292)
    text(60, 331, "research가 필요한 역할만 병렬 호출", 19)
    for y, name, detail in [(354,"runtime", "엔진·컨테이너 상태 / 로그·이벤트·리소스"), (421,"configuration", "Compose 설정 검사 (파일 검토용 4노드와 별개)"), (488,"network", "공유 네트워크·DNS·TCP·리스닝 포트")]:
        box(60, y, 145, 48, name, GREEN)
        text(222, y+19, "plan → observe → report", 15)
        text(222, y+39, detail, 11, MUTED)
    text(60, 574, "각 Worker는 독립 StateGraph. 기본 plan은 재차 LLM을 호출하지 않음.", 12, MUTED)
    rect(40, 623, 820, 207, "#EDF3F8")
    text(60, 653, "기본 실행 한도와 종료 규칙", 19)
    for i, line in enumerate([
        "요청 60초 / 모델 8회 / 도구 24회 / 추가 수집 최대 2회",
        "읽기 동시성 4 / 모델 동시성 1 / 검증 시도 1 / Worker 추가 계획 OFF",
        "검증 거절: 미검증 원고 대신 구조화 관측. 승인·변경 제안 제거.",
        "분석은 부족한 근거를 대신하지 않음. 원인 불명은 facts_finish로 종료.",
        "finalize는 변경하지 않음. 실제 변경은 별도 승인 API에서만 실행."
    ]): text(60, 687+i*27, line, 14)
    text(930, 704, "점선: 설정을 늘렸을 때만", 12, MUTED)
    text(930, 729, "가능한 선택적 반복", 12, MUTED)
    text(930, 780, "출처: backend/graph.py", 12, MUTED)
    text(930, 803, "backend/agents.py", 12, MUTED)
    c.showPage()

    base(3, "03  서브그래프와 실행 인프라는 서로 다른 층", "아래 인프라 박스는 LangGraph 노드가 아닙니다. SQLite 저장, MCP 도구 호출, SSE 전달을 구분합니다.")
    rect(40, 140, 1115, 166, "#F3EFFF")
    text(60, 169, "교육 / 로컬 자료 검색 (벡터 DB 아님)", 19)
    for x, w, lines in [(60,220,["education_retrieve","knowledge.retrieve"]),(328,230,["write","writer.compose: 검토 원문"]),(606,250,["validate","validator.code_check만"]),(904,220,["finalize","최종 답변 → END"])]:
        box(x,190,w,61,lines,PURPLE)
    for a,b in [(280,328),(558,606),(856,904)]: arrow([(a,220),(b,220)])
    text(60, 282, "라우터가 K1~K5 중 최대 3개 선택 → 로컬 카드 검색 → 원문 조립·출처 검증. 자료 없음: education_unknown → END.", 13)
    rect(40, 327, 1115, 166)
    text(60,356,"설정 검토 / configuration 서브그래프",19)
    for x,w,lines in [(60,220,["config_collect","Compose·Dockerfile·관측"]),(328,230,["config_review","FAST 모델 후보 생성"]),(606,250,["config_validate","주장·줄·스냅샷 대조"]),(904,220,["config_propose","검증 결과·조건부 diff"])]:
        box(x,377,w,61,lines,BLUE)
    for a,b in [(280,328),(558,606),(856,904)]: arrow([(a,407),(b,407)])
    text(60,470,"검토만 요청하면 edits 제거. change도 즉시 수정 아님: diff 제안 → 파일 변경 승인 → 별도 배포 승인 (그래프 밖 API).",13)
    box(40,539,220,86,["브라우저 / API 클라이언트","POST /chat: 최종 JSON","GET /chat/stream: SSE"],GREEN,13)
    box(320,529,500,106,["공통 execute() → compiled docker_supervisor","활성 대상 bundle 1개 / 프로세스 단위 재사용","새 요청: fresh_state + 범위별 대화 이력"],BLUE,15)
    arrow([(260,576),(320,576)],"요청",274,563)
    arrow([(320,616),(285,616),(285,651),(151,651),(151,625)],"실제 Node 이벤트 / 최종 답변",43,681)
    box(880,539,275,86,["LangSmith (선택)","노드·도구·모델 메타데이터","키+flag 필요 / 원문 전송 OFF"],AMBER,13)
    arrow([(820,580),(880,580)],dash=True)
    box(320,714,250,106,["MCP / stdio 세션 2개","docs: 문서 resource","ops: 도구·runbook·prompt","Docker CLI / 고정 대상"],GREEN,13)
    box(610,714,250,106,["SQLite (로컬)","sessions.db: 메시지·HITL","sessions.checkpoints.sqlite","부모 그래프 checkpoint"],PURPLE,13)
    box(900,714,255,106,["ModelGateway / 역할 선택","router: 별도 설정 가능","FAST: 4B / QUALITY: 8B","기본 로컬 Ollama 설정"],BLUE,13)
    arrow([(445,635),(445,714)]); arrow([(735,635),(735,714)])
    arrow([(820,630),(866,630),(866,672),(1028,672),(1028,714)])
    text(40, 851, "사람 답변·승인은 별도 저장소/API로 후속 요청 처리. 현재 LangGraph interrupt/resume 기반 워크플로는 아닙니다.",13)
    c.save()
    print(OUT)


if __name__ == "__main__":
    run()
