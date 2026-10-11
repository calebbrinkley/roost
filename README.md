# Roost

Roost is an owner-only web app for chatting with a Hermes agent. This repository contains the first **internal preview**: password sign-in, a single Messages list for Hermes conversations, and a Jobs interface for scheduled work. Conversations from outside Roost carry a source badge. Jobs can be created, edited, paused, resumed, run immediately, and deleted after confirmation.

The Python server holds the Hermes dashboard credential. Browser code can call only Roost's narrow conversation and Job routes; it never receives the dashboard credential, inference key, or a raw Hermes gateway socket. Conversation text and Job prompts are rendered through safe DOM nodes, never injected as raw HTML.

The interface follows the approved Roost UI samples: a Messages list and Jobs switch, forest-green owner bubbles, and neutral agent bubbles. Jobs now use the same list-and-detail layout as Messages: select a job in the sidebar to see its schedule, instructions, last report, and run history, with run and edit actions below. On a phone, the Jobs list opens a full job detail with a back button. The light/dark toggle follows the system theme until you choose one, then remembers your choice in this browser. The preview's mic is visibly disabled until voice is implemented; push notification screens remain design references.

## Try it with sample data

The local demo runs Roost beside a disposable Hermes stand-in. It binds both services to loopback, seeds conversations from Roost and Telegram plus two Jobs, and generates sample replies. It does not contact a live Hermes agent.

```bash
.venv/bin/python dev/demo.py
```

Open `http://127.0.0.1:8787` and sign in with `roost-demo-only`. This is sample data served by a mock Hermes endpoint, not a live agent. The demo uses `.roost-demo-state/`, which is ignored by Git. Stop it with Ctrl+C. With the demo running, `.venv/bin/python dev/smoke.py` exercises sign-in, chat, Jobs, deletion confirmation, and sign-out.

## Run the internal preview

Requirements: Python 3.11 or newer and a reachable Hermes dashboard with its password provider enabled. Installing Roost installs its pinned `aiohttp` dependency. Hermes must be configured to use the desired agent and model. Roost does not start Hermes or a model.

```bash
cd roost
python3 -m venv .venv
.venv/bin/python -m pip install -e .
export ROOST_STATE_DIR="$PWD/.roost-state"
.venv/bin/python -m roost set-password
```

Set these values in the service environment, using your own paths and endpoint. Keep the Hermes password in a readable file that is **outside version control**. The public origin must match the browser's address exactly, with no trailing slash or path.

```bash
export ROOST_PUBLIC_ORIGIN="http://127.0.0.1:8787"
export ROOST_HERMES_URL="https://REPLACE_HERMES_DASHBOARD_HOST:8445"
export ROOST_HERMES_USERNAME="REPLACE_OPERATOR_USERNAME"
export ROOST_HERMES_PASSWORD_FILE="REPLACE_PATH_TO_HERMES_PASSWORD_FILE"
.venv/bin/python -m roost serve
```

Open `http://127.0.0.1:8787`. Set the Roost password using `set-password`; it is separate from the Hermes dashboard password. Roost binds to loopback by default. For access from another device, put the service behind a private HTTPS front door and set `ROOST_PUBLIC_ORIGIN` to its HTTPS origin. Keep the service private; its authenticated routes can submit work to the agent.

### Configuration

| Variable | Purpose |
|---|---|
| `ROOST_PUBLIC_ORIGIN` | Exact browser origin; required. HTTPS except local HTTP preview. |
| `ROOST_HERMES_URL` | Hermes dashboard base URL; required. |
| `ROOST_HERMES_USERNAME` | Hermes dashboard basic-auth user; required. |
| `ROOST_HERMES_PASSWORD_FILE` | Readable file containing its password; required. |
| `ROOST_STATE_DIR` | Password hash and revocable session database; defaults to `.roost-state`. |

The browser session lasts up to 30 days, uses a server-side revocation table, and is carried by a SameSite Strict, HttpOnly cookie. The cookie is Secure when the public origin uses HTTPS. Changing the Roost password does not automatically revoke existing sessions; sign out on each device or remove the session database while Roost is stopped if you need to revoke all sessions.

## Hermes contract

Roost signs into the Hermes dashboard through `/auth/password-login`. It uses session list and transcript REST routes, the cron Jobs REST routes, and a single-use ticket for the dashboard gateway WebSocket. Its server chooses only `session.create`, `session.resume`, and `prompt.submit` RPC calls. A submitted turn continues on Hermes after Roost disconnects; the app polls for the resulting transcript. A long answer can therefore take time to appear.

The integration targets the pinned Hermes version in `local-ai-stack` (`v2026.9.24`). On 2026-10-11 a loopback preview passed against a live dashboard: sign-in, the session list and a transcript, a real two-turn chat (the conversation is listed with source `roost`), and Job create, pause, edit, resume and delete. Live Jobs store `schedule` as an object (`expr` or `run_at`, plus `display`), use `enabled` rather than `paused`, report `last_status` as `ok`, `delivery_failed`, `blocked_config` or `error`, and keep no report on the job; the demo's mock follows those shapes. A phone browser has **not** been verified yet.

## Integration with local-ai-stack

Roost's intended stack service is CPU-only. It should join `skynet-internal`, call `http://skynet-hermes:9119`, mount the Hermes dashboard password read-only, and keep its own state at `/srv/skynet-ai/state/roost`. It needs an HTTPS front door on the private tailnet; the browser must never call Hermes directly. The public stack's installer requires a digest-pinned image or a hash-locked artifact, a rendered Quadlet, a generated Roost password secret, an authenticated remote probe, documentation, and host commissioning before deployment. Those installer changes are **not yet implemented**. This repository has no image release or artifact digest to pin yet.

## Preview boundaries

- Text chat and Jobs are implemented in source. Voice, push notifications, and the direct Hermes plugin are later release work.
- Job run history displays the data Hermes returns; full report delivery in push belongs to the later notification release.
- A Jobs schedule is sent to Hermes as entered. Roost does not offer a schedule builder yet.
- Roost does not call the inference gate directly. Hermes retains model and gaming-gate behavior.
- No host service, remote front door, or iPhone installation has been changed by this source work.
