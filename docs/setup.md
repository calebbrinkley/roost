# Connect Roost to Hermes

Roost requires Python 3.11+ and a running Hermes dashboard. Install the package with the demo instructions in the [README](../README.md), then follow these steps.


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
| `ROOST_STATE_DIR` | Directory for the password hash, session database and settings. | `.roost-state` |
| `ROOST_REVISION` | The commit Roost was built from, shown in Settings › About. Set by whatever builds Roost. | Unset (shown as unknown) |
| `ROOST_TRUST_FORWARDED` | Set to `1` only when a trusted proxy is the service's sole entry point and controls `X-Forwarded-For`. | Off |

The server command also accepts `--host` and `--port`; defaults are `127.0.0.1` and `8787`. With forwarded-header trust off, only loopback peers' forwarded addresses are trusted. Turning it on trusts that header from every peer, so direct access must be blocked.

The agent's name comes from Roost's Settings if set there, otherwise from the display name of the Hermes profile the dashboard serves, otherwise "Agent".

**Restart Roost** in Settings exits the process with status 75 so a service manager restarts it. Run Roost under one that restarts on failure (systemd `Restart=on-failure`, for example), or the button just stops it.

## Security

The Python server holds the Hermes dashboard credential. The browser receives only Roost's conversation, Job and Settings routes; it does not receive dashboard credentials, inference keys or direct gateway access. Conversation text and Job prompts are rendered without raw HTML injection.

**Passkeys.** Once signed in, add a passkey in Settings › Account; the sign-in page then offers it. Passkeys are bound to the host name in `ROOST_PUBLIC_ORIGIN` (the WebAuthn relying party ID), so changing that name makes existing passkeys unusable; the password keeps working. Every passkey use requires user verification (Face ID, Touch ID or the device PIN). No attestation is requested, because only a signed-in owner can add one. Removing a passkey in Roost doesn't delete it from the device, so also remove it from the device's passwords. Passkeys need HTTPS, or `localhost`; an IP-address origin can't use them. Failed passkey sign-ins count toward the same per-device limit as passwords.

Sessions last up to 30 days and use revocable, server-side records with HttpOnly, SameSite Strict cookies. HTTPS enables Secure cookies. Sign-in is rate limited.

Run `set-password` again to change your Roost password; no restart is needed. **Existing sessions remain valid.** To revoke them, sign out on each device, or stop Roost and remove its session database before restarting.

Keep credentials, state and conversation data out of version control. For suspected exposure or vulnerabilities, follow [SECURITY.md](../SECURITY.md).


See [compatibility and preview status](compatibility.md) before deployment.
