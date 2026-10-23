import importlib.util
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_public_content.py"
SPEC = importlib.util.spec_from_file_location("public_guard", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class PublicContentTests(unittest.TestCase):
    def test_private_home_is_rejected_without_echoing_value(self):
        sample = "/Users/" + "example-person/private-file"
        findings = guard.inspect_text(sample, "sample.md")
        self.assertTrue(findings)
        self.assertNotIn(sample, str(findings[0]))

    def test_private_and_tailnet_addresses_are_rejected(self):
        for sample in ["10." + "2.3.4", "100." + "64.0.42"]:
            with self.subTest(kind=sample.split(".")[0]):
                self.assertTrue(guard.inspect_text(sample, "sample.md"))

    def test_loopback_documentation_addresses_and_placeholders_are_allowed(self):
        sample = "127.0.0.1 0.0.0.0 192.0.2.10 203.0.113.20 example.ts.net api_key=REPLACE_WITH_LOCAL_SECRET"
        self.assertEqual(guard.inspect_text(sample, "sample.md"), [])

    def test_tailnet_identity_is_rejected(self):
        sample = "example-host." + "tail-example" + ".ts.net"
        self.assertTrue(guard.inspect_text(sample, "sample.md"))

    def test_credentials_and_keys_are_rejected(self):
        token = "ghp_" + "X" * 30
        key = "-----BEGIN " + "RSA PRIVATE KEY-----"
        literal = 'api_key="' + "A" * 32 + '"'
        for sample in [token, key, literal]:
            findings = guard.inspect_text(sample, "sample.md")
            self.assertTrue(findings)
            self.assertNotIn(sample, "\n".join(map(str, findings)))

    def test_vendor_key_shapes_are_rejected_without_echoing_value(self):
        # Built at runtime so this source never contains a key-shaped literal.
        samples = {
            "Anthropic API key": "k = sk-ant-" + "api03-" + "A" * 40 + "-" + "b" * 20,
            "OpenAI API key": "k = sk-" + "proj-" + "a" * 30 + "_" + "B" * 30,
            "Hugging Face token": "k " + "hf_" + "a" * 34,
            "Tailscale key": "up --auth-key tskey-" + "auth-" + "a" * 14 + "CNTRL-" + "b" * 30,
            "cloud provider key": "id " + "AKIA" + "A" * 16,
            "JSON web token": "t " + "eyJ" + "a" * 12 + "." + "eyJ" + "b" * 12 + "." + "c" * 12,
        }
        samples["cloud provider key #2"] = "g " + "AIza" + "a" * 35
        samples["cloud provider key #3"] = "s " + "xoxb-" + "1" * 12 + "-" + "a" * 24
        for expected, sample in samples.items():
            with self.subTest(kind=expected):
                findings = guard.inspect_text(sample, "sample.md")
                self.assertTrue(findings)
                self.assertIn(expected.split(" #")[0], {f.category for f in findings})
                for finding in findings:
                    self.assertNotIn(sample.split()[-1], str(finding))

    def test_environment_style_assignments_are_rejected(self):
        value = "aZ3kQ9xLm2Pw7Rt5Yv8Nc4Hd6Jf1Bg0Us"
        for name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HF_TOKEN", "INFERENCE_SECRET",
                     "client_secret", "db-password", "SWAP_AUTH_KEY"]:
            for sample in [name + "=" + value, name + ": " + value, '"' + name + '": "' + value + '"']:
                with self.subTest(sample=name):
                    categories = {f.category for f in guard.inspect_text(sample, "sample.md")}
                    self.assertIn("credential-shaped literal", categories)

    def test_references_to_secrets_are_not_findings(self):
        sample = "\n".join([
            "OPENAI_API_KEY=REPLACE_WITH_LOCAL_SECRET",
            "api_key: ${OPENAI_API_KEY}",
            "SWAP_KEY_ENV=EXAMPLE_SWAP_KEY",
            "secret_path = /etc/example-agent/secrets/inference",
            "token_file = /etc/example-agent/secrets/controller",
            "password = short",
            "sha256 = " + "a" * 64,
            "image = example.invalid/component@sha256:" + "b" * 64,
            "secret=str(token)",
            "https://user:REPLACE_PASSWORD@example.invalid/path",
        ])
        self.assertEqual(guard.inspect_text(sample, "sample.md"), [])

    def test_long_uppercase_tokens_are_not_mistaken_for_environment_names(self):
        for value in ["A" * 32, "ABCDEF0123456789" * 2, "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"]:
            with self.subTest(value=len(value)):
                self.assertTrue(guard.inspect_text("api_key = " + value, "sample.md"))

    def test_credentials_embedded_in_urls_are_rejected(self):
        sample = "https://service:" + "a" * 20 + "@host.example.invalid/path"
        self.assertIn("credentials embedded in URL", {f.category for f in guard.inspect_text(sample, "sample.md")})

    def test_guard_source_does_not_trip_itself(self):
        self.assertEqual(guard.inspect_file(SCRIPT, SCRIPT.parents[1]), [])

    def test_private_contact_is_rejected_but_reserved_example_is_allowed(self):
        sample = "example-user@" + "private-domain.test"
        self.assertTrue(guard.inspect_text(sample, "sample.md"))
        self.assertEqual(guard.inspect_text("user@example.invalid", "sample.md"), [])
        # Service no-reply addresses appear in merge commits and co-author trailers.
        self.assertEqual(guard.inspect_text("Co-Authored-By: Example <noreply@anthropic.com>", "sample.md"), [])
        self.assertTrue(guard.inspect_text("other@" + "anthropic.com", "sample.md"))

    def test_live_environment_binary_and_runtime_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in [".env", "walkthrough.pdf", "state/agent.db"]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test")
                self.assertTrue(guard.inspect_file(path, root))

    def test_example_environment_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".env.example"
            path.write_text("INFERENCE_SECRET_REFERENCE=REPLACE_INFERENCE_SECRET_REFERENCE\n")
            self.assertEqual(guard.inspect_file(path, root), [])

    def test_symlink_is_rejected_without_reading_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "source.md"
            path.symlink_to(root / "missing-private-target")
            self.assertTrue(guard.inspect_file(path, root))


class ReviewedAssetTests(unittest.TestCase):
    def test_historical_asset_pin_does_not_approve_current_or_modified_files(self):
        name = "assets/retired-example.png"
        original = b"\x00reviewed example asset"
        digest = hashlib.sha256(original).hexdigest()
        with patch.dict(guard.REVIEWED_ASSET_HISTORY, {name: {digest}}):
            self.assertTrue(guard.reviewed_asset(name, original, history=True))
            self.assertFalse(guard.reviewed_asset(name, original))
            self.assertFalse(guard.reviewed_asset(name, original + b"modified", history=True))
            self.assertFalse(guard.reviewed_asset("assets/other.png", original, history=True))
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_bytes(original)
                self.assertTrue(guard.inspect_file(path, root))

    def test_reviewed_font_passes_only_with_its_pinned_content(self):
        name = next(iter(guard.REVIEWED_ASSETS))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / name
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\x00not the reviewed font")
            self.assertEqual([f.category for f in guard.inspect_file(path, root)],
                             ["reviewed asset changed; review and update its pin"])
            other = root / "roost/static/unreviewed.ttf"
            other.write_bytes(b"\x00binary")
            self.assertTrue(guard.inspect_file(other, root))


if __name__ == "__main__":
    unittest.main()
