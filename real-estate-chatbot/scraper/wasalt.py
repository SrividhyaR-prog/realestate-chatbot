"""Scrape all public Wasalt (wasalt.sa) listings, projects and blog posts.

Phase 1 (discovery): collect every listing/project URL, from sitemaps if robots.txt lists them,
otherwise by crawling the search/index pages (paginated with ?page=N). The state is saved to disk
so an interrupted discovery resumes.
Phase 2 (details): fetch each listing/project page and parse it. Resumable: already-saved IDs are skipped.
Phase 3 (blog): public WordPress REST API of blog.wasalt.sa.

Personal data is deliberately NOT stored: no broker/agent names, phone numbers or licence numbers.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

from bs4 import BeautifulSoup
from tqdm import tqdm

from .common import (
    BlockedError,
    Fetcher,
    JsonlStore,
    extract_links,
    iter_sitemap_urls,
    make_soup,
    meta_content,
    now_iso,
    strip_noise,
    text_lines,
)

log = logging.getLogger("scraper.wasalt")

BASE = "https://wasalt.sa"
HOSTS = {"wasalt.sa", "www.wasalt.sa"}
BLOG_API = "https://blog.wasalt.sa/en/wp-json/wp/v2/posts"

LISTING_RE = re.compile(r"^/en/property/.+-(\d+)$")
PROJECT_RE = re.compile(r"^/en/project/.+-(\d+)$")
INDEX_RE = re.compile(r"^/en/(?:[a-z0-9-]*for-(?:sale|rent)[a-z0-9-]*|projects?(?:-[a-z0-9-]+)?|daily-rent[a-z0-9-]*)$")
MAX_PAGES_PER_INDEX = 1000

CITIES = [
    "riyadh", "jeddah", "makkah", "madinah", "dammam", "khobar", "dhahran", "al-ahsa", "taif", "abha",
    "khamis-mushayt", "jazan", "najran", "tabuk", "hail", "buraidah", "unaizah", "al-jubail", "yanbu",
    "al-kharj", "qatif", "hafar-al-batin", "al-baha", "arar", "sakaka",
]
# Unverified slugs are harmless: a 404 is skipped and real index links are discovered from pages.
SEEDS = [f"{BASE}/en", f"{BASE}/en/projects"] + [
    f"{BASE}/en/properties-for-{p}-in-{c}" for p in ("sale", "rent") for c in CITIES
]

PROPERTY_TYPES = (
    "Rest house", "Apartment", "Villa", "Land", "Floor", "Building", "Farm", "Palace", "Duplex", "Chalet",
    "Studio", "Townhouse", "Office", "Shop", "Warehouse", "Showroom", "Room",
)
ATTRIBUTE_LABELS = (
    "Property usage", "Age", "Facade", "Street width", "Furnishing", "Floor number", "Direction",
    "Postal Code", "Water", "Electricity",
)
PERIODS = {"yearly": "year", "year": "year", "monthly": "month", "month": "month",
           "daily": "day", "day": "day", "night": "day"}
FURNISHING = {"Furnished", "Un-Furnished", "Unfurnished", "Semi-Furnished"}
PROJECT_STATUSES = ("Ready to Move-in", "Under Construction", "Off Plan", "Off-Plan", "Completed", "Upcoming")
# Lines that carry personal / licensing data or page chrome: never stored.
DROP_LINE_RE = re.compile(
    r"FAL|REGA|Licen[cs]e|Ad source|Ref no|Advertiser|Broker|Agent|\+?966|\b05\d{8}\b|people viewed|"
    r"Calculate EMI|Est\. Repayment|Is this helpful|Open app|Wasalt app|Small Logo|Property Image|Map view$",
    re.I,
)
DESC_STOP = ("Price:", "Guarantees", "Location & Nearby", "Similar Properties", "Recommended", "Other obligations")


# ---- URL classification -----------------------------------------------------------------------
def classify(url: str) -> tuple[str, str | None, str] | None:
    """Return (kind, id, clean_url) where kind is listing | project | index, or None if out of scope."""
    p = urlparse(url)
    if p.netloc.lower() not in HOSTS:
        return None
    raw_path = p.path.rstrip("/")
    path = unquote(raw_path)
    if not path.startswith("/en/"):
        return None  # English pages only; Arabic pages repeat the same IDs
    clean = urlunparse(("https", "wasalt.sa", raw_path, "", "", ""))
    if m := LISTING_RE.match(path):
        return "listing", m.group(1), clean
    if m := PROJECT_RE.match(path):
        return "project", m.group(1), clean
    if INDEX_RE.match(path):
        return "index", None, clean
    return None


# ---- Phase 1: discovery -----------------------------------------------------------------------
def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"listings": {}, "projects": {}, "queue": [], "seen_index": [], "sitemaps_done": False,
            "from_sitemap": 0, "complete": False}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def discover(
    fetcher: Fetcher,
    state_path: Path,
    use_sitemaps: bool = True,
    force_crawl: bool = False,
    max_index_pages: int | None = None,
) -> dict:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state(state_path)
    if state["complete"]:
        log.info("Discovery already complete: %d listings, %d projects (use --rediscover to redo)",
                 len(state["listings"]), len(state["projects"]))
        return state

    def add(kind, ident, url) -> bool:
        bucket = state["listings"] if kind == "listing" else state["projects"]
        if ident in bucket:
            return False
        bucket[ident] = url
        return True

    if use_sitemaps and not state["sitemaps_done"]:
        for sm in dict.fromkeys(fetcher.sitemaps(BASE + "/") + [f"{BASE}/sitemap.xml"]):
            for u in iter_sitemap_urls(fetcher, sm):
                c = classify(u)
                if c and c[0] in ("listing", "project") and add(*c):
                    state["from_sitemap"] += 1
        state["sitemaps_done"] = True
        _save_state(state_path, state)
        log.info("Sitemaps gave %d listing/project URLs", state["from_sitemap"])

    if state["from_sitemap"] and not force_crawl:
        state["complete"] = True
        _save_state(state_path, state)
        return state

    # Index crawl: breadth-first over search pages; paginate a page only while it yields new IDs.
    seen = set(state["seen_index"])
    queue: deque = deque(tuple(x) for x in state["queue"])
    if not queue and not seen:
        for s in SEEDS:
            seen.add(f"{s}|1")
            queue.append((s, 1))

    pages = 0
    with tqdm(desc="Wasalt discovery", unit="page") as bar:
        while queue:
            if max_index_pages and pages >= max_index_pages:
                log.warning("Stopped discovery at --max-index-pages=%d", max_index_pages)
                break
            base, page = queue.popleft()
            r = fetcher.get(base if page == 1 else f"{base}?page={page}")
            pages += 1
            bar.update(1)
            if r is not None:
                new = 0
                for link in extract_links(make_soup(r.text), base):
                    c = classify(link)
                    if not c:
                        continue
                    if c[0] == "index":
                        key = f"{c[2]}|1"
                        if key not in seen:
                            seen.add(key)
                            queue.append((c[2], 1))
                    elif add(*c):
                        new += 1
                if new and page < MAX_PAGES_PER_INDEX:
                    key = f"{base}|{page + 1}"
                    if key not in seen:
                        seen.add(key)
                        queue.appendleft((base, page + 1))  # finish this index before moving on
            bar.set_postfix(listings=len(state["listings"]), projects=len(state["projects"]), queued=len(queue))
            if pages % 50 == 0:
                state["queue"], state["seen_index"] = [list(x) for x in queue], sorted(seen)
                _save_state(state_path, state)

    state["queue"], state["seen_index"] = [list(x) for x in queue], sorted(seen)
    state["complete"] = not queue
    _save_state(state_path, state)
    log.info("Discovery: %d listings, %d projects (%s)", len(state["listings"]), len(state["projects"]),
             "complete" if state["complete"] else "partial, re-run to continue")
    return state


# ---- Phase 2: parsing -------------------------------------------------------------------------
def _num(s: str | None):
    if not s:
        return None
    s = s.replace(",", "").strip()
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _count(body: str, label: str):
    m = re.search(rf"(?m)^(\d+\+?)\s*\n\s*{label}\s*$", body, re.I) or re.search(rf"\b(\d+\+?)\s+{label}\b", body, re.I)
    if not m:
        return None
    v = m.group(1)
    return v if v.endswith("+") else int(v)


def _labeled(lines: list[str], labels) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        if len(line) > 80:
            continue
        for label in labels:
            if label in out:
                continue
            if line == label and i + 1 < len(lines):
                out[label] = lines[i + 1]
            elif line.startswith(label):
                rest = line[len(label):].strip(" :")
                if rest and (rest[0].isupper() or rest[0].isdigit()):
                    out[label] = rest
    return out


def _location(lines: list[str], body: str) -> str | None:
    for i, line in enumerate(lines):
        if line == "Map view" and i > 0:  # "Map view" rendered as its own link after the location
            return lines[i - 1].strip(" ,")
        if line.endswith("Map view") and len(line) > len("Map view") + 2:
            return line[: -len("Map view")].strip(" ,")
    m = re.search(r"for (?:Sale|Rent) in ([^\n]+)", body)
    return m.group(1).strip() if m else None


def _section(lines: list[str], start: str, stops=DESC_STOP, limit: int = 60) -> list[str]:
    for i, line in enumerate(lines):
        if line.lower().startswith(start.lower()):
            out = []
            for l in lines[i + 1:]:
                if l.startswith(stops) or len(out) >= limit:
                    break
                if not DROP_LINE_RE.search(l):
                    out.append(l)
            return out
    return []


def _list_after(soup: BeautifulSoup, label: str) -> list[str]:
    node = soup.find(string=re.compile(label, re.I))
    if not node:
        return []
    ul = node.find_next(["ul", "ol"])
    if not ul:
        return []
    return [t for li in ul.find_all("li") if (t := li.get_text(" ", strip=True))][:50]


def _base_record(kind: str, ident: str, url: str, soup: BeautifulSoup) -> dict:
    h1 = soup.find("h1")
    return {
        "key": f"wasalt:{kind}:{ident}",
        "source": "wasalt",
        "doc_type": kind,
        "id": ident,
        "url": url,
        "title": h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else None),
        "meta_description": meta_content(soup, "description"),
        "has_next_data": soup.find("script", id="__NEXT_DATA__") is not None,
    }


def parse_listing(url: str, ident: str, soup: BeautifulSoup) -> dict:
    rec = _base_record("listing", ident, url, soup)
    amenities = _list_after(soup, "Property amenities")
    fixtures = _list_after(soup, "Property fixtures")
    strip_noise(soup)
    lines = [l for l in text_lines(soup.body or soup)]
    body = "\n".join(lines)
    title = rec["title"] or ""

    path_parts = unquote(urlparse(url).path).split("/")  # ['', 'en', 'property', '<segment>', '<slug-id>']
    segment = path_parts[3].lower() if len(path_parts) > 4 else ""
    # The page's own badge wins: some URLs contain /rent/ while the page says "For Sale".
    if "Daily Rent" in lines:
        purpose = "daily_rent"
    elif "For Rent" in lines:
        purpose = "rent"
    elif "For Sale" in lines:
        purpose = "sale"
    elif "daily" in segment:
        purpose = "daily_rent"
    elif "rent" in segment:
        purpose = "rent"
    else:
        purpose = "sale"

    ptype = next((t for t in PROPERTY_TYPES if title.lower().startswith(t.lower())), None)
    if not ptype:
        m = re.search(r"\b(" + "|".join(PROPERTY_TYPES) + r") for (?:Sale|Rent)\b", body, re.I)
        ptype = m.group(1).title() if m else None

    m = re.search(r"Price:\s*([\d,\.]+)\s*SAR", body) or re.search(
        r"(?m)^([\d,]{4,})\s*$\n(?:SAR\s*\n)?[\d,\.]+\s*(?:SAR\s*)?/\s*sqm", body)
    price = _num(m.group(1)) if m else None
    m = re.search(r"([\d,\.]+)\s*(?:SAR\s*)?/\s*sqm", body)
    price_per_sqm = _num(m.group(1)) if m else None
    period = None
    if purpose != "sale":
        m = re.search(r"/\s*(Yearly|Year|Monthly|Month|Daily|Day|Night)\b", body, re.I)
        period = PERIODS.get(m.group(1).lower()) if m else None

    m = re.search(r"Land Area:\s*([\d,\.]+)", body) or re.search(r"([\d,\.]+)\s*SQM\s*\n\s*land area", body, re.I)
    land = _num(m.group(1)) if m else None
    m = re.search(r"Built[ -]?up Area:\s*([\d,\.]+)", body, re.I)
    built = _num(m.group(1)) if m else None
    m = re.search(r"Building year:\s*(\d{4})", body)
    year = int(m.group(1)) if m else None

    location = _location(lines, body)
    parts = [p.strip() for p in location.split(",")] if location else []

    if not amenities:
        amenities = [l.lstrip("-• ").strip() for l in _section(lines, "Property amenities", limit=40) if l[:1] in "-•"]

    description = _section(lines, "Property Description")
    rec.update({
        "purpose": purpose,
        "property_type": ptype,
        "price_sar": price,
        "price_period": period,
        "price_per_sqm_sar": price_per_sqm,
        "location": location,
        "district": parts[0] if parts else None,
        "city": parts[-1] if parts else None,
        "bedrooms": _count(body, r"bedrooms?"),
        "bathrooms": _count(body, r"bathrooms?"),
        "living_rooms": _count(body, r"living rooms?"),
        "guest_rooms": _count(body, r"guest rooms?"),
        "floors": _count(body, r"floors?"),
        "land_area_sqm": land,
        "built_up_area_sqm": built,
        "building_year": year,
        "furnishing": next((l for l in lines if l in FURNISHING), None),
        "attributes": _labeled(lines, ATTRIBUTE_LABELS),
        "amenities": amenities,
        "fixtures": fixtures,
        "description": "\n".join(description)[:5000],
        "text": "\n".join(l for l in lines[:250] if not DROP_LINE_RE.search(l))[:6000],
        "scraped_at": now_iso(),
    })
    return rec


def parse_project(url: str, ident: str, soup: BeautifulSoup) -> dict:
    rec = _base_record("project", ident, url, soup)
    strip_noise(soup)
    lines = text_lines(soup.body or soup)
    body = "\n".join(lines)
    types = {}
    for l in lines:
        if m := re.match(r"^(" + "|".join(PROPERTY_TYPES) + r")\s*\((\d+)\)$", l, re.I):
            types[m.group(1).title()] = int(m.group(2))
    location = _location(lines, body)
    parts = [p.strip() for p in location.split(",")] if location else []
    labeled = _labeled(lines, ("Developer", "Developed by", "Completion", "Handover", "Starting from", "Starting Price"))
    rec.update({
        "status": next((s for s in PROJECT_STATUSES if s in lines), None),
        "location": location,
        "district": parts[0] if parts else None,
        "city": parts[-1] if parts else None,
        "units": _count(body, r"units?"),
        "available_types": types,
        "attributes": labeled,
        "description": "\n".join(_section(lines, "About Project", limit=80))[:6000],
        "text": "\n".join(l for l in lines[:300] if not DROP_LINE_RE.search(l))[:8000],
        "scraped_at": now_iso(),
    })
    return rec


def scrape_details(fetcher: Fetcher, state: dict, out_path: Path, workers: int = 2,
                   max_items: int | None = None) -> Counter:
    store = JsonlStore(out_path)
    jobs = [("project", i, u) for i, u in state["projects"].items()] + [
        ("listing", i, u) for i, u in state["listings"].items()]
    jobs = [j for j in jobs if f"wasalt:{j[0]}:{j[1]}" not in store]
    if max_items:
        jobs = jobs[:max_items]
    log.info("%d pages to fetch (%d already saved)", len(jobs), len(store))
    counts: Counter = Counter()

    def work(job):
        kind, ident, url = job
        r = fetcher.get(url)
        if r is None:
            return None
        soup = make_soup(r.text)
        rec = parse_listing(url, ident, soup) if kind == "listing" else parse_project(url, ident, soup)
        store.write(rec)
        return kind

    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [ex.submit(work, j) for j in jobs]
        with tqdm(total=len(futures), desc="Wasalt details", unit="page") as bar:
            for f in as_completed(futures):
                try:
                    kind = f.result()
                    if kind:
                        counts[kind] += 1
                except BlockedError:
                    raise
                except Exception:  # one bad page must not kill a long run
                    log.exception("Parse error")
                bar.update(1)
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
        store.close()
    return counts


# ---- Phase 3: blog ----------------------------------------------------------------------------
def scrape_blog(fetcher: Fetcher, out_path: Path) -> int:
    store = JsonlStore(out_path)
    fields = "id,date,link,title,content,excerpt"
    first = fetcher.get(f"{BLOG_API}?per_page=100&page=1&_fields={fields}")
    if first is None:
        log.warning("Blog API not reachable or disallowed; skipping blog")
        store.close()
        return 0
    total_pages = int(first.headers.get("X-WP-TotalPages", "1"))
    saved = 0
    for page in tqdm(range(1, total_pages + 1), desc="Wasalt blog", unit="page"):
        r = first if page == 1 else fetcher.get(f"{BLOG_API}?per_page=100&page={page}&_fields={fields}")
        if r is None:
            continue
        for post in r.json():
            key = f"wasalt:blog:{post['id']}"
            if key in store:
                continue
            content = BeautifulSoup(post.get("content", {}).get("rendered", ""), "lxml")
            store.write({
                "key": key,
                "source": "wasalt",
                "doc_type": "blog",
                "id": str(post["id"]),
                "url": post.get("link"),
                "title": html_lib.unescape(BeautifulSoup(post.get("title", {}).get("rendered", ""), "lxml").get_text()),
                "date": post.get("date"),
                "text": "\n".join(text_lines(content))[:20000],
                "scraped_at": now_iso(),
            })
            saved += 1
    store.close()
    return saved
