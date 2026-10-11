#!/usr/bin/env python3
"""Install the pre-push public-content hook into this clone. Never overwrites a different hook."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/hooks/pre-push"


def hook_path(root: Path = ROOT) -> Path:
    # --git-path honours worktrees and core.hooksPath; its output is relative to the -C directory.
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "hooks/pre-push"],
                            capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("Not a git checkout; nothing to install.")
    return (root / result.stdout.strip()).resolve()


def install(root: Path = ROOT, source: Path = SOURCE) -> str:
    target = hook_path(root)
    if target.exists() or target.is_symlink():
        if target.is_file() and not target.is_symlink() and target.read_bytes() == source.read_bytes():
            target.chmod(0o755)
            return "Pre-push hook already installed: " + str(target)
        raise SystemExit("A different pre-push hook exists at " + str(target) +
                         "; merge it with scripts/hooks/pre-push by hand.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755)
    return "Installed pre-push hook: " + str(target)


if __name__ == "__main__":
    print(install())
    sys.exit(0)
