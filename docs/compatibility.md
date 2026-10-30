# External service contracts

Roost uses ordinary HTTP APIs. Chat, scheduling, persona, memory, history and push intent belong
to Roost; external services provide inference, optional audio and optional search. Compatibility
with a server's chat endpoint does not imply its model supports thinking or function tools.

## Model server

A saved base such as `https://models.example.com/v1` supplies:

- `POST /chat/completions`: JSON `model`, `messages`, `stream`, optional thinking controls and
  function tools. Streaming replies use SSE `data:` frames with `choices[].delta`, optional usage
  and a finish reason; `[DONE]` is supported. Non-streaming completions supply
  `choices[].message.content`, used for titles and summaries.
- Optional `GET /models`: `{data:[{id}]}`. Roost can use manually curated IDs when discovery is
  unavailable. New discoveries start disabled.
- Tool calls carry IDs and function name/JSON arguments, including fragmented streamed arguments.
  Follow-up messages use `role:"tool"` and matching `tool_call_id`. Roost executes only its own
  allowlisted search/page and remember tools, with bounded rounds and results.
- Reasoning may arrive as `reasoning_content`, `reasoning`, or think tags, including split tags.
  Usage uses prompt/completion token counts when supplied; missing measurements display as unknown.

Bearer keys remain on the server. Redirects are refused. Upstream response bodies and credentials
are not echoed in errors. HTTP 429/503 are busy responses; connect/read/total timeouts are bounded.
Context overflow becomes a stored turn failure or a bounded compaction attempt, never an unbounded
replay. See [model setup](model-server.md), [fallback](fallback.md) and [API](api.md).

## Speech server (optional)

The default target is the primary model server; Settings › Voice can select a separate `/v1` base
and write-only key. It must implement:

- `POST /audio/transcriptions`: multipart `model` and `file`, with the recorded format as MIME type
  and filename extension; returns JSON `{text:"..."}` or plain text.
- `POST /audio/speech`: JSON `{model,input,voice,response_format:"mp3"}`, returning audio bytes.

Model names and voice are editable. Roost bounds recording/transcript/audio sizes and timeouts;
its browser audio URLs support byte ranges. See [voice](voice.md) for limits and error mapping.
A real microphone, automatic playback and installed-phone behavior need separate device checks.

## SearXNG (optional)

Roost calls `<base>/search?q=<query>&format=json`. Enable JSON output in SearXNG's `search.formats`;
instances that disable it can return 403. The expected object has a `results` list with title,
URL and content snippets. See the [SearXNG Search API](https://docs.searxng.org/dev/search_api.html).
The configured search base may be private; redirects are refused. Model-selected page URLs use a
separate public-web guard: every resolved IP and redirect is checked, with private/reserved and
carrier-grade NAT destinations blocked. Provider keys and cookies never accompany page reads.

## Validation boundary

Repository tests and smoke use deterministic local model, speech and search fakes. Compose smoke
adds container packaging and persistence checks. These do not prove compatibility with any live
provider, browser push service or physical device. Verify your selected endpoints and model/template
combination after configuration.
