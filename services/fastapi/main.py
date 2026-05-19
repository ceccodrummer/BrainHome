from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
import httpx
import os

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
MOBILE_UI_VERSION = "1.1.0"
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

@app.get("/health")
def health():
    return {"status": "ok", "dify_url": dify_url, "version": MOBILE_UI_VERSION}

@app.post("/proxy")
async def proxy(payload: dict):
    timeout = httpx.Timeout(120.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{dify_url}/query", json=payload)
        response.raise_for_status()
        return response.json()


@app.post("/proxy/stream")
async def proxy_stream(payload: dict):
    async def _passthrough():
        # read=None: no timeout between SSE chunks — Ollama can take 60-120s to respond.
        # The server-side LLM_TIMEOUT handles stalled Ollama requests.
        timeout = httpx.Timeout(connect=20.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{dify_url}/query/stream", json=payload) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk
    return StreamingResponse(_passthrough(), media_type="text/event-stream")
