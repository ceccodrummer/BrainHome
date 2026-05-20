"""
Tool definitions and executor
==============================
All agent-tools are described here as OpenAI-compatible function schemas.
The LLM sees these schemas and autonomously decides which tools to call.

Each schema maps 1:1 to an agent-tools HTTP endpoint:
  write_file      → POST  /write
  read_file       → GET   /read
  append_file     → POST  /append
  delete_file     → DELETE /delete
  list_files      → GET   /list
  tree_workspace  → GET   /tree
  search_files    → POST  /search
  move_file       → POST  /move
  run_script      → POST  /run
  git_commit      → POST  /git-commit
"""

import asyncio
import json
import logging
import os
from typing import Any
import uuid

import httpx

logger = logging.getLogger("tool_executor")

AGENT_TOOLS_URL: str = os.getenv("AGENT_TOOLS_URL", "http://agent-tools:8001")
AGENT_TOOLS_TOKEN: str = os.getenv("AGENT_TOOLS_TOKEN", "")
AGENT_BROKER_URL: str = os.getenv("AGENT_BROKER_URL", "http://fastapi:80")
AGENT_BROKER_TOKEN: str = os.getenv("AGENT_BROKER_TOKEN", "")
AGENT_ID: str = os.getenv("AGENT_ID", "dify")
AGENT_MESSAGE_CONTRACT_VERSION: str = os.getenv("AGENT_MESSAGE_CONTRACT_VERSION", "1.0")
AGENT_MESSAGE_MAX_CHARS: int = int(os.getenv("AGENT_MESSAGE_MAX_CHARS", "4000"))
AGENT_MESSAGE_REASON_MAX_CHARS: int = int(os.getenv("AGENT_MESSAGE_REASON_MAX_CHARS", "200"))
AGENT_MESSAGE_CALL_DELAY: float = float(os.getenv("AGENT_MESSAGE_CALL_DELAY", "0.5"))

MESSAGE_AGENT_TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "message_agent",
        "description": (
            "Invia un messaggio strutturato a un altro agente specializzato. "
            "Usare quando un altro agente e piu pertinente o quando l'utente chiede esplicitamente di coinvolgerlo. "
            "Supporta le modalita ask, delegate e notify."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent_id": {
                    "type": "string",
                    "description": "ID dell'agente destinatario presente nel registry. Esempio: 'agent-3'.",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "Messaggio operativo da inviare al destinatario. "
                        f"Lunghezza massima: {AGENT_MESSAGE_MAX_CHARS} caratteri."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Motivazione breve della delega. "
                        f"Lunghezza massima: {AGENT_MESSAGE_REASON_MAX_CHARS} caratteri."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["ask", "delegate", "notify"],
                    "description": "ask = chiedi un contributo, delegate = assegna un sottotask, notify = notifica senza risposta.",
                    "default": "ask",
                },
                "await_response": {
                    "type": "boolean",
                    "description": "Se true attende la risposta del destinatario. Deve essere false per mode=notify.",
                    "default": True,
                },
                "protocol_version": {
                    "type": "string",
                    "description": "Versione del contratto agente->agente.",
                    "default": AGENT_MESSAGE_CONTRACT_VERSION,
                },
            },
            "required": ["to_agent_id", "message"],
        },
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# Tool schemas (OpenAI function-calling format)
# ──────────────────────────────────────────────────────────────────────────────

TOOL_SCHEMAS: list[dict] = [
    MESSAGE_AGENT_TOOL_SCHEMA,
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Crea o sovrascrive un file nel workspace dell'agente con il contenuto specificato. "
                "Usare per generare codice, note, script o qualsiasi file di testo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Percorso del file relativo al workspace. Esempio: 'prova.txt', 'note/todo.txt', 'src/main.py'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Il testo completo da scrivere nel file. Esempio: 'Ciao mondo!'. DEVE essere una stringa, non un oggetto.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Legge e restituisce il contenuto di un file nel workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Percorso del file relativo al workspace.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "Aggiunge testo alla fine di un file esistente nel workspace senza sovrascriverlo. "
                "Utile per aggiungere voci a un log, una lista o un file di note."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Percorso del file relativo al workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Testo da aggiungere alla fine del file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Elimina permanentemente un file dal workspace. "
                "Usare con cautela — l'operazione non è reversibile."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Percorso del file da eliminare, relativo al workspace.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Elenca i file e le cartelle presenti in una directory del workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Percorso della directory da elencare. Vuoto = radice del workspace.",
                        "default": "",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tree_workspace",
            "description": (
                "Restituisce l'albero ricorsivo di file e cartelle del workspace. "
                "Ideale per capire la struttura del progetto prima di agire."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_depth": {
                        "type": "integer",
                        "description": "Profondità massima dell'albero (default: 4).",
                        "default": 4,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Cerca un testo o pattern regex nei file del workspace. "
                "Restituisce nome file, numero riga e testo trovato."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Testo esatto o espressione regolare da cercare.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Sottocartella in cui limitare la ricerca. Vuoto = tutto il workspace.",
                        "default": "",
                    },
                    "is_regex": {
                        "type": "boolean",
                        "description": "Se true, interpreta il pattern come regex Python.",
                        "default": False,
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Sposta o rinomina un file all'interno del workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Percorso attuale del file, relativo al workspace.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Nuovo percorso del file, relativo al workspace.",
                    },
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": (
                "Esegue un file Python (.py) nel workspace in modo sandboxato con timeout. "
                "Restituisce stdout, stderr ed exit code. "
                "Usare per verificare il funzionamento di codice appena scritto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Percorso del file .py relativo al workspace.",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Argomenti da passare allo script (può essere vuoto).",
                        "default": [],
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout massimo in secondi (default: 10).",
                        "default": 10,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": (
                "Esegue git add e git commit nel workspace dell'agente. "
                "Usare dopo aver scritto o modificato file per salvare le modifiche con un messaggio descrittivo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Messaggio del commit (chiaro e descrittivo).",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File specifici da includere nel commit. Se vuoto, esegue git add -A.",
                        "default": [],
                    },
                },
                "required": ["message"],
            },
        },
    },
]

