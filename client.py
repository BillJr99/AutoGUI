"""
client.py — Async OpenWebUI / Ollama API client.

Supports both OpenWebUI (POST {base_url}/api/chat/completions) and direct
Ollama access (POST {base_url}/v1/chat/completions) via the ``api_path``
config key.  Set ``api_path`` to ``/v1/chat/completions`` in config.json to
bypass OpenWebUI entirely and hit Ollama's native OpenAI-compatible endpoint.
"""

import json
import logging
import traceback
import uuid
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_API_PATH = "/api/chat/completions"


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

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        temperature: float | None = None,
    ) -> dict:
        """Send a chat completion request and return the parsed JSON response."""
        if stream:
            raise NotImplementedError("[client.py:chat] Streaming not yet implemented.")

        if not self.model:
            try:
                models = await self.fetch_models(prefer_tools_capable=bool(tools))
                if models:
                    self.model = models[0]
                    logger.info(
                        "[client.py:chat] No model configured; auto-selected %r.",
                        self.model,
                    )
                else:
                    raise ValueError(
                        "[client.py:chat] No model configured and endpoint returned no models. "
                        "Set 'openwebui.model' in config.json."
                    )
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(
                    f"[client.py:chat] No model configured and could not auto-fetch: {exc}. "
                    "Set 'openwebui.model' in config.json."
                ) from exc

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
            "stream": False,
            # OpenWebUI v0.9.5+ crashes with NoneType.startswith when chat_id
            # is absent from /api/chat/completions requests (issue #24550).
            "chat_id": self._chat_id,
        }
        if tools:
            payload["tools"] = tools
            # Omit tool_choice — some OpenWebUI versions crash when tool_choice
            # is set explicitly and the model's FC template is null.  Omitting
            # it is spec-equivalent (defaults to "auto" when tools are present).

        headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

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
                            detail = json.loads(raw).get("detail", "")
                        except Exception:
                            pass
                        if "startswith" in detail or "NoneType" in detail:
                            logger.error(
                                "[client.py:chat] OpenWebUI returned HTTP %d: '%s'. "
                                "The model %r does not have tool-calling configured in "
                                "OpenWebUI.  Workaround: set openwebui.api_path to "
                                "'/v1/chat/completions' and openwebui.base_url to "
                                "'http://localhost:11434' in config.json to bypass "
                                "OpenWebUI and call Ollama directly.",
                                resp.status, detail, self.model,
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
                            def _tools_key(item: dict) -> int:
                                caps = (
                                    (item.get("info") or {})
                                    .get("meta") or {}
                                ).get("capabilities") or {}
                                return 0 if caps.get("tools") else 1
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
