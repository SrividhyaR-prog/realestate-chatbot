"""OpenRouter chat client (streaming) and prompt building for retrieval-augmented answers."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from . import config
from .data import Document
from .search import Filters

log = logging.getLogger("app.llm")


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


SYSTEM_PROMPT = """You are a property assistant. Your knowledge comes ONLY from data scraped from two public websites:
- DarGlobal (darglobal.co.uk): a London-listed luxury developer with projects in the UAE, Saudi Arabia, Qatar, Oman, Spain and the UK.
- Wasalt (wasalt.sa): a Saudi property portal with listings and projects.

Rules:
1. Answer only from the CONTEXT below. If it does not contain the answer, say so plainly and suggest what the user could ask instead. Never invent properties, prices, dates or features.
2. Cite sources with their number in square brackets, like [1] or [2][4], right after the facts they support.
3. Prices: Wasalt prices are in SAR. DarGlobal does not publish prices; say they are available on request from DarGlobal.
4. When listing several properties, use a short bulleted list with the key facts: name or type, location, price, size, bedrooms.
5. Some records are partial (e.g. missing price or city). If a detail is missing, say it was not captured rather than guessing.
6. The data is a snapshot from the websites; availability and prices may have changed. This is information, not financial or investment advice.
7. Reply in the user's language (Arabic or English). Be concise and direct.

Dataset overview: {overview}"""


def build_context(docs: list[Document], max_chars: int) -> str:
    parts, used = [], 0
    for n, d in enumerate(docs, 1):
        block = f"[{n}] source={d.source} type={d.doc_type} url={d.url}\n{d.text}"
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)]
        if not block:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def build_messages(question: str, history: list[dict], docs: list[Document], filters: Filters,
                   overview: str) -> list[dict]:
    note = ""
    if filters.relaxed:
        note = ("\nNOTE: No record matched every condition in the question. These filters were relaxed to find the "
                f"closest results: {', '.join(filters.relaxed)}. Tell the user that there were no exact matches.")
    context = build_context(docs, config.MAX_CONTEXT_CHARS) or "(no matching records)"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT.format(overview=overview)}]
    msgs += history[-6:]
    msgs.append({"role": "user", "content": f"CONTEXT:\n{context}{note}\n\nQUESTION: {question}"})
    return msgs


async def stream_chat(messages: list[dict]) -> AsyncIterator[str]:
    if not config.OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY is not set")
    body: dict = {"messages": messages, "stream": True, "temperature": 0.2, "max_tokens": config.LLM_MAX_TOKENS}
    if config.OPENROUTER_FALLBACK_MODELS:
        body["models"] = [config.OPENROUTER_MODEL, *config.OPENROUTER_FALLBACK_MODELS]
    else:
        body["model"] = config.OPENROUTER_MODEL
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "HTTP-Referer": config.APP_URL,
        "X-Title": config.APP_TITLE,
    }
    timeout = httpx.Timeout(config.LLM_TIMEOUT_SECONDS, connect=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{config.OPENROUTER_BASE_URL}/chat/completions",
                                 json=body, headers=headers) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise LLMError(_explain(resp.status_code, detail), resp.status_code)
            got_text = False
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise LLMError(f"Model error: {chunk['error'].get('message', 'unknown')}")
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or choice.get("message") or {}
                content = delta.get("content")
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                if content:
                    got_text = True
                    yield content
            if not got_text:
                raise LLMError("The model returned an empty answer")


def _explain(status: int, detail: str) -> str:
    if status == 401:
        return "OpenRouter rejected the API key (401). Check OPENROUTER_API_KEY."
    if status == 402:
        return "OpenRouter requires credits for this model (402). Use a ':free' model or openrouter/free."
    if status == 429:
        return "The free model quota or rate limit was reached (429). Try again later."
    if status == 404:
        return f"Model not found (404). Check OPENROUTER_MODEL ({config.OPENROUTER_MODEL})."
    return f"OpenRouter error {status}: {detail}"
