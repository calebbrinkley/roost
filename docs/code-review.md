# Runtime and UI review

Reviewed on 2026-10-11. This record describes reproduced issues and regression coverage; it is not a live deployment assessment.

## Fixes

| Area | Problem | Result |
| --- | --- | --- |
| Sign-in | Concurrent attempts passed the throttle before password verification finished. | Pending attempts count toward both the device and overall limits; capacity is released on error. |
| Input validation | Very long offsets raised an exception, dot identifiers could change upstream paths, and JSON numeric `1` passed deletion confirmation. | Offsets and identifiers are bounded before forwarding; deletion requires boolean `true`. |
| Chat resume | URL encoding was applied to a stored ID inside a WebSocket RPC. | HTTP paths are encoded; RPC IDs retain their original value. |
| Origin configuration | Uppercase schemes and default ports did not match browser origins; uppercase HTTP bypassed the CLI binding check. | Origins are normalized, invalid ports/hosts are rejected, IPv6 loopback is supported, and all HTTP previews bind only to loopback. |
| Hermes responses | Chunked bodies escaped the response-size check; invalid JSON and RPC shapes raised uncaught exceptions. | Response bodies are limited to 8 MiB while reading; malformed data produces a safe upstream error. Invalid list rows are skipped without breaking valid UI content. |
| Chat deadline | Unrelated gateway events restarted the receive timeout indefinitely. | Each RPC has a fixed deadline, including sends and intervening events. |
| Message submission | A failed refresh restored an already accepted message; request completion switched the selected conversation or overwrote a newer draft. | Accepted sends remain accepted, and completion respects the current view and draft. |
| Polling | Older responses could replace newer transcripts or repopulate state after sign-out. | Request ordering and sign-in generations reject stale responses. |
| Sign-out | Private drafts, reports and open dialogs remained in browser state; network failure appeared to be successful sign-out. | Private state and dialogs clear after successful revocation; a failed revocation is reported. |
| Pagination | New conversations created a gap between the refreshed first page and retained older pages. | Refreshes rebuild the loaded range, with server offsets tracked independently of deduplicated rows. |
| Job actions | Repeated saves and Run now clicks submitted duplicate mutations. | Job mutations share a pending guard; controls re-enable after completion. Conversation rename/delete also reject repeated submissions. |
| Job history | Cached runs stopped refreshing and failures stayed on “Loading.” | Runs refresh with the selected Job; failed initial loads offer a retry. |
| Mobile UI | Notices disappeared with the hidden detail panel; cancelled swipes could open Delete; IME Enter submitted early. | Notices appear from list views, cancelled gestures reset, and composition confirmation does not send. Header text and safe-area spacing are bounded. |
| Demo and smoke | One run linked to a missing transcript; delayed replies could target a deleted conversation; smoke could test an existing server on a fixed port. | Demo links resolve, deleted threads are skipped, and smoke reserves ephemeral ports with temporary state and cleanup. |

## Verification

- `make check`: 47 Python tests and 18 JavaScript tests, plus public-content/history scans and documentation checks.
- `make smoke`: isolated loopback Hermes stand-in; sign-in, origin rejection, per-device lockout, password replacement without restart, multi-turn chat, rename/duplicate refusal, linked run transcripts, Jobs CRUD/actions, confirmed deletion and session revocation.
- Headless Chrome with fake demo data at 320×740, 390×844, 1024×768 and 1440×900 in light and dark themes: phone and desktop layouts, chat submission with forced refresh failure, Job creation/pause/deletion, notices and sign-out. Screenshots were inspected locally and are not published as device-validation evidence.
- GitHub’s advisory API returned no published advisory affecting `aiohttp==3.14.4` at review time. This query does not establish that every transitive dependency is vulnerability-free.

The JavaScript regressions use Node.js 18+ and its built-in test runner. Python runtime tests require the installed Roost dependencies. CI installs Roost before running the same checks. The smoke check creates its own servers; an existing demo can remain running.

The streaming response check follows [aiohttp’s stream API](https://docs.aiohttp.org/en/stable/streams.html); RPC deadlines use [Python’s timeout context](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout).

## Remaining validation

These fixes have not been exercised against a live Hermes agent, physical iPhone, Safari, or an installed PWA. The earlier live check recorded in [compatibility](compatibility.md) remains historical evidence. Recheck two-turn chat, conversation management, Job scheduling/actions and report history against the pinned Hermes version before deploying this revision. Recheck keyboard, safe areas and gestures on a physical phone.

Voice and push notifications remain planned features. No service deployment or model activity was performed during this review.
