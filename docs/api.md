# Browser ↔ server API

Roost owns conversations, turns, personas, memory and scheduled jobs. All routes use JSON except
multipart attachment uploads, audio, downloads and SSE. Errors are `{"error":"safe explanation"}`.
Invalid input/state returns 400, no owner session 401, refused origin 403, missing resource 404,
conflicting request ID 409, and a full turn queue 429. Upstream busy, timeout and other failures
map to 503, 504 and 502. Failures after send acceptance are stored on the turn.

Every private route requires the owner cookie. Public routes are `/api/health`, `/api/me`,
password login and passkey login/options. Every mutation, including login, requires an exact
`Origin: <ROOST_PUBLIC_ORIGIN>` and `X-Roost-Request: 1`. There are no alternate credential routes.

## Account, settings and models

| Route | Contract |
| --- | --- |
| `GET /api/health` | `{status:"ready"}`; liveness only, no upstream check. |
| `GET /api/me` | `{signed_in,demo,agent,accent,time_zone,passkeys}`. `agent` is the default persona's name even before sign-in; private preferences are withheld until sign-in. |
| `POST /api/login` | `{password}`; issues an HttpOnly, SameSite Strict cookie (Secure under HTTPS). |
| `POST /api/logout` | Revokes the calling session and expires its cookie. |
| `GET /api/settings` | `{accent,time_zone,search_url,use_fallback,accents,agent}`. |
| `PATCH /api/settings` | One or more saved preference fields; same view. Unknown fields are rejected. Enabling fallback requires a configured fallback/default. |
| `GET /api/settings/servers` | `{servers:[{id,base_url,models,default_model,has_key}]}` for primary and optional fallback. |
| `PUT /api/settings/servers/{server_id}` | `{base_url?,key?,clear_key?}`. Blank key keeps the current one; `clear_key:true` removes it. Never returns key material or key references. |
| `POST /api/settings/servers/{server_id}/discover` | Optional `/models` discovery; returns `{models}` merged with curated entries. New models start disabled. |
| `PUT /api/settings/servers/{server_id}/models` | `{models,default_model}`. Each model has `id,display_name,enabled,thinking_mode`, optional boolean `tools` (defaults true) and integer `context_tokens` (1–100000000). Default must be enabled. |
| `GET /api/models` | Enabled primary models, `default_model`, and `web_available`. |
| `POST /api/settings/web-search/test` | `{query}` (1–2000 chars), returns bounded SearXNG results. |
| `GET /api/about` | Only `roost:{version,revision}`, `runtime:{python,aiohttp}` and process/disk `stats`. |
| `POST /api/service/restart-roost` | Replies before exiting for the service manager; sessions persist. |

Thinking modes: `reasoning_effort`, `template_flag`, `unsupported`. Chat `reasoning` accepts
`none`/`off`, `low`, `medium`, `high`; `default` is accepted as Off. Unsupported models receive
no thinking parameter. Roost sends no sampling controls. See [model servers](model-server.md).

## Conversations

