# Native Phase 2: server contract for the UI task

Hermes remains the default. Select Native with `PATCH /api/settings` and `{"backend":"native"}`. Existing native history and Hermes history remain separate. Switching the view does not cancel accepted native turns. Native starts without Hermes configuration or connectivity. No UI files change in this phase.

All endpoints below require the existing owner cookie, except `/api/me` which retains its public signed-out shape. Mutations retain the existing matching `Origin` and `X-Roost-Request: 1` checks. JSON errors are `{"error":"safe explanation"}`: invalid input/state 400, unauthenticated 401, rejected origin 403, missing conversation 404, conflicting request ID 409, full native queue 429. Existing model-configuration/provider error mappings remain in place. An accepted generation's upstream failure is recorded on its turn, rather than changing the already returned send status.

## Settings and models

| Endpoint | Request | Response / change |
| --- | --- | --- |
| `GET /api/settings` | — | Existing settings plus `backend: "hermes" \| "native"`. Native returns empty `hermes_agent_name`; agent uses saved name or `Agent`. |
| `PATCH /api/settings` | Existing fields and/or `backend` | Same settings view. Invalid backend rejected without saving. |
| `GET /api/me` | — | Existing fields plus `backend`; signed-out response uses `hermes`. |
| `GET /api/settings/servers` | — | Existing `servers` array; curated models may additionally have `context_tokens`. Keys remain write-only. |
| `PUT /api/settings/servers/{server_id}/models` | Existing `{models,default_model}` | Existing server view. Each curated model still requires exactly `id,display_name,enabled,thinking_mode`, with optional integer `context_tokens` from 1 to 100000000. Omit unknown context size; null is rejected. |
| `POST /api/settings/servers/{server_id}/discover` | — | Existing `{models}` merge retains optional curated context sizes. |
| `GET /api/models` | — | `{models:[{id,display_name,enabled,thinking_mode,context_tokens?}],default_model}` from primary, enabled models only. Available in both modes. |

Thinking modes remain `reasoning_effort`, `template_flag`, `unsupported`. Native chat settings keep the existing field name `reasoning`: `none` means Off, with `low`, `medium`, `high`; writes also accept `off` and the old `default` as Off. Models declaring unsupported thinking receive no thinking parameters. No sampling parameters are sent.

## Conversations (Native mode at existing URLs)

| Endpoint | Request | Response |
| --- | --- | --- |
| `GET /api/conversations?offset=0` | Offset 0–100000 | `{sessions:[{id,title,title_source,preview,source:"roost",last_active_at,working}],total}`; newest activity first, 50/page. |
| `POST /api/conversations` | `{text,request_id?,reasoning?,instructions?,model?}` | 201, acceptance shape below. `model` is an enabled primary model ID or null. |
| `POST /api/conversations/{sid}/messages` | `{text,request_id?}` | 200, acceptance shape below. Configure an existing chat separately before sending. |
| `GET /api/conversations/{sid}` | — | Transcript shape below, newest 500 rows in execution order. |
| `GET /api/conversations/{sid}?before={message_id}&limit=500` | Stable cursor belonging to this chat; limit 1–500 | Same transcript shape for the immediately older rows. `has_older` is exact. Explicit `limit` requires `before`. |
| `GET /api/conversations/{sid}/stream` | Existing authenticated EventSource | SSE snapshots below; works for active, queued, or settled turns. |
| `PATCH /api/conversations/{sid}` | `{title}` (up to 200 chars, may be empty) | `{ok:true}`; sets `title_source:"user"`, permanently preventing automatic replacement. |
| `DELETE /api/conversations/{sid}` | `{confirm:true}` | `{ok:true}`; cancels work, removes native rows and search entries. |
| `DELETE /api/conversations` | `{confirm:"DELETE"}` | `{deleted:number}`; same cancellation and cascade for all native chats. |
| `GET /api/conversations/{sid}/settings` | — | `{instructions,reasoning,model}`; model nullable, otherwise default primary model. |
| `PUT /api/conversations/{sid}/settings` | Any nonempty subset of `{instructions,reasoning,model}` | Same settings shape. Instructions max 4000 chars. Applies to future accepted turns. |
| `GET /api/conversations/{sid}/export` | — | Downloadable JSON `{conversation,exported,instructions,reasoning,model,messages}` containing all rows, including partial and queued rows, in execution order. |
| `GET /api/search?q=...` | Nonblank query, max 200 chars | `{sessions:[{...list_row,snippet}]}`; distinct conversations, newest activity first, max 50. Terms are escaped literal FTS phrases joined with AND; operators, column selectors and wildcards are not interpreted as syntax. Hermes returns `{sessions:[]}`. |

