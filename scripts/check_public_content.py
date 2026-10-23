#!/usr/bin/env python3
"""Conservative public-content guard; findings never include matched values.

Modes:
  (default)         scan the working tree's candidate files
  --history         scan every reachable commit: file versions, paths, messages, identities
  --pre-push R URL  git pre-push hook entry: scan only what the push would publish

Standard library only; runs on Python 3.9+ because a hook uses whatever python3 is installed.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS = {"private", "instance", "secrets", "data", "state", "models", "backups", "logs", "captures", "artifacts", "output"}
FORBIDDEN_SUFFIXES = {".pdf", ".gguf", ".safetensors", ".bin", ".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx", ".tar", ".gz", ".zip"}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"
))
DOCUMENTATION_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"
))
# Vendor-shaped credentials. Each rule matches a documented prefix plus a long random tail.
TEXT_RULES = (
    ("personal home path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")),
    ("private key material", re.compile(r"-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----")),
    ("credential token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{24,})\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    # The marker is split so this source does not contain the string it detects.
    ("OpenAI API key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}|T3Blbk" r"FJ")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("Tailscale key", re.compile(r"\btskey-[A-Za-z0-9-]{16,}")),
    ("cloud provider key", re.compile(
        r"\b(?:(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|[sr]k_live_[A-Za-z0-9]{16,})\b")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)
IP_PATTERN = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
TAILNET_PATTERN = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9.-]*\.ts\.net\b", re.I)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "example.invalid", "users.noreply.github.com"}
# Service no-reply addresses that appear in merge commits and co-author trailers.
SERVICE_ADDRESSES = {"noreply@github.com", "noreply@anthropic.com"}
# Dependabot uses a public service address in this exact footer. It is permitted only in a
# bot-authored commit message, not in files, general message text or commit identities.
DEPENDABOT_AUTHOR = "49699333+dependabot[bot]@users.noreply.github.com"
DEPENDABOT_SIGNOFF = "Signed-off-by: dependabot[bot] <support" + "@github.com>"
# NAME_API_KEY=..., "client_secret": "...", HF_TOKEN: ...  The name may carry any prefix or suffix.
ASSIGNMENT_PATTERN = re.compile(
    r'''(?i)[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|auth[_-]?key|private[_-]?key|credential)'''
    r'''[A-Za-z0-9_.-]*["']?\s*[:=]\s*["']?([A-Za-z0-9_+/=.~-]{20,})'''
)
# A URL whose authority part carries a user and password before the host.
URL_CREDENTIAL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:([^\s/@]{6,})@[^\s/]+")
ZERO_SHA = re.compile(r"^0+$")
# Binary files that were reviewed and may be published, pinned by SHA-256 of their content so a
# replaced or modified file fails again. Asset licenses are recorded in LICENSES/.
REVIEWED_ASSETS = {
    "roost/static/SpaceGrotesk-Regular.ttf": "54f91de389ffe508d384a0a0f7e0ca489f170acf40ceebc458086903cf483bc7",
    "roost/static/SpaceGrotesk-Bold.ttf": "54c4dc92c236f5eca503670045125c77d61ef3a22601c76f2f252cf3cad4a088",
    "roost/static/icon-192.png": "393c5118288910757e38e0edf4651b711effc35da3444ec7c855c1c1a40851b1",
    "roost/static/icon-512.png": "f48fca30dfd45d6b1692af98e339d5cca09dd94e4ad7f04ab8ef6121be2c711c",
    "roost/static/apple-touch-icon.png": "cd72bcfb9b14c5d385456e98ae9d0ad2063e08a22783cee3679aa760ef4ab182",
    "assets/roost-logo.png": "d4739d7b3dec098486d6e6e5adb71651e56d7ab457bbf535e5f2f5284020d377",
    "assets/roost-iphone-chat-dark.png": "4ea75dd5311e7a6ea775698071395ba176014221c541bcccf032c36b0760d5bc",
    "assets/roost-iphone-dark.png": "9862004e1c1906df99ff07bdbebafd2b3fa141dc96e33b56bb55898531510294",
    "assets/roost-product-tour.gif": "250423f875fef1245cd5fed409fc1a09fd44e2ba1ccc4c5b42dffaa79133293a",
    "assets/roost-readme-hero.png": "99fb7158d7ccd0e0e5a969127d55cc70fbcdcb11a5898a751b59c0f39bb8573d",
}


# Historical file versions (git blob IDs) whose only finding was reviewed as a false positive:
# a demo stand-in's example password and two `name = function(...)` calls read as values. Current
# files no longer trigger the rule; only that one category is suppressed, and only for these blobs.
REVIEWED_HISTORY = {
    "20c0343e3b194a8dd0faa66eaaf0d887696dbc8a": "credential-shaped literal",  # dev/demo.py demo password
    "237a6cd92bc100b4c905c7fba093baf0c5900752": "credential-shaped literal",  # dev/demo.py demo password
    "ffc5535b6c74144cd834724cf727df403116ab78": "credential-shaped literal",  # dev/demo.py demo password
    "61bf2c6761003a66a53e2f0a9a901e0b165ca957": "credential-shaped literal",  # roost/auth.py token_urlsafe call
    "1e13e56b2f0304ccb7b20b7165efe2401d94751c": "credential-shaped literal",  # roost/hermes.py read_text call
    "311a1c594b3ba952f545ec355315d5afc37d9fe8": "credential-shaped literal",  # roost/hermes.py read_text call
}


# Previously published assets remain reviewed for history scans only. Current files must match
# their current pin; a retired asset is not approved for reintroduction into the working tree.
REVIEWED_ASSET_HISTORY = {
    "assets/roost-iphone-dark.png": {"47bd3737d5d61418207dee8328342223cff6c063ca9ebcc5420ad0b9340f4db0"},
    "assets/roost-macbook-light.png": {"5b443696cf1fca80141d52287d3ca92e6207c86853c06c133ceac4b8670b9279"},
}


def reviewed_asset(name: str, body: bytes, *, history: bool = False) -> bool:
    digest = hashlib.sha256(body).hexdigest()
    return REVIEWED_ASSETS.get(name) == digest or (
        history and digest in REVIEWED_ASSET_HISTORY.get(name, set())
    )


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.category}"


def is_placeholder(value: str) -> bool:
    """A reference to a secret, not a secret: placeholders, env var names and filesystem paths.

    An env var name needs an underscore, so a long all-caps token such as a base32 or
    hexadecimal secret is still reported.
    """
    return ("REPLACE_" in value or value.startswith("/") or "example" in value.lower()
            or re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", value) is not None)


def allowed_address(address: str) -> bool:
    domain = address.rsplit("@", 1)[1].lower()
    return domain in PLACEHOLDER_DOMAINS or address.lower() in SERVICE_ADDRESSES


def inspect_text(text: str, name: str, label: str = "", *, allow_dependabot_signoff: bool = False) -> list[Finding]:
    """Findings for text. `name` selects exemptions; `label` (default: name) is what gets reported."""
    findings = []
    label = label or name
    for number, line in enumerate(text.splitlines(), 1):
        categories = set()
        for category, pattern in TEXT_RULES:
            if pattern.search(line):
                categories.add(category)
        for match in IP_PATTERN.finditer(line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if any(address in net for net in DOCUMENTATION_NETWORKS):
                continue
            if any(address in net for net in PRIVATE_NETWORKS):
                # Network constants in this guard are generic policy, not instance values.
                if name == "scripts/check_public_content.py" and match.group() + "/" in line:
                    continue
                categories.add("private or tailnet device address")
        for match in TAILNET_PATTERN.finditer(line):
            hostname = match.group().lower()
            if hostname != "example.ts.net" and not hostname.endswith(".example.ts.net"):
                categories.add("non-placeholder tailnet hostname")
        for match in EMAIL_PATTERN.finditer(line):
            if not allowed_address(match.group()):
                if allow_dependabot_signoff and line.strip() == DEPENDABOT_SIGNOFF:
                    continue
                categories.add("non-placeholder contact address")
        for match in ASSIGNMENT_PATTERN.finditer(line):
            if not is_placeholder(match.group(1)):
                categories.add("credential-shaped literal")
        for match in URL_CREDENTIAL_PATTERN.finditer(line):
            if not is_placeholder(match.group(1)):
                categories.add("credentials embedded in URL")
        findings.extend(Finding(label, number, category) for category in sorted(categories))
    return findings


def path_finding(name: str):
    """Rules that depend only on a file's path; None when the path is acceptable."""
    parts = Path(name).parts
    leaf = parts[-1] if parts else name
    if any(part in FORBIDDEN_DIRS for part in parts):
        return Finding(name, 1, "private/runtime directory")
    if leaf.startswith(".env") and not leaf.endswith(".example"):
        return Finding(name, 1, "live environment file")
    if Path(leaf).suffix.lower() in FORBIDDEN_SUFFIXES or ".local." in leaf or leaf.endswith(".local"):
        return Finding(name, 1, "private/binary artifact type")
    return None


