import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = Path(os.getenv("KB_DIR", "/app/data"))
KB_CONFIG_PATH = KB_DIR / "kb_config.json"
KB_PATH = Path(os.getenv("KB_PATH", "/app/data/kb.json"))
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "Sei un Senior Software Architect e assistente IA del progetto Brain-Home. "
        "Rispondi sempre in italiano. "
        "Usa il contesto fornito per rispondere in modo preciso e conciso. "
        "Se il contesto non contiene informazioni sufficienti, dillo chiaramente. "
        "Non inventare informazioni non presenti nel contesto."
    ),
)
LITELLM_URL = os.getenv("LITELLM_URL", "http://host.docker.internal:11434")
LITELLM_MODEL = os.getenv("LITELLM_MODEL", "llama2")

# Agent-Tools integration
AGENT_TOOLS_URL = os.getenv("AGENT_TOOLS_URL", "http://agent-tools:8001")
AGENT_TOOLS_TOKEN = os.getenv("AGENT_TOOLS_TOKEN", "")

# --------------------------------------------------------------------------- #
# Knowledge Base loading
# --------------------------------------------------------------------------- #

def _load_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _load_all_kb() -> dict[str, list]:
    """Load all KB files referenced in kb_config.json, plus the default kb.json."""
    kb_map: dict[str, list] = {}

    # Always load default KB
    default_items = _load_json(KB_PATH)
    kb_map["kb_sistema"] = default_items

    # Load additional KBs from config
    config = _load_json(KB_CONFIG_PATH)
    if isinstance(config, dict):
        for kb_def in config.get("knowledge_bases", []):
            kb_id = kb_def.get("id")
            kb_file = KB_DIR / kb_def.get("file", "")
            if kb_id and kb_file.exists():
                kb_map[kb_id] = _load_json(kb_file)

    return kb_map


def _load_routing_rules() -> tuple[list, str]:
    config = _load_json(KB_CONFIG_PATH)
    if isinstance(config, dict):
        return config.get("routing_rules", []), config.get("default_kb", "kb_sistema")
    return [], "kb_sistema"


ALL_KB: dict[str, list] = _load_all_kb()
ROUTING_RULES, DEFAULT_KB = _load_routing_rules()

# Session store — persisted to disk so memory survives container restarts
SESSIONS_FILE = Path(os.getenv("SESSIONS_FILE", "/app/data/sessions.json"))

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

SESSIONS: dict[str, dict] = _load_sessions()

# --------------------------------------------------------------------------- #
# Routing logic  (2.5.5)
# --------------------------------------------------------------------------- #

def _route_question(question: str) -> str:
    """Return the KB id that best matches the question keywords."""
    question_lower = question.lower()
    best_kb = DEFAULT_KB
    best_score = 0
    for rule in ROUTING_RULES:
        score = sum(1 for kw in rule.get("keywords", []) if kw in question_lower)
        if score > best_score:
            best_score = score
            best_kb = rule.get("target_kb", DEFAULT_KB)
    return best_kb


# --------------------------------------------------------------------------- #
# Retrieval  (2.5.6)
# --------------------------------------------------------------------------- #

def _retrieve_best_document(question: str, kb_id: str) -> Optional[dict]:
    """BM25-lite: word-overlap retrieval within selected KB."""
    items = ALL_KB.get(kb_id) or ALL_KB.get(DEFAULT_KB) or []
    if not items:
        return None

    question_words = set(w.strip(".,!?;:\"'()[]") for w in question.lower().split() if len(w) > 2)
    best_item = None
    best_score = -1

    for item in items:
        text = (item.get("text", "") + " " + item.get("title", "")).lower()
        score = sum(1 for word in question_words if word in text)
        if score > best_score:
            best_score = score
            best_item = item

    return best_item or items[0]


# --------------------------------------------------------------------------- #
# Session management  (2.5.4)
# --------------------------------------------------------------------------- #

def _get_or_create_session(session_id: Optional[str]) -> tuple[str, dict]:
    if not session_id or session_id not in SESSIONS:
        session_id = session_id or str(uuid.uuid4())
        SESSIONS[session_id] = {"history": [], "active_kb": DEFAULT_KB, "created_at": time.time()}
        _save_sessions()
    return session_id, SESSIONS[session_id]


