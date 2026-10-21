from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


def sign_approval(secret: str, action_id: str, tool: str, args: dict, expires_at: float) -> str:
    payload = json.dumps({"id": action_id, "tool": tool, "args": args, "expires_at": expires_at}, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode()
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_approval(secret: str, token: str, tool: str, args: dict) -> dict:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        if payload["tool"] != tool or payload["args"] != args or payload["expires_at"] <= time.time():
            raise ValueError("expired or mismatched approval")
        if not isinstance(payload["id"], str):
            raise ValueError("invalid action id")
        return payload
    except (ValueError, KeyError, TypeError) as exc:
        raise PermissionError("valid, unexpired approval for this exact action required") from exc


@dataclass
class PendingAction:
    id: str
    thread_id: str
    tool: str
    args: dict
    reason: str
    created_at: float
    expires_at: float
    status: str = "pending"


class ApprovalStore:
    def __init__(self, path: Path | str = ":memory:", scope: str = ""):
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.scope = scope
        self.connection.execute("""CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY, thread_id TEXT, tool TEXT, args TEXT, reason TEXT,
            created_at REAL, expires_at REAL, status TEXT, result TEXT)""")
        self.connection.commit()
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(actions)")}
        if "scope" not in columns:
            self.connection.execute("ALTER TABLE actions ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
            self.connection.commit()

    def create(self, thread_id: str, tool: str, args: dict, reason: str, ttl: int = 300) -> PendingAction:
        now = time.time()
        item = PendingAction(secrets.token_urlsafe(24), thread_id, tool, args, reason, now, now + ttl)
        with self.lock, self.connection:
            self.connection.execute("INSERT INTO actions (id,thread_id,tool,args,reason,created_at,expires_at,status,scope) VALUES(?,?,?,?,?,?,?,?,?)",
                (item.id, thread_id, tool, json.dumps(args), reason, now, item.expires_at, item.status, self.scope))
        return item

    def get(self, action_id: str) -> PendingAction:
        with self.lock:
            row = self.connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise KeyError("approval not found")
        return PendingAction(row["id"], row["thread_id"], row["tool"], json.loads(row["args"]), row["reason"], row["created_at"], row["expires_at"], row["status"])

    def claim(self, action_id: str, status: str) -> PendingAction:
        with self.lock, self.connection:
            changed = self.connection.execute(
                "UPDATE actions SET status=? WHERE id=? AND status='pending' AND expires_at>? AND scope=?",
                (status, action_id, time.time(), self.scope)).rowcount
        item = self.get(action_id)
        if not changed:
            raise ValueError(f"action already {item.status}, expired, or belongs to a different Docker scope")
        return item

    def finish(self, action_id: str, status: str, result: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE actions SET status=?,result=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False), action_id))

    def close(self) -> None:
        self.connection.close()

    def invalidate_pending(self) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE actions SET status='superseded' WHERE scope=? AND status='pending'", (self.scope,))