Send text is trimmed, nonblank, max 16000 characters. `request_id` is a lowercase UUID v4; omit only for older clients that cannot retry safely. Acceptance atomically creates the turn, user row, assistant placeholder, and first conversation when needed:

`{session_id,turn_id,message_id,assistant_message_id,state,revision,queue_position}`

`queue_position` is 1-based among queued turns, otherwise null. Repeating a request ID with the same original input returns the same IDs and its **current** state/revision/position, even after settings change or restart. A different payload with that ID returns 409. Accepted settings/model/thinking metadata are captured per turn. Cancelling/deleting/pruning turns also removes their request identities.

Transcript:

`{session_id,messages,has_older,live,working,after,started,turn_id,revision,queue,context}`

- `messages`: `{id,role,content,reasoning,timestamp,turn_id,ordinal,revision,state,client_request_id,error}`. Assistant rows additionally contain `stats` and `roost_usage`.
- `state`: `queued | running | completed | failed | interrupted`. The row's exposed state is the owning turn state, including on the user row.
- `queue`: `[{turn_id,position}]`, 1-based FIFO execution order. `working` includes both running and queued turns. `live` means a turn is currently running.
- `after`, `started`, `turn_id`, `revision` describe the running turn, otherwise null. `started` is its durable acceptance timestamp. `after` preserves the existing numeric UI hint for ordinary FIFO sends.
- `context`: `{prompt_tokens,context_tokens}` from the latest measured completion and its model snapshot; either value may be null.
- `stats`: `{usage:{prompt_tokens?,completion_tokens?,total_tokens?},model_seconds,time_to_first_token,tokens_per_second}` when completed, otherwise `{}`. Unreported usage is omitted rather than invented. Durations are seconds. `model_seconds` includes prefill; TPS divides completion tokens by the time from the first token to the end of the stream, and is null without a first token or when that span is zero.
- `roost_usage`: `{input_tokens,output_tokens,model_seconds,time_to_first_token,tokens_per_second}`; unavailable values are null. This preserves the existing usage renderer.

SSE data frames are **full committed snapshots**, each with `reset:true`:

`{reset,user,started,thoughts,text,approvals:[],turn_id,revision,state,done}`

The first frame always includes the full saved partial/final reply. Later frames replace it; never append a full frame without applying `reset`. A queued turn can initially have empty assistant text. `done` is true for a settled turn; if the queue has already advanced the stream resets to the next turn instead. Deletion emits `{done:true,state:"deleted"}` and ends the stream. Cookie revocation ends streams after the existing heartbeat check (15 seconds). Closing the browser stream never cancels generation. Transcript polling remains the durable recovery path.

## New controls

All controls use `POST /api/conversations/{sid}/turns/{action}` and the existing owner/origin guards. Controls require Native backend; Hermes mode returns 404. Turn IDs must belong to `{sid}`.

| Action | JSON request | Response / effect |
| --- | --- | --- |
| `stop` | `{turn_id}` | `{ok:true}`; running target becomes interrupted with its partial retained, then queue continues. Retrying against the settled target is a no-op, so it cannot stop the next reply. A queued target is rejected. |
| `cancel` | `{turn_id}` | `{ok:true}`; queued target and its message pair are deleted. A running/settled target is rejected. |
| `send-now` | `{turn_id}` | `{ok:true}`; queued target only. Interrupt current reply and assign the selected turn the next execution ordinal; remaining queued turns retain relative order. |
| `edit` | `{turn_id,text,request_id}` | Acceptance shape, 200. Settled target only. Drop target and every later turn, including running/queued turns; accept edited text as a new turn using current chat settings/model. Request ID required; retries return original acceptance. |
| `regenerate` | `{turn_id}` | `{ok:true}`; latest settled turn only. Clear assistant text/reasoning/stats and rerun the same user/model/settings snapshot with the same message and turn IDs; revision increases. |

## Native neutral responses (no Hermes calls)

