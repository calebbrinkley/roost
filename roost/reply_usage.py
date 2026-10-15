"""Whole-reply measurements received from Hermes's Roost plugin, keyed by durable message ID."""

import math
import sqlite3


FIELDS = {"session_id", "message_id", "input_tokens", "output_tokens", "model_seconds"}


def count(value):
    return value if type(value) is int and 0 <= value <= 2 ** 53 - 1 else None


def seconds(value):
    return value if type(value) in (int, float) and 0 < value < 1e9 and math.isfinite(value) else None


class ReplyUsage:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS reply_usage (sid TEXT NOT NULL, message_id INTEGER NOT NULL, "
                   "input_tokens INTEGER, output_tokens INTEGER, model_seconds REAL, PRIMARY KEY(sid, message_id))")
        db.commit()

    def add(self, data: dict):
        self.db.execute("INSERT OR REPLACE INTO reply_usage VALUES (?, ?, ?, ?, ?)",
                        tuple(data[key] for key in ("session_id", "message_id", "input_tokens", "output_tokens", "model_seconds")))
        self.db.commit()

    def attach(self, sid: str, rows: list):
        measured = {row[0]: dict(zip(("input_tokens", "output_tokens", "model_seconds"), row[1:]))
                    for row in self.db.execute("SELECT message_id, input_tokens, output_tokens, model_seconds "
                                              "FROM reply_usage WHERE sid = ?", (sid,))}
        for row in rows:
            if isinstance(row, dict) and row.get("role") == "assistant" and type(row.get("id")) is int and row["id"] in measured:
                row["roost_usage"] = measured[row["id"]]

    def forget(self, sid: str):
        self.db.execute("DELETE FROM reply_usage WHERE sid = ?", (sid,))
        self.db.commit()