| Endpoint | Request | Response |
| --- | --- | --- |
| `GET /api/conversations?offset=0` | Offset 0–100000 | `{sessions:[{id,title,title_source,preview,source:"roost",last_active_at,working}],total}`; newest activity first, 50/page. |
| `POST /api/conversations` | `{text,request_id?,reasoning?,instructions?,model?,web?}` | 201, acceptance shape below. `model` is an enabled primary model ID or null. |
| `POST /api/conversations/{sid}/messages` | `{text,request_id?,web?}` | 200, acceptance shape below. Configure an existing chat separately before sending. |
| `GET /api/conversations/{sid}` | — | Transcript shape below, newest 500 rows in execution order. |
| `GET /api/conversations/{sid}?before={message_id}&limit=500` | Stable cursor belonging to this chat; limit 1–500 | Same transcript shape for the immediately older rows. `has_older` is exact. Explicit `limit` requires `before`. |
| `GET /api/conversations/{sid}/stream` | Existing authenticated EventSource | SSE snapshots below; works for active, queued, or settled turns. |
| `PATCH /api/conversations/{sid}` | `{title}` (up to 200 chars, may be empty) | `{ok:true}`; sets `title_source:"user"`, permanently preventing automatic replacement. |
| `DELETE /api/conversations/{sid}` | `{confirm:true}` | `{ok:true}`; cancels work, removes rows and search entries. |
| `DELETE /api/conversations` | `{confirm:"DELETE"}` | `{deleted:number}`; same cancellation and cascade for all chats. |
| `GET /api/conversations/{sid}/settings` | — | `{instructions,reasoning,model,no_web,keep_local,persona_id}`; model nullable, otherwise default primary model. |
| `PUT /api/conversations/{sid}/settings` | Any nonempty subset of `{instructions,reasoning,model,no_web,keep_local,persona_id}` | Same settings shape. Instructions max 4000 chars; `no_web` is strictly boolean. Applies to future accepted turns. |
| `GET /api/conversations/{sid}/export` | — | Downloadable JSON `{conversation,exported,instructions,reasoning,model,no_web,messages}` containing all rows, including partial and queued rows, in execution order. |
| `GET /api/search?q=...` | Nonblank query, max 200 chars | `{sessions:[{...list_row,snippet}]}`; distinct conversations, newest activity first, max 50. Terms are escaped literal FTS phrases joined with AND; operators, column selectors and wildcards are not interpreted as syntax. |

Send text is trimmed, nonblank, max 16000 characters. `request_id` is a lowercase UUID v4; omit only for older clients that cannot retry safely. Acceptance atomically creates the turn, user row, assistant placeholder, and first conversation when needed:

`{session_id,turn_id,message_id,assistant_message_id,state,revision,queue_position}`

`queue_position` is 1-based among queued turns, otherwise null. Repeating a request ID with the same original input returns the same IDs and its **current** state/revision/position, even after settings change or restart. A different payload with that ID returns 409. Accepted settings/model/thinking metadata are captured per turn. Cancelling/deleting/pruning turns also removes their request identities.

Transcript:

`{session_id,messages,has_older,live,working,after,started,turn_id,revision,queue,context}`

- `messages`: `{id,role,content,reasoning,timestamp,turn_id,ordinal,revision,state,client_request_id,error}`. Assistant rows additionally contain `stats`, `roost_usage`, `trace`, and `sources` (shapes below).
- `state`: `queued | running | completed | failed | interrupted`. The row's exposed state is the owning turn state, including on the user row.
- `queue`: `[{turn_id,position}]`, 1-based FIFO execution order. `working` includes both running and queued turns. `live` means a turn is currently running.
- `after`, `started`, `turn_id`, `revision` describe the running turn, otherwise null. `started` is its durable acceptance timestamp. `after` preserves the existing numeric UI hint for ordinary FIFO sends.
- `context`: `{prompt_tokens,context_tokens}` from the latest measured completion and its model snapshot; either value may be null.
- `stats`: `{usage:{prompt_tokens?,completion_tokens?,total_tokens?},model_seconds,time_to_first_token,tokens_per_second}` when completed, otherwise `{}`. Unreported usage is omitted rather than invented. Durations are seconds. `model_seconds` includes prefill and excludes tool execution; TPS divides completion tokens by the time from the first token to the end of the stream, excluding tool execution, and is null without a first token or when that span is zero.
- `roost_usage`: `{input_tokens,output_tokens,model_seconds,time_to_first_token,tokens_per_second}`; unavailable values are null. This preserves the existing usage renderer.

SSE data frames are **full committed snapshots**, each with `reset:true`:

`{reset,user,started,thoughts,text,turn_id,revision,state,done,trace,sources}`

The first frame always includes the full saved partial/final reply. Later frames replace it; never append a full frame without applying `reset`. A queued turn can initially have empty assistant text. `done` is true for a settled turn; if the queue has already advanced the stream resets to the next turn instead. Deletion emits `{done:true,state:"deleted"}` and ends the stream. Cookie revocation ends streams after the existing heartbeat check (15 seconds). Closing the browser stream never cancels generation. Transcript polling remains the durable recovery path.

