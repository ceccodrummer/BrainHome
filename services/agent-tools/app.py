"""
Brain-Home Agent-Tools Service
Espone API protette per operazioni su file che l'IA può richiamare.

Endpoint:
  POST /write     - scrive testo in un file (dentro WORKSPACE)
  GET  /read      - legge un file (dentro WORKSPACE)
  POST /git-commit - esegue git add + commit automatico
  GET  /health    - stato del servizio

Variabili d'ambiente:
  WORKSPACE_DIR   - directory radice consentita per le operazioni (default: /workspace)
  AGENT_TOKEN     - token Bearer per autenticare le richieste agente
  AUDIT_LOG_PATH  - percorso del file di audit log
  GIT_AUTHOR_NAME - nome per i commit automatici
  GIT_AUTHOR_EMAIL - email per i commit automatici
"""

import logging
import os
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/workspace")).resolve()
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "/app/audit.log")
GIT_AUTHOR_NAME = os.getenv("GIT_AUTHOR_NAME", "BrainHome Agent")
GIT_AUTHOR_EMAIL = os.getenv("GIT_AUTHOR_EMAIL", "agent@brainhome.local")

# Max file size readable in one call (security: prevent memory exhaustion)
MAX_READ_BYTES = int(os.getenv("MAX_READ_BYTES", str(1024 * 512)))  # 512 KB

# --------------------------------------------------------------------------- #
# Logging + audit
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agent-tools")

audit_handler = logging.FileHandler(AUDIT_LOG_PATH, encoding="utf-8")
audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
audit_logger = logging.getLogger("audit")
audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(title="Brain-Home Agent Tools", version="1.0.0")

# --------------------------------------------------------------------------- #
# Security: path validation  (5.4.3, 5.4.4)
# --------------------------------------------------------------------------- #

def _resolve_safe_path(relative_path: str) -> Path:
    """
    Resolve relative_path inside WORKSPACE_DIR and verify no directory traversal.
    Raises HTTPException(400) if the path escapes the workspace.
    """
    # Normalize and resolve
    target = (WORKSPACE_DIR / relative_path.lstrip("/")).resolve()

    # Enforce containment — must start with WORKSPACE_DIR
    try:
        target.relative_to(WORKSPACE_DIR)
    except ValueError:
        audit_logger.info(f"BLOCKED path traversal attempt: {relative_path!r}")
        raise HTTPException(status_code=400, detail="Path outside workspace is not allowed.")

    return target


# --------------------------------------------------------------------------- #
# Auth  (5.4.5)
# --------------------------------------------------------------------------- #

def _verify_token(authorization: str = Header(default="")):
    """Simple Bearer token check. Skip if AGENT_TOKEN is not configured."""
    if not AGENT_TOKEN:
        return  # Auth not configured: open (suitable for local-only deployment)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != AGENT_TOKEN:
        audit_logger.info("UNAUTHORIZED request rejected")
        raise HTTPException(status_code=401, detail="Unauthorized.")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class WriteRequest(BaseModel):
    path: str
    content: str
    create_dirs: bool = True


class AppendRequest(BaseModel):
    path: str
    content: str
    separator: str = "\n"


class DeleteRequest(BaseModel):
    path: str


class MoveRequest(BaseModel):
    source: str
    destination: str


class SearchRequest(BaseModel):
    pattern: str
    path: str = ""          # sub-directory to search (empty = whole workspace)
    is_regex: bool = False
    max_results: int = 50


class RunRequest(BaseModel):
    path: str               # .py file relative to workspace
    args: list[str] = []
    timeout: int = 10       # seconds


class CommitRequest(BaseModel):
    message: str
    paths: list[str] = []   # empty list = git add -A


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    return {
        "status": "ok",
        "workspace": str(WORKSPACE_DIR),
        "workspace_exists": WORKSPACE_DIR.exists(),
        "auth_enabled": bool(AGENT_TOKEN),
    }


