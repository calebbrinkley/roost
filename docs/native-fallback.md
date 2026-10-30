# Fallback model server (Phase 2b-2)

Native only. Roost can keep a second OpenAI-compatible server, the fallback, next to the primary. When the primary cannot start a reply, Roost runs the same turn on the fallback and says so on the reply. Hermes behaviour is unchanged.

## Setup

Settings › Models has a **Fallback server** section that works like the primary's: base URL, a write-only key, discover or add models, tick the ones to enable, edit display names, and choose a default model. The fallback uses the same `PUT /api/settings/servers/fallback`, `.../discover` and `.../models` routes as the primary, with the same validation; keys live in the separate 0600 key file and are never returned.

Below the two servers is one switch: **Use the fallback when the primary can't start a reply**. It is saved as `use_fallback` in `settings.json` (`GET`/`PATCH /api/settings`, a strict boolean) and is off by default. `PATCH` refuses to turn it on until the fallback server has a base URL and an enabled default model. Turning it off always works. The browser also disables the switch until both are saved. If the fallback is later emptied while the switch is on, turns simply run without a fallback.

Replies answered by the fallback leave the primary server, which matters when the fallback is hosted elsewhere. The settings page says so next to the switch.

**Keep this chat local** is a chat setting (`keep_local`, a boolean on `GET`/`PUT /api/conversations/{id}/settings`, included in exports). When on, that chat never uses the fallback. Only the chat's current value counts when a turn needs the fallback, so unticking it and pressing Regenerate tries the fallback.

## When it triggers

Only before the model has produced anything: no text, reasoning or tool-call delta has arrived. This applies to any of the turn's provider calls (the reply, a tool round, a compaction summary). Triggers:

- connection refused or reset, or a DNS failure
- a connect timeout
- HTTP 408, 429, 500, 502, 503 or 504

Never mid-reply, on a context overflow (even with a retryable status), on other 4xx statuses (400, 401, 403, 404, 422), on a read or total timeout, or when the owner stops the turn. A stream that fails after its first delta fails the turn and keeps the partial reply, as before.

Title generation stays on the primary: a failed title is fine. The runner's `generate(tid, fallback=False)` keeps a turn on the primary whatever the settings say; Phase 6 jobs will use it.

## What the fallback gets

The same prompt: persona, memories, summary and history. It runs with the fallback default model's own `thinking_mode`, `context_tokens` and `tools` flag. The `remember` guidance and tool, and web search, are offered according to that model's `tools` flag (web search still needs a search URL and the chat's No web setting). From the switch onward every call in the turn stays on the fallback: later tool rounds, compaction (before the first call, or after a context overflow), and its summaries, which record the fallback model. Fallback replies that report their context size make the context meter use the fallback model's `context_tokens` (`stats.context_tokens`).

If the fallback also fails, the turn fails with: "The primary server could not start this reply (HTTP 503), and the fallback server failed too: Model server returned HTTP 500." Only roles and plain reasons appear, never URLs or keys.

## Recovery

Nothing is sticky and nothing polls. The next turn tries the primary first again, and an edit or regenerate starts over on the primary.

## Wire fields

- Assistant rows (`GET /api/conversations/{id}`, exports) and native SSE snapshot frames carry `served_by: {server:"primary"|"fallback", model, display_name}`. For a primary reply `model` is the turn's model and `display_name` its curated name.
- The turn's `trace` gets a tool-shaped item, like `summarize`: `{kind:"tool",id:"fallback-1",name:"fallback",arguments:"{}",status:"completed"|"error",result}`. `result` is a plain sentence: "Primary couldn't start (HTTP 503); answered with Backup on the fallback server", "...; answering with Backup on the fallback server" (status `running`) while the turn runs, "...; switched to Backup on the fallback server" (status `interrupted`) when it was stopped, or "...; Backup on the fallback server failed too" (status `error`) when the turn failed. When the chat forced a web search the fallback cannot run (its model has tools off), the search is dropped rather than failing the turn, and the sentence adds "; the forced web search wasn't available there". Reasons are "connection refused or reset", "connection timed out" or "HTTP <status>". It is stored as a `fallback_call` turn step, so there is no schema migration; `served_by` is derived from that step. Primary display names are read once per request; if `servers.json` cannot be read, history, exports and streams still work and show the model id.
- The reply's stats line adds "via <display name> · fallback" for fallback replies and nothing for primary ones. Thoughts shows the sentence above.

## Verification

`tests/test_fallback.py` covers each trigger class and each non-trigger (including a mid-stream drop), the switch off, local chat and unconfigured cases, the runner flag, both servers failing, tool rounds and compaction staying on the fallback, the next turn returning to the primary, `served_by`, and the settings API and key privacy. `tests/frontend.test.cjs` covers the settings section, switch gating, the chat toggle, the stats label and the trace step. `dev/native_smoke.py` runs a fallback reply against two fake providers, the primary returning 503.

The untracked root script `capture-native.mjs` has a `fallback` mode: run `node capture-native.mjs fallback` with Playwright and Chromium installed. It writes 16 screenshots under `./captures/fallback/` (phone 390×844 and desktop 1280×800, light and dark): the Fallback server section, the switch, Keep this chat local in the chat settings, and a fallback reply with its label and Thoughts step.