## Turn controls

All controls use `POST /api/conversations/{sid}/turns/{action}` and the existing owner/origin guards. Turn IDs must belong to `{sid}`.

| Action | JSON request | Response / effect |
| --- | --- | --- |
| `stop` | `{turn_id}` | `{ok:true}`; running target becomes interrupted with its partial retained, then queue continues. Retrying against the settled target is a no-op, so it cannot stop the next reply. A queued target is rejected. |
| `cancel` | `{turn_id}` | `{ok:true}`; queued target and its message pair are deleted. A running/settled target is rejected. |
| `send-now` | `{turn_id}` | `{ok:true}`; queued target only. Interrupt current reply and assign the selected turn the next execution ordinal; remaining queued turns retain relative order. |
| `edit` | `{turn_id,text,request_id}` | Acceptance shape, 200. Settled target only. Drop target and every later turn, including running/queued turns; accept edited text as a new turn using current chat settings/model. Request ID required; retries return original acceptance. |
| `regenerate` | `{turn_id}` | `{ok:true}`; latest settled turn only. Clear assistant text/reasoning/stats/steps/sources and rerun the same user/model/settings snapshot with the same message and turn IDs; revision increases. |

## Web tools and citations

Settings › Web search saves `search_url`, a fixed SearXNG base such as `https://search.example.org`. The server calls `/search?q=...&format=json`; this owner-configured destination may be on a LAN. Search redirects are refused. The Test button saves the URL and runs one query. Model settings include a Web tools checkbox.

A turn offers `web_search(query)` and `read_page(url)` only when the URL is configured, its model has `tools:true`, and the chat has `no_web:false`. Normal sends let the model decide. Optional `web:"force"` requires `web_search` on the first model call with named `tool_choice`; HTTP 400/422 before any output retries once without `tool_choice`, with a system instruction to search first. Forced sends while unavailable return 400. Other `web` values are rejected. The composer globe resets after each send; retries retain the original intent.

Acceptance snapshots `tools_offered`, `search_url`, `web`, and `current_date` alongside existing model/thinking/chat settings. Duplicate sends retain that snapshot; regenerate reuses it even if preferences subsequently change. Edit-and-resend uses current settings. The system prompt includes the acceptance date (no time, so it changes once a day) in the owner's configured zone, or UTC, plus instructions to treat web text as untrusted and cite assigned `[n]` markers.

Each assistant tool turn is echoed to the same model with its `tool_calls` and `reasoning_content`, followed by `role:"tool"` messages with matching `tool_call_id`. Calls and full results are saved in `turn_steps`. Unknown tools, invalid JSON/arguments, blocked pages and fetch failures become error tool results. Limits are three tool rounds, eight executed calls per round, 15 seconds per tool call and 60 seconds cumulative tool time. At the round/time limit the next model call has no tools; unsolicited calls in that response fail the turn. Later turns receive only user text and final assistant content, without tool traffic or reasoning.

`read_page` accepts HTTP(S) on ports 80/443 without URL credentials. A connector resolver checks every returned IP and passes those exact records to the connection; DNS caching and connection reuse are disabled. Literal addresses are checked separately. Every redirect is revalidated (at most five). Private, loopback, link-local, multicast, reserved, unspecified, CGNAT, mapped and compatible IPv6 addresses are blocked. Page sessions use no cookies, ambient proxies, Roost credentials or provider keys. Only HTML/plain text is accepted, with a 2 MB decompressed body limit and about 20k extracted characters. The stdlib parser drops script/style/nav content and collapses whitespace. Search/page results explicitly label web text as untrusted data.

Each assistant transcript row and SSE snapshot includes:

- `trace`: an ordered array of `{kind:"reasoning",text}` and `{kind:"tool",id,name,arguments,status,result?}`. Arguments are the assembled JSON string. Status is `running | completed | error | interrupted`. `result` is a plain-text excerpt capped at 16000 characters; full protocol output remains in the store. Reasoning segments and calls stay interleaved. Saved/reloaded and streamed traces have the same shape.
- `sources`: `[{n,title,url}]`, ordered by citation number. Numbers start at 1 per turn and deduplicate exact URLs across rounds. Tool results include `n` alongside each source. The UI links recognized `[n]` markers and shows a compact source list; unknown markers and code remain literal. Titles and tool outputs render as text.

