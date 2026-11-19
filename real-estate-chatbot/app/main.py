"""FastAPI application: serves the chat UI and the /api/chat streaming endpoint."""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import config
from .data import Document, dataset_overview, load_documents
from .llm import LLMError, build_messages, stream_chat
from .search import Filters, SearchIndex, parse_filters, tokenize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


class State:
    index: SearchIndex
    overview: dict
    overview_text: str


state = State()


def load_index() -> None:
    docs = load_documents(config.DATA_DIR)
    state.index = SearchIndex(docs)
    state.overview = dataset_overview(docs)
    state.overview_text = json.dumps(state.overview, ensure_ascii=False)
    log.info("Index ready: %d chunks. %s", len(docs), state.overview_text)


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_index()
    yield


app = FastAPI(title=config.APP_TITLE, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---- request models ---------------------------------------------------------------------------
class Turn(BaseModel):
    role: str
    content: str = Field(max_length=6000)

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        return v


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


# ---- rate limiting ----------------------------------------------------------------------------
_hits: dict[str, deque] = defaultdict(deque)


def _check_rate(request: Request) -> None:
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")
    now, q = time.monotonic(), _hits[ip]
    while q and now - q[0] > config.RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= config.RATE_LIMIT_REQUESTS:
        wait = int(config.RATE_LIMIT_WINDOW_SECONDS - (now - q[0])) + 1
        raise HTTPException(429, f"Too many questions in a short time. Try again in {wait} seconds.")
    q.append(now)


# ---- retrieval for a conversational turn ------------------------------------------------------
def retrieve(message: str, history: list[Turn]) -> tuple[list[Document], Filters]:
    """Short follow-ups ("what about Jeddah?") inherit filters and keywords from the previous question."""
    filters = parse_filters(message)
    query = message
    prev_user = next((t.content for t in reversed(history) if t.role == "user"), None)
    if prev_user and len(tokenize(message)) <= 6:
        prev = parse_filters(prev_user)
        for attr in ("source", "city", "country", "property_type", "purpose", "bedrooms",
                     "min_price", "max_price", "sort"):
            if getattr(filters, attr) is None and getattr(prev, attr) is not None:
                if attr == "country" and filters.city:
                    continue  # a new city overrides the old country
                setattr(filters, attr, getattr(prev, attr))
        query = f"{prev_user} {message}"
    return state.index.search(query, k=config.TOP_K, filters=filters)


def fallback_answer(docs: list[Document], reason: str) -> str:
    if not docs:
        return f"The AI answer is unavailable right now ({reason}), and no matching records were found."
    lines = [f"The AI answer is unavailable right now ({reason}). These are the closest matches in the data:\n"]
    for n, d in enumerate(docs[:6], 1):
        m = d.meta
        bits = [m.get("location")]
        if m.get("price_sar"):
            bits.append(f"{m['price_sar']:,} SAR")
        elif d.source == "darglobal" and d.doc_type == "project":
            bits.append("price on request")
        if m.get("bedrooms"):
            bits.append(f"{m['bedrooms']} bedrooms")
        if m.get("status"):
            bits.append(m["status"])
        lines.append(f"- **{d.title}**: " + ", ".join(b for b in bits if b) + f" [{n}]")
    return "\n".join(lines)


def _event(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


# ---- routes -----------------------------------------------------------------------------------
@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "documents": len(state.index.docs), "model": config.OPENROUTER_MODEL,
            "llm_configured": bool(config.OPENROUTER_API_KEY)}


@app.get("/api/stats")
async def stats() -> dict:
    return state.overview


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    _check_rate(request)
    docs, filters = retrieve(req.message, req.history)
    history = [t.model_dump() for t in req.history]

    async def events():
        yield _event({"type": "sources", "items": [d.card() for d in docs],
                      "filters": filters.active(), "relaxed": filters.relaxed})
        try:
            messages = build_messages(req.message, history, docs, filters, state.overview_text)
            async for piece in stream_chat(messages):
                yield _event({"type": "delta", "text": piece})
        except LLMError as exc:
            log.warning("LLM unavailable: %s", exc)
            yield _event({"type": "sources", "items": []})
            yield _event({"type": "notice", "message": f"Unable to generate an answer: {_short_reason(exc)}. Please try again."})
        except Exception:  # network failures etc.
            log.exception("Chat failed")
            yield _event({"type": "sources", "items": []})
            yield _event({"type": "notice", "message": "Unable to generate an answer due to a connection problem. Please try again."})
        yield _event({"type": "done"})

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _short_reason(exc: LLMError) -> str:
    if exc.status == 429:
        return "free model quota reached"
    if not config.OPENROUTER_API_KEY:
        return "no API key configured"
    return "model error"
