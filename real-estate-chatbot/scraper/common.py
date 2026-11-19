"""Shared helpers: polite HTTP fetching, robots.txt, rate limiting, resumable JSONL storage, text cleanup."""
from __future__ import annotations

import gzip
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("scraper")

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; RealEstateChatbotBot/1.0; technical-assignment; polite crawler)"
)


class BlockedError(RuntimeError):
    """The site keeps refusing requests (rate-limited or blocked). Stop and resume later."""


class RateLimiter:
    """Global cap on requests per second, shared across threads, with light jitter."""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.01)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval * random.uniform(0.8, 1.2)
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


class Fetcher:
    """requests.Session with retries, robots.txt checks (per host), rate limiting and block detection."""

    def __init__(
        self,
        rps: float = 1.0,
        user_agent: str = DEFAULT_UA,
        timeout: int = 30,
        max_consecutive_failures: int = 25,
    ):
        self.session = requests.Session()
        retry = Retry(
            total=4,
            connect=3,
            read=3,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            }
        )
        self.user_agent = user_agent
        self.timeout = timeout
        self.limiter = RateLimiter(rps)
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._robots_lock = threading.Lock()
        self._fail_lock = threading.Lock()
        self._consecutive_failures = 0
        self.max_consecutive_failures = max_consecutive_failures

    # ---- robots.txt -------------------------------------------------------------------------
    def _robots_for(self, url: str) -> robotparser.RobotFileParser:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
        rp = robotparser.RobotFileParser()
        try:
            self.limiter.wait()
            r = self.session.get(origin + "/robots.txt", timeout=self.timeout)
            if r.status_code in (401, 403):
                rp.disallow_all = True
                rp.modified()
                log.warning("robots.txt for %s returned %s: treating site as disallowed", origin, r.status_code)
            elif r.status_code == 200:
                rp.parse(r.text.splitlines())
                log.info("Loaded robots.txt for %s", origin)
            else:
                rp.parse([])  # no robots.txt -> everything allowed
        except requests.RequestException as exc:
            log.warning("Could not load robots.txt for %s (%s); assuming allowed", origin, exc)
            rp.parse([])
        with self._robots_lock:
            self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.user_agent, url)

    def sitemaps(self, url: str) -> list[str]:
        return list(self._robots_for(url).site_maps() or [])

    # ---- fetching ---------------------------------------------------------------------------
    def _failure(self, url: str, reason: str) -> None:
        with self._fail_lock:
            self._consecutive_failures += 1
            n = self._consecutive_failures
        log.warning("Failed %s (%s) [%d consecutive]", url, reason, n)
        if n >= self.max_consecutive_failures:
            raise BlockedError(
                f"{n} consecutive failed requests (last: {reason}). The site may be rate-limiting or "
                "blocking us. Wait a while, then re-run the same command: it resumes where it stopped. "
                "Consider a lower --rps."
            )

    def _success(self) -> None:
        with self._fail_lock:
            self._consecutive_failures = 0

    def get(self, url: str) -> requests.Response | None:
        if not self.allowed(url):
            log.info("robots.txt disallows %s, skipping", url)
            return None
        self.limiter.wait()
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            self._failure(url, type(exc).__name__)
            return None
        if r.status_code == 200:
            self._success()
            return r
        if r.status_code in (404, 410):
            self._success()  # missing pages are normal, not a sign of blocking
            log.debug("Not found: %s", url)
            return None
        self._failure(url, f"HTTP {r.status_code}")
        return None


# ---- storage ----------------------------------------------------------------------------------
class JsonlStore:
    """Append-only JSONL file. Remembers which keys are already saved so runs can resume."""

    def __init__(self, path: Path | str, key_field: str = "key"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_field = key_field
        self.done: set[str] = set()
        self._lock = threading.Lock()
        needs_newline = False
        if self.path.exists() and self.path.stat().st_size > 0:
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.done.add(json.loads(line)[key_field])
                    except (json.JSONDecodeError, KeyError):
                        continue  # half-written last line from an interrupted run
            with self.path.open("rb") as fh:
                fh.seek(-1, 2)
                needs_newline = fh.read(1) != b"\n"
        self._fh = self.path.open("a", encoding="utf-8")
        if needs_newline:
            self._fh.write("\n")

    def __contains__(self, key: str) -> bool:
        return key in self.done

    def __len__(self) -> int:
        return len(self.done)

    def write(self, record: dict) -> None:
        with self._lock:
            key = record[self.key_field]
            if key in self.done:
                return
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
            self.done.add(key)

    def close(self) -> None:
        self._fh.close()


# ---- parsing helpers --------------------------------------------------------------------------
NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "header", "footer", "nav", "template")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_soup(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def strip_noise(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    return soup


def text_lines(node) -> list[str]:
    """Visible text as non-empty, whitespace-normalised lines, with consecutive duplicates removed."""
    if node is None:
        return []
    out: list[str] = []
    for raw in node.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return out


def normalize_url(url: str, keep_query: bool = False) -> str:
    p = urlparse(url)
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query if keep_query else "", ""))


def extract_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "whatsapp:")):
            continue
        links.append(urljoin(base_url, href))
    return links


def meta_content(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    return tag.get("content", "").strip() or None if tag else None


def iter_sitemap_urls(fetcher: Fetcher, sitemap_url: str, _depth: int = 0, _seen: set | None = None):
    """Yield page URLs from a sitemap or sitemap index (handles .gz and nesting)."""
    _seen = _seen if _seen is not None else set()
    if sitemap_url in _seen or _depth > 4:
        return
    _seen.add(sitemap_url)
    r = fetcher.get(sitemap_url)
    if r is None:
        return
    content = r.content
    if sitemap_url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError:
            return
    soup = BeautifulSoup(content, "xml")
    if soup.find("sitemapindex"):
        for loc in soup.select("sitemap > loc"):
            yield from iter_sitemap_urls(fetcher, loc.get_text(strip=True), _depth + 1, _seen)
    else:
        for loc in soup.select("url > loc"):
            yield loc.get_text(strip=True)


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
