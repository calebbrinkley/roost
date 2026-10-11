<p align="center">
  <img src="assets/roost-logo.png" alt="roost" width="240">
</p>

A simple, lightweight Progressive Web App (PWA) for chatting with your Hermes agent and managing its scheduled work.

Roost puts conversations and Jobs in one interface. It is an **early preview**: text chat and Jobs are implemented; voice and push notifications are planned.

## What you can do

- Start and continue conversations, including conversations from other Hermes channels.
- Rename conversations or delete them after confirmation.
- Create, edit, pause, resume, run and delete scheduled Jobs.
- View Job status and the run history Hermes provides.
- Choose an agent display name and switch between light and dark themes.

## Quick start: try the demo

Requires **Python 3.11+**. The demo uses sample conversations and Jobs with a local Hermes stand-in; it does not contact a live agent.

```bash
git clone https://github.com/calebbrinkley/roost.git
cd roost
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python dev/demo.py
```

Open **http://127.0.0.1:8787** and sign in with **`roost-demo-only`**.

Stop the demo with Ctrl+C. Its disposable state lives in `.roost-demo-state/`, which Git ignores.

## Connect your Hermes agent

Install Roost using the first four commands above. You also need a reachable Hermes dashboard with its password provider enabled and the desired agent and model already configured. Roost does not start Hermes or load a model.

### 1. Set your Roost password

```bash
export ROOST_STATE_DIR="$PWD/.roost-state"
.venv/bin/python -m roost set-password
```

This is your Roost sign-in password, separate from the Hermes dashboard password.

### 2. Configure the connection

Store your Hermes dashboard password in a file **outside the checkout**, readable only by the service account. Replace the placeholders below with your dashboard endpoint, username and password-file path.

```bash
export ROOST_PUBLIC_ORIGIN="http://127.0.0.1:8787"
export ROOST_HERMES_URL="https://REPLACE_HERMES_DASHBOARD_HOST:8445"
export ROOST_HERMES_USERNAME="REPLACE_OPERATOR_USERNAME"
export ROOST_HERMES_PASSWORD_FILE="/REPLACE_PRIVATE_DIRECTORY/hermes-password"
export ROOST_AGENT_NAME="Agent"
.venv/bin/python -m roost serve
```

Open **http://127.0.0.1:8787** and sign in with the password you set in step 1.

### 3. Access it from another device

Roost binds to loopback by default. Put it behind a **private HTTPS proxy** and set `ROOST_PUBLIC_ORIGIN` to the exact browser origin, with no path or trailing slash. Keep access restricted to your private network: authenticated users can submit work to the agent.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `ROOST_PUBLIC_ORIGIN` | Exact browser origin; HTTPS required except for local HTTP previews. | Required |
| `ROOST_HERMES_URL` | Hermes dashboard base URL. | Required |
| `ROOST_HERMES_USERNAME` | Dashboard password-provider username. | Required |
| `ROOST_HERMES_PASSWORD_FILE` | File containing the dashboard password. | Required |
| `ROOST_AGENT_NAME` | Agent name shown in the interface. | `Agent` |
| `ROOST_STATE_DIR` | Directory for the password hash and session database. | `.roost-state` |
| `ROOST_TRUST_FORWARDED` | Set to `1` only when a trusted proxy is the service's sole entry point and controls `X-Forwarded-For`. | Off |

The server command also accepts `--host` and `--port`; defaults are `127.0.0.1` and `8787`. With forwarded-header trust off, only loopback peers' forwarded addresses are trusted. Turning it on trusts that header from every peer, so direct access must be blocked.

## Security

The Python server holds the Hermes dashboard credential. The browser receives only Roost's conversation and Job routes; it does not receive dashboard credentials, inference keys or direct gateway access. Conversation text and Job prompts are rendered without raw HTML injection.

Sessions last up to 30 days and use revocable, server-side records with HttpOnly, SameSite Strict cookies. HTTPS enables Secure cookies. Sign-in is rate limited.

Run `set-password` again to change your Roost password; no restart is needed. **Existing sessions remain valid.** To revoke them, sign out on each device, or stop Roost and remove its session database before restarting.

Keep credentials, state and conversation data out of version control. For suspected exposure or vulnerabilities, follow [SECURITY.md](SECURITY.md).

## Preview status

The integration targets Hermes **`v2026.9.24`**. The previous README records a successful live loopback check on 2026-10-11 covering sign-in, conversation listing and transcripts, two-turn chat, and Job creation, pause, edit, resume and deletion. Those live checks have not been repeated for this documentation update.

- Phone layouts are implemented; a phone browser has not been verified.
- Voice, push notifications and a direct Hermes plugin are not implemented.
- Schedules are sent to Hermes as entered; there is no schedule builder yet.
- Job history shows what Hermes returns; push delivery of full reports is future work.
- Replies may take time to appear. Hermes continues submitted turns after Roost disconnects, and Roost polls for the transcript.

Integration with **local-ai-stack** is planned as a CPU-only service. Packaging, installer changes, a private HTTPS front door and host commissioning are separate deployment work. Hermes retains responsibility for model access and the gaming gate.

## Development

Install the pre-push guard once per clone, then run the repository checks:

```bash
make hooks
make PYTHON=.venv/bin/python check
```

For server or interface changes, also run the demo smoke check:

```bash
make PYTHON=.venv/bin/python smoke
```

Checks scan the working tree and reachable Git history for public-content issues. The smoke check uses the local stand-in; it does not validate a live Hermes deployment. See [AGENTS.md](AGENTS.md) for contribution rules.

## License

Roost uses the [MIT License](LICENSE). Bundled Space Grotesk fonts by Florian Karsten use the [SIL Open Font License](LICENSES/SpaceGrotesk-OFL.txt).
