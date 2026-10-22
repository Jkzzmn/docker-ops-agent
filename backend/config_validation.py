"""Small, conservative static review rules. Unknown syntax is not proof of a defect.

Only canonical rule conclusions are publishable; free-form model text is never a
validated claim. Snapshots refer to the original file hash, not masked text hashes.
"""
import re
from pathlib import PurePosixPath
from .sanitize import redact
from .diagnostics import contains_dangerous


def snapshots(evidence):
    rows = []
    for e in evidence:
        value = e["observation"]
        if e["tool"] == "read_dockerfile": value = {"path": e.get("args", {}).get("path"), **value}
        files = value.get("files", []) if e["tool"] == "read_compose_files" else [value] if e["tool"] == "read_dockerfile" else []
        for f in files:
            if not f.get("path"): continue
            content = f.get("content", "")
            rows.append({"evidence_id": e["id"], "path": f["path"], "sha256": f.get("sha256"), "found": f.get("found", f.get("ok")),
                         "complete": (f.get("ok") is True or (f.get("found") is False and "sha256" in f and not f.get("error"))) and not f.get("truncated"), "redacted": f.get("redacted", False),
                         "line_start": 1 if f.get("ok") else None, "line_end": len(content.splitlines()) if f.get("ok") else None, "snippet": content})
    return rows


def instructions(file):
    """Understand ordinary Dockerfile continuations, not heredocs/escape directives."""
    if re.search(r"(?im)^\s*#\s*escape=|<<", file["snippet"]): return None
    result, pending, start = [], "", 1
    for number, line in enumerate(file["snippet"].splitlines(), 1):
        if not pending: start = number
        pending += line.rstrip().removesuffix("\\") + " "
        if line.rstrip().endswith("\\"): continue
        match = re.match(r"\s*(FROM|COPY|ADD|RUN)\s+(.+)", pending, re.I)
        if match: result.append((match[1].upper(), match[2], start, number))
        pending = ""
    return None if pending else result


def dependency_order_notes(files):
    notes = []
    for f in files:
        if not f["complete"] or not f["found"] or f["redacted"] or not PurePosixPath(f["path"]).name.startswith("Dockerfile"): continue
        rows = instructions(f)
        if rows is None: continue
        copies = []
        for index, (kind, command, start, end) in enumerate(rows):
            if kind == "FROM": copies = []
            if kind == "COPY": copies.append(command)
            if kind != "RUN" or not re.search(r"\bpip(?:3)?\s+install\b", command) or not copies: continue
            later = next((r for r in rows[index+1:] if r[0] in {"FROM", "COPY"}), None)
            manifest = r"(?:requirements[^\s/]*\.txt|pyproject\.toml|poetry\.lock|uv\.lock|setup\.cfg)(?:\s|$)"
            if later and later[0] == "COPY" and all(re.match(manifest,re.sub(r"^(?:--\S+\s+)*", "", c)) for c in copies):
                notes.append({"path":f["path"], "line_start":start, "line_end":later[3], "evidence_ids":[f["evidence_id"]],
                    "text":f"pip 설치 단계({start}줄)는 의존성 파일 COPY 뒤, 나머지 파일 COPY({later[2]}줄) 앞에 이미 분리되어 있습니다. 이 순서 자체를 뒤집으라는 개선 제안은 하지 않습니다. 실제 캐시 적중·빌드 속도는 측정하지 않았습니다."})
    return notes


def inspect_rules(files):
    supported, contradicted = [], []
    def claim(rule, f, text, start=1, end=None):
        end = end if end is not None else f["line_end"]
        return {"rule": rule, "claim_kind": "static_risk", "path": f["path"], "sha256": f["sha256"], "line_start": start or None, "line_end": end if start else None,
                "snippet": "\n".join(f["snippet"].splitlines()[start-1:end]), "evidence_ids": [f["evidence_id"]], "text": text, "verdict": "supported"}
    for f in files:
        if not f["complete"]: continue
        if PurePosixPath(f["path"]).name == ".dockerignore" and f["found"] is False:
            supported.append(claim("missing_dockerignore", f, "활성 build context에 .dockerignore가 없습니다. 불필요한 파일이 전송될 수 있는 정적 위험입니다. 실제 context 크기·빌드 속도는 측정하지 않았습니다.", 0, 0))
        if not PurePosixPath(f["path"]).name.startswith("Dockerfile") or not f["found"] or f["redacted"]: continue
        rows = instructions(f)
        if rows is None: continue
        copies, updates = [], []
        for kind, command, start, end in rows:
            if kind == "FROM": copies = []; updates = []
            if kind == "COPY": copies.append((command, start))
            if kind != "RUN": continue
            if re.search(r"(?:^|&&\s*)npm\s+ci\b", command):
                contradicted.append(claim("use_npm_ci", f, "이미 npm ci를 사용합니다.", start, end) | {"verdict": "contradicted"})
            update = re.search(r"(?:^|&&\s*)apt(?:-get)?\s+update\b", command)
            install = re.search(r"(?:^|&&\s*)apt(?:-get)?\s+install\b", command)
            if update and install:
                contradicted.append(claim("combine_apt", f, "apt update와 install이 같은 RUN에 있습니다.", start, end) | {"verdict": "contradicted"})
            elif update: updates.append(start)
            elif install and updates:
                supported.append(claim("combine_apt", f, "같은 stage에서 apt update와 install이 별도 RUN에 있습니다. 캐시 갱신 불일치를 줄이도록 결합을 검토하세요. 실제 빌드는 수행하지 않았습니다.", updates[-1], end))
            if re.search(r"(?:^|&&\s*)(?:python(?:3)?\s+-m\s+)?pip(?:3)?\s+install\b", command):
                # Only ordinary source-directory COPY before a dependency step.
                source = next((n for command, n in copies if re.match(r"(?:\./)?(?:app|src)\s", command) or re.match(r"\.\s", command)), None)
                if source:
                    supported.append(claim("source_before_pip", f, "애플리케이션 소스 COPY 뒤에 pip install이 있어 소스 변경 시 설치 layer 캐시가 무효화될 수 있습니다. 패키징 의존성을 확인한 뒤 분리를 검토하세요. 시간 절감은 측정하지 않았고 자동 순서 변경은 제안하지 않습니다.", source, end))
    return supported, contradicted


