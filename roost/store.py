"""Native chat state. Each public write commits a short transaction before it returns."""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

Thinking = Literal["off", "low", "medium", "high"]
TurnState = Literal["queued", "running", "completed", "failed", "interrupted", "cancelled"]
STATES = ("queued", "running", "completed", "failed", "interrupted", "cancelled")
TERMINAL = frozenset(STATES[2:])


class StoreError(ValueError):
    """A refused chat write, with no changes committed."""


class RequestConflict(StoreError):
    """The request ID already belongs to a different payload."""


class RevisionConflict(StoreError):
    """The caller's snapshot is no longer current."""


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    title_source: str
    model: str | None
    thinking: Thinking
    instructions: str
    no_web: bool
    keep_local: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Turn:
    id: str
    conversation_id: str
    ordinal: int
    request_id: str
    fingerprint: str
    state: TurnState
    model: str
    settings: dict
    revision: int
    error: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Message:
    id: int
    conversation_id: str
    turn_id: str
    role: str
    content: str
    reasoning: str
    state: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Acceptance:
    turn_id: str
    conversation_id: str
    message_id: int
    assistant_message_id: int
    state: str = "queued"


@dataclass(frozen=True)
class Step:
    turn_id: str
    ordinal: int
    kind: str
    payload: dict
    tool_call_id: str | None
    usage: dict | None
    duration: float | None


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def payload_fingerprint(payload: dict) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _chat_up(db: sqlite3.Connection) -> None:
    # Individual statements keep schema and version in the same transaction (no executescript).
    statements = (
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL,
            title_source TEXT NOT NULL CHECK(title_source IN ('auto','user')),
            model TEXT, thinking TEXT NOT NULL CHECK(thinking IN ('off','low','medium','high')),
            instructions TEXT NOT NULL, no_web INTEGER NOT NULL CHECK(no_web IN (0,1)),
            keep_local INTEGER NOT NULL CHECK(keep_local IN (0,1)),
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
        """CREATE TABLE turns (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 1), request_id TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL
                CHECK(state IN ('queued','running','completed','failed','interrupted','cancelled')),
            model TEXT NOT NULL, settings TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
            error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(conversation_id, ordinal), UNIQUE(id, conversation_id))""",
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            turn_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL, reasoning TEXT NOT NULL, state TEXT NOT NULL
                CHECK(state IN ('queued','running','completed','failed','interrupted','cancelled')),
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            FOREIGN KEY(turn_id, conversation_id) REFERENCES turns(id, conversation_id) ON DELETE CASCADE,
            UNIQUE(turn_id, role))""",
        "CREATE INDEX messages_conversation ON messages(conversation_id, id)",
        """CREATE TABLE turn_steps (
            turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 1), kind TEXT NOT NULL, payload TEXT NOT NULL,
            tool_call_id TEXT, usage TEXT, duration REAL, PRIMARY KEY(turn_id, ordinal))""",
    )
    for statement in statements:
        db.execute(statement)


def _chat_down(db: sqlite3.Connection) -> None:
    # A rollback must never silently discard native history.
    if db.execute("SELECT 1 FROM conversations LIMIT 1").fetchone():
        raise StoreError("Cannot roll back a nonempty chat store. Preserve its data first.")
    for table in ("turn_steps", "messages", "turns", "conversations"):
        db.execute(f"DROP TABLE {table}")


MIGRATIONS = ((_chat_up, _chat_down),)


class Store:
    """One owner's native history, separate from the Hermes/auth database. Use on its owning thread."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create privately before SQLite opens its journal files.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.migrate()
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def _write(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def migrate(self, target: int = len(MIGRATIONS)) -> None:
        """Apply ordered migrations, or reverse an empty store to an earlier version."""
        if type(target) is not int or not 0 <= target <= len(MIGRATIONS):
            raise StoreError("Unknown schema version.")
        with self._write():
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO schema_version VALUES (1, 0)")
            version = self.db.execute("SELECT version FROM schema_version WHERE id=1").fetchone()[0]
            if not 0 <= version <= len(MIGRATIONS):
                raise StoreError("Chat store was created by a newer Roost version.")
            while version != target:
                if version < target:
                    MIGRATIONS[version][0](self.db)
                    version += 1
                else:
                    MIGRATIONS[version - 1][1](self.db)
                    version -= 1
                self.db.execute("UPDATE schema_version SET version=? WHERE id=1", (version,))

    def close(self) -> None:
        self.db.close()

    def create_conversation(self, title: str = "", *, title_source: str = "auto", model: str | None = None,
                            thinking: Thinking = "off", instructions: str = "", no_web: bool = False,
                            keep_local: bool = False) -> Conversation:
        if (not isinstance(title, str) or len(title) > 200 or title_source not in ("auto", "user")
                or (model is not None and (not isinstance(model, str) or not model or len(model) > 256))
                or thinking not in ("off", "low", "medium", "high")
                or not isinstance(instructions, str) or len(instructions) > 4000
                or type(no_web) is not bool or type(keep_local) is not bool):
            raise StoreError("Invalid conversation settings.")
        cid, now = str(uuid4()), time.time()
        with self._write():
            self.db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (cid, title, title_source, model, thinking, instructions, no_web, keep_local, now, now))
        return self.get_conversation(cid)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self.db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            return None
        return Conversation(**{**dict(row), "no_web": bool(row["no_web"]), "keep_local": bool(row["keep_local"])})

    def list_conversations(self, *, limit: int = 50, offset: int = 0) -> list[Conversation]:
        if not 1 <= limit <= 500 or offset < 0:
            raise StoreError("Invalid conversation page.")
        rows = self.db.execute("SELECT id FROM conversations ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", (limit, offset))
        return [self.get_conversation(row[0]) for row in rows]

    def delete_conversation(self, conversation_id: str) -> None:
        with self._write():
            self.db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def accept_turn(self, conversation_id: str, request_id: str, content: str, *, model: str,
                    settings: dict | None = None) -> Acceptance:
        """Atomically reserve a turn and its user/assistant pair; retries return the original IDs."""
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 128
                or not isinstance(content, str) or not content.strip() or len(content) > 200000
                or not isinstance(model, str) or not model or len(model) > 256
                or (settings is not None and not isinstance(settings, dict))):
            raise StoreError("Invalid turn payload.")
        with self._write():
            conversation = self.get_conversation(conversation_id)
            if not conversation:
                raise StoreError("Conversation does not exist.")
            snapshot = {"thinking": conversation.thinking, "instructions": conversation.instructions,
                        "no_web": conversation.no_web, "keep_local": conversation.keep_local, **(settings or {})}
            fingerprint = payload_fingerprint({"conversation_id": conversation_id, "content": content,
                                               "model": model, "settings": snapshot})
            previous = self.db.execute("SELECT id, fingerprint FROM turns WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise RequestConflict("Request ID was already used for a different payload.")
                return self._acceptance(previous["id"])
            tid, now = str(uuid4()), time.time()
            ordinal = self.db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM turns WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            self.db.execute("INSERT INTO turns VALUES (?,?,?,?,?,'queued',?,?,0,NULL,?,?)",
                            (tid, conversation_id, ordinal, request_id, fingerprint, model, _json(snapshot), now, now))
            for role, text, state in (("user", content, "completed"), ("assistant", "", "queued")):
                self.db.execute("INSERT INTO messages (conversation_id,turn_id,role,content,reasoning,state,created_at,updated_at) VALUES (?,?,?,?,'',?,?,?)",
                                (conversation_id, tid, role, text, state, now, now))
            self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            return self._acceptance(tid)

    def _acceptance(self, turn_id: str) -> Acceptance:
        messages = self.db.execute("SELECT id, conversation_id, role FROM messages WHERE turn_id=? ORDER BY id", (turn_id,)).fetchall()
        return Acceptance(turn_id, messages[0]["conversation_id"], messages[0]["id"], messages[1]["id"])

    def get_turn(self, turn_id: str) -> Turn | None:
        row = self.db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return Turn(**{**dict(row), "settings": json.loads(row["settings"])}) if row else None

    def list_turns(self, conversation_id: str) -> list[Turn]:
        rows = self.db.execute("SELECT id FROM turns WHERE conversation_id=? ORDER BY ordinal", (conversation_id,))
        return [self.get_turn(row[0]) for row in rows]

    def get_message(self, message_id: int) -> Message | None:
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return Message(**dict(row)) if row else None

    def list_messages(self, conversation_id: str, *, after: int = 0, limit: int = 500) -> list[Message]:
        if after < 0 or not 1 <= limit <= 500:
            raise StoreError("Invalid message page.")
        return [Message(**dict(row)) for row in self.db.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND id>? ORDER BY id LIMIT ?", (conversation_id, after, limit))]

    def _current(self, turn_id: str, expected_revision: int | None) -> Turn:
        turn = self.get_turn(turn_id)
        if not turn:
            raise StoreError("Turn does not exist.")
        if expected_revision is not None and turn.revision != expected_revision:
            raise RevisionConflict("Turn changed since the last snapshot.")
        return turn

    def checkpoint_assistant(self, turn_id: str, content: str, reasoning: str = "", *,
                             append: bool = False, expected_revision: int | None = None) -> int:
        """Save a full checkpoint, or append deltas, and return the committed revision."""
        if not isinstance(content, str) or not isinstance(reasoning, str):
            raise StoreError("Assistant content and reasoning must be text.")
        with self._write():
            turn = self._current(turn_id, expected_revision)
            if turn.state in TERMINAL:
                raise StoreError("Turn is already settled.")
            now = time.time()
            expression = "content || ?, reasoning=reasoning || ?" if append else "?, reasoning=?"
            self.db.execute(f"UPDATE messages SET content={expression}, updated_at=? WHERE turn_id=? AND role='assistant'",
                            (content, reasoning, now, turn_id))
            self.db.execute("UPDATE turns SET revision=revision+1, updated_at=? WHERE id=?", (now, turn_id))
            self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, turn.conversation_id))
            return turn.revision + 1

    def append_assistant(self, turn_id: str, content: str = "", reasoning: str = "", *,
                         expected_revision: int | None = None) -> int:
        return self.checkpoint_assistant(turn_id, content, reasoning, append=True, expected_revision=expected_revision)

    def set_turn_state(self, turn_id: str, state: TurnState, *, error: str | None = None,
                       expected_revision: int | None = None) -> int:
        if state not in STATES or (error is not None and (not isinstance(error, str) or len(error) > 2000)):
            raise StoreError("Invalid turn state or error.")
        with self._write():
            turn = self._current(turn_id, expected_revision)
            if turn.state == state and turn.error == error:
                return turn.revision
            if turn.state in TERMINAL or state == "queued":
                raise StoreError("Invalid turn state transition.")
            now = time.time()
            self.db.execute("UPDATE turns SET state=?, error=?, revision=revision+1, updated_at=? WHERE id=?", (state, error, now, turn_id))
            self.db.execute("UPDATE messages SET state=?, updated_at=? WHERE turn_id=? AND role='assistant'", (state, now, turn_id))
            self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, turn.conversation_id))
            return turn.revision + 1

    def append_step(self, turn_id: str, kind: str, payload: dict, *, tool_call_id: str | None = None,
                    usage: dict | None = None, duration: float | None = None) -> Step:
        if not isinstance(kind, str) or not kind or not isinstance(payload, dict) or (usage is not None and not isinstance(usage, dict)):
            raise StoreError("Invalid turn step.")
        with self._write():
            self._current(turn_id, None)
            ordinal = self.db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM turn_steps WHERE turn_id=?", (turn_id,)).fetchone()[0]
            self.db.execute("INSERT INTO turn_steps VALUES (?,?,?,?,?,?,?)",
                            (turn_id, ordinal, kind, _json(payload), tool_call_id, _json(usage) if usage is not None else None, duration))
        return Step(turn_id, ordinal, kind, payload, tool_call_id, usage, duration)

    def list_steps(self, turn_id: str) -> list[Step]:
        return [Step(**{**dict(row), "payload": json.loads(row["payload"]),
                        "usage": json.loads(row["usage"]) if row["usage"] is not None else None})
                for row in self.db.execute("SELECT * FROM turn_steps WHERE turn_id=? ORDER BY ordinal", (turn_id,))]
