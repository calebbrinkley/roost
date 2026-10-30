# Connect a model server

Roost calls an OpenAI-compatible Chat Completions API. In **Settings › Models**, set the primary
base URL, for example `https://models.example.com/v1`, save an optional API key, and discover or
manually add model IDs. Enable models, choose an enabled default and save the model list. Keys are
write-only; leaving the field blank keeps the saved key. Clear saved key removes it.

The address must be reachable from the Roost server/container, rather than just from your browser.
Use a trusted network or HTTPS for credentials. Model discovery is optional; a model absent from
discovery can still be entered manually. Display names are local labels; requests use exact model IDs.

## Server choices

- **llama.cpp server**: use its OpenAI-compatible `/v1` base. Select the served model ID or configured
  alias. Tool use needs a supported model/chat template and the server's Jinja support; test the
  actual model/template combination. See the [server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
- **vLLM**: use the OpenAI-compatible serving endpoint and served model name. Configure the model's
  chat template and tool/reasoning parsers on the server as needed. See [vLLM serving](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/).
- **Ollama**: use its `/v1` compatibility endpoint and an installed model's exact ID. Its local API
  normally needs no key. Configure the model's actual context size on the server rather than assuming
  a per-request setting. See [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility).
- **Hosted API**: use the provider's documented compatible base URL and key. Check support for streamed
  Chat Completions, tools, thinking controls and any separate speech endpoints. Selected memory,
  chat history and attachment text travel to that provider with the prompt.

## Per-model controls

| Setting | What Roost sends |
| --- | --- |
| Thinking `unsupported` | No thinking parameter; the thinking picker is hidden. |
| Thinking `reasoning_effort` | `reasoning_effort` for the selected level. |
| Thinking `template_flag` | `chat_template_kwargs.enable_thinking`, false for Off and true for a thinking level. |
| Context size | Optional positive token count used for the context meter and compaction threshold; it does not reconfigure the server. |
| Tools enabled | Allows Roost's supported function tools. Disable for models/templates that cannot call tools reliably. |

Roost normalizes reasoning fields and streamed think tags into Thoughts. The context size should
match the server's effective limit; omit it when unknown. Roost sends no temperature or other
sampling overrides. Verify a short reply, thinking, a multi-round tool call and a longer chat with
your actual model before relying on them.

For web tools, configure SearXNG in **Settings › Web search**. A chat's **No web** switch disables web
tools; the globe requests a search for the next send. Memory remains separate from web search.
The [fallback](fallback.md) is optional and never used for speech or scheduled jobs.

See [external contracts](compatibility.md) and the [browser API](api.md) for exact request shapes.