def inspect_file(path: Path, root: Path) -> list[Finding]:
    name = path.relative_to(root).as_posix()
    if path.is_symlink():
        return [Finding(name, 1, "symlink requires explicit public-content review")]
    forbidden = path_finding(name)
    if forbidden:
        return [forbidden]
    if name in REVIEWED_ASSETS:
        return [] if reviewed_asset(name, path.read_bytes()) else [Finding(name, 1, "reviewed asset changed; review and update its pin")]
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeError, OSError):
        return [Finding(name, 1, "unreadable or non-text file")]
    if "\x00" in text:
        return [Finding(name, 1, "binary file")]
    return inspect_text(text, name)


def candidate_files(root: Path) -> list[Path]:
    git = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
    )
    if git.returncode == 0:
        names = {n.decode("utf-8") for n in git.stdout.split(b"\0") if n}
        return sorted(root / name for name in names if (root / name).exists() or (root / name).is_symlink())
    return sorted(p for p in root.rglob("*") if (p.is_file() or p.is_symlink())
                  and not any(part in {".git", "__pycache__", ".venv"} for part in p.relative_to(root).parts))


# ---- git history -----------------------------------------------------------------------------

def git_output(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError("git " + args[0] + " failed")
    return result.stdout


def is_repository(root: Path) -> bool:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir"], capture_output=True).returncode == 0


def inspect_commits(root: Path, revs: list[str]) -> tuple[list[Finding], int]:
    """Messages and identities of the selected commits. Returns (findings, commit count)."""
    raw = git_output(root, "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x01", *revs)
    findings, count = [], 0
    for record in raw.decode("utf-8", "replace").split("\x01"):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, _author, author_email, _committer, committer_email, message = record.split("\x00", 5)
        count += 1
        where = "commit " + sha[:7]
        for address in (author_email, committer_email):
            # An identity must be a GitHub no-reply or placeholder address, never a machine or personal one.
            if "@" not in address or not allowed_address(address):
                findings.append(Finding(where, 1, "non-noreply commit identity"))
                break
        findings.extend(inspect_text(message, where + " message", allow_dependabot_signoff=(
            _author == "dependabot[bot]" and author_email == DEPENDABOT_AUTHOR
        )))
    return findings, count


def inspect_objects(root: Path, revs: list[str]) -> tuple[list[Finding], int]:
    """Every file version introduced by the selected commits. Returns (findings, blob count)."""
    listing = git_output(root, "rev-list", "--objects", *revs).decode("utf-8", "replace")
    named = {}
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            named.setdefault(sha, set()).add(path)
    findings, blobs = [], 0
    batch = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha, paths in named.items():
            batch.stdin.write((sha + "\n").encode())
            batch.stdin.flush()
            header = batch.stdout.readline().split()
            if len(header) < 3:
                continue
            body = batch.stdout.read(int(header[2]))
            batch.stdout.read(1)
            if header[1] != b"blob":
                continue
            blobs += 1
            for path in sorted(paths):
                where = path + " @" + sha[:7]
                forbidden = path_finding(path)
                if forbidden:
                    findings.append(Finding(where, 1, forbidden.category))
                    continue
                if path in REVIEWED_ASSETS or path in REVIEWED_ASSET_HISTORY:
                    if not reviewed_asset(path, body, history=True):
                        findings.append(Finding(where, 1, "reviewed asset changed; review and update its pin"))
                    continue
                if b"\x00" in body[:8192]:
                    findings.append(Finding(where, 1, "binary file"))
                    continue
                findings.extend(f for f in inspect_text(body.decode("utf-8", "replace"), path, where)
                                if REVIEWED_HISTORY.get(sha) != f.category)
    finally:
        batch.stdin.close()
        batch.stdout.close()
        batch.wait()
    return findings, blobs


def inspect_revisions(root: Path, revs: list[str]) -> tuple[list[Finding], int, int]:
    commit_findings, commits = inspect_commits(root, revs)
    object_findings, blobs = inspect_objects(root, revs)
    return commit_findings + object_findings, commits, blobs


def object_exists(root: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "cat-file", "-e", sha], capture_output=True).returncode == 0


