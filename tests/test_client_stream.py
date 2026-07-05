"""
test_client_stream.py — streaming chat client tests (CI tier, headless).

Boots a local aiohttp server scripted to emit SSE chat-completion chunks and
drives OpenWebUIClient.chat_stream / chat(stream=True) against it.  Covers:
text-only streams, tool-call fragment streams, malformed chunks, keepalive
comments, early disconnect, and shape-equality between the streamed
aggregate and the equivalent non-streaming response.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from aiohttp import web

from client import OpenWebUIClient, StreamAggregator

# ---------------------------------------------------------------------------
# Local SSE test server
# ---------------------------------------------------------------------------

def _sse(chunk: dict | str) -> str:
    """Render one SSE data frame."""
    data = chunk if isinstance(chunk, str) else json.dumps(chunk)
    return f"data: {data}\n\n"


def _chunk(delta: dict, finish_reason: str | None = None, **extra) -> dict:
    """Build a chat.completion.chunk body with a single choice."""
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    body.update(extra)
    return body


@contextlib.asynccontextmanager
async def sse_server(stream_frames: list[str], non_stream_body: dict | None = None):
    """
    Run a local /api/chat/completions endpoint.

    Streaming requests (payload stream=true) get ``stream_frames`` written
    verbatim as an SSE body.  Non-streaming requests get ``non_stream_body``
    as JSON, so tests can compare the two paths against one server.
    Yields an OpenWebUIClient pointed at the server.
    """

    async def handler(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        if not payload.get("stream"):
            return web.json_response(non_stream_body or {"error": "no body scripted"})
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await resp.prepare(request)
        for frame in stream_frames:
            await resp.write(frame.encode("utf-8"))
        return resp

    app = web.Application()
    app.router.add_post("/api/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    client = OpenWebUIClient(
        base_url=f"http://127.0.0.1:{port}",
        api_key="",
        model="test-model",
        timeout_seconds=10,
    )
    try:
        yield client
    finally:
        await runner.cleanup()


MESSAGES = [{"role": "user", "content": "hi"}]

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


# ---------------------------------------------------------------------------
# Text-only stream
# ---------------------------------------------------------------------------

async def test_text_stream_yields_deltas_and_aggregates():
    frames = [
        _sse(_chunk({"role": "assistant", "content": "Hel"})),
        _sse(_chunk({"content": "lo, "})),
        _sse(_chunk({"content": "world."})),
        _sse(_chunk({}, finish_reason="stop", usage=USAGE)),
        _sse("[DONE]"),
    ]
    non_stream = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Hello, world."},
        }],
        "usage": USAGE,
    }
    async with sse_server(frames, non_stream_body=non_stream) as client:
        events = []
        async for event in client.chat_stream(MESSAGES):
            events.append(event)

        text_deltas = [e["text"] for e in events if e["type"] == "text_delta"]
        assert text_deltas == ["Hel", "lo, ", "world."]
        assert events[0]["type"] == "meta"
        assert events[0]["model"] == "test-model"
        assert any(e["type"] == "finish" and e["finish_reason"] == "stop" for e in events)
        assert any(e["type"] == "usage" and e["usage"] == USAGE for e in events)

        # Aggregate must be shape-identical to the non-streaming response.
        agg = StreamAggregator()
        for e in events:
            agg.add(e)
        streamed = agg.response()
        blocking = await client.chat(MESSAGES, stream=False)
        assert streamed == blocking

        # chat(stream=True) returns the same aggregate.
        via_chat = await client.chat(MESSAGES, stream=True)
        assert via_chat == blocking
        # And the standard extractors keep working on it.
        msg = OpenWebUIClient.extract_message(via_chat)
        assert OpenWebUIClient.extract_text(msg) == "Hello, world."
        assert OpenWebUIClient.extract_tool_calls(msg) == []


# ---------------------------------------------------------------------------
# Tool-call fragment stream
# ---------------------------------------------------------------------------

async def test_tool_call_fragments_concatenate_by_index():
    frames = [
        _sse(_chunk({"role": "assistant", "content": None, "tool_calls": [
            {"index": 0, "id": "call_a", "type": "function",
             "function": {"name": "desktop_launch", "arguments": ""}},
        ]})),
        _sse(_chunk({"tool_calls": [
            {"index": 0, "function": {"arguments": '{"applica'}},
        ]})),
        _sse(_chunk({"tool_calls": [
            {"index": 0, "function": {"arguments": 'tion": "notepad"}'}},
            {"index": 1, "id": "call_b", "type": "function",
             "function": {"name": "shell_run", "arguments": '{"command"'}},
        ]})),
        _sse(_chunk({"tool_calls": [
            {"index": 1, "function": {"arguments": ': "ls"}'}},
        ]})),
        _sse(_chunk({}, finish_reason="tool_calls", usage=USAGE)),
        _sse("[DONE]"),
    ]
    non_stream = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {
                        "name": "desktop_launch",
                        "arguments": '{"application": "notepad"}',
                    }},
                    {"id": "call_b", "type": "function", "function": {
                        "name": "shell_run",
                        "arguments": '{"command": "ls"}',
                    }},
                ],
            },
        }],
        "usage": USAGE,
    }
    async with sse_server(frames, non_stream_body=non_stream) as client:
        streamed = await client.chat(MESSAGES, stream=True)
        blocking = await client.chat(MESSAGES, stream=False)
        assert streamed == blocking

        calls = OpenWebUIClient.extract_tool_calls(OpenWebUIClient.extract_message(streamed))
        assert [c["id"] for c in calls] == ["call_a", "call_b"]
        assert json.loads(calls[0]["function"]["arguments"]) == {"application": "notepad"}
        assert json.loads(calls[1]["function"]["arguments"]) == {"command": "ls"}


# ---------------------------------------------------------------------------
# Robustness: malformed chunks, keepalive comments
# ---------------------------------------------------------------------------

async def test_malformed_chunk_is_skipped():
    frames = [
        _sse(_chunk({"content": "before"})),
        "data: {this is not json]\n\n",
        _sse(_chunk({"content": " after"})),
        _sse(_chunk({}, finish_reason="stop")),
        _sse("[DONE]"),
    ]
    async with sse_server(frames) as client:
        response = await client.chat(MESSAGES, stream=True)
        msg = OpenWebUIClient.extract_message(response)
        assert OpenWebUIClient.extract_text(msg) == "before after"
        assert response["choices"][0]["finish_reason"] == "stop"


async def test_keepalive_comments_are_ignored():
    frames = [
        ": keepalive\n\n",
        _sse(_chunk({"content": "hi"})),
        ": ping\n",  # comment glued to the next event without its own blank line
        _sse(_chunk({}, finish_reason="stop")),
        _sse("[DONE]"),
    ]
    async with sse_server(frames) as client:
        events = [e async for e in client.chat_stream(MESSAGES)]
        text = "".join(e["text"] for e in events if e["type"] == "text_delta")
        assert text == "hi"


# ---------------------------------------------------------------------------
# Failure paths: early disconnect, HTTP error
# ---------------------------------------------------------------------------

async def test_early_disconnect_raises_clear_error():
    frames = [
        _sse(_chunk({"content": "partial"})),
        # server stops here — no finish chunk, no [DONE]
    ]
    async with sse_server(frames) as client:
        with pytest.raises(RuntimeError, match=r"before \[DONE\]"):
            async for _ in client.chat_stream(MESSAGES):
                pass

        # chat(stream=True) surfaces the same error rather than returning
        # a silently truncated aggregate.
        with pytest.raises(RuntimeError, match=r"before \[DONE\]"):
            await client.chat(MESSAGES, stream=True)


async def test_http_error_status_raises():
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"detail": "boom"}, status=500)

    app = web.Application()
    app.router.add_post("/api/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    client = OpenWebUIClient(
        base_url=f"http://127.0.0.1:{port}", api_key="", model="test-model",
        timeout_seconds=10,
    )
    try:
        with pytest.raises(RuntimeError, match="HTTP 500") as excinfo:
            async for _ in client.chat_stream(MESSAGES):
                pass
        assert getattr(excinfo.value, "http_status", None) == 500
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Aggregator unit behaviour
# ---------------------------------------------------------------------------

def test_aggregator_defaults_finish_reason():
    agg = StreamAggregator()
    agg.add({"type": "text_delta", "text": "x"})
    assert agg.response()["choices"][0]["finish_reason"] == "stop"

    agg = StreamAggregator()
    agg.add({"type": "tool_call_delta", "index": 0, "id": "c1",
             "name": "t", "arguments": "{}"})
    assert agg.response()["choices"][0]["finish_reason"] == "tool_calls"


def test_aggregator_repeated_name_delta_does_not_concatenate():
    """A provider that repeats the function name across deltas must not
    yield "shell_runshell_run"; only the arguments fragments accumulate."""
    agg = StreamAggregator()
    agg.add({"type": "tool_call_delta", "index": 0, "id": "c1",
             "name": "shell_run", "arguments": '{"comm'})
    # Second delta repeats the name (some providers do this) and streams
    # the rest of the arguments.
    agg.add({"type": "tool_call_delta", "index": 0,
             "name": "shell_run", "arguments": 'and": "ls"}'})
    call = agg.response()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "shell_run"
    assert json.loads(call["function"]["arguments"]) == {"command": "ls"}