# --------------------------------------------------------------------------- #
# Tool dispatch  (agentic actions: write/read file, git commit)
# --------------------------------------------------------------------------- #

def _detect_tool_intent(message: str) -> Optional[dict]:
    """
    Detect if the user wants to execute a tool action rather than ask a question.
    Returns {tool, ...args} or None.

    Supported tools:
      write      - crea/scrivi un file
      append     - aggiungi/appendi a un file
      read       - leggi/mostra un file
      list       - elenca i file (in una directory)
      delete     - cancella/elimina un file
      git-commit - fai un commit
    """
    msg = message.strip()

    # --- APPEND (before write to avoid overlap) ---
    append_kw = re.search(
        r'(?:aggiungi|appendi|aggiunge|inserisci)\s+.*?'
        r'(?:al\s+file|a(?:l\s+file|\s+))\s+(\S+)',
        msg, re.IGNORECASE,
    )
    if append_kw:
        path = append_kw.group(1).rstrip(",:").strip()
        code_block = re.search(r'```(?:\w+)?\n(.*?)```', msg, re.DOTALL)
        if code_block:
            content = code_block.group(1)
        else:
            # Content follows the path, after ":" or "="
            # Note: \S+ may have consumed the ":" in "file.txt: content"
            after = msg[append_kw.end():]
            content_match = re.search(r'[:=]\s*(.+)', after, re.DOTALL)
            if content_match:
                content = content_match.group(1).strip()
            else:
                # colon was consumed by \S+, remaining text IS the content
                content = after.strip()
        return {"tool": "append", "path": path, "content": content}

    # --- WRITE / CREATE FILE ---
    write_kw = re.search(
        r'(?:crea|scrivi|genera|salva)\s+'
        r'(?:un\s+|il\s+|nuovo\s+)?(?:file|documento|script)\s+(?:chiamato\s+|denominato\s+)?(\S+)',
        msg, re.IGNORECASE,
    )
    if write_kw:
        path = write_kw.group(1).rstrip(",:").strip()
        code_block = re.search(r'```(?:\w+)?\n(.*?)```', msg, re.DOTALL)
        if code_block:
            content = code_block.group(1)
        else:
            after = msg[write_kw.end():]
            content_match = re.search(
                r'(?:con(?:tenuto)?|content)?[:=]\s*(.+)', after, re.DOTALL | re.IGNORECASE
            )
            if content_match:
                content = content_match.group(1).strip()
            else:
                # colon may have been consumed by \S+
                content = after.strip()
        return {"tool": "write", "path": path, "content": content}

    # --- DELETE FILE ---
    delete_kw = re.search(
        r'(?:cancella|elimina|rimuovi|delete)\s+'
        r'(?:il\s+)?(?:file\s+)?(\S+)',
        msg, re.IGNORECASE,
    )
    if delete_kw and any(kw in msg.lower() for kw in ["cancella", "elimina", "rimuovi", "delete"]):
        path = delete_kw.group(1).rstrip(",:").strip()
        return {"tool": "delete", "path": path}

    # --- LIST FILES ---
    list_kw = re.search(
        r'(?:elenca|lista|mostra|visualizza|quali sono)\s+'
        r'(?:tutti\s+)?(?:i\s+)?(?:file|files|documenti|contenuti?)\s*'
        r'(?:(?:in|nella(?:\s+cartella)?|nella(?:\s+directory)?|di)\s+(?:cartella\s+|directory\s+)?(\S+))?',
        msg, re.IGNORECASE,
    )
    if list_kw and any(kw in msg.lower() for kw in ["elenca", "lista file", "mostra file", "quali sono i file", "contenuto della cartella", "cartella"]):
        path = list_kw.group(1).rstrip(",:").strip() if list_kw.group(1) else ""
        return {"tool": "list", "path": path}

    # --- TREE ---
    if re.search(r'\b(?:albero|tree|struttura)\b.*?(?:file|cartelle?|workspace|directory)', msg, re.IGNORECASE) or \
       re.search(r'(?:mostra|visualizza|dammi)\s+(?:la\s+)?struttura', msg, re.IGNORECASE):
        return {"tool": "tree", "path": ""}

    # --- SEARCH ---
    search_kw = re.search(
        r'(?:cerca|trovami?|grep|ricerca|trova)\s+'
        r'(?:la\s+parola\s+|il\s+testo\s+|la\s+stringa\s+)?["\']?([^"\']+?)["\']?\s*'
        r'(?:(?:nei|in)\s+(?:tutti\s+)?(?:i\s+)?file(?:\s+(?:di|nella\s+cartella)\s+(\S+))?)?$',
        msg, re.IGNORECASE,
    )
    if search_kw and any(kw in msg.lower() for kw in ["cerca", "trovami", "grep", "ricerca", "trova"]):
        pattern = search_kw.group(1).strip().strip('"\'')
        search_path = search_kw.group(2).rstrip(",:").strip() if search_kw.group(2) else ""
        return {"tool": "search", "pattern": pattern, "path": search_path}

    # --- MOVE / RENAME ---
    move_kw = re.search(
        r'(?:sposta|rinomina|muovi)\s+(?:il\s+file\s+)?(\S+)\s+(?:in|a|come|verso)\s+(\S+)',
        msg, re.IGNORECASE,
    )
    if move_kw:
        return {"tool": "move", "source": move_kw.group(1).rstrip(",:"), "destination": move_kw.group(2).rstrip(",:")}

    # --- RUN SCRIPT ---
    run_kw = re.search(
        r'(?:esegui|avvia|lancia|run|eseguire)\s+(?:lo\s+script\s+|il\s+file\s+)?(\S+\.py)',
        msg, re.IGNORECASE,
    )
    if run_kw:
        return {"tool": "run", "path": run_kw.group(1).rstrip(",:")}

    # --- READ FILE ---
    read_kw = re.search(
        r'(?:leggi|mostra|visualizza|apri|dimmi\s+il\s+contenuto\s+di)\s+'
        r'(?:il\s+)?(?:file\s+)?(\S+)',
        msg, re.IGNORECASE,
    )
    if read_kw and any(kw in msg.lower() for kw in ["leggi", "mostra il file", "visualizza il file", "apri il file", "contenuto di"]):
        path = read_kw.group(1).rstrip(",:").strip()
        return {"tool": "read", "path": path}

    # --- GIT COMMIT ---
    if re.search(r'\bcommit\b', msg, re.IGNORECASE):
        commit_msg_match = re.search(
            r'(?:messaggio|message|msg|nota)[:=]?\s*["\']?([^"\'\n]+)["\']?$',
            msg, re.IGNORECASE,
        )
        commit_message = commit_msg_match.group(1).strip() if commit_msg_match else "Auto-commit da Brain-Home Agent"
        return {"tool": "git-commit", "message": commit_message}

    return None


