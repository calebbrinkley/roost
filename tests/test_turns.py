"""The working rule: the same answer for the interface and for notifications."""

import unittest
from types import SimpleNamespace

from roost import turns

NOW = 1_800_000_000.0


def row(role, at, **extra):
    return {"role": role, "content": "text", "timestamp": at, **extra}


class WorkingTests(unittest.TestCase):
    def test_the_transcript_alone_says_whether_a_turn_started_elsewhere_is_going(self):
        for rows, expected in (
                ([], False),
                ([row("user", NOW - 5)], True),
                ([row("user", NOW - 9), row("tool", NOW - 5)], True),
                ([row("assistant", NOW - 5, tool_calls=[{"id": "a"}])], True),
                ([row("user", NOW - 9), row("assistant", NOW - 5)], False),
                ([row("user", NOW - 9), row("assistant", NOW - 5, finish_reason="error", tool_calls=[{"id": "a"}])], False),
                ([row("user", NOW - turns.QUIET_SECONDS)], False),
                ([{"role": "user", "content": "no time"}], False)):
            with self.subTest(rows=rows):
                self.assertIs(turns.working(rows, now=NOW), expected)

    def test_a_live_turn_is_working_until_it_is_done(self):
        finished = [row("user", NOW - 9), row("assistant", NOW - 5)]
        self.assertTrue(turns.working(finished, turn=SimpleNamespace(done=False), now=NOW))
        self.assertFalse(turns.working(finished, turn=SimpleNamespace(done=True), now=NOW))

    def test_a_sent_message_hermes_has_not_saved_yet_is_working(self):
        earlier = [row("user", NOW - 60), row("assistant", NOW - 50)]
        self.assertTrue(turns.working(earlier, since=NOW - 2, now=NOW), "accepted, nothing saved since")
        saved = earlier + [row("user", NOW - 1)]
        self.assertTrue(turns.working(saved, since=NOW - 2, now=NOW))
        answered = saved + [row("assistant", NOW)]
        self.assertFalse(turns.working(answered, since=NOW - 2, now=NOW))
        failed = saved + [row("assistant", NOW, finish_reason="agent_error")]
        self.assertFalse(turns.working(failed, since=NOW - 2, now=NOW))

    def test_the_finished_reply_is_the_last_answer_written_after_the_message(self):
        self.assertEqual(turns.finished_reply([row("user", NOW - 1), row("assistant", NOW)], NOW - 2), "text")
        self.assertIsNone(turns.finished_reply([row("assistant", NOW - 9)], NOW - 2), "an earlier answer")
        self.assertIsNone(turns.finished_reply([row("assistant", NOW, tool_calls=[{"id": "a"}])], NOW - 2))
        self.assertIsNone(turns.finished_reply([{"role": "assistant", "content": " ", "timestamp": NOW}], NOW - 2))

    def test_after_is_the_newest_row_saved_before_the_message(self):
        rows = [{"id": 1, "timestamp": NOW - 60}, {"id": 2, "timestamp": NOW - 50}, {"id": 3, "timestamp": NOW}, {"id": True}]
        self.assertEqual(turns.after(rows, NOW - 2), 2)
        self.assertEqual(turns.after([], NOW), 0)


if __name__ == "__main__":
    unittest.main()
