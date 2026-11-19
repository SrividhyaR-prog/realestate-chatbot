"""Crawl the whole public English site of DarGlobal (darglobal.co.uk).

Pages are server-rendered (Next.js), so plain HTTP is enough. Every page is saved as one record and
classified as: project, press, partner or page (corporate/other).
"""
from __future__ import annotations

import logging
import re
from collections import Counter, deque
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from tqdm import tqdm

from .common import (
    Fetcher,
    JsonlStore,
    extract_links,
    iter_sitemap_urls,
    make_soup,
    meta_content,
    normalize_url,
    now_iso,
    strip_noise,
    text_lines,
)

log = logging.getLogger("scraper.darglobal")

BASE = "https://darglobal.co.uk"
HOST = "darglobal.co.uk"
SEEDS = [f"{BASE}/projects", f"{BASE}/", f"{BASE}/press", f"{BASE}/about", f"{BASE}/investor"]

SKIP_PATH_RE = re.compile(
    r"(^/ar(/|$))|/product-category/|privacy|cookie|terms-|/login|/cart|/checkout|/my-account|"
    r"\.(pdf|jpe?g|png|webp|gif|svg|mp4|webm|zip|xml|json)$",
    re.I,
)
# Order matters: longer labels before their prefixes ("Area (SQFT)" before "Area").
FACT_LABELS = (
    "Property Type", "Status", "Unit Types", "Units", "Bedrooms", "Area (SQFT)", "Area (SQM)", "Area",
    "Location", "Starting Price", "Price", "Handover", "Completion", "Developer", "Plot Size",
)
DATE_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, \d{4}\b")
CUTOFF_LINES = ("Register Your Interest", "FOLLOW US ON")
BOILERPLATE = {
    "English | العربية", "English", "العربية", "Gallery", "View All Images", "Show more", "Download Brochure",
    "Book Unit", "view all", "discover more", "View all Projects", "Play", "Mute",
}


