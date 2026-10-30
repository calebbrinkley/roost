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
        default = {"accent": "forest", "time_zone": "", "search_url": "", "use_fallback": False}
        self.assertEqual(settings.load(self.path), default)
        for content in ("{not json", "[]", '{"agent_name": 5, "accent": "neon", "time_zone": "../etc"}'):
            self.path.write_text(content)
            self.assertEqual(settings.load(self.path), default, content)

    def test_refused_changes_name_the_problem_and_save_nothing(self):
        for changes in ({"accent": "neon"}, {"accent": "#12345g"},
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

    def test_use_fallback_is_a_strict_boolean_and_off_by_default(self):
        self.assertIs(settings.load(self.path)["use_fallback"], False)
        self.assertIs(settings.update(self.path, {"use_fallback": True})["use_fallback"], True)
        for value in (None, "yes", 1, []):
            with self.assertRaises(settings.SettingsError):
                settings.update(self.path, {"use_fallback": value})
        self.assertIs(settings.load(self.path)["use_fallback"], True)
        self.path.write_text('{"use_fallback": "true"}')
        self.assertIs(settings.load(self.path)["use_fallback"], False)


    def test_changes_are_cleaned_saved_privately_and_kept(self):
        saved = settings.update(self.path, {"accent": "rose"})
        self.assertEqual(saved, {"accent": "rose", "time_zone": "", "search_url": "", "use_fallback": False})
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        settings.update(self.path, {"accent": "#A1B2C3", "time_zone": "America/New_York"})
        self.assertEqual(settings.load(self.path), {"accent": "#a1b2c3", "time_zone": "America/New_York", "search_url": "", "use_fallback": False})
        settings.update(self.path, {"time_zone": ""})
        self.assertEqual(settings.load(self.path), {"accent": "#a1b2c3", "time_zone": "", "search_url": "", "use_fallback": False})

class RetiredSettingsTests(unittest.TestCase):
    def test_retired_keys_are_ignored_when_loading_and_discarded_when_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text('{"backend":"retired","agent_name":"Old name","accent":"rose","time_zone":"UTC"}')
            saved = settings.load(path)
            self.assertEqual(saved, {'accent': 'rose', 'time_zone': 'UTC', 'search_url': '', 'use_fallback': False})
            settings.update(path, {'accent': 'ocean'})
            self.assertNotIn('backend', path.read_text())
            self.assertNotIn('agent_name', path.read_text())


if __name__ == "__main__":
    unittest.main()
