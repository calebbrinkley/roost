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
        default = {"agent_name": "", "accent": "forest", "time_zone": "", "backend": "hermes", "search_url": ""}
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

    def test_search_url_is_fixed_validated_and_can_be_disabled(self):
        saved = settings.update(self.path, {'search_url': 'http://search.example.org/'})
        self.assertEqual(saved['search_url'], 'http://search.example.org')
        for value in (None, [], 'ftp://example.org', 'http://example.org?q=x', 'https://REPLACE_USER:REPLACE_PASSWORD@example.org'):
            with self.assertRaises(settings.SettingsError):
                settings.update(self.path, {'search_url': value})
        self.assertEqual(settings.load(self.path)['search_url'], 'http://search.example.org')
        self.assertEqual(settings.update(self.path, {'search_url': ''})['search_url'], '')

    def test_backend_is_explicit_validated_and_persisted(self):
        settings.update(self.path, {"backend": "native"})
        self.assertEqual(settings.load(self.path)["backend"], "native")
        for value in (None, "other", True, []):
            with self.assertRaises(settings.SettingsError):
                settings.update(self.path, {"backend": value})
        self.assertEqual(settings.load(self.path)["backend"], "native")

    def test_changes_are_cleaned_saved_privately_and_kept(self):
        saved = settings.update(self.path, {"agent_name": "  Robin  ", "accent": "rose"})
        self.assertEqual(saved, {"agent_name": "Robin", "accent": "rose", "time_zone": "", "backend": "hermes", "search_url": ""})
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        settings.update(self.path, {"accent": "#A1B2C3", "time_zone": "America/New_York"})
        self.assertEqual(settings.load(self.path), {"agent_name": "Robin", "accent": "#a1b2c3", "time_zone": "America/New_York", "backend": "hermes", "search_url": ""})
        settings.update(self.path, {"agent_name": "", "time_zone": ""})
        self.assertEqual(settings.load(self.path), {"agent_name": "", "accent": "#a1b2c3", "time_zone": "", "backend": "hermes", "search_url": ""})


if __name__ == "__main__":
    unittest.main()
