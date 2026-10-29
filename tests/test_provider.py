"""Provider protocol variations through a real local HTTP transport."""

import asyncio
import json
import unittest
from unittest.mock import patch

import aiohttp

from roost.provider import (MalformedFrame, Provider, ProviderBusy, ProviderConnectionError, ProviderError,
                            ProviderHTTPError, ProviderTimeout, ToolCall, base_url, thinking_fields)
from openai_fake import OpenAIFake, frame

MESSAGES = [{"role": "user", "content": "Hello"}]
DONE = b"data: [DONE]\n\n"


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = await OpenAIFake().start()
        self.session = aiohttp.ClientSession()
        self.provider = Provider(self.session, self.fake.url, "example-test-key")

    async def asyncTearDown(self):
        await self.session.close()
        await self.fake.close()

    async def events(self, **kwargs):
        return [event async for event in self.provider.stream("example-model", MESSAGES, **kwargs)]

    async def test_arbitrary_chunks_unicode_usage_final_done_and_no_sampling(self):
        self.fake.frames = (b": keepalive\n\n" + frame({"role": "assistant"}) + frame({"content": "Hé"})
                            + frame({"content": "llo"}) + frame({}, finish="stop")
                            + frame(usage={"prompt_tokens": 3, "completion_tokens": 2}) + DONE
                            + b"data: not read after done\n\n")
        for sizes in ((1,), (2, 3, 5, 7), (65536,)):
            self.fake.chunk_sizes = sizes
            events = await self.events(thinking="medium", mode="reasoning_effort")
            self.assertEqual("".join(e.text for e in events if e.kind == "text"), "Héllo")
            self.assertEqual([e.finish_reason for e in events if e.kind == "finish"], ["stop"])
            self.assertEqual([e.usage for e in events if e.kind == "usage"], [{"prompt_tokens": 3, "completion_tokens": 2}])
        _, headers, payload = self.fake.requests[-1]
        self.assertEqual(headers["Authorization"], "Bearer example-test-key")
        self.assertEqual(set(payload), {"model", "messages", "stream", "stream_options", "reasoning_effort"})
        self.assertEqual(payload["reasoning_effort"], "medium")

    async def test_first_reasoning_source_wins_across_chunks(self):
        cases = (
            ([{"reasoning_content": "First"}, {"reasoning_content": " thought", "content": "Answer"}], "First thought"),
            ([{"reasoning": "Other"}, {"reasoning": " thought", "content": "Answer"}], "Other thought"),
            ([{"reasoning_content": "Chosen", "reasoning": "duplicate", "content": "<think>duplicate</think>Answer"}], "Chosen"),
            ([{"content": "<think>First</think>"}, {"reasoning": "duplicate"}, {"reasoning_content": "duplicate", "content": "Answer"}], "First"),
            ([{"reasoning": "First"}, {"reasoning_content": "duplicate", "content": "<think>duplicate</think>"},
              {"reasoning": " thought", "content": "Answer"}], "First thought"),
            ([{"content": "<thi"}, {"reasoning": "First", "content": "nk>duplicate</think>Answer"}], "First"),
            ([{"reasoning_content": "", "reasoning": "Chosen", "content": "<think>duplicate</think>Answer"}], "Chosen"),
        )
        for chunks, reasoning in cases:
            with self.subTest(chunks=chunks):
                self.fake.frames = b"".join(frame(delta) for delta in chunks) + frame({}, finish="stop") + DONE
                events = await self.events()
                self.assertEqual("".join(e.text for e in events if e.kind == "text"), "Answer")
                self.assertEqual("".join(e.text for e in events if e.kind == "reasoning"), reasoning)

    async def test_think_tags_split_at_every_character_boundary(self):
        text = "<think>Quiet thought</think>Answer <thing>literal</thing>"
        for size in (1, 2, 3, 6, 7, 8, len(text)):
            self.fake.frames = (b"".join(frame({"content": text[start:start + size]}) for start in range(0, len(text), size))
                                + frame({}, finish="stop") + DONE)
            events = await self.events()
            self.assertEqual("".join(e.text for e in events if e.kind == "text"), "Answer <thing>literal</thing>")
            self.assertEqual("".join(e.text for e in events if e.kind == "reasoning"), "Quiet thought")
        self.fake.frames = frame({"content": "<think>Unclosed"}) + frame({}, finish="length") + DONE
        events = await self.events()
        self.assertEqual("".join(e.text for e in events if e.kind == "reasoning"), "Unclosed")
        self.assertEqual("".join(e.text for e in events if e.kind == "text"), "")
        self.fake.frames = frame({"content": "Literal <thi"}) + frame({}, finish="stop") + DONE
        self.assertEqual("".join(e.text for e in await self.events() if e.kind == "text"), "Literal <thi")

    async def test_each_reasoning_source_streams_incrementally_before_completion(self):
        for source in ("reasoning_content", "reasoning", "tagged"):
            chunks = ([{"content": "<thi"}, {"content": "nk>First"}, {"content": " thought</think>"}]
                      if source == "tagged" else [{source: "First"}, {source: " thought"}])
            self.fake.frames = b"".join(frame(delta) for delta in chunks) + frame({"content": "Answer"}) + frame({}, finish="stop") + DONE
            iterator = self.provider.stream("example-model", MESSAGES)
            try:
                for text in ("First", " thought"):
                    event = await anext(iterator)
                    self.assertEqual((event.kind, event.text), ("reasoning", text), source)
                self.assertEqual((await anext(iterator)).kind, "text")
            finally:
                await iterator.aclose()

    async def test_fragmented_interleaved_tool_calls_by_index_and_id(self):
        self.fake.frames = (
            frame({"tool_calls": [{"index": 1, "id": "call-two", "type": "function", "function": {"name": "read_", "arguments": '{"url":'}},
                                  {"index": 0, "id": "call-one", "type": "function", "function": {"name": "web_", "arguments": '{"q":'}}]})
            + frame({"tool_calls": [{"index": 0, "function": {"name": "search", "arguments": '"example"}'}},
                                    {"id": "call-two", "function": {"name": "page", "arguments": '"https://example.invalid"}'}}]})
            + frame({}, finish="tool_calls") + DONE)
        events = await self.events(tools=[{"type": "function", "function": {"name": "web_search"}}])
        self.assertEqual([e.tool_call for e in events if e.kind == "tool_call"],
                         [ToolCall(0, "call-one", "web_search", '{"q":"example"}'),
                          ToolCall(1, "call-two", "read_page", '{"url":"https://example.invalid"}')])
        self.assertEqual(events[-1].finish_reason, "tool_calls")

    async def test_sse_line_endings_multiline_data_and_comments(self):
        for ending in (b"\r\n", b"\r", b"\n"):
            self.fake.frames = (b'event: message\nid: sample\nretry: 20\n: comment\ndata: {"choices":\ndata: [{"index":0,"delta":{"content":"Answer"},"finish_reason":"stop"}]}\n\n'
                                + DONE).replace(b"\n", ending)
            self.assertEqual("".join(e.text for e in await self.events() if e.kind == "text"), "Answer")

    async def test_tool_calls_with_only_ids_are_assembled_without_guessing_from_list_position(self):
        self.fake.frames = (
            frame({"tool_calls": [{"id": "call-one", "function": {"name": "search", "arguments": "{"}},
                                  {"id": "call-two", "function": {"name": "read", "arguments": "{"}}]})
            + frame({"tool_calls": [{"id": "call-two", "function": {"arguments": "}"}},
                                    {"id": "call-one", "function": {"arguments": "}"}}]})
            + frame({}, finish="tool_calls") + DONE)
        events = await self.events()
        self.assertEqual([e.tool_call for e in events if e.kind == "tool_call"],
                         [ToolCall(0, "call-one", "search", "{}"), ToolCall(1, "call-two", "read", "{}")])

    async def test_malformed_frames_fail_with_safe_typed_errors(self):
        invalid = (
            b"data: not json\n\n", b"data: []\n\n", b"not SSE\n\n", b"data: \xff\n\n",
            b'data: {"choices":"wrong"}\n\n', b'data: {"choices":[{"delta":5}]}\n\n',
            frame({"content": []}), frame({"reasoning": {}}), frame({"tool_calls": "wrong"}),
            frame({"tool_calls": [{"index": -1}]}),
            frame({"tool_calls": [{"index": 0, "function": {"name": "missing-id"}}]}) + frame({}, finish="tool_calls"),
            frame({"tool_calls": [{"index": 0, "id": "one"}]}) + frame({"tool_calls": [{"index": 0, "id": "changed"}]}),
            frame({"content": "Partial"}) + DONE, frame({"content": "Partial"}),
            b'data: {"error":{"message":"example-test-key"}}\n\n',
            frame({}, finish="stop") + frame({"content": "after finish"}), frame(usage=[]),
        )
        self.fake.chunk_sizes = (65536,)
        for body in invalid:
            self.fake.frames = body
            with self.subTest(body=body), self.assertRaises(MalformedFrame) as caught:
                await self.events()
            self.assertNotIn("example-test-key", str(caught.exception))
        self.fake.content_type = "application/json"
        with self.assertRaises(MalformedFrame):
            await self.events()

    async def test_line_frame_stream_and_nonstream_limits(self):
        self.fake.chunk_sizes = (65536,)
        for limit, body in (("MAX_LINE", b"data: " + b"x" * 200),
                            ("MAX_FRAME", b"data: " + b"x" * 60 + b"\ndata: " + b"y" * 60 + b"\n\n"),
                            ("MAX_RESPONSE", frame({"content": "x" * 200}) + frame({}, finish="stop") + DONE)):
            self.fake.frames = body
            with patch("roost.provider." + limit, 100), self.assertRaises(MalformedFrame):
                await self.events()
        with patch("roost.provider.MAX_RESPONSE", 20), self.assertRaises(MalformedFrame):
            await self.provider.complete("example-model", MESSAGES)

    async def test_http_errors_busy_redaction_redirect_and_no_retry(self):
        for status in (400, 401, 429, 503, 500, 302):
            self.fake.status = status
            self.fake.error_body = "Refused example-test-key " + "x" * 1000
            count = len(self.fake.requests)
            with self.subTest(status=status), self.assertRaises(ProviderHTTPError) as caught:
                await self.events()
            error = caught.exception
            self.assertEqual(error.status, status)
            self.assertEqual(isinstance(error, ProviderBusy), status in (429, 503))
            self.assertNotIn("example-test-key", str(error))
            self.assertEqual(str(error), f"Model server returned HTTP {status}.")
            self.assertLessEqual(len(error._excerpt), 300)
            self.assertNotIn("example-test-key", error._excerpt)
            self.assertFalse(hasattr(error, "excerpt"))
            self.assertEqual(len(self.fake.requests), count + 1)

    async def test_models_and_optional_auth(self):
        self.assertEqual(await self.provider.list_models(), self.fake.models)
        public = Provider(self.session, self.fake.url + "/")
        await public.list_models()
        self.assertNotIn("Authorization", self.fake.requests[-1][1])
        self.fake.models = ["same", "same"]
        self.assertEqual(await public.list_models(), ["same"])
        self.fake.models = [None]
        with self.assertRaises(MalformedFrame):
            await public.list_models()
        self.fake.status = 503
        with self.assertRaises(ProviderBusy):
            await public.list_models()

    async def test_json_escaped_keys_are_redacted_from_upstream_errors(self):
        key = 'example-"quoted-key'
        self.fake.status = 401
        self.fake.error_body = json.dumps({"error": "Refused " + key})
        with self.assertRaises(ProviderHTTPError) as caught:
            await Provider(self.session, self.fake.url, key).list_models()
        self.assertNotIn(key, str(caught.exception))
        self.assertNotIn(json.dumps(key)[1:-1], str(caught.exception))
        self.assertEqual(str(caught.exception), "Model server returned HTTP 401.")
        self.assertIn("[redacted]", caught.exception._excerpt)

    async def test_slash_escaped_key_and_body_never_enter_public_error(self):
        key = "a/b"
        self.fake.status = 401
        self.fake.error_body = r'"a\/b"'
        with self.assertRaises(ProviderHTTPError) as caught:
            await Provider(self.session, self.fake.url, key).list_models()
        error = caught.exception
        self.assertEqual(str(error), "Model server returned HTTP 401.")
        self.assertNotIn(key, str(error))
        self.assertNotIn(self.fake.error_body, str(error))
        self.assertNotIn(key, error._excerpt)
        self.assertNotIn(r"a\/b", error._excerpt)
        self.assertEqual(error.args, ("Model server returned HTTP 401.",))

    async def test_provider_errors_classify_context_overflow_privately(self):
        overflow = (
            "This model's maximum context length is 8192 tokens. However, you requested 9000 tokens.",
            "the request exceeds the available context size. try increasing the context size",
            "request (9000 tokens) exceeds the available context size (8192 tokens)",
            "Prompt is too long", "Input is too long for this model", "context window exceeded",
            "context_length_exceeded", "exceed_context_size_error",
        )
        for message in (*overflow, "Model is busy", "Invalid request", "Failed to allocate context", ""):
            for transport in ("http", "stream"):
                with self.subTest(message=message, transport=transport):
                    value = {"error": {"message": message + " example-test-key " + "x" * 1000}}
                    if transport == "http":
                        self.fake.status = 400
                        self.fake.error_body = json.dumps(value)
                    else:
                        self.fake.status = 200
                        self.fake.frames = frame({"content": "Partial"}) + b"data: " + json.dumps(value).encode() + b"\n\n"
                    with self.assertRaises(ProviderError) as caught:
                        await self.events()
                    error = caught.exception
                    self.assertEqual(error.kind, "context_overflow" if message in overflow else "other")
                    self.assertLessEqual(len(error._excerpt), 300)
                    self.assertNotIn("example-test-key", error._excerpt)
                    self.assertNotIn(message or "example-test-key", str(error))
                    self.assertNotIn("example-test-key", repr(error))

    async def test_stream_finish_without_done_is_complete_and_keeps_usage(self):
        self.fake.frames = (frame({"content": "Answer"}) + frame({}, finish="stop")
                            + frame(usage={"completion_tokens": 1}))
        events = await self.events()
        self.assertEqual([e.finish_reason for e in events if e.kind == "finish"], ["stop"])
        self.assertEqual(events[-1].usage, {"completion_tokens": 1})

    async def test_chat_and_discovery_timeouts_have_separate_defaults_and_overrides(self):
        expected = aiohttp.ClientTimeout(total=None, connect=10, sock_read=900)
        self.assertEqual(self.provider.timeout, expected)
        request = self.session.request
        with patch.object(self.session, "request", wraps=request) as captured:
            await self.events()
            self.assertEqual(captured.call_args.kwargs["timeout"], expected)
            await self.provider.complete("example-model", MESSAGES)
            self.assertEqual(captured.call_args.kwargs["timeout"], expected)
            await self.provider.list_models()
            self.assertEqual(captured.call_args.kwargs["timeout"].total, 30)
        custom = aiohttp.ClientTimeout(total=0.01)
        provider = Provider(self.session, self.fake.url, timeout=custom)
        self.fake.delay = 0.05
        for method in (provider.complete, provider.stream):
            with self.assertRaises(ProviderTimeout):
                result = method("example-model", MESSAGES)
                if hasattr(result, "__aiter__"):
                    await anext(result)
                else:
                    await result
        # An override also reaches discovery, whose fake handler returns without delay.
        with patch.object(self.session, "request", wraps=request) as captured:
            await provider.list_models()
            self.assertEqual(captured.call_args.kwargs["timeout"], custom)

    async def test_nonstreaming_normalization_tools_usage_and_thinking(self):
        for message in ({"content": "<think>Thought</think>Answer"},
                        {"content": "Answer", "reasoning": "Thought"},
                        {"content": "<think>duplicate</think>Answer", "reasoning": "duplicate", "reasoning_content": "Thought"}):
            self.fake.completion["choices"][0]["message"] = message
            result = await self.provider.complete("example-model", MESSAGES, thinking="low", mode="template_flag")
            self.assertEqual((result.content, result.reasoning, result.finish_reason), ("Answer", "Thought", "stop"))
            self.assertEqual(result.usage, self.fake.completion["usage"])
        self.assertEqual(self.fake.requests[-1][2]["chat_template_kwargs"], {"enable_thinking": True})
        self.fake.completion["choices"][0].update(message={"content": None, "tool_calls": [
            {"id": "call-one", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]}, finish_reason="tool_calls")
        result = await self.provider.complete("example-model", MESSAGES)
        self.assertEqual(result.tool_calls, [ToolCall(0, "call-one", "web_search", "{}")])
        self.assertEqual(result.content, "")
        for response in ({}, {"choices": [5]}, {"choices": [{"message": []}]},
                         {"choices": [{"message": {"content": []}, "finish_reason": "stop"}]}):
            self.fake.completion = response
            with self.assertRaises(MalformedFrame):
                await self.provider.complete("example-model", MESSAGES)

    async def test_connection_failure_and_timeout_are_typed_and_safe(self):
        url = self.fake.url
        await self.fake.close()
        with self.assertRaises(ProviderConnectionError) as caught:
            await Provider(self.session, url, "example-test-key").list_models()
        self.assertNotIn(url, str(caught.exception))
        self.fake = await OpenAIFake().start()
        self.fake.delay = 0.05
        timed = Provider(self.session, self.fake.url, timeout=aiohttp.ClientTimeout(total=0.01))
        with self.assertRaises(ProviderTimeout):
            await timed.complete("example-model", MESSAGES)


class RequestTests(unittest.TestCase):
    def test_thinking_modes_are_exact_and_send_no_sampling(self):
        for level in ("off", "low", "medium", "high"):
            self.assertEqual(thinking_fields(level, "reasoning_effort"), {"reasoning_effort": "none" if level == "off" else level})
            self.assertEqual(thinking_fields(level, "template_flag"), {"chat_template_kwargs": {"enable_thinking": level != "off"}})
            self.assertEqual(thinking_fields(level, "unsupported"), {})
        for level, mode in (("default", "unsupported"), ("low", "other")):
            with self.assertRaises(ValueError):
                thinking_fields(level, mode)

    def test_base_url_rejects_credentials_queries_fragments_and_bad_ports(self):
        for value in ("file:///tmp/model", "https://name:example-test-key@example.invalid", "https://example.invalid?key=sample",
                      "https://example.invalid#fragment", "https://example.invalid:bad", "https://example.invalid\n", None,
                      123, True, [], {}, b"https://example.invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                base_url(value)
        self.assertEqual(base_url("https://models.example.invalid/v1/"), "https://models.example.invalid/v1")
