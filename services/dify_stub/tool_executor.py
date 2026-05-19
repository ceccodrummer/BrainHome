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

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("tool_executor")

AGENT_TOOLS_URL: str = os.getenv("AGENT_TOOLS_URL", "http://agent-tools:8001")
AGENT_TOOLS_TOKEN: str = os.getenv("AGENT_TOOLS_TOKEN", "")

# ──────────────────────────────────────────────────────────────────────────────
# Tool schemas (OpenAI function-calling format)
# ──────────────────────────────────────────────────────────────────────────────

TOOL_SCHEMAS: list[dict] = [
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

    async with httpx.AsyncClient(timeout=60.0) as client:

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
