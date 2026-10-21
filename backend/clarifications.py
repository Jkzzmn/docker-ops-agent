"""Small durable question ledger, independent of mutation approvals and graph checkpoints."""
import json
from pathlib import Path
import secrets
import re
import sqlite3
import threading
import time

from .sanitize import redact
from .planning import unsafe_execution_request


def question_schema(value: dict) -> dict:
    if not isinstance(value, dict): raise ValueError("clarification must be an object")
    for key in ("question", "reason"):
        if not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 500:
            raise ValueError("clarification needs a bounded question and reason")
    options = value.get("options", [])
    if not isinstance(options, list) or len(options) > 3 or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in options):
        raise ValueError("clarification supports up to three short choices or free text only")
    if len(set(options)) != len(options): raise ValueError("duplicate choices")
    text = " ".join([value["question"], *options])
    if re.search(r"비밀번호|패스워드|api\s*키|api[_-]?key|토큰|token|password|credential|\.env.*(?:내용|원문)", text, re.I) or any(unsafe_execution_request(x) for x in [value["question"], *options]):
        raise ValueError("questions must not solicit secrets or offer unsafe execution")
    return {"question": redact(value["question"], 500), "reason": redact(value["reason"], 500), "options": [redact(x, 200) for x in options]}


def unresolved_question() -> dict:
    return {"question": "어떤 대상을 어떤 방식으로 확인할까요? 아래에서 고르거나 원하는 작업과 서비스명을 직접 알려주세요.",
            "reason": "앞선 대화로도 요청의 대상·작업을 확정할 수 없어 임의로 진단하거나 변경하지 않습니다.",
            "options": ["현재 프로젝트의 Dockerfile·Compose 설정을 읽고 검토", "현재 프로젝트의 서비스 목록과 상태만 조회", "Docker 개념과 사용 방법 학습"]}


class ClarificationStore:
    def __init__(self, path: Path | str, scope: str):
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.scope, self.lock = scope, threading.Lock()
        self.connection.execute("""CREATE TABLE IF NOT EXISTS clarifications (
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, scope TEXT NOT NULL, query TEXT NOT NULL,
            question TEXT NOT NULL, rounds INTEGER NOT NULL, expires_at REAL NOT NULL,
            status TEXT NOT NULL, answer TEXT)""")
        self.connection.commit()

    def create(self, thread_id: str, query: str, question: dict, rounds: int, ttl: int = 900) -> dict:
        value = question_schema(question)
        identity, expiry = secrets.token_urlsafe(24), time.time() + ttl
        with self.lock, self.connection:
            self.connection.execute("UPDATE clarifications SET status='superseded' WHERE thread_id=? AND scope=? AND status='pending'", (thread_id, self.scope))
            self.connection.execute("INSERT INTO clarifications VALUES(?,?,?,?,?,?,?,? ,NULL)",
                (identity, thread_id, self.scope, query, json.dumps(value), rounds, expiry, "pending"))
        return value | {"id": identity, "expires_at": expiry, "status": "pending", "original_query": query}

    def pending(self, thread_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM clarifications WHERE thread_id=? AND scope=? AND status='pending' AND expires_at>? ORDER BY rowid DESC LIMIT 1", (thread_id, self.scope, time.time())).fetchone()
        return self.public(row) if row else None

    @staticmethod
    def public(row) -> dict:
        return json.loads(row["question"]) | {"id": row["id"], "expires_at": row["expires_at"], "status": row["status"], "original_query": row["query"]}

    def supersede(self, thread_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE clarifications SET status='superseded' WHERE thread_id=? AND scope=? AND status='pending'", (thread_id, self.scope))

    def finish(self, identity: str, thread_id: str, *, choice: int | None = None, answer: str | None = None, cancel: bool = False) -> dict:
        with self.lock, self.connection:
            row = self.connection.execute("SELECT * FROM clarifications WHERE id=? AND thread_id=? AND scope=?", (identity, thread_id, self.scope)).fetchone()
            if row is None: raise KeyError("question not found for this conversation/target")
            if row["status"] != "pending" or row["expires_at"] <= time.time(): raise ValueError("question already answered, cancelled, superseded or expired")
            question = json.loads(row["question"])
            if not cancel and (choice is None) == (answer is None): raise ValueError("choose one answer format")
            if cancel: text = ""
            elif choice is not None:
                if type(choice) is not int or not 1 <= choice <= len(question["options"]): raise ValueError("invalid choice")
                text = question["options"][choice-1]
            else:
                text = (answer or "").strip()
                # Typing 1/2/3 (or 1번) is equivalent to clicking the numbered option.
                number = text.removesuffix("번").strip()
                if number in {"1", "2", "3"}:
                    index = int(number)
                    if index > len(question["options"]): raise ValueError("invalid choice")
                    text = question["options"][index-1]
                if not text or len(text) > 1000: raise ValueError("answer must contain 1–1000 characters")
            changed = self.connection.execute("UPDATE clarifications SET status=?,answer=? WHERE id=? AND status='pending' AND expires_at>?", ("cancelled" if cancel else "answered", text, identity, time.time())).rowcount
            if not changed: raise ValueError("question is no longer pending")
            return {"query": row["query"], "question": question, "answer": text, "rounds": row["rounds"], "status": "cancelled" if cancel else "answered"}

    def close(self): self.connection.close()

    def invalidate_pending(self):
        with self.lock, self.connection:
            self.connection.execute("UPDATE clarifications SET status='superseded' WHERE scope=? AND status='pending'", (self.scope,))