def _split_multi_intent(message: str) -> list[str]:
    """
    Split a compound message into individual tool sub-intents.
    e.g. "crea X, poi aggiungi a Y e fai commit" → 3 parts
    """
    connector = re.compile(
        r'\s*[;,]\s*(?:poi\s+|quindi\s+|dopodiché\s+|infine\s+|e\s+poi\s+|e\s+infine\s+)?'
        r'|\s+(?:poi|quindi|dopodiché|infine|e\s+poi|e\s+infine)\s+',
        re.IGNORECASE,
    )
    parts = connector.split(message)
    return [p.strip() for p in parts if len(p.strip()) > 8]


async def _execute_tool(intent: dict) -> str:
    """Execute the detected tool via agent-tools service and return a readable response."""
    tool = intent["tool"]
    headers = {}
    if AGENT_TOOLS_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_TOOLS_TOKEN}"

    async with httpx.AsyncClient() as client:

        if tool == "write":
            path = intent["path"]
            content = intent.get("content", "")
            if not content:
                return (
                    f"Ho capito che vuoi creare il file `{path}`, "
                    f"ma non hai specificato il contenuto.\n"
                    f"Esempio: *crea un file {path} con contenuto: ciao mondo*"
                )
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/write",
                json={"path": path, "content": content},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return f"File creato: `{data['path']}` ({data['bytes_written']} byte scritti)"

        elif tool == "append":
            path = intent["path"]
            content = intent.get("content", "")
            if not content:
                return (
                    f"Ho capito che vuoi aggiungere testo a `{path}`, "
                    f"ma non hai specificato cosa aggiungere.\n"
                    f"Esempio: *aggiungi al file {path}: nuova riga*"
                )
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/append",
                json={"path": path, "content": content},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return f"Aggiunto a `{data['path']}`: {data['bytes_appended']} byte (totale: {data['total_bytes']} byte)"

        elif tool == "read":
            path = intent["path"]
            resp = await client.get(
                f"{AGENT_TOOLS_URL}/read",
                params={"path": path},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            content = resp.text
            lines = content.splitlines()
            if len(lines) > 60:
                preview = "\n".join(lines[:60])
                return f"Contenuto di `{path}` (prime 60 righe su {len(lines)}):\n```\n{preview}\n```"
            return f"Contenuto di `{path}`:\n```\n{content}\n```"

        elif tool == "list":
            path = intent.get("path", "")
            resp = await client.get(
                f"{AGENT_TOOLS_URL}/list",
                params={"path": path} if path else {},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("entries", [])
            if not entries:
                return f"La cartella `{path or '/'}` è vuota."
            lines = [f"Contenuto di `{path or '/'}`:", ""]
            for e in entries:
                icon = "📁" if e["type"] == "dir" else "📄"
                size = f"  ({e['size']} B)" if e.get("size") is not None else ""
                lines.append(f"{icon} `{e['path']}`{size}")
            return "\n".join(lines)

        elif tool == "tree":
            path = intent.get("path", "")
            resp = await client.get(
                f"{AGENT_TOOLS_URL}/tree",
                params={"path": path, "max_depth": intent.get("max_depth", 4)},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return f"Struttura workspace:\n```\n{data['tree_text']}\n```"

        elif tool == "search":
            pattern = intent["pattern"]
            search_path = intent.get("path", "")
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/search",
                json={"pattern": pattern, "path": search_path, "is_regex": intent.get("is_regex", False)},
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return f"Nessuna occorrenza trovata per `{pattern}` ({data.get('files_searched', 0)} file analizzati)."
            lines = [f"Trovate {len(results)} occorrenze per `{pattern}`:", ""]
            for r in results:
                lines.append(f"**{r['file']}** riga {r['line']}: `{r['text'][:120]}`")
            return "\n".join(lines)

        elif tool == "move":
            source = intent["source"]
            dest = intent["destination"]
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/move",
                json={"source": source, "destination": dest},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return f"Spostato: `{data['source']}` → `{data['destination']}`"

        elif tool == "run":
            path = intent["path"]
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/run",
                json={"path": path, "args": intent.get("args", []), "timeout": intent.get("timeout", 10)},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            out = data.get("stdout", "").strip()
            err = data.get("stderr", "").strip()
            lines = [f"Eseguito `{path}` (exit code: {data['exit_code']})"]
            if out:
                lines.append(f"```\n{out[:1000]}\n```")
            if err:
                lines.append(f"⚠️ stderr:\n```\n{err[:500]}\n```")
            return "\n".join(lines)

        elif tool == "delete":
            path = intent["path"]
            resp = await client.request(
                "DELETE",
                f"{AGENT_TOOLS_URL}/delete",
                json={"path": path},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return f"File eliminato: `{data['path']}`"

        elif tool == "git-commit":
            message = intent.get("message", "Auto-commit da Brain-Home Agent")
            resp = await client.post(
                f"{AGENT_TOOLS_URL}/git-commit",
                json={"message": message},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            output = data.get("output", "")
            return f"Commit eseguito — {message}\n```\n{output}\n```"

    return "Strumento non riconosciuto."


async def _get_workspace_tree() -> str:
    """Fetch a compact workspace tree to inject into the LLM context."""
    try:
        headers = {"Authorization": f"Bearer {AGENT_TOOLS_TOKEN}"} if AGENT_TOOLS_TOKEN else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{AGENT_TOOLS_URL}/tree",
                params={"max_depth": 3},
                headers=headers,
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("tree_text", "")
    except Exception:
        pass
    return ""


def _clean_llm_answer(text: str) -> str:
    """
    Strip any prompt echo from the model output.
    Models using /v1/completions sometimes continue generating the prompt
    structure (CONTESTO, Domanda, Risposta) after the actual answer.
    We keep only what precedes the first such echo marker.
    """
    import re
    # Markers that indicate the model has started repeating the prompt
    echo_pattern = re.compile(
        r'\n(?:CONTESTO|Domanda|STORICO CONVERSAZIONE|Sistema|SISTEMA)',
        re.IGNORECASE,
    )
    match = echo_pattern.search(text)
    if match:
        text = text[:match.start()]
    return text.strip()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
def read_root():
    return {
        "status": "dify-stub",
        "message": "Dify placeholder service is running.",
        "kb_ids": list(ALL_KB.keys()),
        "kb_items": {k: len(v) for k, v in ALL_KB.items()},
        "routing_rules": len(ROUTING_RULES),
        "system_prompt_preview": SYSTEM_PROMPT[:80] + "...",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "kb_ids": list(ALL_KB.keys()),
        "total_kb_items": sum(len(v) for v in ALL_KB.values()),
        "active_sessions": len(SESSIONS),
        "model": LITELLM_MODEL,
    }


@app.post("/query/stream")
async def query_stream(payload: dict):
    """Streaming version of /query — emits SSE tokens as the model generates them."""
    question = payload.get("question") or payload.get("query") or payload.get("text") or str(payload)
    session_id_in = payload.get("session_id")
    session_id, session = _get_or_create_session(session_id_in)

    # Tool dispatch (multi-intent + single) — no streaming needed for tools
    sub_parts = _split_multi_intent(question)
    intents = [(p, _detect_tool_intent(p)) for p in sub_parts]
    valid_intents = [(p, i) for p, i in intents if i is not None]

    if len(valid_intents) > 1:
        t_multi = time.time()
        combined_parts = []
        used_tools = []
        for part, intent in valid_intents:
            try:
                part_response = await _execute_tool(intent)
            except Exception as exc:
                part_response = f"Errore su `{intent['tool']}`: {exc}"
            combined_parts.append(f"**{intent['tool'].upper()}**: {part_response}")
            used_tools.append(intent["tool"])
        tool_response = "\n\n".join(combined_parts)
        session["history"].append({"question": question, "answer": tool_response})
        _save_sessions()
        latency = round((time.time() - t_multi) * 1000)

        async def _multi_gen():
            yield f"data: {json.dumps({'type': 'meta', 'kb_used': 'agent-tools', 'session_id': session_id})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'answer': tool_response, 'latency_ms': latency, 'ollama_available': True, 'tools_used': used_tools})}\n\n"

        return StreamingResponse(_multi_gen(), media_type="text/event-stream")

    tool_intent = _detect_tool_intent(question)
    if tool_intent:
        t_tool = time.time()
        try:
            tool_response = await _execute_tool(tool_intent)
        except Exception as exc:
            tool_response = f"Errore nell'esecuzione dello strumento `{tool_intent['tool']}`: {exc}"
        session["history"].append({"question": question, "answer": tool_response})
        _save_sessions()
        latency = round((time.time() - t_tool) * 1000)

        async def _tool_gen():
            yield f"data: {json.dumps({'type': 'meta', 'kb_used': 'agent-tools', 'session_id': session_id})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'answer': tool_response, 'latency_ms': latency, 'ollama_available': True, 'tool_used': tool_intent['tool']})}\n\n"

        return StreamingResponse(_tool_gen(), media_type="text/event-stream")

    # RAG
    kb_id = _route_question(question)
    session["active_kb"] = kb_id
    doc = _retrieve_best_document(question, kb_id)
    context_text = doc.get("text") if doc else "Nessuna informazione trovata nella knowledge base."

    # Workspace context injection
    workspace_ctx = await _get_workspace_tree()

    history_lines = []
    for turn in session["history"][-3:]:
        history_lines.append(f"Utente: {turn['question']}")
        history_lines.append(f"Assistente: {turn['answer']}")
    history_ctx = "\n".join(history_lines)

    prompt_parts = [SYSTEM_PROMPT]
    if history_ctx:
        prompt_parts.append(f"\nSTORICO CONVERSAZIONE:\n{history_ctx}")
    prompt_parts.append(f"\nCONTESTO (da {kb_id}):\n{context_text}")
    if workspace_ctx:
        prompt_parts.append(f"\nFILE NEL WORKSPACE AGENT:\n```\n{workspace_ctx}\n```")
    prompt_parts.append(f"\nDomanda: {question}\nRisposta:")
    prompt = "\n".join(prompt_parts)

    # Echo-detection pattern (same as _clean_llm_answer)
    _echo_re = re.compile(
        r'\n(?:CONTESTO|Domanda|STORICO CONVERSAZIONE|Sistema|SISTEMA)',
        re.IGNORECASE,
    )

    t_start = time.time()

    async def _stream_gen():
        yield f"data: {json.dumps({'type': 'meta', 'kb_used': kb_id, 'session_id': session_id, 'selected_doc': doc})}\n\n"

        full_text = ""
        ollama_available = True
        ollama_error = None

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                async with client.stream(
                    "POST",
                    f"{LITELLM_URL}/v1/completions",
                    json={
                        "model": LITELLM_MODEL,
                        "prompt": prompt,
                        "max_tokens": 512,
                        "temperature": 0.2,
                        "stream": True,
                    },
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
                        token = (chunk.get("choices") or [{}])[0].get("text", "")
                        if not token:
                            continue
                        full_text += token
                        # Stop streaming as soon as echo marker appears
                        m = _echo_re.search(full_text)
                        if m:
                            full_text = full_text[:m.start()]
                            break
                        yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"
        except Exception as exc:
            ollama_available = False
            ollama_error = str(exc)

        if ollama_available:
            answer = _clean_llm_answer(full_text)
        else:
            answer = "[Ollama non disponibile — il modello linguistico non è raggiungibile. Il contesto recuperato dalla knowledge base è visibile nei dettagli.]"

        session["history"].append({"question": question, "answer": answer})
        _save_sessions()

        done_evt: dict = {
            "type": "done",
            "answer": answer,
            "latency_ms": round((time.time() - t_start) * 1000),
            "ollama_available": ollama_available,
        }
        if ollama_error:
            done_evt["ollama_error"] = ollama_error
        yield f"data: {json.dumps(done_evt)}\n\n"

    return StreamingResponse(_stream_gen(), media_type="text/event-stream")


@app.post("/query")
async def query(payload: dict):
    question = payload.get("question") or payload.get("query") or payload.get("text") or str(payload)
    session_id = payload.get("session_id")

    # Session management
    session_id, session = _get_or_create_session(session_id)

    # ---- Multi-intent & tool dispatch ----
    # Try splitting compound commands first ("crea X, poi fai commit")
    sub_parts = _split_multi_intent(question)
    intents = [(p, _detect_tool_intent(p)) for p in sub_parts]
    valid_intents = [(p, i) for p, i in intents if i is not None]

    if len(valid_intents) > 1:
        # Multiple tools detected — execute sequentially
        t_multi = time.time()
        combined_parts = []
        used_tools = []
        for part, intent in valid_intents:
            try:
                part_response = await _execute_tool(intent)
            except Exception as exc:
                part_response = f"Errore su `{intent['tool']}`: {exc}"
            combined_parts.append(f"**{intent['tool'].upper()}**: {part_response}")
            used_tools.append(intent["tool"])
        tool_response = "\n\n".join(combined_parts)
        session["history"].append({"question": question, "answer": tool_response})
        _save_sessions()
        return {
            "status": "ok",
            "answer": tool_response,
            "question": question,
            "session_id": session_id,
            "kb_used": "agent-tools",
            "latency_ms": round((time.time() - t_multi) * 1000),
            "ollama_available": True,
            "tools_used": used_tools,
        }

    # Single tool intent
    tool_intent = _detect_tool_intent(question)
    if tool_intent:
        t_tool = time.time()
        try:
            tool_response = await _execute_tool(tool_intent)
        except Exception as exc:
            tool_response = f"Errore nell'esecuzione dello strumento `{tool_intent['tool']}`: {exc}"
        session["history"].append({"question": question, "answer": tool_response})
        _save_sessions()
        return {
            "status": "ok",
            "answer": tool_response,
            "question": question,
            "session_id": session_id,
            "kb_used": "agent-tools",
            "latency_ms": round((time.time() - t_tool) * 1000),
            "ollama_available": True,
            "tool_used": tool_intent["tool"],
        }
    # ----------------------------------------------------------

    # KB routing
    kb_id = _route_question(question)
    session["active_kb"] = kb_id

    # Retrieval
    doc = _retrieve_best_document(question, kb_id)
    context_text = doc.get("text") if doc else "Nessuna informazione trovata nella knowledge base."

    # Workspace context: inject file tree into non-tool queries so the LLM knows what files exist
    workspace_ctx = await _get_workspace_tree()

    # Build history context (last 3 turns)
    history_lines = []
    for turn in session["history"][-3:]:
        history_lines.append(f"Utente: {turn['question']}")
        history_lines.append(f"Assistente: {turn['answer']}")
    history_ctx = "\n".join(history_lines)

    # Build prompt
    prompt_parts = [SYSTEM_PROMPT]
    if history_ctx:
        prompt_parts.append(f"\nSTORICO CONVERSAZIONE:\n{history_ctx}")
    prompt_parts.append(f"\nCONTESTO (da {kb_id}):\n{context_text}")
    if workspace_ctx:
        prompt_parts.append(f"\nFILE NEL WORKSPACE AGENT:\n```\n{workspace_ctx}\n```")
    prompt_parts.append(f"\nDomanda: {question}\nRisposta:")
    prompt = "\n".join(prompt_parts)

    # LLM call
    answer = None
    ollama_error = None
    t_start = time.time()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{LITELLM_URL}/v1/completions",
                json={
                    "model": LITELLM_MODEL,
                    "prompt": prompt,
                    "max_tokens": 512,
                    "temperature": 0.2,
                },
                timeout=90,
            )
            response.raise_for_status()
            data = response.json()
            answer = data.get("choices", [])[0].get("text") if data.get("choices") else data.get("text")
    except Exception as exc:
        ollama_error = str(exc)

    latency_ms = round((time.time() - t_start) * 1000)

    if answer is None:
        answer = "[Ollama non disponibile — il modello linguistico non è raggiungibile. Il contesto recuperato dalla knowledge base è visibile nei dettagli.]"
        ollama_available = False
    else:
        answer = _clean_llm_answer(answer)
        ollama_available = True

    # Update session history and persist to disk
    session["history"].append({"question": question, "answer": answer})
    _save_sessions()

    return {
        "status": "ok",
        "answer": answer,
        "question": question,
        "session_id": session_id,
        "kb_used": kb_id,
        "selected_doc": doc,
        "latency_ms": latency_ms,
        "ollama_available": ollama_available,
        **({"ollama_error": ollama_error} if ollama_error else {}),
    }


# --------------------------------------------------------------------------- #
# Ingest endpoint  (used by watcher service)
# --------------------------------------------------------------------------- #

@app.post("/ingest")
def ingest(payload: dict):
    """
    Receive a document from the watcher and update the in-memory KB.
    Payload: {action: "upsert"|"delete", doc_id, kb_id, title, text}
    """
    action = payload.get("action", "upsert")
    doc_id = payload.get("doc_id", "")
    kb_id = payload.get("kb_id", DEFAULT_KB)
    title = payload.get("title", "")
    text = payload.get("text", "")

    if kb_id not in ALL_KB:
        ALL_KB[kb_id] = []

    items = ALL_KB[kb_id]

    if action == "delete":
        ALL_KB[kb_id] = [item for item in items if item.get("id") != doc_id]
        return {"status": "ok", "action": "delete", "doc_id": doc_id, "kb_id": kb_id}

    # upsert: replace if exists, otherwise append
    existing = next((i for i, item in enumerate(items) if item.get("id") == doc_id), None)
    doc = {"id": doc_id, "kb": kb_id, "title": title, "text": text}
    if existing is not None:
        ALL_KB[kb_id][existing] = doc
    else:
        ALL_KB[kb_id].append(doc)

    return {
        "status": "ok",
        "action": "upsert",
        "doc_id": doc_id,
        "kb_id": kb_id,
        "kb_size": len(ALL_KB[kb_id]),
    }

