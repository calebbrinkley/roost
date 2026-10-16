"""Roost's settings file at its interface: load and update."""

import os
import tempfile
import unittest
from pathlib import Path

from roost import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "settings.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_defaults_survive_a_missing_or_damaged_file(self):
        default = {"agent_name": "", "accent": "forest", "time_zone": ""}
        self.assertEqual(settings.load(self.path), default)
        for content in ("{not json", "[]", '{"agent_name": 5, "accent": "neon", "time_zone": "../etc"}'):
            self.path.write_text(content)
            self.assertEqual(settings.load(self.path), default, content)

    def test_refused_changes_name_the_problem_and_save_nothing(self):
        for changes in ({"agent_name": "x" * 41}, {"agent_name": "line\nbreak"}, {"accent": "neon"}, {"accent": "#12345g"},
                        {"accent": "#fff"}, {"accent": 5}, {"theme": "dark"}, {}, {"time_zone": "../etc"},
                        {"time_zone": "America/" + "x" * 70}, {"time_zone": 5}, {"accent": "rose", "time_zone": 5}):
            with self.subTest(changes=changes), self.assertRaises(settings.SettingsError):
                settings.update(self.path, changes)
        self.assertFalse(self.path.exists())

    def test_changes_are_cleaned_saved_privately_and_kept(self):
        saved = settings.update(self.path, {"agent_name": "  Robin  ", "accent": "rose"})
        self.assertEqual(saved, {"agent_name": "Robin", "accent": "rose", "time_zone": ""})
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        settings.update(self.path, {"accent": "#A1B2C3", "time_zone": "America/New_York"})
        self.assertEqual(settings.load(self.path), {"agent_name": "Robin", "accent": "#a1b2c3", "time_zone": "America/New_York"})
        settings.update(self.path, {"agent_name": "", "time_zone": ""})
        self.assertEqual(settings.load(self.path), {"agent_name": "", "accent": "#a1b2c3", "time_zone": ""})


if __name__ == "__main__":
    unittest.main()