Stop, Send now, edit, delete and shutdown cancel active fetches with the generation task. Partial reasoning/trace is retained for interrupted turns; unfinished tool steps display Interrupted. Stream disconnects do not cancel work.

## Personas

Migration 4 seeds an Assistant persona, including on an upgraded database. Its prompt is editable. Personas have `{id,name,prompt,created_at,updated_at}`; names are trimmed, printable, 1–60 characters, and prompts are at most 8000 characters. Timestamps are Unix seconds.

| Route | Request | Response |
| --- | --- | --- |
| `GET /api/personas` | — | `{personas,default_persona_id}` |
| `POST /api/personas` | `{name,prompt}` | 201, the created persona |
| `PUT /api/personas/{id}` | `{name,prompt}` | Updated persona |
| `DELETE /api/personas/{id}` | — | `{ok:true}`; its chats revert to the default |
| `PUT /api/personas/default` | `{default_persona_id}` | `{default_persona_id}` |

The default cannot be deleted. `default_persona_id` is stored transactionally in the chat database's `native_settings` table. Conversation settings GET/PUT and exports add nullable `persona_id`; null follows the current default. New chats start with null. Settings › Persona manages presets, and Chat settings selects one per chat. Chat headers show the current effective persona name. Each reply keeps the persona name from when its turn started, including after renaming or deleting that persona.

## Memory

A fact is `{id,text,origin,source_message_id,conversation_id,created_at,updated_at}`. Text is trimmed, nonempty, at most 500 characters, and cannot contain NUL. Case-insensitive exact duplicates (Unicode case folding) are rejected. The server estimates tokens as the sum of `ceil(len(text)/4)` for each fact. The budget is 2000 tokens; the browser only displays server counts.

| Route | Request | Response |
| --- | --- | --- |
| `GET /api/memory` | — | `{facts,used_tokens,budget_tokens}`; facts ordered by id |
| `POST /api/memory` | `{text,source_message_id?,conversation_id?}` | 201, `{fact,facts,used_tokens,budget_tokens}`; always origin `user` |
| `PUT /api/memory/{id}` | `{text}` | `{facts,used_tokens,budget_tokens}`; preserves origin and source |
| `DELETE /api/memory/{id}` | — | `{facts,used_tokens,budget_tokens}` |

A source must exist and belong to the supplied conversation. Omitting the conversation while supplying a message infers its conversation. Deleting the message or chat sets those references to null and preserves the fact. Facts never enter chat FTS. Manual adds and edits may exceed the budget; the UI warns that model saves are paused. Settings › Memory offers add/edit/delete, You/Model origin labels, and a `~N / 2,000 tokens` meter. Settled user and assistant messages have Remember…; its sheet prefills the selected text inside that message, otherwise the message text, cut to 500 characters.

## Turn timing, prompt, and tools

Persona and facts are read and saved atomically when a queued turn enters `running`, including regeneration. Queued turns pick up later persona switches, default/prompt edits, and fact edits. Other accepted settings retain their acceptance-time snapshot behavior.

The prompt starts with one system message whose parts are joined by blank lines, ordered for a stable prompt prefix:

1. Persona prompt, when nonempty.
2. All facts ordered by id (`Memory facts:` followed by `[id] text` lines), then remember guidance if the tool is offered. Without facts, omit the facts block; guidance remains when remember is available.
3. Chat instructions, when present.
4. The citation and untrusted-web-text instruction.
5. `Today's date: <weekday, D Month YYYY> (<IANA zone>)`, from the turn's settings snapshot (`current_date`). It carries no time, so the message is identical across turns on the same day and the server's prompt cache keeps its prefix.
6. When a summary applies, `Summary of the earlier conversation:` and its text.

