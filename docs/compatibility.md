# Compatibility and preview status


The integration targets Hermes **`v2026.9.24`**. The project records a successful live loopback check on 2026-10-11 covering sign-in, conversation listing and transcripts, two-turn chat, and Job creation, pause, edit, resume and deletion. These recorded live checks have not been repeated as part of repository governance work. Repository CI uses a local stand-in, not a live agent.

- Phone layouts are implemented; a phone browser has not been verified.
- Voice, push notifications and a direct Hermes plugin are not implemented.
- Schedules are sent to Hermes as entered; there is no schedule builder yet.
- Job history shows what Hermes returns; push delivery of full reports is future work.
- Replies may take time to appear. Hermes continues submitted turns after Roost disconnects, and Roost polls for the transcript.

Integration with **local-ai-stack** is planned as a CPU-only service. Packaging, installer changes, a private HTTPS front door and host commissioning are separate deployment work. Hermes retains responsibility for model access and the gaming gate.


The README images are AI-generated device mockups based on demo captures. They are not evidence of physical iPhone testing.

Runtime and UI fixes and their regression coverage are recorded in the [code review](code-review.md). Those checks use fake data and do not repeat the historical live Hermes validation.
