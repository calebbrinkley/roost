"""Measurements remain attached to the exact durable assistant identity across restarts."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from roost.reply_usage import ReplyUsage


class ReplyUsageTests(unittest.TestCase):
    def test_persistence_session_isolation_zero_counts_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            db = sqlite3.connect(path)
            store = ReplyUsage(db)
            body = {"session_id": "one", "message_id": 2, "input_tokens": 0, "output_tokens": 0, "model_seconds": 2}
            store.add(body)
            db.close()
            db = sqlite3.connect(path)
            store = ReplyUsage(db)
            rows = [{"role": "assistant", "id": 2}, {"role": "user", "id": 2}, {"role": "assistant", "id": 3}]
            store.attach("other", rows)
            self.assertTrue(all("roost_usage" not in row for row in rows))
            store.attach("one", rows)
            self.assertEqual(rows[0]["roost_usage"], {"input_tokens": 0, "output_tokens": 0, "model_seconds": 2})
            self.assertNotIn("roost_usage", rows[1])
            self.assertNotIn("roost_usage", rows[2])
            store.forget("one")
            fresh = [{"role": "assistant", "id": 2}]
            store.attach("one", fresh)
            self.assertNotIn("roost_usage", fresh[0])
            db.close()
