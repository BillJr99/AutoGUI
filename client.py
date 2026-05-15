"""
client.py — Async OpenWebUI API client.

OpenWebUI exposes an OpenAI-compatible REST API at:
  POST {base_url}/api/chat/completions

Authentication uses a Bearer token derived from the OpenWebUI API key
(generated under Settings → Account → API Keys in the OpenWebUI UI).

Tool calling follows the OpenAI function-calling schema:
  - Request includes a `tools` list (JSON Schema descriptors).
  - Response may contain `tool_calls` in the assistant message.
  - The caller appends tool results as role="tool" messages and re-invokes.

This module is intentionally framework-agnostic: it returns raw dicts so
that agent.py can manage message history and dispatch without coupling to
any particular HTTP library abstraction.
"""

import json
import logging
import traceback
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class OpenWebUIClient:
    """
    Thin async wrapper around the OpenWebUI /api/chat/completions endpoint.

    Parameters
    ----------
    base_url : str
        Root URL of the OpenWebUI instance, e.g. "http://localhost:3000".
    api_key : str
        Bearer token for authentication.
    model : str
        Model identifier as registered in OpenWebUI (e.g. "llama3.1:70b").
    temperature : float
        Sampling temperature passed to the model.
    max_tokens : int
        Maximum completion tokens per request.
    timeout_seconds : int
        Per-request timeout; prevents indefinite hangs on slow inference.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
    ):
        self.base_url = (base_url or "http://localhost:3000").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._endpoint = f"{self.base_url}/api/chat/completions"

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float | None = None,
    ) -> dict:
        """
        Send a chat completion request and return the parsed JSON response.

        Parameters
        ----------
        messages : list[dict]
            Full conversation history in OpenAI message format.
            Each message is {"role": ..., "content": ...} with optional
            "tool_calls" or "tool_call_id" fields as required by the protocol.
        tools : list[dict] or None
            Tool descriptors in OpenAI function-calling JSON Schema format.
            Pass None to disable tool calling for this request.
        stream : bool
            Streaming is not yet implemented; must be False.

        Returns
        -------
        dict
            Raw parsed JSON response from the API, containing at minimum:
            {
              "choices": [
                {
                  "message": {
                    "role": "assistant",
                    "content": "...",          # may be None if tool_calls present
                    "tool_calls": [...]        # present when model invokes tools
                  },
                  "finish_reason": "stop" | "tool_calls" | "length"
                }
              ],
              "usage": {"prompt_tokens": N, "completion_tokens": M, "total_tokens": K}
            }

        Raises
        ------
        RuntimeError
            If the HTTP response is not 2xx or JSON parsing fails.
        """
        if stream:
            raise NotImplementedError("[client.py:chat] Streaming not yet implemented.")

        if not self.model:
            try:
                models = await self.fetch_models()
                if models:
                    self.model = models[0]
                    logger.info(
                        "[client.py:chat] No model configured; auto-selected %r from OpenWebUI.",
                        self.model,
                    )
                else:
                    raise ValueError(
                        "[client.py:chat] No model configured and OpenWebUI returned no models. "
                        "Set 'openwebui.model' in config.json or the OPENWEBUI_MODEL env var."
                    )
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(
                    f"[client.py:chat] No model configured and could not auto-fetch from OpenWebUI: {exc}. "
                    "Set 'openwebui.model' in config.json or the OPENWEBUI_MODEL env var."
                ) from exc

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": (
                self.temperature if temperature is None else float(temperature)
            ),
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            # "auto" lets the model decide when to call tools vs. reply directly.
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Exclude brotli: aiohttp may advertise 'br' but can't decode it
            # without the optional brotli/brotlipy package installed.
            "Accept-Encoding": "gzip, deflate",
        }

        logger.debug(
            "[client.py:chat] POST %s | model=%s | messages=%d | tools=%d",
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
                        # Tag auth failures so callers can detect them
                        # programmatically (e.g. fast-client → primary
                        # auto-demote without needing to grep error text).
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
    # Convenience extractors
    # ------------------------------------------------------------------

    @staticmethod
    def extract_message(response: dict) -> dict:
        """
        Pull the assistant message dict from a chat completion response.

        Returns the full message object, which may have both `content` (str | None)
        and `tool_calls` (list | None) fields depending on finish_reason.
        """
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
        """
        Return the list of tool_call objects from an assistant message, or []
        if the message contains a direct text reply instead.

        Each tool_call has the shape:
          {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "tool_name", "arguments": "{...json string...}"}
          }
        """
        return message.get("tool_calls") or []

    @staticmethod
    def extract_text(message: dict) -> str:
        """
        Return the text content of an assistant message, or "" if the message
        is a pure tool-call response (content is None or absent).
        """
        return message.get("content") or ""

    async def fetch_models(self) -> list[str]:
        """
        Fetch the list of available model IDs from /api/models.

        Returns
        -------
        list[str]
            Sorted list of model ID strings.

        Raises
        ------
        PermissionError
            HTTP 401 — the API key is invalid or missing.
        ConnectionError
            Network-level failure (server unreachable, DNS, timeout, etc.).
        RuntimeError
            Any other non-200 HTTP status.
        """
        url = f"{self.base_url}/api/models"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept-Encoding": "gzip, deflate",
        }
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
                        raise RuntimeError(
                            f"Unexpected HTTP {resp.status} from {url}"
                        )
                    data = await resp.json(content_type=None)
                    models = [
                        item["id"]
                        for item in data.get("data", [])
                        if item.get("id")
                    ]
                    return sorted(models)
        except (PermissionError, RuntimeError):
            raise
        except Exception as e:
            raise ConnectionError(
                f"Cannot reach OpenWebUI at {self.base_url}: {e}"
            ) from e

    async def health_check(self) -> bool:
        """
        Verify connectivity to the OpenWebUI instance by hitting /api/models.
        Returns True if reachable, False otherwise.
        """
        try:
            await self.fetch_models()
            return True
        except Exception as e:
            print(f"[client.py:health_check] {e}")
            return False
