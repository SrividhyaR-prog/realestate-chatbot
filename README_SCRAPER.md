# Step 2: Scrapers (DarGlobal + Wasalt)

## Setup (once)

```bash
cd real-estate-chatbot
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements-scraper.txt
```

## Run order

```bash
# 1. DarGlobal: whole site (~100-200 pages, a few minutes)
python -m scraper.run darglobal

# 2. Wasalt TEST run: 50 detail pages + a small discovery, to verify parsing before the long run
python -m scraper.run wasalt --max-index-pages 30 --max-items 50 --skip-blog
python -m scraper.run stats

# 3. Wasalt FULL run: all listings, projects and blog (several hours; safe to stop and resume)
python -m scraper.run wasalt
```

Stopping with Ctrl+C is safe. Re-running the same command resumes. Saved pages are skipped and discovery continues from its saved queue.

If a page parses badly, run `python -m scraper.run probe <url>`. It saves the raw HTML to `data/raw/probe/` and prints the parsed record.

## Outputs (`data/raw/`)

| File | Contents |
|---|---|
| `darglobal.jsonl` | One record per page: `doc_type` = project / press / partner / page, plus facts, amenities, FAQs, full text |
| `wasalt.jsonl` | Listings (price, type, city, district, rooms, areas, amenities, description) and projects |
| `wasalt_blog.jsonl` | Blog posts from the public WordPress API |
| `wasalt_discovery.json` | Discovered URL list + crawl queue (for resuming) |
| `scrape.log` | Full log |

## Design notes

- **Polite by default.** Requests are capped at 1 request per second (`--rps`) with jitter. robots.txt is checked for every host and always respected. 429 and 5xx responses are retried with backoff, honouring `Retry-After`. After 25 consecutive failures the run stops and tells you to resume later.
- **Discovery.** Uses sitemaps if robots.txt lists them. Otherwise it crawls search/index pages, paginating each one only while it keeps producing new listing IDs. Listings are deduplicated by the numeric ID in their URL.
- **No personal data.** Broker and agent names, phone numbers, and FAL/REGA licence numbers are filtered out and never stored.
- **English only.** The Arabic pages mirror the same listings and projects.
