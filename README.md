<p align="center">
  <img src="assets/roost-logo.png" alt="roost" width="240">
</p>

<p align="center"><strong>A private home for your Hermes agent.</strong><br>Conversations, voice messages, and scheduled work in one clean web app.</p>

<p align="center">
  <a href="https://github.com/calebbrinkley/roost/actions/workflows/checks.yml"><img src="https://github.com/calebbrinkley/roost/actions/workflows/checks.yml/badge.svg" alt="Repository checks"></a>
</p>

<p align="center">
  <a href="#try-the-demo">Try the demo</a> ·
  <a href="docs/setup.md">Setup</a> ·
  <a href="docs/compatibility.md">Compatibility</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <a href="assets/roost-iphone-chat-dark.png"><img src="assets/roost-iphone-chat-dark.png" alt="Roost Messages in dark mode with a demo conversation" width="280"></a>
  <a href="assets/roost-iphone-dark.png"><img src="assets/roost-iphone-dark.png" alt="Roost Jobs in dark mode with sample scheduled work" width="280"></a>
</p>
<p align="center"><sub>Messages and Jobs. AI-generated device mockups with demo data; not physical-device test captures.</sub></p>

Roost is an owner-only Progressive Web App for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It connects to your existing Hermes dashboard and brings conversations and scheduled Jobs into a focused interface for desktop and phone layouts. Your dashboard credentials stay on the Roost server.

**Early preview.** Core chat and Job operations have recorded live Hermes checks. Newer features have automated coverage against local stand-ins; phone browsers and device-specific behavior remain unverified. See [compatibility and validation status](docs/compatibility.md).

## What you can do

| Area | Capabilities |
| --- | --- |
| **Conversations** | Start a chat or continue one from another Hermes channel. Search, rename, delete, add conversation instructions, and download history as JSON. Replies to messages sent from Roost stream as they are written. |
| **Voice** | Hold the microphone to send a voice message. Play replies aloud, with automatic playback for voice-message replies, using Hermes's configured speech services. |
| **Jobs** | Create and edit scheduled work, search Jobs, pause or resume them, run one immediately, and inspect its history. Schedules are entered directly; there is no schedule builder yet. |
| **Notifications** | Enable reply notifications per device. Install the bundled Hermes platform plugin to receive Job reports and messages the agent sends on its own. |
| **Personalization** | Choose a system, light, or dark theme per device. Set a shared accent color and display time zone, name your agent, and edit its SOUL instructions. |
| **Account and service** | Sign in with a password or add passkeys. Manage notification devices, view build and service status, and restart Roost or the Hermes gateway. |

## Try the demo

Requires **Python 3.11+**. No running Hermes agent or model is needed: the demo uses a local stand-in and disposable sample data.

```bash
git clone https://github.com/calebbrinkley/roost.git
cd roost
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python dev/demo.py
```

Open **http://127.0.0.1:8787** and sign in with **`roost-demo-only`**.

Stop with Ctrl+C. Demo state lives in the Git-ignored `.roost-demo-state/` directory. The demo previews the interface; it does not validate a live Hermes connection.

## Connect your agent

Follow the [setup guide](docs/setup.md) to connect a running Hermes dashboard with its password provider enabled. The integration currently targets **Hermes `v2026.9.24`**.

1. Install Roost and set its own owner password.
2. Store the Hermes dashboard password in a private file outside the checkout and configure the connection.
3. Run Roost behind a private HTTPS proxy for access from other devices.
4. Optionally install the bundled Hermes plugin for Job reports and agent-initiated messages.

Roost binds to loopback by default. Keep remote access private: signing in lets the owner submit work to the agent. Roost does not start Hermes or load models; Hermes handles agent execution and model access.

The setup guide covers configuration, passkeys, notification delivery, and service restarts.

## Use it on your phone

Open your private HTTPS Roost address in the browser and add it to your Home Screen for an app-style experience.

On iPhone and iPad, reply notifications require a Home Screen installation on **iOS/iPadOS 16.4+**. Voice, push delivery, and passkeys have not yet been verified on physical devices. Review the [platform limitations](docs/compatibility.md) before relying on them.

## Privacy and access

Roost has its own sign-in and keeps the Hermes dashboard credential on the server. The browser uses Roost's selected routes rather than receiving the dashboard password or inference keys. Sessions are revocable and stored server-side; sign-in is rate limited.

Keep credentials, state, and conversation data outside version control. Use a private HTTPS front door for remote access, and follow [SECURITY.md](SECURITY.md) for vulnerability reports or accidental disclosure.

## Development

The runtime is a Python server with a plain HTML, CSS, and JavaScript interface. **Node.js 18+** is needed for the browser-logic tests, not to run Roost.

After installing the package, work on a short-lived branch and run:

```bash
make hooks
make PYTHON=.venv/bin/python check
```

Server or interface changes also need:

```bash
make PYTHON=.venv/bin/python smoke
```

These checks use sample data and local stand-ins. They do not commission a deployment or prove behavior against a live agent or phone. See [CONTRIBUTING.md](CONTRIBUTING.md) for the pull request workflow and [repository security](docs/repository-security.md) for publication rules.

## License

[MIT](LICENSE). Bundled Space Grotesk fonts by Florian Karsten use the [SIL Open Font License](LICENSES/SpaceGrotesk-OFL.txt). Asset notices are in [LICENSES](LICENSES).
