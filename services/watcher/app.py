"""
Brain-Home Watcher Service
Monitora il file system e sincronizza i documenti modificati con la KB (dify_stub).

Variabili d'ambiente:
  WATCH_DIRS      - lista JSON di directory da monitorare, es: ["/watch/data", "/watch/services"]
  DIFY_URL        - URL del servizio dify_stub, es: http://dify:3000
  IGNORE_PATTERNS - pattern glob da ignorare, es: ["*.pyc", "__pycache__", "*.log"]
  CHUNK_SIZE      - dimensione massima chunk in caratteri (default: 1500)
  RETRY_MAX       - tentativi massimi di push verso Dify (default: 3)
  RETRY_DELAY     - secondi tra i retry (default: 2)
  AUDIT_LOG_PATH  - percorso file audit log (default: /app/audit.log)
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from fnmatch import fnmatch
from typing import Optional

import httpx
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

WATCH_DIRS: list[str] = json.loads(os.getenv("WATCH_DIRS", '["/watch"]'))
DIFY_URL: str = os.getenv("DIFY_URL", "http://dify:3000")
CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "1500"))
RETRY_MAX: int = int(os.getenv("RETRY_MAX", "3"))
RETRY_DELAY: float = float(os.getenv("RETRY_DELAY", "2"))
AUDIT_LOG_PATH: str = os.getenv("AUDIT_LOG_PATH", "/app/audit.log")

# File types da indicizzare  (3.5.2)
INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".md", ".txt", ".rst",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".env",
    ".html", ".css",
    ".sh", ".ps1",
    ".sql",
}

# Pattern da ignorare  (3.5.2)
DEFAULT_IGNORE_PATTERNS: list[str] = json.loads(
    os.getenv(
        "IGNORE_PATTERNS",
        '["*.pyc", "*.pyo", "__pycache__", "*.log", ".git", ".DS_Store", "node_modules", "*.tmp", "*.swp", "~*"]',
    )
)

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("watcher")

audit_handler = logging.FileHandler(AUDIT_LOG_PATH, encoding="utf-8")
audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
audit_logger = logging.getLogger("audit")
audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _should_ignore(path: str) -> bool:
    """Return True if the file matches any ignore pattern."""
    parts = Path(path).parts
    for pattern in DEFAULT_IGNORE_PATTERNS:
        for part in parts:
            if fnmatch(part, pattern):
                return True
    return False


def _is_indexable(path: str) -> bool:
    return Path(path).suffix.lower() in INDEXABLE_EXTENSIONS


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    """
    Split text into chunks of at most chunk_size characters.
    Tries to split on paragraph/line boundaries first.  (3.5.4)
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    # Split on double newlines (paragraphs) first
    paragraphs = re.split(r"\n{2,}", text)
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= chunk_size:
            current = (current + "\n\n" + para).lstrip()
        else:
            if current:
                chunks.append(current)
            # If single paragraph is still too large, split by lines
            if len(para) > chunk_size:
                lines = para.splitlines()
                current = ""
                for line in lines:
                    if len(current) + len(line) + 1 <= chunk_size:
                        current = (current + "\n" + line).lstrip()
                    else:
                        if current:
                            chunks.append(current)
                        current = line
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks or [text[:chunk_size]]


def _derive_kb_id(file_path: str) -> str:
    """
    Map file path to a KB id based on directory structure.  (3.5.3)
    """
    p = Path(file_path)
    path_lower = str(p).lower()
    if "frontend" in path_lower or "static" in path_lower or "templates" in path_lower:
        return "kb_frontend"
    if "ai" in path_lower or "dify" in path_lower or "llm" in path_lower:
        return "kb_ai"
    return "kb_sistema"


# --------------------------------------------------------------------------- #
# Dify push with retry  (3.5.9, 3.5.10)
# --------------------------------------------------------------------------- #

def _push_to_dify(doc_id: str, title: str, text: str, kb_id: str, action: str) -> bool:
    """Push a document chunk to dify_stub /ingest endpoint with retry logic."""
    payload = {
        "action": action,   # "upsert" | "delete"
        "doc_id": doc_id,
        "kb_id": kb_id,
        "title": title,
        "text": text,
    }
    for attempt in range(1, RETRY_MAX + 1):
        try:
            with httpx.Client(timeout=15) as client:
                response = client.post(f"{DIFY_URL}/ingest", json=payload)
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.warning(f"Push attempt {attempt}/{RETRY_MAX} failed for {doc_id}: {exc}")
            if attempt < RETRY_MAX:
                time.sleep(RETRY_DELAY)
    logger.error(f"All {RETRY_MAX} push attempts failed for {doc_id}")
    return False


# --------------------------------------------------------------------------- #
# Event handler  (3.5.6)
# --------------------------------------------------------------------------- #

class BrainHomeHandler(FileSystemEventHandler):
    def _handle_upsert(self, path: str):
        if _should_ignore(path) or not _is_indexable(path):
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning(f"Cannot read {path}: {exc}")
            return

        kb_id = _derive_kb_id(path)
        title = Path(path).name
        rel_path = path.replace("\\", "/")

        # Chunk handling  (3.5.8)
        chunks = _chunk_text(text)
        success = True
        for idx, chunk in enumerate(chunks):
            doc_id = f"{rel_path}::{idx}" if len(chunks) > 1 else rel_path
            ok = _push_to_dify(doc_id, title, chunk, kb_id, "upsert")
            if not ok:
                success = False

        status = "ok" if success else "error"
        audit_logger.info(f"UPSERT {status} path={path} kb={kb_id} chunks={len(chunks)}")
        logger.info(f"Indexed {path} → {kb_id} ({len(chunks)} chunk{'s' if len(chunks)>1 else ''})")

    def _handle_delete(self, path: str):
        if _should_ignore(path) or not _is_indexable(path):
            return
        kb_id = _derive_kb_id(path)
        rel_path = path.replace("\\", "/")
        ok = _push_to_dify(rel_path, "", "", kb_id, "delete")
        status = "ok" if ok else "error"
        audit_logger.info(f"DELETE {status} path={path} kb={kb_id}")
        logger.info(f"Removed {path} from {kb_id}")

    def on_modified(self, event):
        if not event.is_directory:
            self._handle_upsert(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle_upsert(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._handle_delete(event.src_path)

    def on_moved(self, event):  # (3.5.6 renamed)
        if not event.is_directory:
            self._handle_delete(event.src_path)
            self._handle_upsert(event.dest_path)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    logger.info(f"Brain-Home Watcher starting")
    logger.info(f"Watching: {WATCH_DIRS}")
    logger.info(f"Dify URL: {DIFY_URL}")
    logger.info(f"Chunk size: {CHUNK_SIZE} chars | Retry: {RETRY_MAX}x{RETRY_DELAY}s")

    handler = BrainHomeHandler()
    observer = Observer()

    for watch_dir in WATCH_DIRS:
        p = Path(watch_dir)
        if p.exists():
            observer.schedule(handler, str(p), recursive=True)
            logger.info(f"  Scheduled: {p}")
        else:
            logger.warning(f"  Directory not found, skipping: {p}")

    observer.start()
    audit_logger.info("WATCHER_START dirs=" + json.dumps(WATCH_DIRS))

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass

    observer.stop()
    observer.join()
    audit_logger.info("WATCHER_STOP")
    logger.info("Watcher stopped.")


if __name__ == "__main__":
    main()
