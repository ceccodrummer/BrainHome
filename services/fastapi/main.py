from pathlib import Path
import json
import re
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
import os
from pydantic import BaseModel, Field, validator

app = FastAPI()

# Disable caching for static files so JS/CSS changes are picked up immediately
class NoCacheStaticFiles(StaticFiles):
    async def __call__(self, scope, receive, send):
        async def send_with_nocache(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                headers[b"cache-control"] = b"no-store, no-cache, must-revalidate"
                headers[b"pragma"] = b"no-cache"
                message = {**message, "headers": list(headers.items())}
            await send(message)
        await super().__call__(scope, receive, send_with_nocache)

app.mount(
    "/static",
    NoCacheStaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

dify_url = os.getenv("DIFY_URL", "http://dify:3000")
dify_api_key = os.getenv("DIFY_API_KEY", "")
MOBILE_UI_VERSION = "1.2.0"
AGENTS_CONFIG_PATH = os.getenv("AGENTS_CONFIG_PATH", "/config/agents.json")
AGENT_BROKER_TOKEN = os.getenv("AGENT_BROKER_TOKEN", "")
AGENT_MESSAGE_CONTRACT_VERSION = "1.0"
AGENT_MESSAGE_MAX_CHARS = 4000
AGENT_MESSAGE_REASON_MAX_CHARS = 200
AGENT_MESSAGE_ANSWER_MAX_CHARS = 8000
AGENT_MESSAGE_MAX_HOPS = 8
AGENT_MESSAGE_DEFAULT_HOPS = 3
AGENT_BROKER_TIMEOUT_SECONDS = float(os.getenv("AGENT_BROKER_TIMEOUT_SECONDS", "45"))

AgentMessageMode = Literal["ask", "delegate", "notify"]
AgentMessageErrorCode = Literal[
    "unauthorized",
    "unknown_target",
    "policy_denied",
    "timeout",
    "loop_blocked",
    "invalid_payload",
]


class AgentMessageRequest(BaseModel):
    to_agent_id: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=AGENT_MESSAGE_MAX_CHARS)
    reason: str = Field(default="", max_length=AGENT_MESSAGE_REASON_MAX_CHARS)
    mode: AgentMessageMode = "ask"
    await_response: bool = True
    protocol_version: str = AGENT_MESSAGE_CONTRACT_VERSION

    @validator("to_agent_id")
    def validate_to_agent_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("to_agent_id vuoto.")
        return value

    @validator("message")
    def validate_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message vuoto.")
        return value

    @validator("reason")
    def validate_reason(cls, value: str) -> str:
        return value.strip()

    @validator("await_response")
    def validate_await_response(cls, value: bool, values: dict) -> bool:
        if values.get("mode") == "notify" and value:
            raise ValueError("await_response deve essere false quando mode=notify.")
        return value


class AgentMessageEnvelope(AgentMessageRequest):
    from_agent_id: str = Field(..., min_length=1, max_length=64)
    trace_id: str = Field(..., min_length=1, max_length=128)
    conversation_id: str = Field(..., min_length=1, max_length=128)
    hop_count: int = Field(..., ge=1, le=AGENT_MESSAGE_MAX_HOPS)
    max_hops: int = Field(default=AGENT_MESSAGE_DEFAULT_HOPS, ge=1, le=AGENT_MESSAGE_MAX_HOPS)
    visited_agents: list[str] = Field(default_factory=list)

    @validator("from_agent_id", "trace_id", "conversation_id")
    def validate_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Campo obbligatorio vuoto.")
        return value

    @validator("visited_agents")
    def validate_visited_agents(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("visited_agents contiene duplicati.")
        return cleaned

    @validator("max_hops")
    def validate_hop_window(cls, value: int, values: dict) -> int:
        hop_count = values.get("hop_count")
        if hop_count is not None and value < hop_count:
            raise ValueError("max_hops non puo essere inferiore a hop_count.")
        return value


class AgentMessageError(BaseModel):
    code: AgentMessageErrorCode
    message: str = Field(..., min_length=1, max_length=500)
    retryable: bool = False


class AgentMessageResponse(BaseModel):
    status: Literal["ok", "error"]
    trace_id: str = Field(..., min_length=1, max_length=128)
    target_agent_id: str = Field(..., min_length=1, max_length=64)
    mode: AgentMessageMode
    latency_ms: int = Field(..., ge=0)
    answer: str = Field(default="", max_length=AGENT_MESSAGE_ANSWER_MAX_CHARS)
    target_session_id: str = Field(default="", max_length=128)
    error: AgentMessageError | None = None
    protocol_version: str = AGENT_MESSAGE_CONTRACT_VERSION

    @validator("answer")
    def validate_answer(cls, value: str) -> str:
        return value.strip()

    @validator("error")
    def validate_error_consistency(cls, value: "AgentMessageError | None", values: dict):
        status = values.get("status")
        if status == "error" and value is None:
            raise ValueError("error obbligatorio quando status=error.")
        if status == "ok" and value is not None:
            raise ValueError("error deve essere assente quando status=ok.")
        return value


def _agent_message_error_response(
    *,
    trace_id: str,
    target_agent_id: str,
    mode: AgentMessageMode,
    code: AgentMessageErrorCode,
    message: str,
    retryable: bool = False,
    latency_ms: int = 0,
) -> AgentMessageResponse:
    return AgentMessageResponse(
        status="error",
        trace_id=trace_id,
        target_agent_id=target_agent_id,
        mode=mode,
        latency_ms=latency_ms,
        error=AgentMessageError(code=code, message=message, retryable=retryable),
    )

def _load_agents_registry() -> tuple[list[dict], str]:
    registry_path = Path(AGENTS_CONFIG_PATH)
    if registry_path.exists():
        agents = json.loads(registry_path.read_text(encoding="utf-8"))
        return agents, str(registry_path)

    agents_raw = os.getenv("AGENTS_CONFIG", "")
    if agents_raw:
        return json.loads(agents_raw), "env:AGENTS_CONFIG"

    return [{"id": "dify", "name": "Principale", "url": dify_url, "mention": "principale", "role": "primary"}], "default"


def _validate_agents_registry(agents: list[dict]) -> list[dict]:
    if not isinstance(agents, list) or not agents:
        raise RuntimeError("Agent registry vuoto o non valido.")

    validated: list[dict] = []
    seen_ids: set[str] = set()
    seen_mentions: set[str] = set()
    for idx, agent in enumerate(agents):
        if not isinstance(agent, dict):
            raise RuntimeError(f"Agent registry entry #{idx} non valida.")
        agent_id = str(agent.get("id", "")).strip()
        name = str(agent.get("name", "")).strip()
        url = str(agent.get("url", "")).strip()
        mention = str(agent.get("mention", agent_id)).strip().lower()
        role = str(agent.get("role", "")).strip()
        if not agent_id or not name or not url:
            raise RuntimeError(f"Agent registry entry incompleta per indice {idx}.")
        if agent_id in seen_ids:
            raise RuntimeError(f"Agent id duplicato nel registry: {agent_id}")
        if mention in seen_mentions:
            raise RuntimeError(f"Agent mention duplicata nel registry: {mention}")
        seen_ids.add(agent_id)
        seen_mentions.add(mention)
        validated.append({
            "id": agent_id,
            "name": name,
            "url": url,
            "mention": mention,
            "role": role,
        })
    return validated


AGENTS, AGENTS_REGISTRY_SOURCE = _load_agents_registry()
AGENTS = _validate_agents_registry(AGENTS)


def _find_agent_by_id(agent_id: str) -> dict | None:
    for agent in AGENTS:
        if agent["id"] == agent_id:
            return agent
    return None

def _agent_url(target: str | None) -> str:
    if not target:
        return dify_url
    agent = _find_agent_by_id(target)
    if agent:
        return agent["url"]
    return dify_url


def _find_agent_by_mention(token: str) -> dict | None:
    token = token.strip().lower()
    for agent in AGENTS:
        mention = agent.get("mention", "").lower()
        if mention and mention == token:
            return agent
        if agent["id"].lower() == token:
            return agent
        if agent["name"].lower() == token:
            return agent
    return None


def _parse_agent_forwarding(question: str) -> dict | None:
    patterns = [
        r"(?:invia|manda|spedisci|passa)\s+(?:a\s+)?(?:l'|all'|allagente\s+)?(?P<target>[\w\s]+?)\s*(?:a|al|allo|alla|all'|:|,|che|di|per|$)",
        r"(?:chiedi|domanda|interroga)\s+(?:all'|allagente\s+)?(?P<target>[\w\s]+?)\s*(?:a|al|allo|alla|all'|:|,|che|di|per|$)",
        r"@(?P<target>[\w\s]+?)\b",
    ]
    lower = question.lower()
    for pattern in patterns:
        match = re.search(pattern, lower)
        if not match:
            continue
        target_token = match.group("target").strip()
        agent = _find_agent_by_mention(target_token)
        if not agent:
            continue
        prompt = question[:match.start()] + question[match.end():]
        prompt = prompt.strip(' :,-').strip()
        if not prompt:
            prompt = question
        return {
            "target": agent,
            "prompt": prompt,
        }
    return None

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"

def mobile_page_html():
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    return html.replace("{{version}}", MOBILE_UI_VERSION)

@app.get("/", response_class=HTMLResponse)
def root():
    return mobile_page_html()

@app.get("/mobile", response_class=HTMLResponse)
def mobile():
    return mobile_page_html()

@app.get("/agents")
def get_agents():
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "mention": a.get("mention", a["id"]),
            "role": a.get("role", ""),
        }
        for a in AGENTS
    ]

