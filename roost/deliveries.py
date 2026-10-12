"""Deliveries from Hermes's Roost plugin: job reports and messages the agent sends on its own.
The plugin authenticates with a token Roost generates; Roost keeps the newest deliveries."""

import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

KEEP = 200
TEXT_LIMIT = 48000
# Hermes wraps a job report like this (cron/scheduler_delivery.py, v2026.9.24) unless the job's
# cron.wrap_response is off; the plugin's out-of-process lane has no other way to say which job.
REPORT = re.compile(r"\ACronjob Response: (?P<name>[^\n]*)\n\(job_id: (?P<job>[^)\n]*)\)\n-+\n\n(?P<body>.*?)"
                    r"(?:\n\nTo stop or manage this job, send me a new message[^\n]*)?\Z", re.S)


def parse(text: str, job_id: str | None) -> dict:
    """The report's job name and id when Hermes wrapped it, and the text without the wrapper."""
    match = REPORT.match(text)
    if not match:
        return {"title": "", "job_id": job_id, "text": text}
    return {"title": match["name"].strip(), "job_id": job_id or match["job"].strip() or None, "text": match["body"]}


class Deliveries:
    """Shares the sessions database; the token lives in its own file in the state directory."""

    def __init__(self, db: sqlite3.Connection, token_path: Path):
        self.db, self.token_path = db, token_path
        db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, created REAL NOT NULL, "
                   "job_id TEXT, title TEXT NOT NULL, text TEXT NOT NULL)")
        db.commit()
        if not token_path.exists():
            self.rotate()

    def token(self) -> str:
        return self.token_path.read_text(encoding="utf-8").strip()

    def rotate(self) -> str:
        fresh = secrets.token_urlsafe(32)
        temporary = self.token_path.with_name(self.token_path.name + ".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(fresh + "\n")
        os.replace(temporary, self.token_path)
        return fresh

    def authorized(self, header: str) -> bool:
        scheme, _, value = (header or "").partition(" ")
        return scheme == "Bearer" and bool(value) and hmac.compare_digest(value.encode(), self.token().encode())

    def add(self, text: str, job_id: str | None) -> dict:
        item = {"id": secrets.token_hex(8), "created": time.time(), **parse(text, job_id)}
        self.db.execute("INSERT INTO deliveries VALUES (?, ?, ?, ?, ?)",
                        (item["id"], item["created"], item["job_id"], item["title"], item["text"]))
        self.db.execute("DELETE FROM deliveries WHERE id NOT IN (SELECT id FROM deliveries ORDER BY created DESC LIMIT ?)", (KEEP,))
        self.db.commit()
        return item

    def get(self, delivery_id: str) -> dict | None:
        row = self.db.execute("SELECT id, created, job_id, title, text FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        return dict(zip(("id", "created", "job_id", "title", "text"), row)) if row else None

    def last(self) -> float | None:
        return self.db.execute("SELECT MAX(created) FROM deliveries").fetchone()[0]