def push_revisions(root: Path, remote: str, lines: list[str]) -> list[str]:
    """Revision arguments selecting the commits a push would add to the remote."""
    revs = []
    excluded = ["--remotes=" + remote]
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = fields
        if ZERO_SHA.match(local_sha):
            continue  # branch deletion publishes nothing
        revs.append(local_sha)
        if not ZERO_SHA.match(remote_sha) and object_exists(root, remote_sha):
            excluded.append(remote_sha)
    return [*revs, "--not", *excluded] if revs else []


# ---- reporting -------------------------------------------------------------------------------

def report(findings: list[Finding], summary: str) -> int:
    for finding in sorted(set(findings), key=lambda f: (f.path, f.line, f.category)):
        print(finding)
    print(summary.format(len(set(findings))))
    return 1 if findings else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--history", action="store_true", help="scan all reachable commits instead of the working tree")
    parser.add_argument("--pre-push", nargs=2, metavar=("REMOTE", "URL"),
                        help="git pre-push hook mode; reads ref updates from standard input")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.pre_push:
        remote = args.pre_push[0]
        revs = push_revisions(root, remote, sys.stdin.read().splitlines())
        if not revs:
            return 0
        findings, commits, blobs = inspect_revisions(root, revs)
        status = report(findings, "Public-content check of the push (" + str(commits) + " commits, " +
                        str(blobs) + " file versions): {} findings.")
        if status:
            print("Push blocked. Fix or remove the listed content; rewrite unpushed commits rather than "
                  "adding a fix on top. Do not bypass this check for a public repository without review.",
                  file=sys.stderr)
        return status

    if args.history:
        if not is_repository(root):
            print("Public-content history check skipped: not a git repository.")
            return 0
        findings, commits, blobs = inspect_revisions(root, ["--all"])
        return report(findings, "Public-content history check (" + str(commits) + " commits, " +
                      str(blobs) + " file versions): {} findings.")

    files = candidate_files(root)
    findings = [finding for path in files for finding in inspect_file(path, root)]
    return report(findings, "Public-content check: " + str(len(files)) + " candidate files, {} findings.")


if __name__ == "__main__":
    raise SystemExit(main())