def validate_review(evidence, review):
    files = snapshots(evidence)
    unsupported = [f["path"] for f in files if f["complete"] and f["found"] and not f["redacted"] and PurePosixPath(f["path"]).name.startswith("Dockerfile") and instructions(f) is None]
    supported, contradictions = inspect_rules(files)
    for i, item in enumerate(supported, 1): item["claim_id"] = f"C{i}"
    decisions = []
    for candidate in review.get("findings", []):
        rule, path = candidate.get("rule"), candidate.get("path")
        matches = [c for c in supported if c["rule"] == rule and c["path"] == path]
        against = [c for c in contradictions if c["rule"] == rule and c["path"] == path]
        # Legacy/free-text requests are not reinterpreted as verified assertions.
        verdict = "supported" if matches else "contradicted" if against else "insufficient"
        decisions.append({"rule": rule, "path": path, "verdict": verdict, "verified_scope": "canonical-rule-conclusion-only", "raw_text_verified": False,
                          "reason": "정적 규칙으로 대조 (원래 모델 문장 검증 아님)" if matches or against else "지원 검증 규칙 또는 충분한 스냅샷 없음"})
    edits = []
    for edit in review.get("edits", []):
        f = next((f for f in files if f["path"] == edit.get("path") and f["complete"] and not f["redacted"]), None)
        c = next((c for c in supported if c["path"] == edit.get("path") and c["rule"] == "missing_dockerignore"), None)
        # Initial automatic edit scope: adding an observed-missing .dockerignore.
        # Other static risks need packaging/runtime evidence before an exact edit.
        if f and c and edit.get("find") == "" and isinstance(edit.get("replace"), str) and edit["replace"].strip():
            edits.append({"path": f["path"], "base_hash": f["sha256"], "find": "", "replace": edit["replace"], "claim_id": c["claim_id"]})
    return {"claims": supported, "candidates": decisions, "snapshots": files, "edits": edits, "unsupported_files": unsupported,
            "supported_count": len(supported), "contradicted_count": sum(x["verdict"] == "contradicted" for x in decisions),
            "insufficient_count": sum(x["verdict"] == "insufficient" for x in decisions)}


def grounded_suggestions(validation, review):
    """Exact source anchoring is necessary, not sufficient: an independent model must still judge meaning."""
    result = []
    for index, candidate in enumerate(review.get("findings", [])):
        if index >= len(validation["candidates"]) or validation["candidates"][index]["verdict"] != "insufficient": continue
        # Observed facts are rendered by executable checks and code-order notes.
        # Free-form "fact" prose can smuggle unmeasured performance guarantees.
        if candidate.get("claim_kind") == "fact": continue
        text, snippet = candidate.get("text"), candidate.get("snippet")
        if not isinstance(text, str) or not 8 <= len(text) <= 400 or not isinstance(snippet, str) or not 8 <= len(snippet) <= 300: continue
        if contains_dangerous(text) or redact(text, 1000) != text or "[REDACTED]" in text or "[REDACTED]" in snippet: continue
        # A second model can repeat the same semantic mistake. COPY --chown
        # alone does not prove extra recursive ownership layers/performance cost.
        # Such hypotheses need more evidence than this COPY-only source span.
        if candidate.get("claim_kind") != "fact" and "--chown" in snippet:
            parsed = instructions({"snippet": snippet})
            if parsed and all(row[0] == "COPY" for row in parsed): continue
        file = next((f for f in validation["snapshots"] if f["path"] == candidate.get("path") and f["complete"] and f["found"] and not f["redacted"]), None)
        if not file or file["evidence_id"] not in candidate.get("evidence_ids", []): continue
        if file["snippet"].count(snippet) != 1: continue
        start = file["snippet"].index(snippet)
        line = file["snippet"].count("\n", 0, start) + 1
        result.append({"id": f"A{index+1}", "candidate_index": index, "path": file["path"], "line_start": line,
                       "line_end": line + snippet.count("\n"), "snippet": snippet, "text": text,
                       "claim_kind": "fact" if candidate.get("claim_kind") == "fact" else "static_risk",
                       "evidence_ids": [file["evidence_id"]], "sha256": file["sha256"]})
    return result[:3]
