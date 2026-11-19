---
title: DarGlobal & Wasalt Property Assistant
emoji: 🏛️
colorFrom: blue
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# DarGlobal & Wasalt Property Assistant

An AI chatbot that answers questions about real estate using data scraped from
[DarGlobal](https://darglobal.co.uk) (international luxury developer) and
[Wasalt](https://wasalt.sa) (Saudi property portal). Answers cite the listings they come from.

## How it works

```
scraper/  ──► data/raw/*.jsonl ──► app/data.py   (records → documents + metadata)
                                   app/search.py (BM25 + filters parsed from the question)
                                   app/llm.py    (prompt + OpenRouter streaming)
                                   app/main.py   (FastAPI: /api/chat, /api/stats, UI)
```

1. **Scraping** (`scraper/`, see `README_SCRAPER.md`): polite, resumable, robots.txt-aware crawlers for both sites. Personal data (broker names, phones, licence numbers) is never stored.
2. **Retrieval**: each question is parsed for filters (city, country, property type, bedrooms, price range, rent/sale, cheapest/most expensive). Matching records are ranked with BM25. If nothing matches every condition, filters are relaxed one at a time and the answer says so. Short follow-ups ("what about Jeddah?") inherit the previous question's filters.
3. **Generation**: the top results are sent to a free OpenRouter model with instructions to answer only from that context and cite sources `[n]`. The default `openrouter/free` routes to whichever free model is currently available.
4. **Resilience**: if the model is unavailable (no key, free quota reached, outage), the app still answers with the closest matching listings. Each visitor is rate-limited to protect the shared free quota.

## Run locally

```bash
cp .env.example .env          # add your OpenRouter key (free at openrouter.ai/keys)
docker compose up --build     # then open http://localhost:7860
```

With an environment variable instead of `.env`:

```bash
OPENROUTER_API_KEY=sk-or-v1-... docker compose up --build
```

For Hugging Face or another Docker deployment, set `OPENROUTER_API_KEY` as a secret and keep port `7860` as `PORT=7860`.

Without Docker:

```bash
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-v1-...
uvicorn app.main:app --port 7860
```

Tests (no key or network needed): `pip install pytest && pytest -q`

## Refresh the data

```bash
pip install -r requirements-scraper.txt
python -m scraper.run darglobal      # whole DarGlobal site, a few minutes
python -m scraper.run wasalt         # all Wasalt listings, several hours, resumable
```

Then rebuild the container. The app loads every `data/raw/*.jsonl` file at startup.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | (required) | OpenRouter key |
| `OPENROUTER_MODEL` | `openrouter/free` | Any `:free` model ID, or the free router |
| `OPENROUTER_FALLBACK_MODELS` | empty | Comma-separated models tried in order if the first fails |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | 20 / 600 | Questions per visitor per window |
| `APP_URL` | `http://localhost:7860` | Public URL, sent to OpenRouter |

## Known limitations

- OpenRouter free models allow a limited number of requests per day (about 50 until an account has bought $10 of credits). When the limit is hit, the app falls back to listing matches.
- DarGlobal does not publish prices; the bot says prices are available on request.
- The bundled dataset is a starting sample (all 38 DarGlobal projects, 9 in full detail, plus 31 Wasalt listings and 2 projects). Run the scrapers for full coverage.
- Retrieval is keyword-based (BM25), which keeps the image small and fast; semantic embeddings would improve recall for loosely worded questions.
