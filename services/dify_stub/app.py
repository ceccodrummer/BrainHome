"""
Brain-Home AI Core Service  v2.0
=================================
Agentic LLM orchestration with native function calling.

Key features:
  - Swappable LLM provider via env vars (Ollama to OpenAI to Gemini in seconds)
  - True agentic loop: LLM autonomously calls tools until it has a final answer
  - BM25-lite RAG over local knowledge bases
  - Persistent multi-turn sessions (survive container restarts)
  - SSE streaming with real-time tool activity events

Environment variables: see llm_provider.py and tool_executor.py for the full list.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

import llm_provider as llm
from tool_executor import TOOL_SCHEMAS, execute as execute_tool

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
MAX_TOOL_ITERATIONS: int = int(os.getenv("MAX_TOOL_ITERATIONS", "8"))
HISTORY_TURNS: int = int(os.getenv("HISTORY_TURNS", "6"))
MAX_TOOL_RESULT_CHARS: int = int(os.getenv("MAX_TOOL_RESULT_CHARS", "1500"))

SYSTEM_PROMPT: str = os.getenv(
    "SYSTEM_PROMPT",
    (
        "Sei un Senior Software Architect e assistente IA del progetto Brain-Home. "
        "Rispondi sempre in italiano. "
        "Hai accesso a strumenti per leggere, scrivere, cercare e gestire file nel workspace "
        "dell'agente, eseguire script Python e fare commit Git. "
        "Usa gli strumenti quando la richiesta lo richiede. "
        "Quando non servono azioni, rispondi in modo preciso e conciso. "
        "Non inventare informazioni non presenti nel contesto fornito. "
        "Se il contesto non e sufficiente, dillo chiaramente."
    ),
)

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
    items = ALL_KB.get(kb_id) or ALL_KB.get(DEFAULT_KB) or []
    if not items:
        return None
    question_words = {w.strip(".,!?;:\"'()[]") for w in question.lower().split() if len(w) > 2}
    best_item, best_score = None, -1
    for item in items:
        text = (item.get("text", "") + " " + item.get("title", "")).lower()
        score = sum(1 for word in question_words if word in text)
        if score > best_score:
            best_score = score
            best_item = item
    return best_item or items[0]


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
# Message builder
# -------------------------------------------------------------------
def _build_messages(question: str, session: dict, doc: "Optional[dict]") -> "list[dict]":
    system_parts = [SYSTEM_PROMPT]
    if doc:
        kb_id = session.get("active_kb", DEFAULT_KB)
        system_parts.append(f"\n\nCONTESTO KNOWLEDGE BASE ({kb_id}):\n{doc.get('text', '')}")

    messages = [{"role": "system", "content": "\n".join(system_parts)}]
    for turn in session["history"][-HISTORY_TURNS:]:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})
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


# -------------------------------------------------------------------
# Agentic loop (non-streaming)
# -------------------------------------------------------------------
async def _agentic_loop(messages: list) -> "tuple[str, list[str]]":
    tools_used: list = []
    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response = await llm.chat(messages, TOOL_SCHEMAS)
        except Exception as exc:
            logger.warning("LLM error (iter %d): %s", iteration, exc)
            return (
                f"Errore LLM ({llm.LLM_BASE_URL}, modello: {llm.LLM_MODEL}): {exc}",
                tools_used,
            )

        if response.wants_tool:
            _append_tool_calls(messages, response)
            for tc in response.tool_calls:
                logger.info("Tool [%d]: %s(%s)", iteration, tc.name, tc.arguments)
                result = await execute_tool(tc.name, tc.arguments)
                tools_used.append(tc.name)
                _append_tool_result(messages, tc, result)
        else:
            return response.content or "(nessuna risposta)", tools_used

    return (
        f"Limite {MAX_TOOL_ITERATIONS} iterazioni raggiunto. "
        f"Strumenti usati: {', '.join(tools_used) or 'nessuno'}.",
        tools_used,
    )


# -------------------------------------------------------------------
# Agentic loop (streaming)
# -------------------------------------------------------------------
async def _agentic_loop_stream(messages: list):
    tools_used: list = []
    for iteration in range(MAX_TOOL_ITERATIONS):
        buffered_tokens: list = []
        final_response = None
        try:
            async for item in llm.chat_stream(messages, TOOL_SCHEMAS):
                if isinstance(item, str):
                    buffered_tokens.append(item)
                elif isinstance(item, llm.LLMResponse):
                    final_response = item
        except Exception as exc:
            logger.warning("LLM stream error (iter %d): %s", iteration, exc)
            yield {"type": "final", "content": f"Errore LLM: {exc}", "tools_used": tools_used}
            return

        if final_response is None or final_response.finish_reason == "error":
            yield {"type": "final", "content": "Errore durante la generazione.", "tools_used": tools_used}
            return

        if final_response.wants_tool:
            _append_tool_calls(messages, final_response)
            for tc in final_response.tool_calls:
                logger.info("Tool [stream/%d]: %s(%s)", iteration, tc.name, tc.arguments)
                yield {"type": "tool_start", "tool": tc.name, "args": tc.arguments}
                result = await execute_tool(tc.name, tc.arguments)
                tools_used.append(tc.name)
                preview = result[:700] + "\u2026" if len(result) > 700 else result
                yield {"type": "tool_result", "tool": tc.name, "result": preview}
                _append_tool_result(messages, tc, result)
        else:
            for token in buffered_tokens:
                yield {"type": "token", "text": token}
            final_text = final_response.content or "".join(buffered_tokens)
            yield {"type": "final", "content": final_text, "tools_used": tools_used}
            return

    yield {
        "type": "final",
        "content": f"Limite {MAX_TOOL_ITERATIONS} iterazioni raggiunto. Strumenti: {', '.join(tools_used) or 'nessuno'}.",
        "tools_used": tools_used,
    }


# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI(title="Brain-Home AI Core", version="2.0.0")


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "version": "2.0.0",
        "llm_base_url": llm.LLM_BASE_URL,
        "llm_model": llm.LLM_MODEL,
        "kb_ids": list(ALL_KB.keys()),
        "kb_items": {k: len(v) for k, v in ALL_KB.items()},
        "routing_rules": len(ROUTING_RULES),
        "max_tool_iterations": MAX_TOOL_ITERATIONS,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "llm_base_url": llm.LLM_BASE_URL,
        "llm_model": llm.LLM_MODEL,
        "kb_ids": list(ALL_KB.keys()),
        "total_kb_items": sum(len(v) for v in ALL_KB.values()),
        "active_sessions": len(SESSIONS),
        "max_tool_iterations": MAX_TOOL_ITERATIONS,
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
    messages = _build_messages(question, session, doc)
    t_start = time.time()

    async def _sse_gen():
        yield f"data: {json.dumps({'type': 'meta', 'kb_used': kb_id, 'session_id': session_id})}\n\n"
        final_answer = ""
        tools_used: list = []
        async for event in _agentic_loop_stream(messages):
            etype = event["type"]
            if etype in ("token", "tool_start", "tool_result"):
                yield f"data: {json.dumps(event)}\n\n"
            elif etype == "final":
                final_answer = event.get("content", "")
                tools_used = event.get("tools_used", [])
        session["history"].append({"question": question, "answer": final_answer})
        _save_sessions()
        done_payload = {
            "type": "done",
            "answer": final_answer,
            "latency_ms": round((time.time() - t_start) * 1000),
            "tools_used": tools_used,
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
    messages = _build_messages(question, session, doc)
    t_start = time.time()
    answer, tools_used = await _agentic_loop(messages)
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
        return {"status": "ok", "action": "upsert", "kb_id": kb_id, "doc_id": doc_id}
    if action == "delete":
        ALL_KB[kb_id] = [d for d in ALL_KB[kb_id] if d.get("id") != doc_id]
        return {"status": "ok", "action": "delete", "kb_id": kb_id, "doc_id": doc_id}
    return {"status": "error", "message": f"Azione non riconosciuta: {action}"}
