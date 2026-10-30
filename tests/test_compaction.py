"""Compaction trigger and summary prompt, without a provider."""

import unittest

from roost import compaction


class CompactionPolicyTests(unittest.TestCase):
    def test_trigger_is_inclusive_at_three_quarters_and_needs_both_sizes(self):
        self.assertTrue(compaction.due(4000, 2999, "abcd"))
        self.assertFalse(compaction.due(4000, 2998, "abcd"))
        self.assertTrue(compaction.due(4000, 3000, ""))
        for context, reported in ((None, 3000), (4000, None), (0, 10), (True, 10)):
            self.assertFalse(compaction.due(context, reported, "x" * 4000))

    def test_summary_request_holds_the_previous_summary_and_only_the_folded_pairs(self):
        first = compaction.messages("", [("hello", "hi")])
        self.assertEqual([m["role"] for m in first], ["system", "user"])
        self.assertIn("untrusted", first[0]["content"])
        self.assertIn("Previous summary: none yet.", first[1]["content"])
        later = compaction.messages("Earlier summary.", [("question", ""), ("more", "answer")])
        self.assertIn("Previous summary:\nEarlier summary.", later[1]["content"])
        self.assertIn("User: question\n\nUser: more\n\nAssistant: answer", later[1]["content"])


if __name__ == "__main__":
    unittest.main()
