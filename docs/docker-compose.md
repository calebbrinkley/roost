# Docker Compose

The shipped Compose service builds Roost, runs as the configured unprivileged UID/GID, drops all
capabilities, uses a read-only root filesystem and publishes only on host loopback. It does not
start a model, search or speech server. Configure those independently.

## Install

Create a private directory outside the checkout and copy `compose.env.example` into it as
`compose.env`. Create the state directory and model-key file named there. Keep the directory 0700
and key file 0600, owned by the UID/GID you specify. An empty key file is valid for a provider
without authentication. Do not commit this configuration.

Set `ROOST_PUBLIC_ORIGIN=https://roost.example.com` to the exact browser origin. Optionally set
`ROOST_MODEL_URL=https://models.example.com/v1` for installer prefill. The provider must be
reachable from the container: its loopback is not the host's loopback. Keys are mounted read-only
at `/run/secrets/model_key`; `ROOST_MODEL_KEY_FILE` points there inside the container.

From the checkout, use the same external env file for every command:

```sh
docker compose --env-file /REPLACE_PRIVATE_DIRECTORY/compose.env config --quiet
docker compose --env-file /REPLACE_PRIVATE_DIRECTORY/compose.env build
docker compose --env-file /REPLACE_PRIVATE_DIRECTORY/compose.env run --rm roost set-password
docker compose --env-file /REPLACE_PRIVATE_DIRECTORY/compose.env up -d --wait
```

Route the configured HTTPS origin through your trusted proxy to `127.0.0.1:8787`, or the chosen
`ROOST_PORT`. Sign in and finish model setup in Settings, including enabled models and a default.
Configure optional SearXNG, speech and fallback services in Settings. See [setup](setup.md).

## Persistence and updates

All writable state is bind-mounted into `/var/lib/roost`; Compose refuses to create a missing host
state directory. Back it up privately before an update. Rebuild from the reviewed revision and run
`up -d --wait`. Recreating the container retains chats, personas, jobs, credentials and account data.
Do not downgrade across a populated store migration without checking its rollback constraints.

The health check calls `/api/health`; it establishes that Roost answers, not that any external
service works. Validate a real chat, speech, search and notification separately after installation.

## Disposable installation check

```sh
make PYTHON=.venv/bin/python smoke-compose
```

This requires Docker Compose v2 and a working engine under an unprivileged account. It builds the
actual image and uses temporary state and deterministic containerized providers. It checks owner
authentication, provider reply, optional service contracts, jobs, non-root/read-only execution,
secret mounting and state persistence through restart/recreation. It makes no live-provider claim.
