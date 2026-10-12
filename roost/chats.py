"""Settings Roost keeps for each conversation. Hermes has no per-conversation instructions a client can
set, so Roost sends them once, with the next message after they change, between markers that the
browser removes again to show that message as it was typed."""

import sqlite3

INSTRUCTIONS_LIMIT = 4000
# The browser (app.js) strips exactly these lines; change both together.
BEGIN = "[Instructions for this conversation, set by the owner in Roost. Follow them until they change.]"
END = "[End of instructions]"
CLEARED = "The earlier instructions for this conversation no longer apply."


def with_instructions(text: str, instructions: str) -> str:
    return f"{BEGIN}\n{instructions or CLEARED}\n{END}\n\n{text}"


class Chats:
    """Shares the sessions database."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        # unsent: changed since the last message to this conversation, so the next one carries them.
        db.execute("CREATE TABLE IF NOT EXISTS chat_settings (sid TEXT PRIMARY KEY, instructions TEXT NOT NULL, "
                   "unsent INTEGER NOT NULL)")
        db.commit()

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
        self.db.commit()

    def forget(self, sid: str | None = None) -> None:
        """One conversation's settings, or every conversation's."""
        if sid is None:
            self.db.execute("DELETE FROM chat_settings")
        else:
            self.db.execute("DELETE FROM chat_settings WHERE sid = ?", (sid,))
        self.db.commit()
