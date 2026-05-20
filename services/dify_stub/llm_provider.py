"""
LLM Provider — provider-agnostic OpenAI-compatible client
=========================================================
Swap between Ollama, OpenAI, Gemini, Groq, Together, Mistral, LM Studio, or
any OpenAI-compatible service by changing *only* environment variables.

Supported providers (all expose /v1/chat/completions):
  Ollama     → LLM_BASE_URL=http://host.docker.internal:11434/v1  LLM_API_KEY=(empty)
  OpenAI     → LLM_BASE_URL=https://api.openai.com/v1             LLM_API_KEY=sk-...
  Gemini     → LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
  Groq       → LLM_BASE_URL=https://api.groq.com/openai/v1        LLM_API_KEY=gsk_...
  Together   → LLM_BASE_URL=https://api.together.xyz/v1           LLM_API_KEY=...
  Mistral    → LLM_BASE_URL=https://api.mistral.ai/v1             LLM_API_KEY=...
  LM Studio  → LLM_BASE_URL=http://localhost:1234/v1              LLM_API_KEY=(empty)

Environment variables:
  LLM_BASE_URL      Base URL of the /v1 endpoint (see above)
  LLM_API_KEY       API key (empty = no auth, suitable for local Ollama)
  LLM_MODEL         Model name (e.g. "qwen2.5-coder:7b", "gpt-4o-mini", "gemini-2.0-flash")
  LLM_TEMPERATURE   Sampling temperature (default: 0.2)
  LLM_MAX_TOKENS    Max tokens in response (default: 1024)
  LLM_TIMEOUT       HTTP timeout in seconds (default: 120)

Backward-compatible aliases (still accepted):
  LITELLM_URL   → LLM_BASE_URL
  LITELLM_MODEL → LLM_MODEL
"""

import asyncio
import json
import logging
import os
import random
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger("llm_provider")

# ──────────────────────────────────────────────────────────────────────────────
# Config (read from env, with backward-compat aliases)
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_base_url() -> str:
    """Return a clean /v1 base URL, normalising legacy Ollama root URLs."""
    raw = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("LITELLM_URL", "http://host.docker.internal:11434")
    ).rstrip("/")
    # Legacy: LITELLM_URL pointed to Ollama root — append /v1
    if not raw.endswith("/v1"):
        raw = raw + "/v1"
    return raw


LLM_BASE_URL: str = _resolve_base_url()
LLM_API_KEY: str = os.getenv("LLM_API_KEY") or os.getenv("LITELLM_API_KEY", "")
LLM_MODEL: str = (
    os.getenv("LLM_MODEL") or os.getenv("LITELLM_MODEL", "qwen2.5-coder:7b")
)
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "1024"))
LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "120.0"))
LLM_RETRY_MAX: int = int(os.getenv("LLM_RETRY_MAX", "5"))
LLM_RETRY_BASE_DELAY: float = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.5"))
LLM_RETRY_MAX_DELAY: float = float(os.getenv("LLM_RETRY_MAX_DELAY", "20.0"))
LLM_RETRY_JITTER: float = float(os.getenv("LLM_RETRY_JITTER", "0.25"))
LLM_FALLBACK_BASE_URL: str = os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/")
if LLM_FALLBACK_BASE_URL and not LLM_FALLBACK_BASE_URL.endswith("/v1"):
    LLM_FALLBACK_BASE_URL += "/v1"
LLM_FALLBACK_API_KEY: str = os.getenv("LLM_FALLBACK_API_KEY", "")
LLM_FALLBACK_MODEL: str = os.getenv("LLM_FALLBACK_MODEL", LLM_MODEL)

logger.info(
    "LLM provider ready — base_url=%s  model=%s  auth=%s",
    LLM_BASE_URL,
    LLM_MODEL,
    "yes" if LLM_API_KEY else "no",
)


def _headers(api_key: str | None = None) -> dict:
    key = api_key if api_key is not None else LLM_API_KEY
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _has_fallback() -> bool:
    return bool(LLM_FALLBACK_BASE_URL)


def _retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                delay = float(retry_after)
                return min(delay, LLM_RETRY_MAX_DELAY)
            except ValueError:
                pass
    delay = min(LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1)), LLM_RETRY_MAX_DELAY)
    jitter = delay * LLM_RETRY_JITTER
    return max(0.0, delay + random.uniform(-jitter, jitter))


def _should_retry_status(status_code: int) -> bool:
    return status_code in {429, 502, 503, 504}