History after the summary and the current user message follow. A model with `tools:true` gets `remember({fact:string})`, including when search is unconfigured or the chat has No web. A successful result is `{status:"saved",id,fact}`; refusals return short `{error}` results for duplicates, too-long text, memory full, invalid input, or the call limit. Saves have origin `model` and link to this turn's assistant message and conversation. A save whose resulting total would exceed 2000 tokens is refused. Facts saved mid-reply appear in the prompt beginning with the next running turn.

Remember has a separate cap of three calls per reply, including invalid/refused attempts. It does not consume the three web rounds, and still works after the web time budget expires. A batch's fourth remember call is refused. The maximum is six tool rounds plus a final call without tools. Forced search still requires web_search; if the model returns only remember or prose despite the forced choice, the runner supplies the required search before continuing. The existing HTTP 400/422 fallback remains.

## Wire transparency and Thoughts

Transcript responses add top-level `persona_name` for the chat's current effective persona. Every assistant row and SSE full snapshot adds:

- `persona_id`, `persona_name`: the persona selected when the turn started, null on a queued or pre-migration turn without a snapshot.
- `memories`: `[{id,text}]` containing exactly the injected facts, ordered by id. Later edits and deletions do not alter these snapshots.
- Successful remember trace steps add `fact_id`, `fact`, and `forgotten` to the existing tool trace shape. A deleted save keeps its original text and has `forgotten:true`. The raw JSON result remains in `result`.

Thoughts contains a collapsed Used N memories disclosure with the saved snapshot texts. Remember steps use Remembered: text and a × forget control; after deletion the label becomes Forgotten: text. Refused saves show the short reason. Memory deletion increments affected trace revisions for polling/SSE refresh. Export includes the same message fields.

## Compacting long chats

When a chat nears the model's context size, the same model summarizes the older turns and Roost sends that summary in place of them. The stored messages are untouched: the transcript still shows every message, search still reads the originals, and exports are unchanged.

## When it runs

Before a turn's first provider call, Roost compacts when the model has `context_tokens`, the chat's last completed turn reported `prompt_tokens`, and that number plus an estimate of the new message (characters ÷ 4) is at least 75% of `context_tokens`. A model without `context_tokens` never triggers this way.

Independently, if the provider reports a context overflow (`kind:"context_overflow"`) before the first token of the turn's first call, Roost compacts keeping fewer recent turns and retries once. If summarizing fails, nothing is left to fold, or the retry overflows again, the turn fails with: "This chat is too long for this model's context, and the earlier messages could not be summarized." An overflow after a tool round fails the turn with a different message (the web results made the reply too long), since no summary was involved. If summarizing fails on the 75% path, the turn continues with the full history.

## What is folded

The most recent settled turns stay verbatim: about the last 25% of `context_tokens` by the same estimate, never fewer than 2 turns (1 on the overflow retry, which keeps only the latest). Everything older that no summary covers yet is folded. A new summary is written from the previous summary plus only the newly folded turns, never from the full history again.

Folding runs in chunks, oldest turns first, one summary request per chunk. Each chunk saves a summary row so progress survives, and when the last chunk succeeds it replaces the pass's earlier rows in the same transaction, so one compaction leaves one summary (and one divider). A pass that stops early keeps its rows. A chunk carries about half of `context_tokens` of old turns (or half the failed prompt's estimate when the context size is unknown, whichever is smaller) minus the previous summary. If a chunk request itself overflows, the chunk is halved and retried; a single turn larger than a chunk is cut to its head and tail with a `[…]` marker. A compaction makes at most 8 summary requests; any turns left over stay verbatim after the last summary. The turn's trace still shows one `summarize` step whose result says how many turns were folded. A summary that is empty or cut off at the token limit counts as a failed summary.

The summary call uses the turn's model with `complete()`: non-streaming, thinking off, no tools, `max_tokens` 2000, and no other sampling parameters. Its system prompt asks for a faithful third-person summary of at most about 600 words (decisions, facts, open questions and stated preferences) and treats tool and web text as untrusted data. It runs as part of the turn, not at low priority.

## Prompt layout

