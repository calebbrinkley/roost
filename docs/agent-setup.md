# Set up Roost with a Hermes agent

This runbook is for the agent installing Roost for its owner. Read [AGENTS.md](../AGENTS.md), the [setup guide](setup.md), and [compatibility status](compatibility.md) before changing the host. Install a connected Roost instance, not the disposable demo. Roost is a personal Hermes client; it does not require a new model server or a separate worker.

## 1. Discover the existing installation

Inspect what you can before asking the owner for information. Never print secret values or live transcripts into chat, logs, or a public issue.

- Find the machine and account that should run Roost, its operating system, Git, and either Docker with Compose v2 or a Python 3.11+ interpreter. Node.js is only needed for development checks.
- Find the existing Hermes dashboard URL, password-provider username, version, and the profile it serves. Confirm a model is already configured. A running messaging gateway alone is not a dashboard endpoint.
- Find any existing Roost checkout, service, private configuration, state directory, and HTTPS proxy. Inspect port availability and reuse the installation when appropriate; do not overwrite state, reset the owner's password, or create a duplicate service.
- Determine the browser URL and how the owner's devices reach it. Prefer an existing private HTTPS proxy; Tailscale Serve is an option. Do not publish Roost to the public internet or change unrelated proxy routes.

Ask only for details you cannot discover, including the intended host or access method when ambiguous. Report your intended changes before installing. If Hermes is missing its dashboard or password authentication, complete that prerequisite using the installed Hermes version's documentation first; do not guess commands or silently downgrade Hermes. Roost targets `v2026.9.24`; treat a different version as requiring connection and protocol validation.

## 2. Install and configure

Use the [Docker Compose guide](docker-compose.md) if the owner already uses Docker; otherwise follow [setup steps 0–3](setup.md#0-install-roost) on the Roost host. Keep an existing checkout's local changes intact. Do not use `dev/demo.py`, its sample password, or `ROOST_DEMO=1` for this installation. With Compose, use its password-setup command and service lifecycle commands instead of installing a host virtual environment or a separate service manager.

For Compose, complete its guide and then continue at [verification](#4-verify-before-handing-over); Compose manages the service, and its guide distinguishes host paths from container paths. For a native installation, choose an absolute private state directory outside the checkout, owned by the service account. Use `umask 077` when creating private directories and files. Persist these environment values in the service manager's private configuration:

| Variable | Value to discover or arrange |
| --- | --- |
| `ROOST_STATE_DIR` | The same absolute state directory for password setup and every service start. |
| `ROOST_PUBLIC_ORIGIN` | Exact browser origin: private HTTPS for other devices; `http://127.0.0.1:8787` is allowed for a local check. No path or trailing slash. |
| `ROOST_HERMES_URL` | Reachable Hermes dashboard base URL, as seen from the Roost process. Loopback refers to that process's host or container. |
| `ROOST_HERMES_USERNAME` | Existing dashboard password-provider username. |
| `ROOST_HERMES_PASSWORD_FILE` | Absolute path to a private file outside the checkout containing the dashboard password. |
| `ROOST_REVISION` | Installed commit from `git rev-parse HEAD`. |

Have the owner enter the Hermes password locally in a private file, or use an existing service-readable secret file. Set mode `600` and restrict the parent directory. Do not ask for passwords in chat or put them in command arguments. Have the owner run the interactive `set-password` command in [setup step 1](setup.md#1-set-your-roost-password) with the chosen state directory. It requires at least 12 characters. Reuse an existing password hash when present.

Start Roost with `.venv/bin/python -m roost serve` and those environment values. Keep its default `127.0.0.1:8787` binding for a same-host proxy; choose an unused port explicitly if necessary. Configure the private HTTPS front door and exact public origin together. Do not disable TLS certificate verification or enable `ROOST_TRUST_FORWARDED` without the proxy isolation described in [configuration](setup.md#configuration).

## 3. Make the service persistent

Use the host's existing service manager: systemd on Linux or launchd on macOS, for example. Configure the absolute virtual-environment Python path, `-m roost serve` arguments, working directory, and private environment. Run as an unprivileged service account that can read the dashboard credential and write Roost state. Do not put secrets in a public unit or checkout. Configure restart on failure, including exit code 75, so the in-app restart button works. Check the manager's environment-file syntax; a shell `export` script is not automatically a systemd environment file.

Verify the process stays up after the setup terminal closes and starts through the manager. Check its boot/login startup configuration and report whether persistence depends on the owner being logged in. Do not reboot the owner's machine just to test this; mark reboot persistence as pending if untested. Preserve any pre-existing service definition for recovery.

## 4. Verify before handing over

Perform these checks against the actual installation. Do not report a stand-in, healthy process, or static screenshot as proof of a Hermes turn.

1. On the host, request `/api/health` on Roost's loopback port. Expect HTTP 200 with `{"status": "ready"}`. This endpoint does not test Hermes.
2. Without a signed-in browser session, request `/api/conversations`. Expect HTTP 401. Do not include credentials in the probe command.
3. Have the owner open the final private HTTPS URL and sign in with the Roost password. Verify Messages and Jobs load without upstream authentication errors. Empty lists are valid on a new agent.
4. With the owner's agreement, send a short harmless message from Roost and confirm a completed Hermes reply. Reload and confirm that the conversation persists. Do not create Jobs or run existing Jobs just to test setup.
5. Restart Roost through the service manager, repeat the health check, and confirm the same account, settings, and conversation remain available. Check Settings › About for the installed revision.
6. If phone access is wanted, have the owner open the URL on that device and add it to the Home Screen. Record anything they have not yet tested. Passkeys need a hostname and HTTPS; iOS notifications need a Home Screen installation on iOS/iPadOS 16.4+.

If a check fails, diagnose that layer before continuing: health means the Roost process; a 401 before sign-in is expected; upstream errors after sign-in mean the dashboard URL/authentication or compatibility needs attention. Do not weaken authentication or expose the service as a workaround.

## 5. Optional Hermes plugin

Offer the bundled plugin for reply measurements, Job reports, and agent-initiated messages. Basic chat does not require it. Follow [the plugin instructions](setup.md#4-optional-reply-measurements-and-job-reports-from-hermes): download from Settings, install into the active Hermes home's `plugins/roost`, enable it, and configure `ROOST_URL`, `ROOST_DELIVERY_TOKEN`, and `ROOST_HOME_CHANNEL=roost` in Hermes's private environment. `ROOST_URL` is the address Hermes can reach, which can differ from the browser origin.

Keep the token private, preserve existing plugins and environment values, and coordinate any Hermes restart with the owner so you do not interrupt their active work. Reply measurements require restarting the Hermes process that serves Roost, not only a separate messaging gateway. Report plugin delivery as unverified until a real delivery succeeds.

## 6. Leave a usable handoff

Give the owner the final URL, installed Roost commit and Hermes version, private configuration and state locations (no secret values), and the exact commands for their service manager to start, stop, restart, inspect status, and view local logs. Summarize checks passed and checks awaiting the owner.

Document updates and recovery for this installation: record the previous commit, stop the service, securely back up state and private configuration, fast-forward a clean checkout, reinstall with the virtual-environment Python, update `ROOST_REVISION`, restart, and repeat acceptance checks. If an update fails, stop the service and restore the previous code/dependencies and matching state backup before restarting. Do not reset a dirty checkout or overwrite a running database.