@app.post("/write", dependencies=[Depends(_verify_token)])
def write_file(req: WriteRequest):
    """Write content to a file inside the workspace.  (5.4.2)"""
    target = _resolve_safe_path(req.path)

    if req.create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)

    try:
        target.write_text(req.content, encoding="utf-8")
    except Exception as exc:
        audit_logger.info(f"WRITE ERROR path={req.path} error={exc}")
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")

    audit_logger.info(f"WRITE OK path={target} bytes={len(req.content.encode())}")
    return {
        "status": "ok",
        "path": str(target.relative_to(WORKSPACE_DIR)),
        "bytes_written": len(req.content.encode("utf-8")),
    }


@app.get("/read", dependencies=[Depends(_verify_token)])
def read_file_endpoint(path: str):
    """Read a file from the workspace.  (5.4.2)"""
    target = _resolve_safe_path(path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file.")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size} bytes). Max: {MAX_READ_BYTES}.",
        )

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Read failed: {exc}")

    audit_logger.info(f"READ OK path={target} bytes={size}")
    return PlainTextResponse(content)


@app.post("/git-commit", dependencies=[Depends(_verify_token)])
def git_commit(req: CommitRequest):
    """Stage files and create a git commit.  (5.4.8)"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    }

    try:
        # Stage files
        add_targets = req.paths if req.paths else ["-A"]
        # Validate each provided path (5.4.4)
        for p in req.paths:
            _resolve_safe_path(p)

        subprocess.run(
            ["git", "add"] + add_targets,
            cwd=str(WORKSPACE_DIR),
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )

        # Commit
        result = subprocess.run(
            ["git", "commit", "-m", req.message],
            cwd=str(WORKSPACE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode not in (0, 1):  # 1 = nothing to commit
            raise RuntimeError(result.stderr or result.stdout)

        commit_output = result.stdout.strip()
        audit_logger.info(f"GIT_COMMIT OK message={req.message!r} paths={req.paths}")
        return {"status": "ok", "output": commit_output}

    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr)
        audit_logger.info(f"GIT_COMMIT ERROR: {err}")
        raise HTTPException(status_code=500, detail=f"Git error: {err}")
    except Exception as exc:
        audit_logger.info(f"GIT_COMMIT ERROR: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/list", dependencies=[Depends(_verify_token)])
def list_files(path: str = ""):
    """List files and directories inside a workspace path."""
    target = _resolve_safe_path(path) if path else WORKSPACE_DIR

    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found.")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory.")

    entries = []
    for item in sorted(target.iterdir()):
        rel = str(item.relative_to(WORKSPACE_DIR))
        entries.append({
            "name": item.name,
            "path": rel,
            "type": "dir" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        })

    audit_logger.info(f"LIST OK path={target} entries={len(entries)}")
    return {"status": "ok", "path": path or "/", "entries": entries}


@app.delete("/delete", dependencies=[Depends(_verify_token)])
def delete_file(req: DeleteRequest):
    """Delete a file from the workspace."""
    target = _resolve_safe_path(req.path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory. Only files can be deleted.")

    try:
        target.unlink()
    except Exception as exc:
        audit_logger.info(f"DELETE ERROR path={req.path} error={exc}")
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")

    audit_logger.info(f"DELETE OK path={target}")
    return {"status": "ok", "path": str(target.relative_to(WORKSPACE_DIR))}


@app.post("/append", dependencies=[Depends(_verify_token)])
def append_file(req: AppendRequest):
    """Append content to an existing file (creates it if missing)."""
    target = _resolve_safe_path(req.path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    new_content = existing + req.separator + req.content if existing else req.content

    try:
        target.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        audit_logger.info(f"APPEND ERROR path={req.path} error={exc}")
        raise HTTPException(status_code=500, detail=f"Append failed: {exc}")

    appended_bytes = len(req.content.encode("utf-8"))
    audit_logger.info(f"APPEND OK path={target} appended_bytes={appended_bytes}")
    return {
        "status": "ok",
        "path": str(target.relative_to(WORKSPACE_DIR)),
        "bytes_appended": appended_bytes,
        "total_bytes": len(new_content.encode("utf-8")),
    }


@app.get("/tree", dependencies=[Depends(_verify_token)])
def tree(path: str = "", max_depth: int = 4):
    """Return a recursive file tree as both a list and a text representation."""
    root = _resolve_safe_path(path) if path else WORKSPACE_DIR
    if not root.exists():
        raise HTTPException(status_code=404, detail="Path not found.")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory.")

    lines: list[str] = []
    nodes: list[dict] = []

    def _walk(cur: Path, depth: int, prefix: str):
        if depth > max_depth:
            return
        children = sorted(cur.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{child.name}" + ("/" if child.is_dir() else ""))
            rel = str(child.relative_to(WORKSPACE_DIR))
            nodes.append({"path": rel, "type": "dir" if child.is_dir() else "file"})
            if child.is_dir():
                extension = "    " if is_last else "│   "
                _walk(child, depth + 1, prefix + extension)

    lines.append((root.relative_to(WORKSPACE_DIR) if root != WORKSPACE_DIR else Path(".")).as_posix() + "/")
    _walk(root, 1, "")

    audit_logger.info(f"TREE OK path={root} nodes={len(nodes)}")
    return {"status": "ok", "tree_text": "\n".join(lines), "nodes": nodes}


@app.post("/search", dependencies=[Depends(_verify_token)])
def search_files(req: SearchRequest):
    """Search for a text pattern across files in the workspace."""
    import re as _re

    search_root = _resolve_safe_path(req.path) if req.path else WORKSPACE_DIR
    if not search_root.exists():
        raise HTTPException(status_code=404, detail="Search path not found.")

    try:
        pat = _re.compile(req.pattern, _re.IGNORECASE) if req.is_regex else None
    except _re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {e}")

    results: list[dict] = []
    total_searched = 0

    for file in sorted(search_root.rglob("*")):
        if not file.is_file():
            continue
        if file.stat().st_size > MAX_READ_BYTES:
            continue
        # Skip binary files by extension
        if file.suffix.lower() in {".pyc", ".pyo", ".exe", ".bin", ".jpg", ".png", ".gif", ".zip", ".tar", ".gz"}:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        total_searched += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(results) >= req.max_results:
                break
            matched = (pat.search(line) is not None) if pat else (req.pattern.lower() in line.lower())
            if matched:
                results.append({
                    "file": str(file.relative_to(WORKSPACE_DIR)),
                    "line": lineno,
                    "text": line.rstrip(),
                })
        if len(results) >= req.max_results:
            break

    audit_logger.info(f"SEARCH OK pattern={req.pattern!r} results={len(results)} searched={total_searched}")
    return {
        "status": "ok",
        "pattern": req.pattern,
        "results": results,
        "total_matches": len(results),
        "files_searched": total_searched,
    }


@app.post("/move", dependencies=[Depends(_verify_token)])
def move_file(req: MoveRequest):
    """Move or rename a file/directory inside the workspace."""
    source = _resolve_safe_path(req.source)
    dest = _resolve_safe_path(req.destination)

    if not source.exists():
        raise HTTPException(status_code=404, detail="Source not found.")
    if dest.exists():
        raise HTTPException(status_code=409, detail="Destination already exists.")

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(dest)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Move failed: {exc}")

    audit_logger.info(f"MOVE OK source={source} dest={dest}")
    return {
        "status": "ok",
        "source": str(source.relative_to(WORKSPACE_DIR)),
        "destination": str(dest.relative_to(WORKSPACE_DIR)),
    }


@app.post("/run", dependencies=[Depends(_verify_token)])
def run_script(req: RunRequest):
    """Execute a Python script that lives inside the workspace. Output is captured."""
    if not req.path.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files can be executed.")

    target = _resolve_safe_path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Script not found.")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file.")

    # Sanitize args — no shell metacharacters
    for arg in req.args:
        if any(c in arg for c in (";", "&", "|", "`", "$", ">", "<", "\n")):
            raise HTTPException(status_code=400, detail=f"Unsafe argument: {arg!r}")

    try:
        result = subprocess.run(
            ["python3", str(target)] + req.args,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=req.timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail=f"Script timed out after {req.timeout}s.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Execution failed: {exc}")

    audit_logger.info(f"RUN OK path={target} exit_code={result.returncode}")
    return {
        "status": "ok",
        "path": str(target.relative_to(WORKSPACE_DIR)),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
