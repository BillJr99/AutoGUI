"""
client.py — Async OpenWebUI / Ollama API client.

Supports both OpenWebUI (POST {base_url}/api/chat/completions) and direct
Ollama access (POST {base_url}/v1/chat/completions) via the ``api_path``
config key.  Set ``api_path`` to ``/v1/chat/completions`` in config.json to
bypass OpenWebUI entirely and hit Ollama's native OpenAI-compatible endpoint.

Streaming: ``chat_stream()`` yields structured delta events parsed from the
server's SSE stream, and ``StreamAggregator`` reassembles those deltas into
a response dict shape-identical to the non-streaming ``chat()`` return, so
callers can stream for display and still hand the aggregate to code that
expects the blocking shape.
"""

import json
import logging
import traceback
import uuid
from typing import Any, AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_API_PATH = "/api/chat/completions"


class StreamAggregator:
    """
    Assemble ``chat_stream`` delta events into a final response dict.

    Feed every event yielded by ``OpenWebUIClient.chat_stream`` to ``add()``,
    then call ``response()`` to get a dict shape-identical to what the
    non-streaming ``chat()`` call would have returned for the same
    completion: ``{"id", "object", "created", "model", "choices": [{"index",
    "finish_reason", "message": {...}}], "usage"?}``.

    Tool-call fragments are keyed by their ``index`` field and their
    ``arguments`` fragments are concatenated in arrival order, matching the
    OpenAI streaming tool-call protocol.
    """

    def __init__(self) -> None:
        self._meta: dict = {}
        self._text_parts: list[str] = []
        self._tool_calls: dict[int, dict] = {}
        self._finish_reason: str | None = None
        self._usage: dict | None = None

    def add(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "meta":
            for key in ("id", "model", "created"):
                if event.get(key) is not None:
                    self._meta[key] = event[key]
        elif etype == "text_delta":
            self._text_parts.append(event.get("text") or "")
        elif etype == "tool_call_delta":
            try:
                idx = int(event.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            slot = self._tool_calls.setdefault(idx, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if event.get("id"):
                slot["id"] = event["id"]
            if event.get("name"):
                slot["function"]["name"] += event["name"]
            if event.get("arguments"):
                slot["function"]["arguments"] += event["arguments"]
        elif etype == "finish":
            if event.get("finish_reason"):
                self._finish_reason = event["finish_reason"]
        elif etype == "usage":
            if isinstance(event.get("usage"), dict):
                self._usage = event["usage"]

    def response(self) -> dict:
        """Return the aggregated non-streaming-shaped response dict."""
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self._text_parts),
        }
        if self._tool_calls:
            message["tool_calls"] = [
                self._tool_calls[i] for i in sorted(self._tool_calls)
            ]
        finish_reason = self._finish_reason or (
            "tool_calls" if self._tool_calls else "stop"
        )
        response: dict[str, Any] = {
            "id": self._meta.get("id", ""),
            "object": "chat.completion",
            "created": self._meta.get("created", 0),
            "model": self._meta.get("model", ""),
            "choices": [{
                "index": 0,
                "finish_reason": finish_reason,
                "message": message,
            }],
        }
        if self._usage is not None:
            response["usage"] = self._usage
        return response


class OpenWebUIClient:
    """
    Thin async wrapper around an OpenAI-compatible chat completions endpoint.

    Parameters
    ----------
    base_url : str
        Root URL, e.g. "http://localhost:3000" (OpenWebUI) or
        "http://localhost:11434" (Ollama).
    api_key : str
        Bearer token.  Pass "" for Ollama (no auth required).
    model : str
        Model identifier, e.g. "qwen3:14b".
    api_path : str
        Path appended to base_url for completions.
        Default "/api/chat/completions" (OpenWebUI).
        Use "/v1/chat/completions" to talk directly to Ollama.
    temperature : float
        Sampling temperature passed to the model.
    max_tokens : int
        Maximum completion tokens per request.
    timeout_seconds : int
        Per-request timeout.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        api_path: str = _DEFAULT_API_PATH,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
    ):
        self.base_url = (base_url or "http://localhost:3000").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.api_path = api_path or _DEFAULT_API_PATH
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._endpoint = f"{self.base_url}{self.api_path}"
        # Stable session ID sent as chat_id — OpenWebUI v0.9.5+ requires this
        # field in every /api/chat/completions request; absent = NoneType crash.
        self._chat_id = str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------

    async def _ensure_model(self, tools: list[dict] | None, caller: str) -> None:
        """Auto-select a model from the endpoint when none is configured."""
        if self.model:
            return
        try:
            models = await self.fetch_models(prefer_tools_capable=bool(tools))
            if models:
                self.model = models[0]
                logger.info(
                    "[client.py:%s] No model configured; auto-selected %r.",
                    caller,
                    self.model,
                )
            else:
                raise ValueError(
                    f"[client.py:{caller}] No model configured and endpoint returned no models. "
                    "Set 'openwebui.model' in config.json."
                )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                f"[client.py:{caller}] No model configured and could not auto-fetch: {exc}. "
                "Set 'openwebui.model' in config.json."
            ) from exc

    def _build_payload(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float | None,
        stream: bool,
    ) -> dict:
        """Build the chat completions request body shared by chat/chat_stream."""
        # Coerce null content to "" — some pipeline code calls .startswith()
        # on message["content"] without guarding against null.
        sanitized: list[dict] = []
        for m in messages:
            if m.get("content") is None:
                m = {**m, "content": ""}
            sanitized.append(m)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": sanitized,
            "temperature": (
                self.temperature if temperature is None else float(temperature)
            ),
            "max_tokens": self.max_tokens,
            "stream": stream,
            # OpenWebUI v0.9.5+ crashes with NoneType.startswith when chat_id
            # is absent from /api/chat/completions requests (issue #24550).
            "chat_id": self._chat_id,
        }
        if stream:
            # Ask OpenAI-compatible servers to append a final usage chunk so
            # the aggregate can carry the same usage dict as blocking chat().
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            # Omit tool_choice — some OpenWebUI versions crash when tool_choice
            # is set explicitly and the model's FC template is null.  Omitting
            # it is spec-equivalent (defaults to "auto" when tools are present).
        return payload

    def _build_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float | None = None,
    ) -> dict:
        """Send a chat completion request and return the parsed JSON response.

        With ``stream=True`` the request is served via ``chat_stream`` and the
        deltas are aggregated into the same response shape as the blocking
        path, so callers get identical dicts either way.
        """
        if stream:
            aggregator = StreamAggregator()
            async for event in self.chat_stream(
                messages, tools=tools, temperature=temperature
            ):
                aggregator.add(event)
            return aggregator.response()

        await self._ensure_model(tools, caller="chat")

        payload = self._build_payload(messages, tools, temperature, stream=False)
        headers = self._build_headers()

        logger.info(
            "[client.py:chat] POST %s | model=%r | messages=%d | tools=%d",
            self._endpoint,
            self.model,
            len(messages),
            len(tools) if tools else 0,
        )

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    self._endpoint, json=payload, headers=headers
                ) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        import sys
                        detail = ""
                        try:
                            # .get("detail") may return None, dict, or list depending
                            # on the error format; normalize to str to avoid TypeError.
                            detail = str(json.loads(raw).get("detail") or "")
                        except Exception:
                            pass
                        if resp.status == 400 and (
                            "startswith" in detail or "NoneType" in detail
                        ):
                            logger.error(
                                "[client.py:chat] OpenWebUI returned HTTP 400: '%s'. "
                                "The model %r does not have tool-calling configured in "
                                "OpenWebUI.  Workaround: set openwebui.api_path to "
                                "'/v1/chat/completions' and openwebui.base_url to "
                                "'http://localhost:11434' in config.json to bypass "
                                "OpenWebUI and call Ollama directly.",
                                detail, self.model,
                            )
                        msg = (
                            f"\n========== HTTP {resp.status} ==========\n"
                            f"endpoint: {self._endpoint}\n"
                            f"model:    {self.model!r}\n"
                            f"--- response body ---\n{raw}\n"
                            f"=====================================\n"
                        )
                        print(msg, flush=True)
                        print(msg, file=sys.stderr, flush=True)
                        logger.error(
                            "[client.py:chat] HTTP %d from %s | model=%r | body=%s",
                            resp.status, self._endpoint, self.model, raw,
                        )
                        err = RuntimeError(
                            f"[client.py:chat] API returned HTTP {resp.status}: {raw[:500]}"
                        )
                        err.http_status = resp.status  # type: ignore[attr-defined]
                        raise err
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as e:
                        print(f"[client.py:chat] JSON decode error: {e}")
                        traceback.print_exc()
                        raise RuntimeError(
                            f"[client.py:chat] Failed to parse API response as JSON: {raw[:200]}"
                        ) from e

        except aiohttp.ClientError as e:
            print(f"[client.py:chat] HTTP client error: {e}")
            traceback.print_exc()
            raise RuntimeError(f"[client.py:chat] Connection error: {e}") from e

        logger.debug(
            "[client.py:chat] Response: finish_reason=%s",
            data.get("choices", [{}])[0].get("finish_reason", "unknown"),
        )
        return data

    # ------------------------------------------------------------------
    # Streaming interface
    # ------------------------------------------------------------------

    @staticmethod
    def _delta_events_from_chunk(chunk: dict) -> list[dict]:
        """Translate one parsed SSE chunk into structured delta events."""
        events: list[dict] = []
        choices = chunk.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}

        content = delta.get("content")
        if content:
            events.append({"type": "text_delta", "text": content})

        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            events.append({
                "type": "tool_call_delta",
                "index": tc.get("index", 0),
                "id": tc.get("id"),
                "name": fn.get("name"),
                "arguments": fn.get("arguments") or "",
            })

        if choice.get("finish_reason"):
            events.append({
                "type": "finish",
                "finish_reason": choice["finish_reason"],
            })

        if isinstance(chunk.get("usage"), dict):
            events.append({"type": "usage", "usage": chunk["usage"]})
        return events

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[dict]:
        """
        Send a streaming chat completion request and yield delta events.

        Parses the server's SSE response (``data:`` lines terminated by a
        ``data: [DONE]`` sentinel) and yields structured events:

          {"type": "meta", "id": ..., "model": ..., "created": ...}
              — once, from the first parseable chunk.
          {"type": "text_delta", "text": str}
              — one per assistant-content fragment.
          {"type": "tool_call_delta", "index": int, "id": str|None,
           "name": str|None, "arguments": str}
              — one per tool-call fragment; concatenate ``arguments``
              fragments per ``index`` to rebuild each call.
          {"type": "finish", "finish_reason": str}
          {"type": "usage", "usage": dict}

        Feed every event to a ``StreamAggregator`` to rebuild the blocking
        ``chat()`` response shape.  Malformed chunks are logged and skipped;
        SSE comment lines (keepalives) are ignored.  If the connection drops
        before ``[DONE]`` arrives a RuntimeError is raised so the caller can
        fall back to a non-streaming retry.
        """
        await self._ensure_model(tools, caller="chat_stream")

        payload = self._build_payload(messages, tools, temperature, stream=True)
        headers = self._build_headers()

        logger.info(
            "[client.py:chat_stream] POST %s | model=%r | messages=%d | tools=%d",
            self._endpoint,
            self.model,
            len(messages),
            len(tools) if tools else 0,
        )

        meta_sent = False
        done = False
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    self._endpoint, json=payload, headers=headers
                ) as resp:
                    if resp.status != 200:
                        raw = await resp.text()
                        logger.error(
                            "[client.py:chat_stream] HTTP %d from %s | model=%r | body=%s",
                            resp.status, self._endpoint, self.model, raw,
                        )
                        err = RuntimeError(
                            f"[client.py:chat_stream] API returned HTTP {resp.status}: {raw[:500]}"
                        )
                        err.http_status = resp.status  # type: ignore[attr-defined]
                        raise err

                    # SSE framing: events are separated by blank lines; each
                    # event's payload is the concatenation of its data: lines.
                    data_lines: list[str] = []
                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if line == "":
                            if not data_lines:
                                continue
                            data = "\n".join(data_lines)
                            data_lines = []
                            if data.strip() == "[DONE]":
                                done = True
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "[client.py:chat_stream] Skipping malformed SSE chunk: %.200s",
                                    data,
                                )
                                continue
                            if not meta_sent:
                                meta_sent = True
                                yield {
                                    "type": "meta",
                                    "id": chunk.get("id", ""),
                                    "model": chunk.get("model", self.model),
                                    "created": chunk.get("created", 0),
                                }
                            for event in self._delta_events_from_chunk(chunk):
                                yield event
                            continue
                        if line.startswith(":"):
                            continue  # SSE comment — keepalive; ignore.
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        # Other SSE fields (event:, id:, retry:) are ignored.

                    # A trailing [DONE] without a final blank line still counts.
                    if not done and data_lines and "\n".join(data_lines).strip() == "[DONE]":
                        done = True

        except aiohttp.ClientError as e:
            print(f"[client.py:chat_stream] HTTP client error: {e}")
            traceback.print_exc()
            raise RuntimeError(f"[client.py:chat_stream] Connection error: {e}") from e

        if not done:
            # The server closed the connection before sending [DONE] — the
            # completion is (potentially) truncated.  Raise so the caller can
            # discard the partial aggregate and retry non-streaming.
            raise RuntimeError(
                "[client.py:chat_stream] Stream disconnected before [DONE]; "
                "partial response discarded."
            )

    # ------------------------------------------------------------------
    # Convenience extractors
    # ------------------------------------------------------------------

    @staticmethod
    def extract_message(response: dict) -> dict:
        try:
            return response["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            print(f"[client.py:extract_message] Malformed response structure: {e}")
            traceback.print_exc()
            raise ValueError(
                f"[client.py:extract_message] Cannot extract message from response: {response}"
            ) from e

    @staticmethod
    def extract_tool_calls(message: dict) -> list[dict]:
        return message.get("tool_calls") or []

    @staticmethod
    def extract_text(message: dict) -> str:
        return message.get("content") or ""

    async def fetch_models(self, prefer_tools_capable: bool = False) -> list[str]:
        """
        Fetch available model IDs.  Works with both OpenWebUI (/api/models)
        and Ollama (/api/tags or /v1/models).
        """
        # Try OpenWebUI-style endpoint first, then Ollama-style fallbacks.
        candidates = [
            (f"{self.base_url}/api/models", "openwebui"),
            (f"{self.base_url}/v1/models", "openai"),
            (f"{self.base_url}/api/tags", "ollama"),
        ]
        headers = {"Accept-Encoding": "gzip, deflate"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_exc: Exception | None = None
        for url, style in candidates:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 401:
                            raise PermissionError(
                                "Authentication failed (HTTP 401) — API key is invalid or missing."
                            )
                        if resp.status != 200:
                            last_exc = RuntimeError(f"HTTP {resp.status} from {url}")
                            continue
                        data = await resp.json(content_type=None)
                        if style == "ollama":
                            items_raw = data.get("models", [])
                            items = [
                                {"id": m.get("name") or m.get("model", "")}
                                for m in items_raw
                                if m.get("name") or m.get("model")
                            ]
                        else:
                            items_raw = data.get("data", [])
                            items = [i for i in items_raw if i.get("id")]

                        if prefer_tools_capable and style == "openwebui":
                            def _tools_key(item: dict) -> tuple:
                                caps = (
                                    (item.get("info") or {})
                                    .get("meta") or {}
                                ).get("capabilities") or {}
                                # Compound key: tools-capable first, then alphabetical
                                # by id for deterministic ordering within each group.
                                return (0 if caps.get("tools") else 1, item.get("id", ""))
                            items = sorted(items, key=_tools_key)
                        else:
                            items = sorted(items, key=lambda x: x["id"])

                        return [item["id"] for item in items if item["id"]]
            except PermissionError:
                raise
            except Exception as e:
                last_exc = e
                continue

        raise ConnectionError(
            f"Cannot reach endpoint at {self.base_url}: {last_exc}"
        )

    async def health_check(self) -> bool:
        try:
            await self.fetch_models()
            return True
        except Exception as e:
            print(f"[client.py:health_check] {e}")
            return False
