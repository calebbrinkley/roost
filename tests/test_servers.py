"""Owner-only model configuration, separate credentials and first-start environment defaults."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from roost import server
from roost.servers import ServerError, ServerStorageError, Servers
from hermes_fake import fake_hermes
from openai_fake import OpenAIFake

CURATED = [{"id": "example-model", "display_name": "Example", "enabled": True, "thinking_mode": "template_flag"},
           {"id": "manual-model", "display_name": "Manual", "enabled": False, "thinking_mode": "unsupported"}]


class ServersTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        self.servers = Servers(self.state, environ={})

    def tearDown(self):
        self.directory.cleanup()

    def test_key_is_write_only_private_and_can_be_kept_rotated_or_cleared(self):
        value = self.servers.update("primary", {"base_url": "https://models.example.invalid/v1", "key": "example-test-key"})
        self.assertTrue(value["has_key"])
        self.assertNotIn("key", value)
        self.assertNotIn("key_id", value)
        self.assertEqual(self.servers.key("primary"), "example-test-key")
        self.assertNotIn("example-test-key", self.servers.path.read_text())
        self.assertEqual(self.servers.keys_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.servers.path.stat().st_mode & 0o777, 0o600)
        self.servers.update("primary", {"key": ""})
        self.assertEqual(self.servers.key("primary"), "example-test-key")
        self.servers.update("fallback", {"base_url": "https://fallback.example.invalid/v1", "key": "fallback-test-key"})
        self.servers.update("primary", {"key": "rotated-test-key"})
        self.assertNotIn("example-test-key", self.servers.keys_path.read_text())
        self.assertEqual(self.servers.key("fallback"), "fallback-test-key")
        self.servers.update("primary", {"clear_key": True})
        self.assertFalse(self.servers.get("primary")["has_key"])
        self.assertEqual(self.servers.key("primary"), "")
        self.assertNotIn("rotated-test-key", self.servers.keys_path.read_text())
        self.assertEqual(Servers(self.state, environ={}).list(), self.servers.list())

    def test_env_prefill_only_on_first_start_even_if_no_url_was_initially_set(self):
        env_key = self.state / "installer-key"
        env_key.write_text("example-test-key\n")
        env = {"ROOST_MODEL_URL": "https://models.example.invalid/v1", "ROOST_MODEL_KEY_FILE": str(env_key)}
        # An existing empty configuration is still an owner's saved configuration.
        self.assertEqual(Servers(self.state, environ=env).get("primary")["base_url"], "")
        with tempfile.TemporaryDirectory() as directory:
            initialized = Servers(Path(directory), environ=env)
            self.assertEqual(initialized.get("primary")["base_url"], env["ROOST_MODEL_URL"])
            self.assertTrue(initialized.get("primary")["has_key"])
            initialized.update("primary", {"base_url": "https://changed.example.invalid/v1", "clear_key": True})
            env_key.unlink()
            loaded = Servers(Path(directory), environ=env)
            self.assertEqual(loaded.get("primary")["base_url"], "https://changed.example.invalid/v1")
            self.assertFalse(loaded.get("primary")["has_key"])

    def test_rejected_changes_preserve_both_files(self):
        self.servers.update("primary", {"base_url": "https://models.example.invalid/v1", "key": "example-test-key"})
        before = (self.servers.path.read_bytes(), self.servers.keys_path.read_bytes())
        for changes in ({}, {"unknown": 3}, {"key": []}, {"key": "line\nbreak"}, {"clear_key": "true"},
                        {"key": "new-test-key", "clear_key": True}, {"base_url": "ftp://example.invalid"},
                        {"base_url": "https://REPLACE_USER:REPLACE_PASSWORD@example.invalid"}, {"base_url": None}):
            with self.subTest(changes=changes), self.assertRaises(ServerError):
                self.servers.update("primary", changes)
            self.assertEqual((self.servers.path.read_bytes(), self.servers.keys_path.read_bytes()), before)
        with self.assertRaises(ServerError):
            self.servers.update("third", {"base_url": "https://example.invalid/v1"})

    def test_model_defaults_validation_and_manual_models(self):
        self.servers.update("primary", {"base_url": "https://models.example.invalid/v1"})
        self.servers.set_models("primary", {"models": CURATED, "default_model": "example-model"})
        saved = self.servers.path.read_bytes()
        cases = [
            {"models": CURATED, "default_model": "manual-model"},
            {"models": CURATED, "default_model": None},
            {"models": CURATED * 2, "default_model": "example-model"},
            {"models": [{**CURATED[0], "enabled": 1}], "default_model": "example-model"},
            {"models": [{**CURATED[0], "thinking_mode": "unknown"}], "default_model": "example-model"},
            {"models": [{**CURATED[0], "key": "example-test-key"}], "default_model": "example-model"},
            {"models": {}, "default_model": None}, {"models": []},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ServerError):
                self.servers.set_models("primary", changes)
            self.assertEqual(self.servers.path.read_bytes(), saved)
        manual = [{**CURATED[1], "enabled": True}]
        saved = self.servers.set_models("primary", {"models": manual, "default_model": "manual-model"})
        self.assertEqual(saved["models"], manual)
        self.servers.set_models("primary", {"models": [], "default_model": None})

    def test_failure_between_key_and_config_commit_retains_original_pair(self):
        from roost import servers
        self.servers.update("primary", {"base_url": "https://models.example.invalid/v1", "key": "original-test-key"})
        save = servers._save
        def fail_config(path, value):
            if path == self.servers.path:
                raise OSError("synthetic failure")
            return save(path, value)
        with patch("roost.servers._save", side_effect=fail_config), self.assertRaises(ServerStorageError):
            self.servers.update("primary", {"base_url": "https://changed.example.invalid/v1", "key": "changed-test-key"})
        self.assertEqual(self.servers.get("primary")["base_url"], "https://models.example.invalid/v1")
        self.assertEqual(self.servers.key("primary"), "original-test-key")
        loaded = Servers(self.state, environ={})
        self.assertNotIn("changed-test-key", loaded.keys_path.read_text())
        self.assertEqual(loaded.key("primary"), "original-test-key")

    def test_clear_failure_after_commit_is_recovered_on_startup(self):
        self.servers.update("primary", {"base_url": "https://models.example.invalid/v1", "key": "example-test-key"})
        with patch.object(self.servers, "_prune", side_effect=OSError("synthetic failure")), self.assertRaises(ServerStorageError):
            self.servers.update("primary", {"clear_key": True})
        self.assertFalse(self.servers.get("primary")["has_key"])
        loaded = Servers(self.state, environ={})
        self.assertEqual(json.loads(loaded.keys_path.read_text()), {})

    def test_corrupt_config_is_not_silently_reset_and_restored_permissions_are_fixed(self):
        os.chmod(self.servers.keys_path, 0o644)
        Servers(self.state, environ={})
        self.assertEqual(self.servers.keys_path.stat().st_mode & 0o777, 0o600)
        self.servers.path.write_text("not json")
        with self.assertRaises(ServerStorageError):
            Servers(self.state, environ={"ROOST_MODEL_URL": "https://models.example.invalid/v1"})
        self.assertEqual(self.servers.path.read_text(), "not json")


class ServerEndpointsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        (self.state / "password.hash").write_text("unused-demo-password-hash")
        self.fake = await OpenAIFake().start()
        env = {"ROOST_STATE_DIR": str(self.state), "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid",
               "ROOST_HERMES_URL": "https://hermes.example.invalid", "ROOST_HERMES_USERNAME": "demo",
               "ROOST_HERMES_PASSWORD_FILE": str(self.state / "password"), "ROOST_MODEL_URL": "", "ROOST_MODEL_KEY_FILE": ""}
        with patch.dict(os.environ, env), patch("roost.server.Hermes", return_value=fake_hermes()):
            self.app = server.create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"Origin": self.app["origin"], "X-Roost-Request": "1"}
        self.cookie = {server.COOKIE: self.app["sessions"].issue()}
        self.path = "/api/settings/servers"

    async def asyncTearDown(self):
        await self.client.close()
        await self.fake.close()
        self.directory.cleanup()

    async def put(self, suffix, value, **kwargs):
        return await self.client.put(self.path + suffix, headers=self.headers, cookies=self.cookie, json=value, **kwargs)

    async def test_all_routes_require_owner_auth_and_mutations_require_origin(self):
        for method, path, payload in (("GET", self.path, None), ("PUT", self.path + "/primary", {}),
                                      ("PUT", self.path + "/primary/models", {}), ("POST", self.path + "/primary/discover", {})):
            response = await self.client.request(method, path, headers=self.headers, json=payload)
            self.assertEqual(response.status, 401)
        for headers in ({}, {"Origin": "https://other.example.invalid", "X-Roost-Request": "1"}, {"Origin": self.app["origin"]}):
            for method, suffix in (("PUT", "/primary"), ("PUT", "/primary/models"), ("POST", "/primary/discover")):
                response = await self.client.request(method, self.path + suffix, headers=headers, cookies=self.cookie, json={})
                self.assertEqual(response.status, 403)
        self.assertEqual(self.fake.requests, [])

    async def test_config_discovery_merge_and_keys_never_echoed(self):
        response = await self.put("/primary", {"base_url": self.fake.url, "key": "example-test-key"})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["has_key"])
        self.assertNotIn("example-test-key", await response.text())
        response = await self.put("/primary/models", {"models": CURATED, "default_model": "example-model"})
        self.assertEqual(response.status, 200)
        response = await self.client.post(self.path + "/primary/discover", headers=self.headers, cookies=self.cookie)
        self.assertEqual(response.status, 200)
        models = (await response.json())["models"]
        self.assertEqual(models[:2], CURATED)
        self.assertEqual(models[2], {"id": "another-model", "display_name": "another-model", "enabled": False, "thinking_mode": "unsupported"})
        self.assertEqual(self.fake.requests[-1][1]["Authorization"], "Bearer example-test-key")
        response = await self.client.get(self.path, cookies=self.cookie)
        data = await response.json()
        self.assertEqual(data["servers"][0]["models"], CURATED)
        self.assertNotIn("example-test-key", await response.text())
        self.assertNotIn("key_id", await response.text())
        self.assertNotIn("example-test-key", self.app["servers"].path.read_text())
        self.assertEqual(self.app["servers"].keys_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.app["store"].db.execute("SELECT count(*) FROM conversations").fetchone()[0], 0)
        response = await self.put("/primary", {"key": "", "clear_key": True})
        self.assertFalse((await response.json())["has_key"])

    async def test_validation_and_upstream_errors_are_json_and_safe(self):
        response = await self.put("/primary", {"base_url": 123})
        self.assertEqual(response.status, 400)
        self.assertIn("HTTP(S)", (await response.json())["error"])
        response = await self.put("/third", {"base_url": self.fake.url})
        self.assertEqual(response.status, 400)
        response = await self.put("/primary", {"base_url": "https://name:example-test-key@example.invalid"})
        self.assertEqual(response.status, 400)
        self.assertNotIn("example-test-key", await response.text())
        response = await self.client.put(self.path + "/primary", headers=self.headers, cookies=self.cookie, data="not json")
        self.assertEqual(response.status, 400)
        await self.put("/primary", {"base_url": self.fake.url, "key": "example-test-key"})
        for status, expected in ((503, 503), (429, 503), (401, 502)):
            self.fake.status, self.fake.error_body = status, "Refused example-test-key"
            response = await self.client.post(self.path + "/primary/discover", headers=self.headers, cookies=self.cookie)
            self.assertEqual(response.status, expected)
            self.assertNotIn("example-test-key", (await response.json())["error"])

    async def test_slash_escaped_key_and_upstream_body_never_reach_endpoint_response(self):
        await self.put("/primary", {"base_url": self.fake.url, "key": "a/b"})
        self.fake.status, self.fake.error_body = 401, r'"a\/b"'
        response = await self.client.post(self.path + "/primary/discover", headers=self.headers, cookies=self.cookie)
        self.assertEqual(response.status, 502)
        body = await response.json()
        self.assertEqual(body, {"error": "Model server returned HTTP 401."})
        self.assertNotIn("a/b", json.dumps(body))
        self.assertNotIn(r"a\/b", body["error"])

    async def test_native_files_do_not_change_existing_auth_or_hermes_mode(self):
        self.assertTrue(self.app["sessions"].valid(self.cookie[server.COOKIE]))
        self.assertEqual((await self.client.get("/api/conversations", cookies=self.cookie)).status, 200)
        self.app["hermes"].sessions.assert_awaited_once()
        self.assertEqual(self.app["passkeys"].list(), [])
        self.assertEqual(self.app["servers"].get("primary")["base_url"], "")
        self.assertTrue((self.state / "sessions.sqlite3").exists())
        self.assertTrue((self.state / "native.sqlite3").exists())
        self.assertFalse(self.app["sessions"].db.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone())
