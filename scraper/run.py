"""Command-line entry point.

    python -m scraper.run darglobal                 # whole DarGlobal site
    python -m scraper.run wasalt --max-items 50     # quick Wasalt test run
    python -m scraper.run wasalt                    # all of Wasalt (long; resumable)
    python -m scraper.run all                       # both
    python -m scraper.run probe <url> [<url> ...]   # save raw HTML + parsed record, for debugging
    python -m scraper.run stats                     # what was collected, field fill rates
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from . import darglobal, wasalt
from .common import DEFAULT_UA, BlockedError, Fetcher, make_soup, setup_logging, strip_noise

log = logging.getLogger("scraper")


def _paths(out_dir: Path) -> dict[str, Path]:
    return {
        "darglobal": out_dir / "darglobal.jsonl",
        "wasalt": out_dir / "wasalt.jsonl",
        "wasalt_blog": out_dir / "wasalt_blog.jsonl",
        "wasalt_state": out_dir / "wasalt_discovery.json",
    }


def run_darglobal(args, fetcher: Fetcher) -> None:
    counts = darglobal.crawl(fetcher, _paths(args.out_dir)["darglobal"], max_pages=args.max_pages)
    log.info("DarGlobal saved this run: %s", dict(counts) or "nothing new")


def run_wasalt(args, fetcher: Fetcher) -> None:
    paths = _paths(args.out_dir)
    if args.rediscover and paths["wasalt_state"].exists():
        paths["wasalt_state"].unlink()
    if not args.skip_listings:
        state = wasalt.discover(
            fetcher, paths["wasalt_state"], use_sitemaps=not args.no_sitemap,
            force_crawl=args.force_crawl, max_index_pages=args.max_index_pages,
        )
        counts = wasalt.scrape_details(fetcher, state, paths["wasalt"], workers=args.workers,
                                       max_items=args.max_items)
        log.info("Wasalt saved this run: %s", dict(counts) or "nothing new")
    if not args.skip_blog:
        n = wasalt.scrape_blog(fetcher, paths["wasalt_blog"])
        log.info("Wasalt blog posts saved this run: %d", n)


def run_probe(args, fetcher: Fetcher) -> None:
    probe_dir = args.out_dir / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    for url in args.urls:
        r = fetcher.get(url)
        if r is None:
            print(f"\n=== {url}\nFAILED (see log above)")
            continue
        name = re.sub(r"[^A-Za-z0-9]+", "_", urlparse(url).netloc + urlparse(url).path).strip("_")[:120]
        (probe_dir / f"{name}.html").write_text(r.text, encoding="utf-8")
        soup = make_soup(r.text)
        info = {
            "status": r.status_code, "final_url": r.url, "bytes": len(r.content),
            "has___NEXT_DATA__": soup.find("script", id="__NEXT_DATA__") is not None,
            "json_ld_blocks": len(soup.find_all("script", type="application/ld+json")),
        }
        c = wasalt.classify(url)
        if "darglobal" in url:
            rec = darglobal.parse_page(darglobal.normalize_url(r.url), strip_noise(soup), set())
        elif c and c[0] == "listing":
            rec = wasalt.parse_listing(c[2], c[1], soup)
        elif c and c[0] == "project":
            rec = wasalt.parse_project(c[2], c[1], soup)
        else:
            rec = None
        print(f"\n=== {url}\n{json.dumps(info, indent=2)}")
        if rec:
            rec = {**rec, "text": (rec.get("text") or "")[:600] + " ..."}
            print(json.dumps(rec, indent=2, ensure_ascii=False))
        print(f"Raw HTML saved to {probe_dir / (name + '.html')}")


def run_stats(args) -> None:
    for name, path in _paths(args.out_dir).items():
        if path.suffix != ".jsonl" or not path.exists():
            continue
        records = [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]
        print(f"\n{path} : {len(records)} records")
        for k, v in Counter(r["doc_type"] for r in records).most_common():
            print(f"  {k:10s} {v}")
        listings = [r for r in records if r["doc_type"] == "listing"]
        if listings:
            print("  Listing field fill rates:")
            for f in ("price_sar", "property_type", "city", "district", "bedrooms", "bathrooms",
                      "land_area_sqm", "description", "amenities"):
                filled = sum(1 for r in listings if r.get(f) not in (None, "", [], {}))
                print(f"    {f:18s} {100 * filled / len(listings):5.1f}%")
            print("  By city (top 10):", Counter(r.get("city") for r in listings).most_common(10))
            print("  By purpose:", dict(Counter(r.get("purpose") for r in listings)))
        projects = [r for r in records if r["doc_type"] == "project" and r["source"] == "darglobal"]
        if projects:
            with_facts = sum(1 for r in projects if r.get("facts"))
            print(f"  DarGlobal projects with key facts: {with_facts}/{len(projects)}")


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out-dir", type=Path, default=Path("data/raw"))
    common.add_argument("--rps", type=float, default=1.0, help="max requests per second (default 1.0)")
    common.add_argument("--user-agent", default=DEFAULT_UA)
    common.add_argument("-v", "--verbose", action="store_true")

    ap = argparse.ArgumentParser(prog="python -m scraper.run", description="DarGlobal + Wasalt scrapers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("darglobal", parents=[common], help="crawl the whole DarGlobal site")
    d.add_argument("--max-pages", type=int, default=2000)

    w = sub.add_parser("wasalt", parents=[common], help="scrape all Wasalt listings, projects and blog")
    w.add_argument("--workers", type=int, default=2, help="parallel connections (rate still capped by --rps)")
    w.add_argument("--max-items", type=int, help="only fetch this many detail pages (for test runs)")
    w.add_argument("--max-index-pages", type=int, help="cap discovery pages (for test runs)")
    w.add_argument("--rediscover", action="store_true", help="throw away saved discovery state")
    w.add_argument("--force-crawl", action="store_true", help="crawl index pages even if sitemaps worked")
    w.add_argument("--no-sitemap", action="store_true")
    w.add_argument("--skip-blog", action="store_true")
    w.add_argument("--skip-listings", action="store_true")

    a = sub.add_parser("all", parents=[common], help="DarGlobal, then Wasalt (default settings)")
    p = sub.add_parser("probe", parents=[common], help="fetch URLs, save raw HTML, print parsed record")
    p.add_argument("urls", nargs="+")
    sub.add_parser("stats", parents=[common], help="summarise collected data")

    args = ap.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.verbose, args.out_dir / "scrape.log")
    if args.cmd == "stats":
        run_stats(args)
        return 0

    fetcher = Fetcher(rps=args.rps, user_agent=args.user_agent)
    try:
        if args.cmd == "darglobal":
            run_darglobal(args, fetcher)
        elif args.cmd == "wasalt":
            run_wasalt(args, fetcher)
        elif args.cmd == "all":
            args.max_pages = 2000
            run_darglobal(args, fetcher)
            for k, v in dict(workers=2, max_items=None, max_index_pages=None, rediscover=False,
                             force_crawl=False, no_sitemap=False, skip_blog=False, skip_listings=False).items():
                setattr(args, k, v)
            run_wasalt(args, fetcher)
        elif args.cmd == "probe":
            run_probe(args, fetcher)
    except BlockedError as exc:
        log.error(str(exc))
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress is saved; re-run the same command to resume.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