| Endpoint(s) | Request | Native response |
| --- | --- | --- |
| `GET /api/jobs` | — | `{jobs:[]}` |
| `GET /api/jobs/{jid}/runs` | — | `{runs:[]}` |
| `DELETE /api/jobs` | `{confirm:"DELETE"}` | `{deleted:0}` |
| `POST /api/jobs`, `GET/PUT/DELETE /api/jobs/{jid}`, `POST /api/jobs/{jid}/{action}` | Ignored while disabled | `{ok:false,disabled:true}` |
| `GET /api/conversations/{sid}/approvals` | — | `{approvals:[]}` |
| `POST /api/conversations/{sid}/approvals` | Ignored while disabled | `{resolved:false}` |
| `GET /api/settings/soul` | — | `{content:""}` |
| `PUT /api/settings/soul`, `POST /api/service/restart-agent` | Ignored while disabled | `{ok:false,disabled:true}` |
| `GET /api/hermes` | — | `{token:"",url:"",last_delivery:null}` |
| `GET /api/deliveries/{did}` | — | `{}` |
| `POST /api/hermes/token`, `GET /api/hermes/plugin.zip` | Ignored while disabled | `{ok:false,disabled:true}` |
| `POST /api/hermes/deliveries`, `POST /api/hermes/reply-usage` | Any | 404 `{error}`; plugin auth exemptions do not grant native access. |
| `GET /api/about` | — | Existing Roost/runtime/stats fields; `hermes:{reachable:false}` without checking Hermes. |
| `POST /api/audio/transcribe`, `GET /api/conversations/{sid}/speech/{mid}` | Ignored while disabled | `{ok:false,disabled:true}`; native voice comes later. Silence/audio unlock remains available. |

## Execution, migrations, and verification

Migration 1 is unchanged. Migration 2 adds reply statistics and an FTS5 external-content index, backfills existing messages, and maintains it with insert/update/delete triggers. Downgrade removes the derived index and statistics column; it refuses populated statistics rather than silently losing measurements. Any refusal rolls back the whole migration. Native history remains separate from account/passkey/push state.

One application-owned dispatcher admits up to `ROOST_NATIVE_ADMISSION` provider calls (default 1, range 1–16). Each chat executes FIFO, interactive work precedes pending titles, and at most 500 turns can be queued globally. Checkpoints commit about every 250 ms or 1024 accumulated characters; SSE only reads saved revisions. Startup keeps partial output and marks running turns interrupted, then resumes queued turns. There is no automatic replay of a partially streamed call. Titles use the replying model, thinking Off, and `max_tokens:32`; cleaned one-line titles are at most 60 characters. Low-priority title work is best effort and not durably queued.

Completion uses the existing push transport and recent-presence suppression, including immediate `sent_from_here` presence. Push delivery is best effort, with a stable turn tag; no durable outbox in this phase.

Run `.venv/bin/python dev/demo.py --native` for a fake-provider native demo, or `.venv/bin/python dev/smoke.py --native` for just native smoke. `make PYTHON=.venv/bin/python smoke` runs both Hermes and native flows. Tests cover send retry/order, admission/title priority, quiet-stream checkpoints, restart recovery, controls, deletion while streaming, FTS migration/escaping, owner guards, models, rename, and presence-suppressed push.

## Risks for the UI reviewer

- Message IDs are stable allocation IDs, **not** execution order after Send now. Preserve response order or order by `ordinal`, then user before assistant. The old history merge sorts numerically by ID and its `after` overlay hint assumes increasing IDs; the UI follow-up must use turn identity for reordered transcripts. The server already returns pages and exports in correct execution order.
- Control URLs and the `queue`, `context`, `stats` wrappers are new choices; reasoning names and existing send/SSE/transcript wrappers are preserved to limit UI churn.
- Native voice is explicitly disabled until its later phase. Hide the neutral Hermes-only sections/actions and native voice affordances in the UI task.
- Controls other than Stop/edit do not carry request IDs: retry after an uncertain response by refreshing the transcript first. Send-now/cancel reject targets that are no longer queued; regenerate can intentionally be repeated on the latest settled reply.
- Running title calls are not preempted by newly arriving interactive work; priority applies at admission. Interrupted title work falls back to the first user line.
- Validation uses deterministic loopback fakes. No real provider, live Hermes, browser, physical device, or push-service delivery was validated. Hook installation cannot chmod the shared Git hook outside the writable worktree; no commit or push was attempted.
