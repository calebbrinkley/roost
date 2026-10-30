# Set up Roost

Run one Roost process with a private writable state directory and access to an OpenAI-compatible
model API. For containers, follow [Docker Compose](docker-compose.md).

## Local installation

Python 3.11+ is required. From the checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
export ROOST_STATE_DIR=/REPLACE_PRIVATE_DIRECTORY/state
export ROOST_PUBLIC_ORIGIN=https://roost.example.com
.venv/bin/python -m roost set-password
.venv/bin/python -m roost serve
```

Put a trusted HTTPS reverse proxy in front of loopback port 8787. Use a service manager that restarts
on failure; Restart Roost exits with status 75 after replying. A local HTTP preview is allowed only
on loopback; HTTPS is required for normal installed-app use. Keep the origin exact, including a
non-default port. Never trust client-supplied forwarding headers on an unprotected public listener.

## Connect services in Settings

1. **Models**: save a `/v1` base URL and optional API key; discover or add model IDs, enable models,
   choose the default, and save. Set thinking mode, tools and context size per model. See
   [model servers](model-server.md).
2. **Persona**: edit the default preset's name and instructions. Its name labels the assistant,
   including before sign-in. Chats may select another preset and have their own instructions.
3. **Memory**: manage facts used across chats. The model may save facts when its tools are enabled.
4. **Web search** (optional): save a SearXNG base URL with JSON search enabled, then Test.
5. **Voice** (optional): use the primary API's speech endpoints or configure a custom speech `/v1`
   base and key. Test playback and then try a short recording.
6. **Notifications** (optional): add this browser/device and send a test. An installed PWA and
   device permission may be required. The server needs outbound HTTPS to browser push services.

[Jobs](jobs.md), [attachments](attachments.md), [voice](voice.md), [fallback](fallback.md) and the
[API](api.md) describe their controls and limits.

## Environment and state

| Variable | Meaning | Default |
| --- | --- | --- |
| `ROOST_PUBLIC_ORIGIN` | Exact HTTPS browser origin; HTTP allowed only for loopback preview | Required |
| `ROOST_STATE_DIR` | Private writable state directory | `.roost-state` |
| `ROOST_MODEL_URL` | Installer prefill for an absent or empty primary URL | Empty |
| `ROOST_MODEL_KEY_FILE` | Installer key prefill only while filling that URL, and only if no key exists | Empty |
| `ROOST_ADMISSION` | Concurrent model turns, 1–16 | `1` |
| `ROOST_PUSH_SUBJECT` | Public HTTPS origin or mailto VAPID subject | Browser origin |
| `ROOST_TRUST_FORWARDED` | Trust forwarding header from any peer; only behind an exclusive trusted proxy | `0` |
| `ROOST_REVISION` | Optional hexadecimal build revision displayed on About | Empty |

Prefill never changes a configured primary URL or key. An earlier start without env may have saved
an empty primary; later env prefill still works. Settings remains authoritative after configuration.
Models must still be curated and a default selected. Search and speech are configured in Settings.
The earlier `ROOST_NATIVE_ADMISSION` name remains an alias when `ROOST_ADMISSION` is unset; it
controls concurrency only.

State includes `password.hash`, `sessions.sqlite3` (sessions, passkeys, push subscriptions),
`native.sqlite3` (chat, personas, memory, jobs and outbox), `servers.json`, `model-keys.json`,
`settings.json`, `voice.json`, `speech-keys.json` and `vapid.pem`. Keep the complete directory private;
back it up with the service stopped, or use SQLite's backup facilities for a consistent live copy.
The existing chat-store filename is retained for installed users. Unknown old preference keys and
retired credential/token files are ignored; they need not be removed to start Roost.

Model/speech keys are write-only in the UI and stored in separate 0600 files. A blank key field
keeps the saved key; Clear saved key removes it. Rotate credentials through their provider if exposed.
See [security](../SECURITY.md).
