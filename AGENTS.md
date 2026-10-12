# Repository instructions

Roost is a public repository. Treat every change, commit, pull request, issue and release as public.

- Keep out secrets, credentials, personal contact details, account identities, tailnet names, device IPs, personal usernames or home paths, transcripts, databases and live logs. Examples use generic values or conspicuous `REPLACE_...` placeholders; real configuration lives outside the checkout.
- When validating or troubleshooting, report a finding's path, line and category, never the sensitive value.
- Commit with a GitHub no-reply identity: confirm `git config user.email` is set, because an unset one silently becomes the OS user and hostname.
- Run `make hooks` once per clone. The pre-push hook scans exactly what a push would publish; push only through it, never with `--no-verify`.
- A new binary file (font, image) must be reviewed and pinned by SHA-256 in `REVIEWED_ASSETS` in `scripts/check_public_content.py`, with its license in `LICENSES/`.
- If a leak is suspected, stop publishing and follow `SECURITY.md`; never paste the value into an issue.

## Repository workflow

Use a short-lived work branch and a pull request to `main`; do not push directly to `main`. GitHub requires the `check` job and resolved review conversations. Approval is optional. Use squash merges, never force-push, and follow [CONTRIBUTING.md](CONTRIBUTING.md). Repository policy is recorded in [docs/repository-security.md](docs/repository-security.md).

## Verification

Run `make check` for every change, and `make PYTHON=.venv/bin/python smoke` for anything that touches the server or the interface. Neither contacts a live Hermes; say plainly what was checked against a real agent and what was not.
