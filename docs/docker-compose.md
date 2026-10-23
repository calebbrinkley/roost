# Install with Docker Compose

Requires Git, Docker Engine or Docker Desktop with Compose v2, and an existing Hermes dashboard with password authentication. Roost runs as one container; Hermes and your private HTTPS proxy stay separate. No host Python installation is needed.

## 1. Prepare private configuration

Run from a regular user account on the Docker host:

```bash
git clone https://github.com/calebbrinkley/roost.git
cd roost
umask 077
ROOST_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/roost"
mkdir -p "$ROOST_CONFIG/state"
cp compose.env.example "$ROOST_CONFIG/compose.env"
touch "$ROOST_CONFIG/hermes-password"
chmod 700 "$ROOST_CONFIG" "$ROOST_CONFIG/state"
chmod 600 "$ROOST_CONFIG/compose.env" "$ROOST_CONFIG/hermes-password"
id -u
id -g
```

Use a private text editor to put the dashboard password in `hermes-password` and edit `compose.env`. Do not enter passwords in shell commands or chat. Replace every placeholder:

| Setting | Value |
| --- | --- |
| `ROOST_PUBLIC_ORIGIN` | Your exact private **HTTPS** browser origin, with no path or trailing slash. |
| `ROOST_HERMES_URL` | Hermes dashboard URL reachable **from the container**. |
| `ROOST_HERMES_USERNAME` | Dashboard password-provider username. |
| `ROOST_HERMES_PASSWORD_SOURCE` | Absolute host path to the private `hermes-password` file. |
| `ROOST_STATE_PATH` | Absolute host path to the private `state` directory. |
| `ROOST_UID`, `ROOST_GID` | The two numbers printed by `id -u` and `id -g`. The container uses these IDs to read the mode-600 credential and write your state. |

`ROOST_PORT` optionally changes the host loopback port; it defaults to 8787. State and credentials stay outside the checkout and image. Use the same paths and IDs for every start, password change, and update. For an existing installation, stop its service and back up state before reusing its directory; do not run two instances against the same database.

**Hermes connectivity:** `localhost` and `127.0.0.1` inside the container refer to the container itself. A dashboard listening only on the host's loopback address needs a reachable private proxy, or you can keep the [native Python installation](setup.md). Docker Desktop also provides `host.docker.internal` for host access; Linux host networking differs. Do not expose the dashboard publicly to make it reachable. See [Docker networking](https://docs.docker.com/desktop/features/networking/networking-how-tos/#connect-a-container-to-a-service-on-the-host).

## 2. Build and set the owner password

Run these commands from the checkout, with `ROOST_CONFIG` set to your private folder:

```bash
docker compose --env-file "$ROOST_CONFIG/compose.env" build
docker compose --env-file "$ROOST_CONFIG/compose.env" run --rm roost set-password
```

Enter a new Roost password of at least 12 characters at the hidden prompts. This is separate from the dashboard password. For an existing installation, keep its password hash instead of resetting it. The container runs without root privileges and uses a read-only root filesystem; only your mounted state directory is writable.

## 3. Start and connect

```bash
docker compose --env-file "$ROOST_CONFIG/compose.env" up -d --wait
docker compose --env-file "$ROOST_CONFIG/compose.env" ps
```

Roost publishes **127.0.0.1:8787** on the Docker host. Point your existing private HTTPS proxy at that loopback port and use the same HTTPS origin configured above. The [private HTTPS instructions](setup.md#3-access-it-from-another-device) include a Tailscale Serve example. Open the HTTPS address, sign in, load Messages and Jobs, and send a short test message. Container health checks confirm Roost is running; they do not verify Hermes authentication or a completed reply.

HTTP origins are not supported by this container setup, because the server must listen on the container's network interface. Use the [local demo](../README.md#try-the-demo) for an HTTP-only preview. Keep the host port bound to loopback. Leave forwarded-header trust off unless your proxy is the sole entry point and overwrites `X-Forwarded-For`; see [configuration](setup.md#configuration).

## Manage and update

```bash
docker compose --env-file "$ROOST_CONFIG/compose.env" logs --tail 50 roost
docker compose --env-file "$ROOST_CONFIG/compose.env" restart roost
docker compose --env-file "$ROOST_CONFIG/compose.env" stop
docker compose --env-file "$ROOST_CONFIG/compose.env" up -d --wait
```

Run `set-password` again to change the Roost password. Existing sessions remain valid, as described in [security](setup.md#security). `restart: unless-stopped` restarts Roost after its in-app restart and after Docker restarts, except when you explicitly stop it. Docker Desktop must be running; unattended startup depends on your host's Docker configuration.

To update, record the current Git commit, stop Roost, and securely back up the private state and configuration folders. Fast-forward a clean checkout with `git pull --ff-only`, then run `build` and `up -d --wait` again and repeat the connection check. Keep the previous commit and matching state backup for recovery. `down` removes the container and network, while your host state folder remains. Deleting that folder deletes your Roost account and settings. Never commit backups or share live logs.

Compose secrets are file mounts, not an encrypted credential vault; protect the source file and its parent directory. Details: [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).
