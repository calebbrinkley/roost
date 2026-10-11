#!/usr/bin/env python3
"""Check that the core repository files exist and that relative Markdown links resolve."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ["README.md", "AGENTS.md", "SECURITY.md", "LICENSE", "Makefile", "pyproject.toml",
            "LICENSES/SpaceGrotesk-OFL.txt", ".github/workflows/checks.yml"]


def main() -> int:
    errors = [f"missing required file: {p}" for p in REQUIRED if not (ROOT / p).is_file()]
    for path in ROOT.rglob("*.md"):
        if any(part in {".git", ".venv"} for part in path.parts):
            continue
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text()):
            if target.startswith(("https://", "http://", "#", "mailto:")):
                continue
            if not (path.parent / target.split("#", 1)[0]).exists():
                errors.append(f"{path.relative_to(ROOT)}: broken relative link to {target}")
    for error in errors:
        print(error)
    print(f"Repository check: {len(errors)} errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
