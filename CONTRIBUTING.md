# Contributing to Roost

Roost is public. Keep credentials, personal identifiers, conversation transcripts and machine configuration outside the checkout. Read [AGENTS.md](AGENTS.md) and [SECURITY.md](SECURITY.md) before contributing.

## Branches and pull requests

`main` is the integration branch. Create a short-lived branch for each change, using `feat/`, `fix/`, `docs/` or `security/`. Start new work from current `main`.

```bash
git fetch origin
git switch main
git merge --ff-only origin/main
git switch -c feat/REPLACE_CHANGE_NAME
make hooks
```

Commit with a GitHub no-reply email. Confirm `git config user.email` is set before the first commit; do not let Git fall back to a machine identity.

Push through the installed pre-push guard, then open a pull request against `main`. Never bypass the hook. GitHub's required `check` job must pass against the latest base, and review conversations must be resolved. Reviewer approval is optional, so a solo maintainer can merge their own passing pull request.

Squash-merge through GitHub after reviewing the final diff and checks. Normal work does not push directly to `main`. Force pushes are blocked on all branches; deletion of `main` is blocked. Merged work branches are automatically deleted.

If `main` moves while a pull request is open, merge current `origin/main` into the work branch and push again. Do not force-push a rewritten work branch. There is no deployment attached to a merge.

## Validation

Install Roost in a Python 3.11+ virtual environment and have Node.js 18+ available for the browser-logic regressions, then run:

```bash
make PYTHON=.venv/bin/python check
```

For server or interface changes, also run:

```bash
make PYTHON=.venv/bin/python smoke
```

The JavaScript tests use Node’s built-in test runner and synthetic DOM objects; no browser package is required. Override `NODE` when Node is outside your PATH.

`make check` scans candidate files and reachable history, checks documentation links, and runs unit tests. The smoke check reserves ephemeral loopback ports and uses temporary state, so it can run alongside an existing demo. It exercises durable chat, queue controls, tools, memory, compaction, attachments, fallback, voice and jobs against deterministic local providers. Neither proves behavior against real model/search/speech/push services or on a physical phone.

Describe the validation performed and any remaining device or host checks in the pull request. Do not attach live logs, credentials or transcripts.

## Dependencies and assets

Dependabot proposes weekly Python and GitHub Actions updates through pull requests. Security update PRs are enabled; they still require CI and maintainer review of the diff before merging.

Actions must use a full commit SHA. The repository permits `actions/checkout` plus repository-local actions and workflows; adding another external action requires an explicit allowlist update and review. See [repository security](docs/repository-security.md).

Review new or replaced images and fonts, record their license in `LICENSES/`, and pin their SHA-256 in `REVIEWED_ASSETS` in `scripts/check_public_content.py`. Keep retired, previously reviewed hashes only in `REVIEWED_ASSET_HISTORY` for history scanning. Current assets must match their current pins.

## Security reports

Use GitHub's [private vulnerability reporting](https://github.com/calebbrinkley/roost/security/advisories/new) for vulnerabilities. For an accidental credential disclosure, follow [SECURITY.md](SECURITY.md) and rotate the affected credential first.
