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

import json
import logging
import os
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

logger.info(
    "LLM provider ready — base_url=%s  model=%s  auth=%s",
    LLM_BASE_URL,
    LLM_MODEL,
    "yes" if LLM_API_KEY else "no",
)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        h["Authorization"] = f"Bearer {LLM_API_KEY}"
    return h


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
    """
    text = text.strip()
    if not text.startswith(("{", "[")):
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        # Try extracting the first JSON object/array from mixed text
        import re
        m = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
        if not m:
            return []
        try:
            parsed = json.loads(m.group(1))
        except Exception:
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


# ──────────────────────────────────────────────────────────────────────────────
# Non-streaming call
# ──────────────────────────────────────────────────────────────────────────────

async def chat(
    messages: list[dict],
    tools: Optional[list[dict]] = None,
) -> LLMResponse:
    """
    POST /v1/chat/completions and return a parsed LLMResponse.
    Raises httpx exceptions on network / HTTP errors.
    """
    payload: dict = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=httpx.Timeout(LLM_TIMEOUT)) as client:
        resp = await client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return _parse_response(resp.json())


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
        tool_calls = _try_parse_text_tool_calls(full_content)
        if tool_calls:
            finish_reason = "tool_calls"
            full_content = ""

    yield LLMResponse(
        content=full_content or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
    )


def tool_call_to_message(tc: ToolCall) -> dict:
    """Convenience re-export for app.py."""
    return _tool_call_to_dict(tc)
