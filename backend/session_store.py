from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS messages(thread_id TEXT, role TEXT, content TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")

    def history(self, thread_id: str, limit: int = 8, *, aliases=()) -> list[dict[str, str]]:
        ids = sorted({thread_id, *aliases})
        with sqlite3.connect(self.path) as c:
            rows = c.execute(f"SELECT role,content FROM messages WHERE thread_id IN ({','.join('?' for _ in ids)}) ORDER BY rowid DESC LIMIT ?", (*ids, limit)).fetchall()
        return [{"role": r, "content": v} for r, v in reversed(rows)]

    def add(self, thread_id: str, role: str, content: str) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO messages(thread_id,role,content) VALUES(?,?,?)", (thread_id, role, content))

    def export(self, thread_id: str) -> str:
        return json.dumps(self.history(thread_id, 100), ensure_ascii=False)

