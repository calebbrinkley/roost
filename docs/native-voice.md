# Native voice (Phase 5)

In Native mode, Roost sends recordings and read-aloud text directly to an OpenAI-compatible speech server. Hermes is not needed. Recording, draft review, silence unlock, reply playback and auto-read use the existing voice controls. Hermes mode retains its existing voice integration.

## Setup

Open **Settings › Voice**. By default, **Same as the model server** uses the primary model server's base URL and current key. The fallback model server is never used for speech. Alternatively choose **Custom speech server**, enter an API base such as `https://speech.example.com/v1`, and optionally save its API key. Leave the key field empty to keep a saved key; use **Clear saved key** to remove it.

Set the transcription model (default `whisper-1`), speech model (default `tts-1`), and voice (default `alloy`). **Save** persists the settings. **Test** saves them and reads “Voice is ready.” using the configured server. Model and voice names must exist on that server; Roost does not download models. Browsers require HTTPS (or localhost) and microphone permission to record.

The base URL includes the API prefix, normally `/v1`. Roost appends `/audio/transcriptions` or `/audio/speech`. API keys never return to the browser. Configuration and write-only keys are saved separately as `voice.json` and `speech-keys.json` beneath the private state directory with mode `0600`, using atomic replacements. Keep that directory outside the public checkout. There are no additional environment variables to configure.

## HTTP contracts

All routes require the owner cookie. Mutations also require the existing matching `Origin` and `X-Roost-Request: 1` headers. Voice settings routes are Native only.

| Route | Request | Response |
| --- | --- | --- |
| `GET /api/settings/voice` | — | `same_as_model`, `base_url`, `transcription_model`, `speech_model`, `voice`, `has_key`. Never a key or key reference. |
| `PUT /api/settings/voice` | Any nonempty subset of the public fields except `has_key`, plus optional write-only `key` and boolean `clear_key` | Same public view. Invalid settings return 400 without saving. |
| `POST /api/settings/voice/test` | `{}` | MP3 audio for the test phrase. |
| `POST /api/audio/transcribe` | Existing `{data_url,mime_type}`, optionally `duration_seconds` | Existing `{transcript}`. |
| `GET /api/conversations/{sid}/speech/{mid}` | Optional `Range` header | MP3 audio, with existing 206/416 byte-range behavior. Only assistant messages belonging to this conversation can be read. |
| `GET /api/audio/silence` | — | Existing short WAV unlock clip. |

Transcription sends multipart `file` and `model` fields. MP4/AAC and WebM/Opus recordings use `.mp4`, `.aac` and `.webm` filenames and their base media types. The upstream response may be JSON `{text: "..."}` or UTF-8 `text/plain`.

Synthesis sends JSON `{model,input,voice,response_format:"mp3"}`. Text comes from the native assistant message, passes through the existing 4,000-character Markdown/code cleanup, and omits numeric citation markers such as `[3]`. The existing eight-clip in-memory cache and in-flight deduplication serve repeated playback and range requests. Saving voice settings clears cached clips.

## Bounds and failures

Native recording stops at 120 seconds in the browser. The API rejects a supplied duration over 120 seconds; duration metadata is not an independent inspection of the encoded media. Decoded recordings are capped at 25 MiB, with the corresponding base64 envelope allowed by the native upload route. Transcript responses are capped at 1 MiB and synthesized audio at 25 MiB. Connect, read, and total deadlines are 10, 60, and 150 seconds. Redirects are refused, preventing credential forwarding.

HTTP 429 and 503 (including `cpu_busy`) become 503 with “Speech is busy right now. Try again in a moment.” Other upstream statuses become 502 with only the status number; upstream bodies, addresses and keys are never included. Connection failures, timeouts and malformed/oversized responses have generic public messages. There are no automatic retries.

## Verification

`make PYTHON=.venv/bin/python check` covers multipart formats, transcript shapes, errors and limits, settings validation, write-only keys, native message cleanup, caching, concurrent deduplication, ranges, and Settings rendering. `make PYTHON=.venv/bin/python smoke` includes native transcription and playback against a deterministic local speech fake without Hermes. These checks do not establish real microphone, Safari audio, or live speech-server compatibility.

Outside the browser-restricted sandbox, run `node capture-native.mjs voice` with Playwright and Chromium installed. The script writes Settings › Voice captures at 390×844 and 1280×800 in light and dark to `./captures/voice/`, for both the default and custom server forms. `node capture-native.mjs voice --check-server` checks its fixture and APIs without launching a browser.
