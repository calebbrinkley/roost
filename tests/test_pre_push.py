"""History scanning, the pre-push hook and its installer, using throwaway git repositories."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/check_public_content.py"
HOOK = REPO / "scripts/hooks/pre-push"
INSTALLER = REPO / "scripts/install_hooks.py"
SPEC = importlib.util.spec_from_file_location("public_guard_history", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)

SAFE_IDENTITY = ("Test Author", "12345+test@users.noreply.github.com")
# Addresses a default git setup falls back to; split so this source holds no contact literal.
LOCAL_IDENTITY = ("Test Author", "someone@" + "machine.local")
PERSONAL_IDENTITY = ("Someone", "person@" + "gmail.com")


def secret_line():
    # Built at runtime so this file never contains a key-shaped literal.
    return "OPENAI_API_KEY=" + "sk-" + "proj-" + "a" * 40 + "\n"


class GitSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.remote, self.work = base / "remote.git", base / "work"
        self.env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, env=self.env)
        subprocess.run(["git", "init", "-q", str(self.work)], check=True, env=self.env)
        self.git("checkout", "-q", "-b", "main")
        self.git("remote", "add", "origin", str(self.remote))
        (self.work / "scripts/hooks").mkdir(parents=True)
        shutil.copy(SCRIPT, self.work / "scripts/check_public_content.py")
        shutil.copy(HOOK, self.work / "scripts/hooks/pre-push")
        shutil.copy(INSTALLER, self.work / "scripts/install_hooks.py")

    def git(self, *args, identity=SAFE_IDENTITY, check=True):
        result = subprocess.run(["git", "-c", "user.name=" + identity[0], "-c", "user.email=" + identity[1], *args],
                                cwd=self.work, capture_output=True, text=True, env=self.env)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def commit(self, name, content, identity=SAFE_IDENTITY, message="change"):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git("add", name)
        self.git("commit", "-q", "-m", message, identity=identity)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def install_hook(self):
        self.git("add", "scripts")
        self.git("commit", "-q", "-m", "tooling")
        self.git("push", "-q", "origin", "main")
        output = subprocess.run([sys.executable, "scripts/install_hooks.py"], cwd=self.work,
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(output.returncode, 0, output.stderr)
        return output


class HistoryScanTests(GitSandbox):
    def test_dependabot_signoff_is_allowed_only_for_the_exact_bot_commit_footer(self):
        bot = ("dependabot[bot]", guard.DEPENDABOT_AUTHOR)
        valid = self.commit("a.txt", "fine\n", identity=bot,
                            message="dependency update\n\n" + guard.DEPENDABOT_SIGNOFF)
        self.assertEqual(guard.inspect_revisions(self.work, [valid])[0], [])
        self.assertTrue(guard.inspect_text(guard.DEPENDABOT_SIGNOFF, "notes.md"))
        self.assertFalse(guard.allowed_address("support" + "@github.com"))

        # Human-authored copies and additional addresses still trigger the contact rule.
        self.commit("b.txt", "fine\n", message="change\n\n" + guard.DEPENDABOT_SIGNOFF)
        self.commit("c.txt", "fine\n", identity=bot,
                    message="dependency update\n\n" + guard.DEPENDABOT_SIGNOFF + "\n" +
                    "Contact: person" + "@private-domain.test")
        self.commit("d.txt", "fine\n", identity=bot,
                    message="Contact: support" + "@github.com")
        findings = guard.inspect_revisions(self.work, ["--all"])[0]
        self.assertEqual(len(findings), 3)
        self.assertEqual({f.category for f in findings}, {"non-placeholder contact address"})

    def test_secret_added_then_deleted_is_still_found_in_history(self):
        self.commit("config.txt", secret_line())
        self.git("rm", "-q", "config.txt")
        self.git("commit", "-q", "-m", "remove it")
        self.assertFalse((self.work / "config.txt").exists())
        findings, commits, blobs = guard.inspect_revisions(self.work, ["--all"])
        self.assertEqual(commits, 2)
        categories = {(f.path.split(" @")[0], f.category) for f in findings}
        self.assertIn(("config.txt", "credential-shaped literal"), categories)
        self.assertTrue(all(secret_line().split("=")[1].strip() not in str(f) for f in findings))

    def test_machine_local_and_personal_identities_are_findings(self):
        self.commit("a.txt", "fine\n", identity=LOCAL_IDENTITY)
        self.commit("b.txt", "fine\n", identity=PERSONAL_IDENTITY)
        self.commit("c.txt", "fine\n")
        findings, commits, _ = guard.inspect_revisions(self.work, ["--all"])
        self.assertEqual(commits, 3)
        self.assertEqual(sorted(f.category for f in findings), ["non-noreply commit identity"] * 2)

    def test_clean_history_with_service_addresses_has_no_findings(self):
        self.commit("a.txt", "fine\n", message="work\n\nCo-Authored-By: Example <noreply@anthropic.com>")
        findings, _, _ = guard.inspect_revisions(self.work, ["--all"])
        self.assertEqual(findings, [])

    def test_forbidden_paths_are_reported_with_their_commit(self):
        self.commit("state/agent.db", "x")
        findings, _, _ = guard.inspect_revisions(self.work, ["--all"])
        self.assertTrue(any(f.category == "private/runtime directory" for f in findings))

    def test_history_mode_is_skipped_outside_a_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(SCRIPT), "--history", "--root", directory],
                                    capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("skipped", result.stdout)


class PreCommitPushTests(GitSandbox):
    def push(self, *refspec):
        return self.git("push", "origin", *(refspec or ("main",)), check=False)

    def test_hook_blocks_secret_and_names_location_without_value(self):
        self.install_hook()
        self.commit("settings.env", secret_line())
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("settings.env", output)
        self.assertIn("credential-shaped literal", output)
        self.assertNotIn(secret_line().split("=")[1].strip(), output)
        self.assertIn("Push blocked", output)
        remote_heads = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "main"],
                                      capture_output=True, text=True, env=self.env).stdout.strip()
        self.assertNotEqual(remote_heads, self.git("rev-parse", "HEAD").stdout.strip())

    def test_hook_blocks_local_commit_identity(self):
        self.install_hook()
        self.commit("notes.md", "fine\n", identity=LOCAL_IDENTITY)
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-noreply commit identity", result.stdout + result.stderr)

    def test_hook_blocks_secret_hidden_in_an_earlier_commit_of_the_push(self):
        self.install_hook()
        self.commit("settings.env", secret_line())
        self.git("rm", "-q", "settings.env")
        self.git("commit", "-q", "-m", "oops, remove it")
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("settings.env", result.stdout + result.stderr)

    def test_hook_allows_a_clean_push_and_ignores_content_already_on_the_remote(self):
        self.install_hook()
        bad = self.commit("settings.env", secret_line())
        # Simulate content that reached the remote before the hook existed.
        subprocess.run(["git", "--git-dir", str(self.remote), "fetch", "-q", str(self.work), bad + ":refs/heads/main"],
                       check=True, env=self.env, capture_output=True)
        self.git("fetch", "-q", "origin")
        self.git("rm", "-q", "settings.env")
        self.git("commit", "-q", "-m", "stop tracking")
        self.commit("README.md", "clean\n")
        result = self.push()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hook_allows_branch_deletion_and_scans_new_branches_fully(self):
        self.install_hook()
        self.git("checkout", "-q", "-b", "topic")
        self.commit("settings.env", secret_line())
        blocked = self.push("topic")
        self.assertNotEqual(blocked.returncode, 0)
        self.git("checkout", "-q", "main")
        self.git("push", "origin", "main")
        subprocess.run(["git", "--git-dir", str(self.remote), "branch", "-q", "doomed", "main"], check=True, env=self.env)
        self.assertEqual(self.push(":doomed").returncode, 0)

    def test_installer_is_idempotent_and_refuses_to_replace_a_different_hook(self):
        first = self.install_hook()
        self.assertIn("Installed", first.stdout)
        again = subprocess.run([sys.executable, "scripts/install_hooks.py"], cwd=self.work, capture_output=True,
                               text=True, env=self.env)
        self.assertEqual(again.returncode, 0)
        self.assertIn("already installed", again.stdout)
        hook = self.work / ".git/hooks/pre-push"
        self.assertTrue(os.access(hook, os.X_OK))
        hook.write_text("#!/bin/sh\nexit 0\n")
        changed = subprocess.run([sys.executable, "scripts/install_hooks.py"], cwd=self.work, capture_output=True,
                                 text=True, env=self.env)
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(hook.read_text(), "#!/bin/sh\nexit 0\n")


if __name__ == "__main__":
    unittest.main()