def _should_retry_primary(status_code: int) -> bool:
    """Retry the primary provider only when fallback is unavailable."""
    if status_code == 429 and _has_fallback():
        return False
    return _should_retry_status(status_code)


async def _retryable_post(client: httpx.AsyncClient, url: str, headers: dict, payload: dict) -> httpx.Response:
    last_exc: httpx.HTTPError | None = None
    for attempt in range(1, LLM_RETRY_MAX + 1):
        try:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            status = exc.response.status_code
            if attempt >= LLM_RETRY_MAX or not _should_retry_status(status):
                raise
            delay = _retry_delay(attempt, exc.response)
            logger.warning(
                "LLM request returned %d; retry %d/%d after %.1fs",
                status,
                attempt,
                LLM_RETRY_MAX,
                delay,
            )
            await asyncio.sleep(delay)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt >= LLM_RETRY_MAX:
                raise
            delay = _retry_delay(attempt)
            logger.warning(
                "LLM transport error; retry %d/%d after %.1fs: %s",
                attempt,
                LLM_RETRY_MAX,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────

class ToolCall:
    """A single tool call requested by the LLM."""

    __slots__ = ("id", "name", "arguments")

    def __init__(self, id: str, name: str, arguments: "str | dict"):
        self.id = id
        self.name = name
        if isinstance(arguments, str):
            try:
                self.arguments: dict = json.loads(arguments)
            except Exception:
                self.arguments = {}
        else:
            self.arguments = arguments or {}

    def __repr__(self) -> str:
        return f"ToolCall(id={self.id!r}, name={self.name!r}, args={self.arguments})"


class LLMResponse:
    """Parsed response from /v1/chat/completions."""

    __slots__ = ("content", "tool_calls", "finish_reason")

    def __init__(
        self,
        content: Optional[str],
        tool_calls: Optional[list[ToolCall]],
        finish_reason: str,
    ):
        self.content = content
        self.tool_calls: list[ToolCall] = tool_calls or []
        self.finish_reason = finish_reason  # "stop" | "tool_calls" | "length" | "error"

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_response(data: dict) -> LLMResponse:
    """Parse a /v1/chat/completions response body into an LLMResponse."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"
    content = msg.get("content")

    raw_calls = msg.get("tool_calls") or []
    tool_calls = [
        ToolCall(
            id=tc.get("id", f"call_{i}"),
            name=tc["function"]["name"],
            arguments=tc["function"].get("arguments", "{}"),
        )
        for i, tc in enumerate(raw_calls)
        if tc.get("function", {}).get("name")
    ]

    # ── Fallback: some models (e.g. qwen2.5-coder) emit tool calls as JSON
    # text in `content` instead of populating the `tool_calls` array.
    # Detect and convert so the agentic loop works regardless of model.
    if not tool_calls and content and finish_reason in ("stop", "tool_calls"):
        tool_calls = _try_parse_text_tool_calls(content)
        if tool_calls:
            finish_reason = "tool_calls"
            content = None  # consumed

    return LLMResponse(content=content, tool_calls=tool_calls, finish_reason=finish_reason)


def _try_parse_text_tool_calls(text: str) -> list[ToolCall]:
    """
    Attempt to parse tool calls that a model emitted as JSON text.

    Handles these common patterns:
      {"name": "fn",  "arguments": {...}}
      {"name": "fn",  "parameters": {...}}
      [{"name": "fn", "arguments": {...}}, ...]
      <tool_call>{...}</tool_call>
      Any text that CONTAINS a JSON object with a "name" key
      ```json {...} ```
    """
    import re
    text = text.strip()

    # Pattern 1: <tool_call>{...}</tool_call>
    xml_match = re.search(r'<tool_call>\s*([\s\S]*?)\s*</tool_call>', text)
    if xml_match:
        text = xml_match.group(1).strip()

    # Pattern 2: ```json...``` or ```...```
    md_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if md_match:
        text = md_match.group(1).strip()

    # Determine candidate JSON string
    if text.startswith(("{", "[")):
        candidate = text
    else:
        # Extract first JSON object or array from mixed text
        m = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
        candidate = m.group(1) if m else None

    if not candidate:
        return []

    try:
        parsed = json.loads(candidate)
    except Exception:
        # Last resort: find smallest valid JSON object with "name" key
        for m in re.finditer(r'\{[^{}]*\}', text):
            try:
                obj = json.loads(m.group())
                if isinstance(obj, dict) and obj.get("name"):
                    parsed = obj
                    break
            except Exception:
                continue
        else:
            return []

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    result = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("function")
        args = item.get("arguments") or item.get("parameters") or item.get("args") or {}
        if not name:
            continue
        result.append(ToolCall(id=f"fallback_{i}", name=name, arguments=args))
    return result


def _tool_call_to_dict(tc: ToolCall) -> dict:
    """Serialise a ToolCall back to the OpenAI message format."""
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.name,
            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
        },
    }


def _is_tool_use_failed(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code != 400:
        return False
    try:
        data = exc.response.json()
    except Exception:
        return False
    err = data.get("error") or {}
    return err.get("code") == "tool_use_failed"


def _textual_tool_instruction(tools: list[dict]) -> str:
    tool_names = []
    for tool in tools:
        fn = (tool or {}).get("function") or {}
        name = fn.get("name")
        if name:
            tool_names.append(name)
    allowed = ", ".join(tool_names) if tool_names else "nessuno"
    return (
        "Se devi usare un tool, NON usare function calling nativo. "
        "Emetti SOLO un blocco nel formato "
        "<tool_call>{\"name\":\"nome_tool\",\"arguments\":{...}}</tool_call>. "
        f"Usa esclusivamente uno di questi tool: {allowed}. "
        "Gli arguments devono essere JSON valido con tipi corretti."
    )


def _messages_with_textual_tool_retry(messages: list[dict], tools: list[dict]) -> list[dict]:
    retried = list(messages)
    retried.append({"role": "system", "content": _textual_tool_instruction(tools)})
    return retried


# ──────────────────────────────────────────────────────────────────────────────
# Non-streaming call
# ──────────────────────────────────────────────────────────────────────────────

async def chat(
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> LLMResponse:
    """
    POST /v1/chat/completions and return a parsed LLMResponse.
    Raises httpx exceptions on network / HTTP errors.
    """
    actual_base_url = base_url or LLM_BASE_URL
    actual_model = model or LLM_MODEL
    payload: dict = {
        "model": actual_model,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=httpx.Timeout(LLM_TIMEOUT)) as client:
        try:
            resp = await _retryable_post(
                client,
                f"{actual_base_url}/chat/completions",
                _headers(api_key),
                payload,
            )
            return _parse_response(resp.json())
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if (
                actual_base_url == LLM_BASE_URL
                and _has_fallback()
                and _should_retry_primary(status)
            ):
                logger.warning(
                    "Primary LLM provider failed with %d; switching to fallback %s",
                    status,
                    LLM_FALLBACK_BASE_URL,
                )
                return await chat(
                    messages,
                    tools=tools,
                    base_url=LLM_FALLBACK_BASE_URL,
                    api_key=LLM_FALLBACK_API_KEY,
                    model=LLM_FALLBACK_MODEL,
                )
            if not tools or not _is_tool_use_failed(exc):
                raise
            logger.warning("Native tool use failed; retrying with textual tool-call fallback")
            retry_payload = {
                "model": actual_model,
                "messages": _messages_with_textual_tool_retry(messages, tools),
                "temperature": LLM_TEMPERATURE,
                "max_tokens": LLM_MAX_TOKENS,
            }
            retry_resp = await _retryable_post(
                client,
                f"{actual_base_url}/chat/completions",
                _headers(api_key),
                retry_payload,
            )
            return _parse_response(retry_resp.json())


# ──────────────────────────────────────────────────────────────────────────────
# Streaming call
# ──────────────────────────────────────────────────────────────────────────────

async def chat_stream(
    messages: list[dict],
    tools: Optional[list[dict]] = None,
) -> AsyncIterator["str | LLMResponse"]:
    """
    Async generator that streams a /v1/chat/completions call.

    Yields:
      str         — text token (only when finish_reason will be "stop")
      LLMResponse — always the last item; contains tool_calls if the model
                    wants to call tools, or the full content for a final answer.

    If the model wants tool calls, text tokens are typically NOT yielded
    (the model outputs structured JSON, not prose). The caller should buffer
    all str items and only emit them to the client once it knows the response
    is a final text answer (finish_reason == "stop").
    """
    payload: dict = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    full_content = ""
    tool_calls_acc: dict[int, dict] = {}
    finish_reason = "stop"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(LLM_TIMEOUT)) as client:
            for attempt in range(1, LLM_RETRY_MAX + 1):
                try:
                    async with client.stream(
                        "POST",
                        f"{LLM_BASE_URL}/chat/completions",
                        headers=_headers(),
                        json=payload,
                    ) as resp:
                        resp.raise_for_status()

                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            chunk_data = line[6:].strip()
                            if chunk_data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(chunk_data)
                            except Exception:
                                continue

                            choice = (chunk.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            fr = choice.get("finish_reason")
                            if fr:
                                finish_reason = fr

                            # Text token
                            token = delta.get("content") or ""
                            if token:
                                full_content += token
                                yield token

                            # Tool call delta accumulation
                            for tc_delta in delta.get("tool_calls") or []:
                                idx = tc_delta.get("index", 0)
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {
                                        "id": tc_delta.get("id", f"call_{idx}"),
                                        "function": {"name": "", "arguments": ""},
                                    }
                                fn = tc_delta.get("function") or {}
                                tool_calls_acc[idx]["function"]["name"] += fn.get("name") or ""
                                tool_calls_acc[idx]["function"]["arguments"] += fn.get("arguments") or ""
                    break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if (
                        _has_fallback()
                        and status in {429, 502, 503, 504}
                    ):
                        logger.warning(
                            "Primary streaming LLM returned %d; switching immediately to fallback %s",
                            status,
                            LLM_FALLBACK_BASE_URL,
                        )
                        fallback_response = await chat(
                            messages,
                            tools=tools,
                            base_url=LLM_FALLBACK_BASE_URL,
                            api_key=LLM_FALLBACK_API_KEY,
                            model=LLM_FALLBACK_MODEL,
                        )
                        yield fallback_response
                        return
                    if attempt >= LLM_RETRY_MAX or not _should_retry_primary(status):
                        raise
                    delay = _retry_delay(attempt, exc.response)
                    logger.warning(
                        "LLM stream request returned %d; retry %d/%d after %.1fs",
                        status,
                        attempt,
                        LLM_RETRY_MAX,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                except httpx.HTTPError as exc:
                    if attempt >= LLM_RETRY_MAX:
                        raise
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "LLM stream transport error; retry %d/%d after %.1fs: %s",
                        attempt,
                        LLM_RETRY_MAX,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue

    except httpx.HTTPError as exc:
        status = None
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
        if tools and isinstance(exc, httpx.HTTPStatusError) and _is_tool_use_failed(exc):
            logger.warning("Native streaming tool use failed; retrying with textual tool-call fallback")
            retry_response = await chat(_messages_with_textual_tool_retry(messages, tools), tools=None)
            yield retry_response
            return
        if _has_fallback():
            logger.warning(
                "Primary streaming LLM failed with %s; switching to fallback provider %s",
                status or exc,
                LLM_FALLBACK_BASE_URL,
            )
            fallback_response = await chat(
                messages,
                tools=tools,
                base_url=LLM_FALLBACK_BASE_URL,
                api_key=LLM_FALLBACK_API_KEY,
                model=LLM_FALLBACK_MODEL,
            )
            yield fallback_response
            return
            logger.warning(
                "Primary streaming LLM failed with %d; switching to fallback provider %s",
                status,
                LLM_FALLBACK_BASE_URL,
            )
            fallback_response = await chat(
                messages,
                tools=tools,
                base_url=LLM_FALLBACK_BASE_URL,
                api_key=LLM_FALLBACK_API_KEY,
                model=LLM_FALLBACK_MODEL,
            )
            yield fallback_response
            return
        logger.warning("LLM stream error: %s", exc)
        yield LLMResponse(content=None, tool_calls=[], finish_reason="error")
        return
    except Exception as exc:
        logger.warning("LLM stream error: %s", exc)
        yield LLMResponse(content=None, tool_calls=[], finish_reason="error")
        return

    # Build final tool_calls list
    tool_calls = [
        ToolCall(
            id=v["id"],
            name=v["function"]["name"],
            arguments=v["function"]["arguments"],
        )
        for v in sorted(tool_calls_acc.values(), key=lambda x: list(tool_calls_acc.values()).index(x))
        if v["function"]["name"]
    ]

    # Streaming fallback: same text-based tool call detection as non-streaming
    if not tool_calls and full_content and finish_reason in ("stop", "tool_calls"):
        logger.info("Streaming fallback check — full_content=%r", full_content[:400])
        tool_calls = _try_parse_text_tool_calls(full_content)
        if tool_calls:
            logger.info("Streaming fallback: parsed %d tool_call(s) from text", len(tool_calls))
            finish_reason = "tool_calls"
            full_content = ""
        else:
            logger.info("Streaming fallback: no tool calls found in content")

    yield LLMResponse(
        content=full_content or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
    )


def tool_call_to_message(tc: ToolCall) -> dict:
    """Convenience re-export for app.py."""
    return _tool_call_to_dict(tc)