The summary is the last part of the single leading system message (see above), after the date line. Then only the turns after the summary's `through_ordinal`, then the new user message.

## Storage

Migration 5 adds `summaries` (`id`, `conversation_id` with cascade delete, `through_ordinal` = the last turn ordinal folded, `text`, `model`, `created_at`) and a nullable `turns.summary_id`. The latest row per conversation is current. Send now renumbers only queued turns, after the highest settled ordinal, so settled ordinals never move and ordinals may have gaps. A turn snapshots the summary it used when it starts running and again if it compacts; a regenerate clears and retakes it. Editing or regenerating a turn at or before a summary's `through_ordinal` deletes that summary, and deleting a chat deletes its summaries. Downgrade to 4 refuses while any summary exists and otherwise drops the table and column, so existing chats survive both directions.

## Wire fields

- `GET /api/conversations/{id}` adds `summaries: [{id,through_ordinal,text,created_at}]`, every current row, oldest first. The newest summary replaces older ones in the prompt, but earlier rows stay listed so each keeps its divider.
- SSE snapshot frames carry the same `summaries` array on the first frame of a stream and on any frame where the conversation's summaries changed since the last frame sent on that stream. A frame without the field means unchanged, so a compaction during a live turn appears without a reload.
- The turn's `trace` gains a tool-shaped item: `{kind:"tool",id:"compaction-<n>",name:"summarize",arguments:"{}",status:"running"|"completed"|"error",result?}` with a short plain-text `result`. It is stored as `compaction_call` and `compaction_result` turn steps. The client labels it "Summarizing earlier messages…", "Summarized earlier messages" or "Couldn't summarize earlier messages".
- Clients place a divider after the last loaded message whose `ordinal <= through_ordinal`, and show none when no loaded message qualifies.

The smoke check sets a tiny `context_tokens` against the fake provider and checks the fourth message of a fresh chat compacts. The context meter continues to use the last reported `prompt_tokens`.

## Other routes

| Route | Contract |
| --- | --- |
| `GET /api/passkeys` | Lists the owner's passkeys. |
| `POST /api/passkeys/register/options` | Creates a challenge bound to the calling session. |
| `POST /api/passkeys` | Registration response plus `name`; verifies origin, challenge and signature. |
| `DELETE /api/passkeys/{pid}` | `{confirm:true}`; removes that passkey. |
| `POST /api/passkeys/login/options`, `POST /api/passkeys/login` | Public authentication challenge and signed assertion; share password sign-in rate limits. |
| `GET /api/push` | `{public_key,devices}`; no subscription secrets. |
| `POST /api/push/devices` | `{subscription,name}`; only supported browser push destinations. |
| `DELETE /api/push/devices/{did}` | `{confirm:true}`. |
| `POST /api/push/test` | `{device}`; sends a test notification to that device. |
| `POST /api/presence` | `{conversation:<id or null>}`; recent visibility suppresses reply push. |

See [attachments](attachments.md), [voice](voice.md), [jobs](jobs.md) and [fallback](fallback.md)
for their routes and complete wire shapes.

## Execution and persistence

The existing `native.sqlite3` filename and `native_settings` schema table remain for installed
chat-store compatibility. Account/passkey/push data remains in `sessions.sqlite3`. Store migrations
are transactional; downgrade refuses to discard populated data. Unknown settings keys and retired
state files are ignored. No history import or data deletion happens on startup.

One application-owned dispatcher admits `ROOST_ADMISSION` turns (default 1, range 1–16).
Each chat runs FIFO; at most 500 turns queue globally. Checkpoints commit every approximately
250 ms or 1024 characters; SSE reads committed revisions. Restart retains partial output, marks
running turns interrupted and resumes queued turns. A disconnected browser never cancels execution.
Titles are best effort low-priority work on the primary, thinking Off, `max_tokens:32`.
Completed replies and job results enter the durable push outbox. Push retries are best effort and
use stable tags; recent presence suppresses ordinary reply notifications.

`make check` and `make smoke` use deterministic local model/search/speech providers. They do not
prove real provider, browser, physical phone, microphone or push-service compatibility.
