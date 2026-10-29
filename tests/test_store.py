"""Native history migrations, durable acceptance and checkpoint transactions."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from roost.auth import Sessions
from roost.passkeys import Passkeys
from roost.push import Push
from roost.store import RequestConflict, RevisionConflict, Store, StoreError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "native.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_empty_migration_rerun_reverse_and_forward(self):
        self.assertEqual(self.store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.store.migrate()
        self.assertEqual(self.store.db.execute("SELECT version FROM schema_version").fetchone()[0], 1)
        self.store.migrate(0)
        self.assertFalse(self.store.db.execute("SELECT name FROM sqlite_master WHERE name='conversations'").fetchone())
        self.store.migrate()
        self.assertEqual(self.store.list_conversations(), [])

    def test_rollback_refuses_history_and_future_schema_is_not_modified(self):
        conversation = self.store.create_conversation("Keep me")
        with self.assertRaises(StoreError):
            self.store.migrate(0)
        self.assertEqual(self.store.get_conversation(conversation.id), conversation)
        self.store.db.execute("UPDATE schema_version SET version=100")
        with self.assertRaises(StoreError):
            Store(self.path)
        self.assertEqual(self.store.db.execute("SELECT version FROM schema_version").fetchone()[0], 100)

    def test_acceptance_is_durable_and_reserves_ordered_message_pairs(self):
        conversation = self.store.create_conversation("First", model="example-model", thinking="high", no_web=True)
        first = self.store.accept_turn(conversation.id, "request-one", "Hello", model="example-model")
        second = self.store.accept_turn(conversation.id, "request-two", "Again", model="example-model")
        self.store.set_turn_state(first.turn_id, "completed")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.accept_turn(conversation.id, "request-one", "Hello", model="example-model"), first)
        messages = self.store.list_messages(conversation.id)
        self.assertEqual([m.role for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertLess(first.assistant_message_id, second.message_id)
        self.assertEqual([t.ordinal for t in self.store.list_turns(conversation.id)], [1, 2])
        self.assertEqual(self.store.get_turn(first.turn_id).settings["thinking"], "high")
        self.assertTrue(self.store.get_turn(first.turn_id).settings["no_web"])
        self.assertEqual(self.store.list_conversations(), [self.store.get_conversation(conversation.id)])

    def test_request_id_covers_every_payload_field_and_conversation(self):
        one, two = self.store.create_conversation(), self.store.create_conversation()
        first = self.store.accept_turn(one.id, "same", "Hello", model="example-model", settings={"nested": {"a": 1, "b": 2}})
        self.assertEqual(self.store.accept_turn(one.id, "same", "Hello", model="example-model", settings={"nested": {"b": 2, "a": 1}}), first)
        for cid, content, model, settings in ((two.id, "Hello", "example-model", {}), (one.id, "Changed", "example-model", {}),
                                               (one.id, "Hello", "other-model", {}), (one.id, "Hello", "example-model", {"thinking": "off"})):
            with self.subTest(cid=cid, content=content, model=model), self.assertRaises(RequestConflict):
                self.store.accept_turn(cid, "same", content, model=model, settings=settings)
        self.assertEqual(len(self.store.list_turns(one.id)), 1)
        self.assertEqual(self.store.list_turns(two.id), [])

    def test_checkpoint_revision_compare_and_terminal_state(self):
        conversation = self.store.create_conversation()
        accepted = self.store.accept_turn(conversation.id, "request", "Hello", model="example-model")
        tid = accepted.turn_id
        self.assertEqual(self.store.set_turn_state(tid, "running", expected_revision=0), 1)
        self.assertEqual(self.store.append_assistant(tid, "Partial", "Thinking", expected_revision=1), 2)
        with self.assertRaises(RevisionConflict):
            self.store.checkpoint_assistant(tid, "Stale", expected_revision=1)
        self.assertEqual(self.store.append_assistant(tid, " answer", " more"), 3)
        message = self.store.get_message(accepted.assistant_message_id)
        self.assertEqual((message.content, message.reasoning), ("Partial answer", "Thinking more"))
        self.assertEqual(self.store.checkpoint_assistant(tid, "Final", "Thoughts", expected_revision=3), 4)
        self.assertEqual(self.store.set_turn_state(tid, "interrupted", error="Generation stopped", expected_revision=4), 5)
        self.assertEqual(self.store.set_turn_state(tid, "interrupted", error="Generation stopped"), 5)
        with self.assertRaises(StoreError):
            self.store.append_assistant(tid, "Late callback")
        with self.assertRaises(StoreError):
            self.store.set_turn_state(tid, "running")
        self.assertEqual(self.store.get_message(accepted.assistant_message_id).state, "interrupted")
        self.assertEqual(self.store.get_message(accepted.message_id).state, "completed")

    def test_acceptance_rolls_back_on_partial_failure(self):
        conversation = self.store.create_conversation()
        self.store.db.execute("CREATE TRIGGER reject_assistant BEFORE INSERT ON messages WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.accept_turn(conversation.id, "request", "Hello", model="example-model")
        self.assertEqual(self.store.list_turns(conversation.id), [])
        self.assertEqual(self.store.list_messages(conversation.id), [])
        self.store.db.execute("DROP TRIGGER reject_assistant")
        self.store.accept_turn(conversation.id, "request", "Hello", model="example-model")

    def test_delete_cascades_steps_turns_messages_and_rejects_late_writes(self):
        conversation = self.store.create_conversation()
        accepted = self.store.accept_turn(conversation.id, "request", "Hello", model="example-model")
        step = self.store.append_step(accepted.turn_id, "tool", {"result": "sample"}, tool_call_id="call-one", usage={"total_tokens": 5}, duration=0.2)
        self.assertEqual(self.store.list_steps(accepted.turn_id), [step])
        other = self.store.create_conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE messages SET conversation_id=? WHERE id=?", (other.id, accepted.message_id))
        self.store.delete_conversation(conversation.id)
        for table in ("turns", "messages", "turn_steps"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        with self.assertRaises(StoreError):
            self.store.append_assistant(accepted.turn_id, "Late")
        self.assertIsNone(self.store.get_turn(accepted.turn_id))
        self.assertIsNone(self.store.get_message(accepted.message_id))

    def test_separate_connections_serialize_acceptance_and_ordinals(self):
        conversation = self.store.create_conversation()
        def accept(request_id):
            store = Store(self.path)
            try:
                return store.accept_turn(conversation.id, request_id, "Hello", model="example-model")
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(accept, ["same"] * 8))
            self.assertTrue(all(result == results[0] for result in results))
            list(pool.map(accept, [f"request-{index}" for index in range(8)]))
        self.assertEqual([t.ordinal for t in self.store.list_turns(conversation.id)], list(range(1, 10)))

    def test_invalid_input_saves_nothing(self):
        with self.assertRaises(StoreError):
            self.store.create_conversation(thinking="default")
        conversation = self.store.create_conversation()
        for request, content, model in (("", "Hello", "example-model"), ("one", "", "example-model"), ("one", "Hello", "")):
            with self.assertRaises(StoreError):
                self.store.accept_turn(conversation.id, request, content, model=model)
        self.assertEqual(self.store.list_turns(conversation.id), [])

    def test_native_migrations_leave_existing_owner_state_unchanged(self):
        state = Path(self.directory.name)
        sessions = Sessions(state / "sessions.sqlite3")
        try:
            token = sessions.issue()
            passkeys = Passkeys(sessions.db, "https://roost.example.invalid", "roost.example.invalid")
            push = Push(sessions.db, state / "vapid.pem", "https://roost.example.invalid")
            sessions.db.execute("INSERT INTO passkeys VALUES ('example-passkey', ?, -7, 0, 'Example', 1, NULL)", (b"example-public-key",))
            sessions.db.execute("INSERT INTO push_subscriptions VALUES ('example-device', 'https://push.example.invalid', ?, ?, 'Example', 1, NULL)",
                                (b"example-public-key", b"example-auth"))
            sessions.db.commit()
            (state / "settings.json").write_text('{"accent":"rose"}')
            (state / "password.hash").write_text("example-password-hash")
            before = {name: (state / name).read_bytes() for name in ("sessions.sqlite3", "vapid.pem", "settings.json", "password.hash")}
            self.store.migrate(0)
            self.store.migrate()
            self.assertEqual(before, {name: (state / name).read_bytes() for name in before})
            self.assertTrue(sessions.valid(token))
            self.assertEqual(passkeys.count(), 1)
            self.assertEqual(len(push.list()), 1)
        finally:
            sessions.close()