def _in_scope(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.removeprefix("www.") == HOST and not SKIP_PATH_RE.search(p.path)


def _clean_lines(soup: BeautifulSoup) -> list[str]:
    lines = text_lines(soup.body or soup)
    for i, line in enumerate(lines):
        if line in CUTOFF_LINES:
            lines = lines[:i]
            break
    return [l for l in lines if l not in BOILERPLATE]


def _facts(lines: list[str]) -> dict[str, str]:
    facts: dict[str, str] = {}
    for i, line in enumerate(lines[:150]):
        if len(line) > 160:
            continue
        for label in FACT_LABELS:
            if label in facts:
                continue
            if line == label and i + 1 < len(lines):
                facts[label] = lines[i + 1]
                break
            if line.startswith(label):
                rest = line[len(label):].strip(" :")
                if rest and (rest[0].isupper() or rest[0].isdigit()):
                    facts[label] = rest
                    break
    return facts


def _amenities(soup: BeautifulSoup) -> list[str]:
    head = soup.find(lambda t: t.name in ("h2", "h3") and t.get_text(strip=True).lower() == "amenities")
    if not head:
        return []
    items: list[str] = []
    for s in head.find_all_next(string=True):
        h2 = s.find_parent("h2")
        if h2 is not None and h2 is not head:
            break
        t = re.sub(r"\s+", " ", s).strip()
        if t and len(t) < 60 and t.lower() != "amenities" and t not in items:
            items.append(t)
        if len(items) >= 60:
            break
    return items


def _faqs(soup: BeautifulSoup) -> list[dict[str, str]]:
    faqs, seen = [], set()
    for q_tag in soup.find_all(["h3", "h4", "button"]):
        q = re.sub(r"\s+", " ", q_tag.get_text(" ", strip=True))
        if not q.endswith("?") or len(q) > 250 or q in seen:
            continue
        parts: list[str] = []
        for el in q_tag.find_all_next():
            if el.name in ("h2", "h3", "h4"):
                break
            if el.name == "button" and el.get_text(strip=True).endswith("?"):
                break
            if el.name in ("p", "div", "span", "li") and not el.find(["p", "div", "li"]):
                t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                if t and t != q and t not in parts:
                    parts.append(t)
            if sum(len(p) for p in parts) > 2500:
                break
        if parts:
            seen.add(q)
            faqs.append({"q": q, "a": " ".join(parts)})
    return faqs


def _project_links(soup: BeautifulSoup, page_url: str) -> set[str]:
    """Project cards on /projects (call on a noise-stripped soup so nav/footer links are excluded)."""
    out = set()
    for link in extract_links(soup, page_url):
        u = normalize_url(link)
        path = urlparse(u).path
        if _in_scope(u) and path not in ("/", "/projects"):
            out.add(u)
    return out


def parse_page(url: str, soup: BeautifulSoup, project_urls: set[str]) -> dict | None:
    """Parse an already noise-stripped DarGlobal page into a record."""
    html = soup.find("html")
    if html is not None and (html.get("lang") or "").lower().startswith("ar"):
        return None  # English only; the Arabic site mirrors the same content

    lines = _clean_lines(soup)
    if not lines:
        return None
    path = urlparse(url).path
    facts = _facts(lines)

    if path.startswith("/press/"):
        doc_type = "press"
    elif path.startswith("/partners/"):
        doc_type = "partner"
    elif url in project_urls or {"Property Type", "Status"} & facts.keys():
        doc_type = "project"
    else:
        doc_type = "page"

    h1 = soup.find("h1")
    date = None
    if doc_type == "press":
        m = DATE_RE.search("\n".join(lines[:60]))
        date = m.group(0) if m else None

    return {
        "key": url,
        "source": "darglobal",
        "doc_type": doc_type,
        "url": url,
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "h1": h1.get_text(" ", strip=True) if h1 else None,
        "meta_description": meta_content(soup, "description"),
        "date": date,
        "facts": facts if doc_type == "project" else {},
        "amenities": _amenities(soup) if doc_type == "project" else [],
        "faqs": _faqs(soup),
        "text": "\n".join(lines)[:30000],
        "scraped_at": now_iso(),
    }


def crawl(fetcher: Fetcher, out_path: Path, max_pages: int = 2000) -> Counter:
    store = JsonlStore(out_path)
    queue: deque[str] = deque()
    seen: set[str] = set()

    def enqueue(u: str) -> None:
        u = normalize_url(u)
        if u not in seen and _in_scope(u):
            seen.add(u)
            queue.append(u)

    for s in SEEDS:  # /projects first, so project URLs are known before project pages are parsed
        enqueue(s)
    for sm in dict.fromkeys(fetcher.sitemaps(BASE + "/") + [f"{BASE}/sitemap.xml"]):
        n = 0
        for u in iter_sitemap_urls(fetcher, sm):
            enqueue(u)
            n += 1
        if n:
            log.info("Sitemap %s: %d URLs", sm, n)

    project_urls: set[str] = set()
    counts: Counter = Counter()
    fetched = 0
    with tqdm(desc="DarGlobal pages", unit="page") as bar:
        while queue and fetched < max_pages:
            url = queue.popleft()
            r = fetcher.get(url)
            fetched += 1
            bar.update(1)
            bar.set_postfix(queued=len(queue), saved=len(store))
            if r is None or "html" not in r.headers.get("content-type", ""):
                continue
            final = normalize_url(r.url)
            soup = make_soup(r.text)
            for link in extract_links(soup, final):  # discover before stripping nav/footer
                enqueue(link)
            strip_noise(soup)
            if urlparse(final).path == "/projects":
                project_urls |= _project_links(soup, final)
                log.info("Found %d project links on /projects", len(project_urls))
            if final in store:
                continue  # resumed run: already saved
            rec = parse_page(final, soup, project_urls)
            if rec:
                store.write(rec)
                counts[rec["doc_type"]] += 1
    if queue:
        log.warning("Stopped at --max-pages=%d with %d URLs still queued", max_pages, len(queue))
    store.close()
    return counts