@app.get("/health")
def health():
    return {
        "status": "ok",
        "dify_url": dify_url,
        "version": MOBILE_UI_VERSION,
        "agents": len(AGENTS),
        "agents_registry_source": AGENTS_REGISTRY_SOURCE,
        "agent_broker_auth_configured": bool(AGENT_BROKER_TOKEN),
        "agent_message_contract_version": AGENT_MESSAGE_CONTRACT_VERSION,
        "agent_message_limits": {
            "message_max_chars": AGENT_MESSAGE_MAX_CHARS,
            "reason_max_chars": AGENT_MESSAGE_REASON_MAX_CHARS,
            "answer_max_chars": AGENT_MESSAGE_ANSWER_MAX_CHARS,
            "default_max_hops": AGENT_MESSAGE_DEFAULT_HOPS,
            "absolute_max_hops": AGENT_MESSAGE_MAX_HOPS,
        },
        "agent_broker_timeout_seconds": AGENT_BROKER_TIMEOUT_SECONDS,
    }


def _verify_agent_broker_token(authorization: str = Header(default="")):
    if not AGENT_BROKER_TOKEN:
        raise HTTPException(status_code=503, detail="Agent broker token non configurato.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != AGENT_BROKER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized.")


async def _forward_agent_message(envelope: AgentMessageEnvelope) -> AgentMessageResponse:
    source_agent = _find_agent_by_id(envelope.from_agent_id)
    if source_agent is None:
        return _agent_message_error_response(
            trace_id=envelope.trace_id,
            target_agent_id=envelope.to_agent_id,
            mode=envelope.mode,
            code="unauthorized",
            message=f"Agente mittente sconosciuto: {envelope.from_agent_id}",
        )

    target_agent = _find_agent_by_id(envelope.to_agent_id)
    if target_agent is None:
        return _agent_message_error_response(
            trace_id=envelope.trace_id,
            target_agent_id=envelope.to_agent_id,
            mode=envelope.mode,
            code="unknown_target",
            message=f"Agente destinatario sconosciuto: {envelope.to_agent_id}",
        )

    if envelope.from_agent_id == envelope.to_agent_id:
        return _agent_message_error_response(
            trace_id=envelope.trace_id,
            target_agent_id=envelope.to_agent_id,
            mode=envelope.mode,
            code="policy_denied",
            message="Self-message non consentito dal broker interno.",
        )

    if envelope.hop_count > envelope.max_hops:
        return _agent_message_error_response(
            trace_id=envelope.trace_id,
            target_agent_id=envelope.to_agent_id,
            mode=envelope.mode,
            code="loop_blocked",
            message="hop_count supera max_hops.",
        )

    target_session_id = f"agentlink:{envelope.conversation_id}:{envelope.from_agent_id}:{envelope.to_agent_id}"
    question = envelope.message
    if envelope.reason:
        question = f"[Motivo delega: {envelope.reason}] {question}"

    payload = {
        "question": question,
        "session_id": target_session_id,
        "agent_context": {
            "from_agent_id": envelope.from_agent_id,
            "from_agent_name": source_agent["name"],
            "from_agent_role": source_agent.get("role", ""),
            "trace_id": envelope.trace_id,
            "conversation_id": envelope.conversation_id,
            "mode": envelope.mode,
            "hop_count": envelope.hop_count,
            "max_hops": envelope.max_hops,
            "visited_agents": envelope.visited_agents,
        },
    }

    timeout = httpx.Timeout(AGENT_BROKER_TIMEOUT_SECONDS, connect=min(20.0, AGENT_BROKER_TIMEOUT_SECONDS))
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(f"{target_agent['url']}/query", json=payload)
            response.raise_for_status()
        except httpx.TimeoutException:
            return _agent_message_error_response(
                trace_id=envelope.trace_id,
                target_agent_id=envelope.to_agent_id,
                mode=envelope.mode,
                code="timeout",
                message=f"Timeout in attesa di {envelope.to_agent_id}.",
                retryable=True,
                latency_ms=int(AGENT_BROKER_TIMEOUT_SECONDS * 1000),
            )
        except httpx.HTTPError as exc:
            detail = str(exc)
            if isinstance(exc, httpx.HTTPStatusError):
                detail = exc.response.text[:300] or detail
            return _agent_message_error_response(
                trace_id=envelope.trace_id,
                target_agent_id=envelope.to_agent_id,
                mode=envelope.mode,
                code="invalid_payload",
                message=f"Errore broker verso {envelope.to_agent_id}: {detail}",
            )

    answer = ""
    target_latency_ms = 0
    data = response.json()
    if envelope.await_response:
        answer = str(data.get("answer", "")).strip()
        if len(answer) > AGENT_MESSAGE_ANSWER_MAX_CHARS:
            answer = answer[:AGENT_MESSAGE_ANSWER_MAX_CHARS]
        target_latency_ms = int(data.get("latency_ms", 0) or 0)

    return AgentMessageResponse(
        status="ok",
        trace_id=envelope.trace_id,
        target_agent_id=envelope.to_agent_id,
        mode=envelope.mode,
        latency_ms=target_latency_ms,
        answer=answer,
        target_session_id=str(data.get("session_id", target_session_id)),
    )


