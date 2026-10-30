# Roost

A self-hosted, installable chat client for any OpenAI-compatible model server. Roost owns your
conversations and runs accepted replies even when the browser disconnects.

- Streaming chat with Thoughts, citations, model selection, thinking controls and reply statistics
- Searchable local chat history, exports, durable queues, Stop, edit and regenerate
- Personas, saved memory and automatic context compaction
- Web search through optional SearXNG, with guarded public-page reading
- Text and PDF attachments
- Optional speech server for transcription and reading replies aloud
- Scheduled jobs with saved runs and Web Push notifications
- Optional fallback model server and per-chat Keep this chat local
- Password/passkey sign-in, light/dark themes and phone/desktop layouts

## Requirements

An OpenAI-compatible `/v1` chat-completions server, such as llama.cpp, vLLM, Ollama's OpenAI
endpoint or a hosted API. Model discovery is optional; you can enter model IDs manually.
SearXNG JSON search and an OpenAI-compatible speech server are optional. See
[service contracts](docs/compatibility.md) and [connecting model servers](docs/model-server.md).

Use HTTPS through a trusted reverse proxy or private network for an installed PWA, passkeys,
microphone capture and notifications. Roost is an owner-only app, not a public multi-user service.

## Quick start with Docker Compose

Install Docker with Compose v2. Clone this repository and create a private configuration directory
outside the checkout. Copy `compose.env.example` there, replace the placeholders and create the
state directory and model-key file it names. The key file may be empty for a server without auth.
Set `ROOST_UID`/`ROOST_GID` to the file owner's IDs (`id -u`, `id -g`). Then, from the checkout:

```sh
docker compose --env-file /REPLACE_PRIVATE_DIRECTORY/compose.env build
docker compose --env-file /REPLACE_PRIVATE_DIRECTORY/compose.env run --rm roost set-password
docker compose --env-file /REPLACE_PRIVATE_DIRECTORY/compose.env up -d --wait
```

Route your configured HTTPS origin to the published loopback port, open Roost and sign in.
In **Settings › Models**, set `https://models.example.com/v1`, enter the write-only API key if
needed, discover or add models, enable them and select a default. The same page configures a
fallback. **Persona** supplies the assistant name and instructions; **Memory**, **Web search**
and **Voice** configure the optional features. See [Compose setup](docs/docker-compose.md).

## Configuration

Settings is the normal configuration path. Installers may prefill an empty primary server with
`ROOST_MODEL_URL` and `ROOST_MODEL_KEY_FILE`. Prefill also repairs a saved primary with an empty
URL, preserves an existing key, and never overwrites a configured URL. Curated models and a default
still need to be selected in Settings.

`ROOST_STATE_DIR` holds private account, chat and configuration data. `ROOST_PUBLIC_ORIGIN` is the
exact HTTPS browser origin. Optional `ROOST_PUSH_SUBJECT` provides a public HTTPS or mailto VAPID
subject. `ROOST_ADMISSION` controls concurrent turns (1–16, default 1). Only enable
`ROOST_TRUST_FORWARDED=1` when a trusted proxy is the sole path into the service.

## Security and data

Provider and speech keys stay on the server in separate private files; they never enter browser
responses or chat exports. All private APIs require an owner session; mutations also check the
origin and request header. Search page reads block private/reserved destinations and revalidate
redirects. Web and attachment content remain untrusted model input. A hosted primary or enabled
fallback receives the prompt, including selected memory and attachments. Keep this chat local
prevents fallback use; it does not change where the primary runs.

Keep state, secrets, logs and transcripts outside the public checkout. Back up the complete state
directory privately. See [security](SECURITY.md), [setup](docs/setup.md), [API](docs/api.md) and
[repository policy](docs/repository-security.md).

## Development

Python 3.11+ and Node.js 18+ are required. No frontend build step or JavaScript packages are needed.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
make hooks
make PYTHON=.venv/bin/python check
make PYTHON=.venv/bin/python smoke
PYTHONPATH=. .venv/bin/python dev/demo.py
```

The loopback demo uses disposable state and deterministic providers; its password is
`roost-demo-only`. `make smoke-compose` additionally requires Docker. Tests and smoke use local
fakes, and do not establish live service or physical-device compatibility. See [contributing](CONTRIBUTING.md).

## License

[MIT](LICENSE). Bundled fonts and artwork have notices in the `LICENSES/` directory.
