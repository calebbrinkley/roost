"""Settings Roost keeps for each conversation. Hermes has no per-conversation instructions a client can
set, so Roost sends them once, with the next message after they change, between markers that the
browser removes again to show that message as it was typed."""

import sqlite3

INSTRUCTIONS_LIMIT = 4000
# The browser (app.js) strips exactly these lines; change both together.
BEGIN = "[Instructions for this conversation, set by the owner in Roost. Follow them until they change.]"
END = "[End of instructions]"
CLEARED = "The earlier instructions for this conversation no longer apply."


# How hard the model thinks, as Hermes's `config.set reasoning` words. "default" leaves Hermes's own setting.
REASONING = ("default", "none", "low", "medium", "high")


def with_instructions(text: str, instructions: str) -> str:
    return f"{BEGIN}\n{instructions or CLEARED}\n{END}\n\n{text}"


class Chats:
    """Shares the sessions database."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        # unsent: changed since the last message to this conversation, so the next one carries them.
        db.execute("CREATE TABLE IF NOT EXISTS chat_settings (sid TEXT PRIMARY KEY, instructions TEXT NOT NULL, "
                   "unsent INTEGER NOT NULL)")
        # No row: never chosen, so Hermes keeps its own setting. A "default" row: chosen back, so the
        # next message restores Hermes's setting once and the row goes.
        db.execute("CREATE TABLE IF NOT EXISTS chat_reasoning (sid TEXT PRIMARY KEY, level TEXT NOT NULL)")
        db.commit()

    def reasoning(self, sid: str) -> str:
        row = self.db.execute("SELECT level FROM chat_reasoning WHERE sid = ?", (sid,)).fetchone()
        return row[0] if row else "default"

    def reasoning_to_apply(self, sid: str) -> str | None:
        """The level the next message must set in Hermes, or None to leave Hermes's setting alone."""
        row = self.db.execute("SELECT level FROM chat_reasoning WHERE sid = ?", (sid,)).fetchone()
        return row[0] if row else None

    def set_reasoning(self, sid: str, level: str) -> None:
        if level not in REASONING:
            raise ValueError("unknown reasoning level")
        if level == "default" and self.reasoning_to_apply(sid) is None:
            return
        self.db.execute("INSERT INTO chat_reasoning VALUES (?, ?) ON CONFLICT(sid) DO UPDATE SET level = excluded.level",
                        (sid, level))
        self.db.commit()

    def reasoning_applied(self, sid: str, level: str) -> None:
        """Hermes has `level`; a restored default needs no further messages to carry it."""
        if level == "default":
            self.db.execute("DELETE FROM chat_reasoning WHERE sid = ? AND level = 'default'", (sid,))
            self.db.commit()

    def instructions(self, sid: str) -> str:
        row = self.db.execute("SELECT instructions FROM chat_settings WHERE sid = ?", (sid,)).fetchone()
        return row[0] if row else ""

    def set_instructions(self, sid: str, instructions: str) -> None:
        if instructions == self.instructions(sid):
            return
        # Cleared instructions stay as an empty row until the model has been told they're gone.
        self.db.execute("INSERT INTO chat_settings VALUES (?, ?, 1) ON CONFLICT(sid) DO UPDATE "
                        "SET instructions = excluded.instructions, unsent = 1", (sid, instructions))
        self.db.commit()

    def unsent(self, sid: str) -> str | None:
        """Instructions the next message must carry ("" for a clearing), or None."""
        row = self.db.execute("SELECT instructions FROM chat_settings WHERE sid = ? AND unsent = 1", (sid,)).fetchone()
        return row[0] if row else None

    def sent(self, sid: str, instructions: str, stored: str) -> None:
        """`instructions` reached Hermes with a message; Hermes now calls the conversation `stored`."""
        # A change made while that message was on its way is still unsent.
        self.db.execute("UPDATE chat_settings SET unsent = 0 WHERE sid = ? AND instructions = ?", (sid, instructions))
        self.db.execute("DELETE FROM chat_settings WHERE sid = ? AND instructions = '' AND unsent = 0", (sid,))
        self.moved(sid, stored)

    def moved(self, sid: str, stored: str) -> None:
        # Hermes continues a compressed conversation under a new identifier.
        if stored != sid:
            self.db.execute("UPDATE OR REPLACE chat_settings SET sid = ? WHERE sid = ?", (stored, sid))
            self.db.execute("UPDATE OR REPLACE chat_reasoning SET sid = ? WHERE sid = ?", (stored, sid))
        self.db.commit()

    def forget(self, sid: str | None = None) -> None:
        """One conversation's settings, or every conversation's."""
        if sid is None:
            self.db.execute("DELETE FROM chat_settings")
            self.db.execute("DELETE FROM chat_reasoning")
        else:
            self.db.execute("DELETE FROM chat_settings WHERE sid = ?", (sid,))
            self.db.execute("DELETE FROM chat_reasoning WHERE sid = ?", (sid,))
        self.db.commit()