# ──────────────────────────────────────────────────────────────────────────────
# Executor
# ──────────────────────────────────────────────────────────────────────────────

def _auth_headers() -> dict:
    if AGENT_TOOLS_TOKEN:
        return {"Authorization": f"Bearer {AGENT_TOOLS_TOKEN}"}
    return {}


def _broker_headers() -> dict:
    headers = {"X-Agent-Id": AGENT_ID}
    if AGENT_BROKER_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_BROKER_TOKEN}"
    return headers


async def execute(tool_name: str, args: dict[str, Any]) -> str:
    """
    Dispatch a tool call to the agent-tools service.
    Returns a human-readable result string — this is what the LLM sees as the
    tool result and uses to formulate its next response.
    """
    try:
        return await _dispatch(tool_name, args)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400]
        logger.warning("Tool %s → HTTP %s: %s", tool_name, exc.response.status_code, body)
        return f"Errore HTTP {exc.response.status_code} da {tool_name}: {body}"
    except Exception as exc:
        logger.warning("Tool %s error: %s", tool_name, exc)
        return f"Errore durante l'esecuzione di {tool_name}: {exc}"


async def _dispatch(tool_name: str, args: dict[str, Any]) -> str:
    headers = _auth_headers()
    broker_headers = _broker_headers()

    async with httpx.AsyncClient(timeout=60.0) as client:

        # ── message_agent ───────────────────────────────────────────────────
        if tool_name == "message_agent":
            conversation_id = str(args.get("conversation_id") or uuid.uuid4())
            trace_id = str(args.get("trace_id") or uuid.uuid4())
            visited_agents = args.get("visited_agents") or [AGENT_ID]
            if AGENT_ID not in visited_agents:
                visited_agents = [AGENT_ID] + list(visited_agents)
            envelope = {
                "from_agent_id": AGENT_ID,
                "to_agent_id": args["to_agent_id"],
                "message": args["message"],
                "reason": args.get("reason", ""),
                "mode": args.get("mode", "ask"),
                "await_response": args.get("await_response", True),
                "protocol_version": args.get("protocol_version", AGENT_MESSAGE_CONTRACT_VERSION),
                "trace_id": trace_id,
                "conversation_id": conversation_id,
                "hop_count": int(args.get("hop_count", 1)),
                "max_hops": int(args.get("max_hops", 3)),
                "visited_agents": visited_agents,
            }
            if AGENT_MESSAGE_CALL_DELAY > 0:
                logger.info("Delaying agent-agent delegation by %.2fs", AGENT_MESSAGE_CALL_DELAY)
                await asyncio.sleep(AGENT_MESSAGE_CALL_DELAY)
            resp = await client.post(
                f"{AGENT_BROKER_URL}/internal/agent-message",
                json=envelope,
                headers=broker_headers,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "error":
                err = data.get("error") or {}
                return (
                    f"Delega fallita verso `{data.get('target_agent_id', args.get('to_agent_id', '?'))}`: "
                    f"{err.get('code', 'unknown')} - {err.get('message', 'errore sconosciuto')}"
                )
            answer = str(data.get("answer", "")).strip()
            if not answer:
                return (
                    f"Messaggio inviato a `{data.get('target_agent_id', args.get('to_agent_id', '?'))}` "
                    f"(mode={data.get('mode', args.get('mode', 'ask'))}, latency={data.get('latency_ms', 0)} ms)."
                )
            return (
                f"Risposta da `{data.get('target_agent_id', args.get('to_agent_id', '?'))}` "
                f"(mode={data.get('mode', args.get('mode', 'ask'))}, latency={data.get('latency_ms', 0)} ms):\n"
                f"{answer}"
            )

        # ── write_file ──────────────────────────────────────────────────────
        if tool_name == "write_file":
            content = args["content"]
            # Sanitize: model sometimes passes schema descriptor dict instead of string
            if isinstance(content, dict):
                content = content.get("description") or json.dumps(content, ensure_ascii=False)
            elif not isinstance(content, str):
                content = str(content)
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/write",
                json={"path": args["path"], "content": content},
                headers=headers,
            )
            resp.raise_for_status()
            d = resp.json()
            return f"File scritto: `{d['path']}` ({d['bytes_written']} byte)"

        # ── read_file ───────────────────────────────────────────────────────
        elif tool_name == "read_file":
            resp = await client.get(
                f"{AGENT_TOOLS_URL}/read",
                params={"path": args["path"]},
                headers=headers,
            )
            resp.raise_for_status()
            content = resp.text
            lines = content.splitlines()
            if len(lines) > 100:
                return (
                    f"[File: {args['path']} — {len(lines)} righe totali, prime 100 mostrate]\n"
                    + "\n".join(lines[:100])
                )
            return content

        # ── append_file ─────────────────────────────────────────────────────
        elif tool_name == "append_file":
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/append",
                json={"path": args["path"], "content": args["content"]},
                headers=headers,
            )
            resp.raise_for_status()
            d = resp.json()
            return f"Aggiunto a `{d['path']}`: {d['bytes_appended']} byte (totale file: {d['total_bytes']} byte)"

        # ── delete_file ─────────────────────────────────────────────────────
        elif tool_name == "delete_file":
            resp = await client.request(
                "DELETE",
                f"{AGENT_TOOLS_URL}/delete",
                json={"path": args["path"]},
                headers=headers,
            )
            resp.raise_for_status()
            d = resp.json()
            return f"File eliminato: `{d['path']}`"

        # ── list_files ──────────────────────────────────────────────────────
        elif tool_name == "list_files":
            params = {}
            if args.get("path"):
                params["path"] = args["path"]
            resp = await client.get(
                f"{AGENT_TOOLS_URL}/list", params=params, headers=headers
            )
            resp.raise_for_status()
            entries = resp.json().get("entries", [])
            if not entries:
                return f"Directory `{args.get('path') or '/'}` vuota."
            lines = []
            for e in entries:
                icon = "[DIR] " if e["type"] == "dir" else "[FILE]"
                size = f"  {e['size']} B" if e.get("size") is not None else ""
                lines.append(f"{icon} {e['path']}{size}")
            return "\n".join(lines)

        # ── tree_workspace ──────────────────────────────────────────────────
        elif tool_name == "tree_workspace":
            resp = await client.get(
                f"{AGENT_TOOLS_URL}/tree",
                params={"max_depth": args.get("max_depth", 4)},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json().get("tree_text", "(workspace vuoto)")

        # ── search_files ────────────────────────────────────────────────────
        elif tool_name == "search_files":
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/search",
                json={
                    "pattern": args["pattern"],
                    "path": args.get("path", ""),
                    "is_regex": args.get("is_regex", False),
                    "max_results": 50,
                },
                headers=headers,
            )
            resp.raise_for_status()
            d = resp.json()
            results = d.get("results", [])
            if not results:
                return (
                    f"Nessuna occorrenza trovata per `{args['pattern']}` "
                    f"({d.get('files_searched', 0)} file analizzati)."
                )
            lines = [f"{len(results)} occorrenze per `{args['pattern']}`:"]
            for r in results:
                lines.append(f"  {r['file']}:{r['line']}: {r['text'][:120]}")
            return "\n".join(lines)

        # ── move_file ───────────────────────────────────────────────────────
        elif tool_name == "move_file":
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/move",
                json={"source": args["source"], "destination": args["destination"]},
                headers=headers,
            )
            resp.raise_for_status()
            d = resp.json()
            return f"Spostato: `{d['source']}` → `{d['destination']}`"

        # ── run_script ──────────────────────────────────────────────────────
        elif tool_name == "run_script":
            timeout_sec = int(args.get("timeout", 10))
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/run",
                json={
                    "path": args["path"],
                    "args": args.get("args", []),
                    "timeout": timeout_sec,
                },
                headers=headers,
                timeout=float(timeout_sec) + 15,
            )
            resp.raise_for_status()
            d = resp.json()
            parts = [f"Script `{args['path']}` — exit code: {d['exit_code']}"]
            if d.get("stdout", "").strip():
                parts.append(f"STDOUT:\n{d['stdout'][:2000]}")
            if d.get("stderr", "").strip():
                parts.append(f"STDERR:\n{d['stderr'][:500]}")
            return "\n".join(parts)

        # ── git_commit ──────────────────────────────────────────────────────
        elif tool_name == "git_commit":
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/git-commit",
                json={
                    "message": args["message"],
                    "paths": args.get("paths", []),
                },
                headers=headers,
            )
            resp.raise_for_status()
            d = resp.json()
            out = d.get("output", "").strip()
            return f"Commit eseguito: {args['message']}\n{out}" if out else f"Commit eseguito: {args['message']}"

    return f"Strumento sconosciuto: {tool_name}"