@app.post("/internal/agent-message", response_model=AgentMessageResponse)
async def internal_agent_message(
    payload: AgentMessageEnvelope,
    authorization: str = Header(default=""),
    x_agent_id: str = Header(default=""),
):
    _verify_agent_broker_token(authorization)

    caller_agent_id = x_agent_id.strip()
    if not caller_agent_id:
        return _agent_message_error_response(
            trace_id=payload.trace_id,
            target_agent_id=payload.to_agent_id,
            mode=payload.mode,
            code="unauthorized",
            message="Header X-Agent-Id mancante.",
        )

    if caller_agent_id != payload.from_agent_id:
        return _agent_message_error_response(
            trace_id=payload.trace_id,
            target_agent_id=payload.to_agent_id,
            mode=payload.mode,
            code="unauthorized",
            message="from_agent_id non coerente con X-Agent-Id.",
        )

    return await _forward_agent_message(payload)

@app.post("/proxy")
async def proxy(payload: dict):
    target = payload.pop("target", None)
    question = payload.get("question") or payload.get("query") or payload.get("text") or ""
    if isinstance(question, str):
        forwarding = _parse_agent_forwarding(question)
        if forwarding:
            target = forwarding["target"]["id"]
            payload["question"] = f"Il collega ti chiede: \"{forwarding['prompt']}\". Rispondi come {forwarding['target']['name']}."
    agent_url = _agent_url(target)
    timeout = httpx.Timeout(120.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{agent_url}/query", json=payload)
        response.raise_for_status()
        return response.json()


@app.post("/proxy/stream")
async def proxy_stream(payload: dict):
    target = payload.pop("target", None)
    question = payload.get("question") or payload.get("query") or payload.get("text") or ""
    if isinstance(question, str):
        forwarding = _parse_agent_forwarding(question)
        if forwarding:
            target = forwarding["target"]["id"]
            payload["question"] = f"Il collega ti chiede: \"{forwarding['prompt']}\". Rispondi come {forwarding['target']['name']}."
    agent_url = _agent_url(target)
    async def _passthrough():
        timeout = httpx.Timeout(connect=20.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{agent_url}/query/stream", json=payload) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk
    return StreamingResponse(_passthrough(), media_type="text/event-stream")
