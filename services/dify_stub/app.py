"""
Brain-Home AI Core Service  v3.0
=================================
Agentic LLM orchestration with native function calling.

Key features:
  - Swappable LLM provider via env vars (Ollama / Groq / OpenAI / Gemini)
  - True agentic loop: LLM autonomously calls tools until it has a final answer
  - Parallel tool execution: multiple tools per iteration run concurrently
  - Semantic RAG with sentence-transformers embeddings (cosine similarity)
  - Persistent multi-turn sessions (survive container restarts)
  - SSE streaming with real-time tool activity events

Environment variables: see llm_provider.py and tool_executor.py for the full list.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

import numpy as np
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from sentence_transformers import SentenceTransformer

import llm_provider as llm
from tool_executor import MESSAGE_AGENT_TOOL_SCHEMA, TOOL_SCHEMAS, execute as execute_tool

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("brainhome")

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
KB_DIR = Path(os.getenv("KB_DIR", "/app/data"))
KB_CONFIG_PATH = KB_DIR / "kb_config.json"
KB_PATH = Path(os.getenv("KB_PATH", "/app/data/kb.json"))
SESSIONS_FILE = Path(os.getenv("SESSIONS_FILE", "/app/data/sessions.json"))
MAX_TOOL_ITERATIONS: int = int(os.getenv("MAX_TOOL_ITERATIONS", "3"))
HISTORY_TURNS: int = int(os.getenv("HISTORY_TURNS", "6"))
MAX_TOOL_RESULT_CHARS: int = int(os.getenv("MAX_TOOL_RESULT_CHARS", "1500"))
AGENT_ID: str = os.getenv("AGENT_ID", "dify")
AGENT_NAME: str = os.getenv("AGENT_NAME", "Principale")
AGENT_ROLE: str = os.getenv("AGENT_ROLE", "primary")
AGENTS_CONFIG_PATH = Path(os.getenv("AGENTS_CONFIG_PATH", "/config/agents.json"))
AGENT_BROKER_TOKEN = os.getenv("AGENT_BROKER_TOKEN", "")

SYSTEM_PROMPT: str = os.getenv(
    "SYSTEM_PROMPT",
    (
        "Sei un Senior Software Architect e assistente IA del progetto Brain-Home. "
        "Rispondi sempre in italiano. "
        "Hai accesso a tool per gestire file nel workspace, eseguire script, fare commit Git e coinvolgere altri agenti specializzati. "
        "REGOLA ASSOLUTA: quando la richiesta implica creare, leggere, modificare, eliminare, "
        "spostare o cercare file, DEVI chiamare il tool corrispondente tramite function call. "
        "NON descrivere l'azione, NON fingere di averla eseguita: chiama SEMPRE il tool. "
        "Se non usi il tool, l'azione NON avviene. "
        "Quando un altro agente e piu pertinente o l'utente chiede esplicitamente di coinvolgerlo, usa il tool message_agent. "
        "Quando non servono azioni su file, rispondi in modo preciso e conciso. "
        "Non inventare informazioni non presenti nel contesto fornito."
    ),
)

# Whether to inject a tool-use directive in the user message.
# Helpful for small/local models; disable for Groq/OpenAI (native tool_calls).
INJECT_TOOL_DIRECTIVE: bool = os.getenv("INJECT_TOOL_DIRECTIVE", "true").lower() == "true"


def _load_agents_registry() -> tuple[list[dict], str]:
    if AGENTS_CONFIG_PATH.exists():
        with AGENTS_CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f), str(AGENTS_CONFIG_PATH)

    raw_agents = os.getenv("AGENTS_CONFIG", "")
    if raw_agents:
        return json.loads(raw_agents), "env:AGENTS_CONFIG"

    return [{"id": "dify", "name": "Principale", "url": "http://dify:3000", "mention": "principale", "role": "primary"}], "default"


def _validate_agents_registry(agents: list[dict]) -> list[dict]:
    if not isinstance(agents, list) or not agents:
        raise RuntimeError("Agent registry vuoto o non valido.")

    validated: list[dict] = []
    seen_ids: set[str] = set()
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
        seen_ids.add(agent_id)
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
CURRENT_AGENT = next((agent for agent in AGENTS if agent["id"] == AGENT_ID), None)
if CURRENT_AGENT is None:
    raise RuntimeError(
        f"AGENT_ID {AGENT_ID!r} non presente nel registry {AGENTS_REGISTRY_SOURCE}."
    )

AGENT_NAME = CURRENT_AGENT.get("name") or AGENT_NAME
AGENT_ROLE = CURRENT_AGENT.get("role") or AGENT_ROLE

# -------------------------------------------------------------------
# Semantic Retriever
# -------------------------------------------------------------------
EMBED_MODEL_NAME: str = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")


class SemanticRetriever:
    """Sentence-transformer embeddings + cosine similarity retrieval."""

    def __init__(self, model_name: str):
        logger.info("Loading embedding model: %s", model_name)
        self._model = SentenceTransformer(model_name)
        self._embeddings: dict = {}  # kb_id -> np.ndarray (N, dim)
        self._docs: dict = {}        # kb_id -> list of docs
        logger.info("Embedding model ready")

    def index(self, kb_map: dict):
        """Build or rebuild embedding index for all KB items."""
        for kb_id, docs in kb_map.items():
            if not docs:
                continue
            texts = [d.get("text", "") + " " + d.get("title", "") for d in docs]
            self._embeddings[kb_id] = self._model.encode(texts, normalize_embeddings=True)
            self._docs[kb_id] = docs
        logger.info("Semantic index built: %d bases", len(self._embeddings))

    def retrieve(self, question: str, kb_id: str, fallback_kb: str) -> Optional[dict]:
        """Return the most relevant document via cosine similarity."""
        embs = self._embeddings.get(kb_id) if kb_id in self._embeddings else self._embeddings.get(fallback_kb)
        docs = self._docs.get(kb_id) if kb_id in self._docs else self._docs.get(fallback_kb)
        if embs is None or not docs:
            return None
        q_emb = self._model.encode([question], normalize_embeddings=True)[0]
        scores = embs @ q_emb
        best_idx = int(np.argmax(scores))
        logger.debug("Semantic retrieval: kb=%s score=%.3f doc=%s",
                     kb_id, float(scores[best_idx]), docs[best_idx].get("id"))
        return docs[best_idx]

    def update_kb(self, kb_id: str, kb_map: dict):
        """Re-index a single KB after an upsert/delete."""
        self.index({kb_id: kb_map.get(kb_id, [])})


retriever = SemanticRetriever(EMBED_MODEL_NAME)

# -------------------------------------------------------------------
# Knowledge Base
# -------------------------------------------------------------------
def _load_json(path: Path) -> "list | dict":
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _load_all_kb() -> "dict[str, list]":
    kb_map: dict = {}
    default_items = _load_json(KB_PATH)
    if isinstance(default_items, list):
        kb_map["kb_sistema"] = default_items
    config = _load_json(KB_CONFIG_PATH)
    if isinstance(config, dict):
        for kb_def in config.get("knowledge_bases", []):
            kb_id = kb_def.get("id")
            kb_file = KB_DIR / kb_def.get("file", "")
            if kb_id and kb_file.exists():
                items = _load_json(kb_file)
                if isinstance(items, list):
                    kb_map[kb_id] = items
    return kb_map


def _load_routing_rules() -> "tuple[list, str]":
    config = _load_json(KB_CONFIG_PATH)
    if isinstance(config, dict):
        return config.get("routing_rules", []), config.get("default_kb", "kb_sistema")
    return [], "kb_sistema"


ALL_KB: dict = _load_all_kb()
ROUTING_RULES, DEFAULT_KB = _load_routing_rules()
logger.info("KB loaded: %d bases, %d total items", len(ALL_KB), sum(len(v) for v in ALL_KB.values()))
retriever.index(ALL_KB)


def _route_question(question: str) -> str:
    ql = question.lower()
    best_kb, best_score = DEFAULT_KB, 0
    for rule in ROUTING_RULES:
        score = sum(1 for kw in rule.get("keywords", []) if kw in ql)
        if score > best_score:
            best_score = score
            best_kb = rule.get("target_kb", DEFAULT_KB)
    return best_kb


def _retrieve_best_document(question: str, kb_id: str) -> "Optional[dict]":
    """Semantic retrieval via cosine similarity on sentence embeddings."""
    return retriever.retrieve(question, kb_id, DEFAULT_KB)


# -------------------------------------------------------------------
# Sessions
# -------------------------------------------------------------------
def _load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            with SESSIONS_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_sessions() -> None:
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SESSIONS_FILE.open("w", encoding="utf-8") as f:
            json.dump(SESSIONS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


SESSIONS: dict = _load_sessions()


def _get_or_create_session(session_id: "Optional[str]") -> "tuple[str, dict]":
    if not session_id or session_id not in SESSIONS:
        session_id = session_id or str(uuid.uuid4())
        SESSIONS[session_id] = {"history": [], "active_kb": DEFAULT_KB, "created_at": time.time()}
        _save_sessions()
    return session_id, SESSIONS[session_id]


# -------------------------------------------------------------------
# Simple query detection (skip tool loop for conversational questions)
# -------------------------------------------------------------------
_TOOL_KEYWORDS = (
    "file", "cartella", "directory", "folder", "scrivi", "leggi", "crea",
    "cancella", "elimina", "sposta", "cerca", "trova", "struttura", "workspace",
    "commit", "git", "script", "esegui", "tree", "lista", "elenco",
    "mostra", "apri", "salva", "modifica", "aggiorna",
)
_AGENT_ROUTING_KEYWORDS = (
    "agente", "agenti", "inoltra", "deleg", "chiedi a", "manda a", "invia a",
    "passa a", "coinvolgi", "contatta", "scrivi a", "frontend", "devops",
)


def _is_agent_routing_intent(question: str) -> bool:
    q = question.lower()
    if any(kw in q for kw in _AGENT_ROUTING_KEYWORDS):
        return True
    if "@" in q:
        return True
    if len(AGENTS) > 1:
        for agent in AGENTS:
            mention = str(agent.get("mention", "")).lower()
            name = str(agent.get("name", "")).lower()
            agent_id = str(agent.get("id", "")).lower()
            if mention and f"@{mention}" in q:
                return True
            if name and name in q:
                return True
            if agent_id and agent_id in q:
                return True
    return False

def _needs_tools(question: str) -> bool:
    """Return True if the question likely requires file operations or agent delegation."""
    q = question.lower()
    if any(kw in q for kw in _TOOL_KEYWORDS):
        return True
    if _is_agent_routing_intent(question):
        return True
    return False


# -------------------------------------------------------------------
# Message builder
# -------------------------------------------------------------------
def _build_messages(question: str, session: dict, doc: "Optional[dict]", use_tools: bool = False) -> "list[dict]":
    system_parts = [SYSTEM_PROMPT]
    if doc:
        kb_id = session.get("active_kb", DEFAULT_KB)
        system_parts.append(f"\n\nCONTESTO KNOWLEDGE BASE ({kb_id}):\n{doc.get('text', '')}")
    if len(AGENTS) > 1:
        agent_lines = []
        for agent in AGENTS:
            if agent["id"] == AGENT_ID:
                continue
            role = f" ({agent.get('role')})" if agent.get("role") else ""
            mention = agent.get("mention", agent["id"])
            agent_lines.append(f"- {agent['id']} / @{mention}: {agent['name']}{role}")
        if agent_lines:
            system_parts.append(
                "\n\nAGENTI DISPONIBILI PER DELEGA:\n"
                + "\n".join(agent_lines)
                + "\nUsa message_agent solo se il destinatario e piu pertinente o se l'utente lo richiede."
            )

    messages = [{"role": "system", "content": "\n".join(system_parts)}]
    for turn in session["history"][-HISTORY_TURNS:]:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})

    # Inject tool directive for small/local models that need a nudge.
    # Disable via INJECT_TOOL_DIRECTIVE=false when using Groq/OpenAI.
    force_tool_directive = _is_agent_routing_intent(question)
    if use_tools and (INJECT_TOOL_DIRECTIVE or force_tool_directive):
        directive = (
            "[ISTRUZIONE: devi usare uno o piu tool per rispondere a questa richiesta. "
            "Chiama il tool appropriato adesso — non descrivere l'azione, eseguila.]"
        )
        if force_tool_directive:
            directive = (
                "[ISTRUZIONE: se la richiesta richiede il coinvolgimento di un altro agente, "
                "usa il tool message_agent. Se servono file o script, usa i tool corrispondenti.]"
            )
        user_content = (
            f"{question}\n\n"
            f"{directive}"
        )
    else:
        user_content = question

    messages.append({"role": "user", "content": user_content})
    return messages


# -------------------------------------------------------------------
# Agentic loop helpers
# -------------------------------------------------------------------
def _append_tool_calls(messages: list, response: "llm.LLMResponse") -> None:
    messages.append({
        "role": "assistant",
        "content": response.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in response.tool_calls
        ],
    })


def _append_tool_result(messages: list, tc: "llm.ToolCall", result: str) -> None:
    # Truncate large outputs to avoid slow LLM inference on huge contexts
    if len(result) > MAX_TOOL_RESULT_CHARS:
        result = result[:MAX_TOOL_RESULT_CHARS] + f"\n[...troncato a {MAX_TOOL_RESULT_CHARS} chars]"
    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})


def _extract_delegation_result(tc: "llm.ToolCall", result: str) -> dict | None:
    if tc.name != "message_agent":
        return None
    target_agent_id = str(tc.arguments.get("to_agent_id", "")).strip()
    mode = str(tc.arguments.get("mode", "ask")).strip() or "ask"
    success = not result.startswith("Delega fallita verso")
    error_code = ""
    error_message = ""
    if not success:
        match = re.search(r":\s*([a-z_]+)\s*-\s*(.+)$", result)
        if match:
            error_code = match.group(1).strip()
            error_message = match.group(2).strip()
        else:
            error_message = result
    return {
        "target_agent_id": target_agent_id,
        "target_agent_name": next((a["name"] for a in AGENTS if a["id"] == target_agent_id), target_agent_id),
        "mode": mode,
        "success": success,
        "error_code": error_code,
        "error_message": error_message,
    }


def _finalize_answer(final_text: str, last_tool_result: str) -> str:
    final_text = (final_text or "").strip()
    if final_text:
        return final_text
    last_tool_result = (last_tool_result or "").strip()
    if last_tool_result:
        return last_tool_result
    return "Nessuna risposta generata dal modello. Riprova oppure formula la richiesta in modo piu esplicito."


# -------------------------------------------------------------------
# Agentic loop (non-streaming)
# -------------------------------------------------------------------
async def _agentic_loop(messages: list, active_tools: "list | None" = None) -> "tuple[str, list[str], list[dict]]":
    tools_used: list = []
    delegations_used: list[dict] = []
    last_tool_result: str = ""
    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response = await llm.chat(messages, active_tools)
        except Exception as exc:
            logger.warning("LLM error (iter %d): %s", iteration, exc)
            return (
                f"Errore LLM ({llm.LLM_BASE_URL}, modello: {llm.LLM_MODEL}): {exc}",
                tools_used,
                delegations_used,
            )

        if response.wants_tool:
            _append_tool_calls(messages, response)
            # Execute all requested tools in parallel
            tc_list = response.tool_calls
            results = await asyncio.gather(
                *[execute_tool(tc.name, tc.arguments) for tc in tc_list],
                return_exceptions=True,
            )
            for tc, result in zip(tc_list, results):
                if isinstance(result, Exception):
                    result = f"Errore tool {tc.name}: {result}"
                logger.info("Tool [%d]: %s(%s)", iteration, tc.name, tc.arguments)
                tools_used.append(tc.name)
                result = str(result)
                if result.strip():
                    last_tool_result = result
                delegation = _extract_delegation_result(tc, result)
                if delegation:
                    delegations_used.append(delegation)
                _append_tool_result(messages, tc, result)
        else:
            return _finalize_answer(response.content or "", last_tool_result), tools_used, delegations_used

    return (
        f"Limite {MAX_TOOL_ITERATIONS} iterazioni raggiunto. "
        f"Strumenti usati: {', '.join(tools_used) or 'nessuno'}.",
        tools_used,
        delegations_used,
    )


# -------------------------------------------------------------------
# Agentic loop (streaming)
# -------------------------------------------------------------------
async def _agentic_loop_stream(messages: list, active_tools: "list | None" = None):
    tools_used: list = []
    delegations_used: list[dict] = []
    last_tool_result: str = ""
    for iteration in range(MAX_TOOL_ITERATIONS):
        yield {"type": "thinking", "iteration": iteration}

        # Queue decouples the LLM async-generator from this generator so we
        # can inject keep-alive events every 4 s during the (long) wait for
        # the first token, preventing SSE connection drops.
        q: asyncio.Queue = asyncio.Queue()

        async def _produce(q=q):
            try:
                async for item in llm.chat_stream(messages, active_tools):
                    await q.put(("item", item))
            except Exception as exc:
                await q.put(("err", exc))
            finally:
                await q.put(("done", None))

        task = asyncio.create_task(_produce())
        buffered_tokens: list = []
        final_response = None

        while True:
            try:
                kind, value = await asyncio.wait_for(q.get(), timeout=4.0)
            except asyncio.TimeoutError:
                yield {"type": "keep_alive"}
                continue

            if kind == "done":
                break
            elif kind == "err":
                logger.warning("LLM stream error (iter %d): %s", iteration, value)
                await task
                yield {"type": "final", "content": f"Errore LLM: {value}", "tools_used": tools_used}
                return
            else:
                item = value
                if isinstance(item, str):
                    buffered_tokens.append(item)
                elif isinstance(item, llm.LLMResponse):
                    final_response = item

        await task

        if final_response is None or final_response.finish_reason == "error":
            yield {"type": "final", "content": "Errore durante la generazione.", "tools_used": tools_used}
            return

        if final_response.wants_tool:
            _append_tool_calls(messages, final_response)
            tc_list = final_response.tool_calls
            # Emit tool_start for all tools immediately, then run them in parallel
            for tc in tc_list:
                yield {"type": "tool_start", "tool": tc.name, "args": tc.arguments}
            results = await asyncio.gather(
                *[execute_tool(tc.name, tc.arguments) for tc in tc_list],
                return_exceptions=True,
            )
            for tc, result in zip(tc_list, results):
                if isinstance(result, Exception):
                    result = f"Errore tool {tc.name}: {result}"
                result = str(result)
                logger.info("Tool [stream/%d]: %s(%s)", iteration, tc.name, tc.arguments)
                tools_used.append(tc.name)
                if result.strip():
                    last_tool_result = result
                preview = result[:700] + "\u2026" if len(result) > 700 else result
                yield {"type": "tool_result", "tool": tc.name, "result": preview}
                delegation = _extract_delegation_result(tc, result)
                if delegation:
                    delegations_used.append(delegation)
                _append_tool_result(messages, tc, result)
        else:
            for token in buffered_tokens:
                yield {"type": "token", "text": token}
            final_text = _finalize_answer(final_response.content or "".join(buffered_tokens), last_tool_result)
            yield {
                "type": "final",
                "content": final_text,
                "tools_used": tools_used,
                "delegations_used": delegations_used,
            }
            return

    yield {
        "type": "final",
        "content": f"Limite {MAX_TOOL_ITERATIONS} iterazioni raggiunto. Strumenti: {', '.join(tools_used) or 'nessuno'}.",
        "tools_used": tools_used,
        "delegations_used": delegations_used,
    }


# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI(title="Brain-Home AI Core", version="3.0.0")


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "version": "3.0.0",
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "agent_role": AGENT_ROLE,
        "agents_registry_source": AGENTS_REGISTRY_SOURCE,
        "agent_broker_auth_configured": bool(AGENT_BROKER_TOKEN),
        "llm_base_url": llm.LLM_BASE_URL,
        "llm_model": llm.LLM_MODEL,
        "kb_ids": list(ALL_KB.keys()),
        "kb_items": {k: len(v) for k, v in ALL_KB.items()},
        "routing_rules": len(ROUTING_RULES),
        "max_tool_iterations": MAX_TOOL_ITERATIONS,
        "embed_model": EMBED_MODEL_NAME,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "3.0.0",
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "agent_role": AGENT_ROLE,
        "agents_registry_source": AGENTS_REGISTRY_SOURCE,
        "agents_known": len(AGENTS),
        "agent_broker_auth_configured": bool(AGENT_BROKER_TOKEN),
        "llm_base_url": llm.LLM_BASE_URL,
        "llm_model": llm.LLM_MODEL,
        "kb_ids": list(ALL_KB.keys()),
        "total_kb_items": sum(len(v) for v in ALL_KB.values()),
        "active_sessions": len(SESSIONS),
        "max_tool_iterations": MAX_TOOL_ITERATIONS,
        "embed_model": EMBED_MODEL_NAME,
    }


@app.post("/query/stream")
async def query_stream(payload: dict):
    """
    SSE streaming endpoint.
    Events: meta | tool_start | tool_result | token | done
    """
    question = (
        payload.get("question")
        or payload.get("query")
        or payload.get("text")
        or str(payload)
    )
    session_id, session = _get_or_create_session(payload.get("session_id"))
    kb_id = _route_question(question)
    session["active_kb"] = kb_id
    doc = _retrieve_best_document(question, kb_id)
    active_tools = TOOL_SCHEMAS if _needs_tools(question) else None
    messages = _build_messages(question, session, doc, use_tools=active_tools is not None)
    logger.info("Query [stream]: tools=%s  q=%s", active_tools is not None, question[:60])
    t_start = time.time()

    async def _sse_gen():
        yield f"data: {json.dumps({'type': 'meta', 'kb_used': kb_id, 'session_id': session_id})}\n\n"
        final_answer = ""
        tools_used: list = []
        delegations_used: list[dict] = []
        if active_tools is not None:
            yield f"data: {json.dumps({'type': 'thinking', 'iteration': 0})}\n\n"
            final_answer, tools_used, delegations_used = await _agentic_loop(messages, active_tools)
        else:
            async for event in _agentic_loop_stream(messages, active_tools):
                etype = event["type"]
                if etype in ("token", "tool_start", "tool_result", "thinking"):
                    yield f"data: {json.dumps(event)}\n\n"
                elif etype == "keep_alive":
                    yield ": keepalive\n\n"  # SSE comment — keeps TCP connection alive
                elif etype == "final":
                    final_answer = event.get("content", "")
                    tools_used = event.get("tools_used", [])
                    delegations_used = event.get("delegations_used", [])
        session["history"].append({"question": question, "answer": final_answer})
        _save_sessions()
        done_payload = {
            "type": "done",
            "answer": final_answer,
            "latency_ms": round((time.time() - t_start) * 1000),
            "tools_used": tools_used,
            "delegations_used": delegations_used,
            "llm_model": llm.LLM_MODEL,
            "kb_used": kb_id,
        }
        yield f"data: {json.dumps(done_payload)}\n\n"

    return StreamingResponse(_sse_gen(), media_type="text/event-stream")


@app.post("/query")
async def query(payload: dict):
    """Non-streaming query endpoint."""
    question = (
        payload.get("question")
        or payload.get("query")
        or payload.get("text")
        or str(payload)
    )
    session_id, session = _get_or_create_session(payload.get("session_id"))
    kb_id = _route_question(question)
    session["active_kb"] = kb_id
    doc = _retrieve_best_document(question, kb_id)
    active_tools = TOOL_SCHEMAS if _needs_tools(question) else None
    messages = _build_messages(question, session, doc, use_tools=active_tools is not None)
    logger.info("Query: tools=%s  q=%s", active_tools is not None, question[:60])
    t_start = time.time()
    answer, tools_used, delegations_used = await _agentic_loop(messages, active_tools)
    session["history"].append({"question": question, "answer": answer})
    _save_sessions()
    return {
        "status": "ok",
        "answer": answer,
        "question": question,
        "session_id": session_id,
        "kb_used": kb_id,
        "selected_doc": doc,
        "latency_ms": round((time.time() - t_start) * 1000),
        "tools_used": tools_used,
        "delegations_used": delegations_used,
        "llm_model": llm.LLM_MODEL,
    }


@app.post("/ingest")
def ingest(payload: dict):
    """Receive a document from the watcher and update the in-memory KB."""
    action = payload.get("action", "upsert")
    doc_id = payload.get("doc_id", "")
    kb_id = payload.get("kb_id", DEFAULT_KB)
    title = payload.get("title", "")
    text = payload.get("text", "")
    if kb_id not in ALL_KB:
        ALL_KB[kb_id] = []
    if action == "upsert":
        idx = next((i for i, d in enumerate(ALL_KB[kb_id]) if d.get("id") == doc_id), None)
        doc = {"id": doc_id, "title": title, "text": text}
        if idx is not None:
            ALL_KB[kb_id][idx] = doc
        else:
            ALL_KB[kb_id].append(doc)
        retriever.update_kb(kb_id, ALL_KB)
        return {"status": "ok", "action": "upsert", "kb_id": kb_id, "doc_id": doc_id}
    if action == "delete":
        ALL_KB[kb_id] = [d for d in ALL_KB[kb_id] if d.get("id") != doc_id]
        retriever.update_kb(kb_id, ALL_KB)
        return {"status": "ok", "action": "delete", "kb_id": kb_id, "doc_id": doc_id}
    return {"status": "error", "message": f"Azione non riconosciuta: {action}"}
