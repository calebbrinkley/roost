# Native personas and memory (Phase 4a)

These features require Native mode. Hermes keeps its existing agent name, instructions, routes, and behavior. New routes require the owner cookie and mutations require the matching Origin and `X-Roost-Request: 1`, just like other native routes. They return 404 in Hermes mode. Invalid payloads, unknown identifiers, and refused writes return 400 `{error}`; authentication and origin failures return 401 and 403.

## Personas

Migration 4 seeds an Assistant persona, including on an upgraded database. Its prompt is editable. Personas have `{id,name,prompt,created_at,updated_at}`; names are trimmed, printable, 1–60 characters, and prompts are at most 8000 characters. Timestamps are Unix seconds.

| Route | Request | Response |
| --- | --- | --- |
| `GET /api/personas` | — | `{personas,default_persona_id}` |
| `POST /api/personas` | `{name,prompt}` | 201, the created persona |
| `PUT /api/personas/{id}` | `{name,prompt}` | Updated persona |
| `DELETE /api/personas/{id}` | — | `{ok:true}`; its chats revert to the default |
| `PUT /api/personas/default` | `{default_persona_id}` | `{default_persona_id}` |

The default cannot be deleted. `default_persona_id` is stored transactionally in the native database's `native_settings` table, separate from Hermes preferences. Conversation settings GET/PUT and exports add nullable `persona_id`; null follows the current default. New chats start with null. Settings › Persona manages presets, and Chat settings selects one per chat. Native headers show the current effective persona name. Each reply keeps the persona name from when its turn started, including after renaming or deleting that persona. The agent-name field is hidden in Native mode.

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

Persona and facts are read and saved atomically when a queued turn enters `running`, including regeneration. Queued turns pick up later persona switches, default/prompt edits, and fact edits. Other accepted settings retain their Phase 2–3 snapshot behavior.

The prompt starts with one system message whose parts are joined by blank lines, ordered for a stable prompt prefix:

1. Persona prompt, when nonempty.
2. All facts ordered by id (`Memory facts:` followed by `[id] text` lines), then remember guidance if the tool is offered. Without facts, omit the facts block; guidance remains when remember is available.
3. Chat instructions, when present.
4. The citation and untrusted-web-text instruction.
5. `Today's date: <weekday, D Month YYYY> (<IANA zone>)`, from the turn's settings snapshot (`current_date`). It carries no time, so the message is identical across turns on the same day and the server's prompt cache keeps its prefix.
6. When a summary applies (Phase 4b), `Summary of the earlier conversation:` and its text.

History after the summary and the current user message follow. A model with `tools:true` gets `remember({fact:string})`, including when search is unconfigured or the chat has No web. A successful result is `{status:"saved",id,fact}`; refusals return short `{error}` results for duplicates, too-long text, memory full, invalid input, or the call limit. Saves have origin `model` and link to this turn's assistant message and conversation. A save whose resulting total would exceed 2000 tokens is refused. Facts saved mid-reply appear in the prompt beginning with the next running turn.

Remember has a separate cap of three calls per reply, including invalid/refused attempts. It does not consume the three web rounds, and still works after the web time budget expires. A batch's fourth remember call is refused. The maximum is six tool rounds plus a final call without tools. Forced search still requires web_search; if the model returns only remember or prose despite the forced choice, the runner supplies the required search before continuing. The existing HTTP 400/422 fallback remains.

## Wire transparency and Thoughts

Native transcript responses add top-level `persona_name` for the chat's current effective persona. Every assistant row and SSE full snapshot adds:

- `persona_id`, `persona_name`: the persona selected when the turn started, null on a queued or pre-migration turn without a snapshot.
- `memories`: `[{id,text}]` containing exactly the injected facts, ordered by id. Later edits and deletions do not alter these snapshots.
- Successful remember trace steps add `fact_id`, `fact`, and `forgotten` to the existing tool trace shape. A deleted save keeps its original text and has `forgotten:true`. The raw JSON result remains in `result`.

Thoughts contains a collapsed Used N memories disclosure with the saved snapshot texts. Remember steps use Remembered: text and a × forget control; after deletion the label becomes Forgotten: text. Refused saves show the short reason. Memory deletion increments affected trace revisions for polling/SSE refresh. Export includes the same message fields.

## Migration and validation

Migration 4 adds `personas`, `native_settings`, nullable conversation persona references, per-turn persona/memory JSON snapshots, and `memory_facts`. Existing chat messages, sources, FTS, and account state survive. Downgrade to 3 removes an untouched seeded persona and unused columns; it refuses custom persona/memory data, persona selections, or turn snapshots so rollback never silently discards them. A refusal is atomic.

Run `make PYTHON=.venv/bin/python check` and `make PYTHON=.venv/bin/python smoke`. The native smoke uses a fake provider for a persona turn, manual fact, model remember save, snapshot, and forgetting, alongside the existing queue/web flows. No live model or Hermes is contacted.

The untracked root script `capture-native.mjs` uses Playwright and a disposable local fake-provider server. Run `node capture-native.mjs` with Playwright and Chromium installed. It writes 20 screenshots under `./captures/phase4/`: phone (390×844) and desktop (1280×800), light and dark, each showing Persona settings, Memory settings over budget, chat persona picker, Thoughts with injected memories and a model save, and the Remember sheet. It also checks for horizontal overflow and browser errors. Browser execution and visual review must be done outside the sandbox.

# Compacting long chats (Phase 4b)

Native only. When a chat nears the model's context size, the same model summarizes the older turns and Roost sends that summary in place of them. The stored messages are untouched: the transcript still shows every message, search still reads the originals, and exports are unchanged.

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
- Native SSE snapshot frames carry the same `summaries` array on the first frame of a stream and on any frame where the conversation's summaries changed since the last frame sent on that stream. A frame without the field means unchanged, so a compaction during a live turn appears without a reload.
- The turn's `trace` gains a tool-shaped item: `{kind:"tool",id:"compaction-<n>",name:"summarize",arguments:"{}",status:"running"|"completed"|"error",result?}` with a short plain-text `result`. It is stored as `compaction_call` and `compaction_result` turn steps. The client labels it "Summarizing earlier messages…", "Summarized earlier messages" or "Couldn't summarize earlier messages".
- Clients place a divider after the last loaded message whose `ordinal <= through_ordinal`, and show none when no loaded message qualifies.

The native smoke sets a tiny `context_tokens` against the fake provider and checks the fourth message of a fresh chat compacts. The context meter continues to use the last reported `prompt_tokens`.
