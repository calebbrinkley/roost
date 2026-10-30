# Native attachments (Phase 2b-1)

In Native mode you can attach text files and text-layer PDFs to a message. Roost reads the file on the server and sends the extracted text to the model as context alongside your message. The file itself is discarded; only the extracted text is kept, with the message. Hermes mode has no attachments and is unchanged.

## What is accepted

- Text: `.txt .md .markdown .csv .tsv .json .jsonl .yaml .yml .toml .xml .html .log` and common source-code and config extensions (the file picker lists them). Decoded as UTF-8, with the replacement character for invalid bytes. Any file with a NUL byte is rejected as binary.
- PDFs with a text layer, read with `pypdf` (pinned in `pyproject.toml`).
- Rejected with a short message: images ("Roost can't read images yet."), scanned PDFs (a text layer that is empty or nearly empty: "This PDF has no text layer, probably a scan. Roost can't read scanned PDFs yet."), encrypted PDFs, empty files and other binaries.

Limits: 20 MB upload, 500 PDF pages, 20 seconds of extraction, 1,000,000 characters of extracted text per file. PDF work runs in a worker thread, off the event loop; it checks its deadline between pages, and the request gives up when the timeout passes.

## Extract a file

`POST /api/attachments/extract` takes `multipart/form-data` with one `file` part. It needs the owner cookie and the usual `Origin` and `X-Roost-Request: 1` checks, and returns 404 outside Native mode. It stores nothing.

Success: `{name, kind: "text" | "pdf", chars, tokens, text}`, where `tokens = ceil(chars / 4)` and `name` has any path parts removed. Errors are `{"error": "..."}`: 400 for a rejected file, 408 when reading took too long, 413 for a file over the limits.

## Send, edit and regenerate

`POST /api/conversations` and `POST /api/conversations/{sid}/messages` accept `attachments: [{name, text}]`, at most 10. The server validates them again:

- `name` is a non-empty string of at most 200 characters, cleaned of path parts. `text` is a string. No other fields.
- All of a message's attachments together are at most 30,000 tokens (total characters / 4, rounded up). Over the cap the send is rejected with a message stating the size, for example "The attachments are about 31,000 tokens. The limit is 30,000 per message."
- The request-id fingerprint includes the attachments. Retrying a `request_id` with different attachments is a 409, like any other changed payload. The JSON body limit for these routes is 2 MB.

`POST /api/conversations/{sid}/turns/edit` keeps the original message's attachments. If the request includes `attachments`, that list replaces them, and an empty list removes them. Regenerate reuses the same attachments.

## Storage and transcript

Migration 6 adds `messages.attachments`, a JSON array of `{name, kind, chars, text}` on user messages (`kind` is `pdf` for a `.pdf` name, otherwise `text`). `content` stays exactly what the user typed, and the search index covers `content` only, so attachment text is not searchable. Rolling back migration 6 is refused while any message has attachments.

Transcript rows (`GET /api/conversations/{sid}`, export) carry `attachments` on user messages that have them, with the full text, so the browser can show it.

## What the model sees

A user message with attachments is sent as one content string: each attachment first as

    <attachment name="notes.md">
    ...text...
    </attachment>

then a blank line and the typed text. A `"` in a name is written `&quot;`, and `</attachment` inside the text is written `<\/attachment`, so a file cannot close its own block. History turns are laid out the same way, so later replies still see earlier attachments.

Compaction counts attachments. The size estimate and the text folded into a summary use the same layout, so a large attachment is summarized (cut to a head and tail if one message is bigger than a summary request can carry) rather than ignored.

## Browser

- A paperclip button in the composer (Native only) opens a file picker restricted to the accepted types. Each file shows as a chip above the input with its name, "~N tokens" and a remove button; a spinner chip shows while the server reads it, and a rejected file shows an inline error you can dismiss.
- The running total appears at 80% of the cap. Over the cap, or while a file is still being read, Send is disabled.
- Sent messages show their chips. Tapping a chip opens a read-only sheet with the extracted text.
- Edit shows the message's chips with remove buttons. The edit request includes `attachments` only when you removed one.
- Chosen files do not survive a reload or a switch to another chat.
- Touch targets are at least 44 px on coarse pointers.

## Verification

Tests: `tests/test_attachments.py` (extraction of text, text PDF, scanned PDF, encrypted PDF, image, NUL binary, the size, page and time caps; endpoint auth; send validation, cap and fingerprint; prompt layout for new and history turns; compaction; edit keep, replace and remove; regenerate; migration 6) and the attachment tests at the end of `tests/frontend.test.cjs`. PDF fixtures are built in the test code. `dev/native_smoke.py` uploads a file, sends it through the fake provider and checks the prompt. The untracked `capture-native.mjs attachments` writes screenshots to `./captures/attachments/`.
