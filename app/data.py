"""Turn raw scraped records (data/raw/*.jsonl) into searchable documents.

Works with both the scraper output and the manually collected records, which share one schema.
Each document carries normalised metadata used for filtering (city, type, price, bedrooms ...).
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("app.data")

CHUNK_CHARS = 1800

COUNTRY_BY_CITY = {
    "dubai": "UAE", "ras al khaimah": "UAE", "abu dhabi": "UAE",
    "riyadh": "Saudi Arabia", "jeddah": "Saudi Arabia", "taif": "Saudi Arabia", "dammam": "Saudi Arabia",
    "khobar": "Saudi Arabia", "makkah": "Saudi Arabia", "madinah": "Saudi Arabia", "jazan": "Saudi Arabia",
    "khamis mushayt": "Saudi Arabia", "abha": "Saudi Arabia", "tabuk": "Saudi Arabia",
    "doha": "Qatar", "simaisma": "Qatar", "muscat": "Oman", "london": "United Kingdom",
    "benahavís": "Spain", "benahavis": "Spain", "cortesin": "Spain",
}
CITY_ALIASES = {"at taif": "Taif", "al taif": "Taif", "rak": "Ras Al Khaimah", "jedda": "Jeddah", "mecca": "Makkah",
                "medina": "Madinah", "al khobar": "Khobar"}
TYPE_WORDS = ("penthouse", "apartment", "villa", "land", "plot", "floor", "building", "duplex", "townhouse",
              "studio", "mansion", "residence", "hotel", "office", "chalet", "farm")


@dataclass
class Document:
    id: str
    source: str            # darglobal | wasalt
    doc_type: str          # project | listing | press | partner | page | blog
    title: str
    url: str | None
    text: str              # what is searched and shown to the model
    meta: dict = field(default_factory=dict)

    def card(self) -> dict:
        """Compact view for the UI's source cards."""
        m = self.meta
        return {
            "id": self.id, "source": self.source, "doc_type": self.doc_type, "title": self.title, "url": self.url,
            "location": m.get("location"), "price_sar": m.get("price_sar"), "purpose": m.get("purpose"),
            "bedrooms": m.get("bedrooms"), "area_sqm": m.get("area_sqm"), "status": m.get("status"),
            "property_type": m.get("property_type"),
        }


# ---- helpers ----------------------------------------------------------------------------------
def _norm_city(city: str | None) -> str | None:
    if not city:
        return None
    c = re.sub(r"\s+", " ", city).strip()
    return CITY_ALIASES.get(c.lower(), c)


def _country_for(city: str | None, location: str | None, given: str | None) -> str | None:
    if given:
        return {"KSA": "Saudi Arabia", "United Arab Emirates": "UAE"}.get(given, given)
    for s in (city, location):
        if not s:
            continue
        low = s.lower()
        for key, country in COUNTRY_BY_CITY.items():
            if key in low:
                return country
        for name, country in (("uae", "UAE"), ("saudi", "Saudi Arabia"), ("ksa", "Saudi Arabia"),
                              ("qatar", "Qatar"), ("oman", "Oman"), ("spain", "Spain"),
                              ("united kingdom", "United Kingdom"), ("london", "United Kingdom")):
            if name in low:
                return country
    return None


def _ptype(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        low = t.lower()
        for w in TYPE_WORDS:
            if re.search(rf"\b{w}s?\b", low):
                return {"plot": "land", "residence": "apartment"}.get(w, w)
    return None


def _types(*texts: str | None) -> list[str]:
    """Every property type mentioned (a DarGlobal tower can hold apartments, duplexes and penthouses)."""
    found = []
    for t in texts:
        low = (t or "").lower()
        for w in TYPE_WORDS:
            if re.search(rf"\b{w}s?\b", low):
                norm = {"plot": "land", "residence": "apartment", "mansion": "villa"}.get(w, w)
                if norm not in found:
                    found.append(norm)
    return found


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.match(r"^\s*(\d+)", str(v))
    return int(m.group(1)) if m else None


def _chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, buf = [], ""
    for para in re.split(r"\n+", text):
        if len(buf) + len(para) + 1 > size and buf:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{para}" if buf else para
        while len(buf) > size:  # one huge paragraph
            out.append(buf[:size])
            buf = buf[size:]
    if buf:
        out.append(buf)
    return out


def _lines(label: str, items) -> str:
    items = [str(i) for i in (items or []) if i]
    return f"{label}: " + "; ".join(items) if items else ""


# ---- builders ---------------------------------------------------------------------------------
def _darglobal_docs(r: dict) -> list[Document]:
    doc_type = r.get("doc_type", "page")
    title = r.get("title") or r.get("h1") or r["url"]
    title = re.sub(r"\s*\|\s*DarGlobal\s*$", "", title, flags=re.I)
    facts = r.get("facts") or {}
    location = r.get("location") or facts.get("Location")
    city = _norm_city(r.get("city"))
    status = facts.get("Status") or facts.get("Stage")
    meta = {
        "location": location, "city": city, "country": _country_for(city, location, r.get("country")),
        "status": status, "brand_partner": r.get("brand_partner"),
        "property_type": _ptype(facts.get("Property Type"), title) if doc_type == "project" else None,
        "types": _types(*facts.values(), title, (r.get("description") or "")[:400]) if doc_type == "project" else [],
        "date": r.get("date"),
    }
    head = [f"{title} (DarGlobal {doc_type})"]
    if location:
        head.append(f"Location: {location}" + (f" ({meta['country']})" if meta["country"] else ""))
    if r.get("brand_partner"):
        head.append(f"Brand partner: {r['brand_partner']}")
    head += [f"{k}: {v}" for k, v in facts.items()]
    if r.get("date"):
        head.append(f"Date: {r['date']}")
    if doc_type == "project":
        head.append("Prices: not published by DarGlobal; available on request from the sales team.")
    body = [r.get("description") or "", _lines("Highlights", r.get("highlights")),
            _lines("Amenities", r.get("amenities"))]
    body += [f"Q: {f['q']} A: {f['a']}" for f in r.get("faqs") or []]
    if r.get("text") and not r.get("description"):
        body.append(r["text"])
    header = "\n".join(head)
    parts = _chunks("\n".join(b for b in body if b)) or [""]
    return [
        Document(id=f"{r['key']}#{i}", source="darglobal", doc_type=doc_type, title=title, url=r.get("url"),
                 text=(header + "\n" + p).strip() if i == 0 else f"{title} (continued)\n{p}", meta=meta)
        for i, p in enumerate(parts)
    ]


def _wasalt_listing(r: dict) -> Document:
    city = _norm_city(r.get("city"))
    area = r.get("built_up_area_sqm") or r.get("land_area_sqm")
    purpose = r.get("purpose") or "sale"
    meta = {
        "location": r.get("location"), "city": city, "district": r.get("district"),
        "country": "Saudi Arabia", "property_type": (r.get("property_type") or "").lower() or _ptype(r.get("title")),
        "purpose": purpose, "price_sar": _num(r.get("price_sar")), "bedrooms": _num(r.get("bedrooms")),
        "area_sqm": area, "status": None,
    }
    meta["types"] = [meta["property_type"]] if meta["property_type"] else []
    lines = [f"{r.get('title')} (Wasalt listing, for {purpose.replace('_', ' ')})"]
    if r.get("location"):
        lines.append(f"Location: {r['location']}, Saudi Arabia")
    if meta["price_sar"]:
        per = f" ({r['price_per_sqm_sar']:,} SAR/sqm)" if r.get("price_per_sqm_sar") else ""
        period = f" per {r['price_period']}" if r.get("price_period") else ""
        lines.append(f"Price: {meta['price_sar']:,} SAR{period}{per}")
    else:
        lines.append("Price: not captured")
    for label, key in (("Bedrooms", "bedrooms"), ("Bathrooms", "bathrooms"), ("Living rooms", "living_rooms"),
                       ("Guest rooms", "guest_rooms"), ("Floors", "floors"), ("Land area (sqm)", "land_area_sqm"),
                       ("Built-up area (sqm)", "built_up_area_sqm"), ("Building year", "building_year"),
                       ("Furnishing", "furnishing")):
        if r.get(key) not in (None, ""):
            lines.append(f"{label}: {r[key]}")
    lines += [f"{k}: {v}" for k, v in (r.get("attributes") or {}).items()]
    lines += [_lines("Amenities", r.get("amenities")), _lines("Fixtures", r.get("fixtures"))]
    if r.get("description"):
        lines.append(r["description"][:1500])
    return Document(id=r["key"], source="wasalt", doc_type="listing", title=r.get("title") or "Wasalt listing",
                    url=r.get("url"), text="\n".join(l for l in lines if l), meta=meta)


def _wasalt_project(r: dict) -> Document:
    city = _norm_city(r.get("city"))
    types = r.get("available_types") or {}
    meta = {"location": r.get("location"), "city": city, "district": r.get("district"), "country": "Saudi Arabia",
            "status": r.get("status"), "property_type": "project",
            "types": [t.lower() for t in types]}
    lines = [f"{r.get('title')} (Wasalt project)", f"Location: {r.get('location')}, Saudi Arabia" if r.get("location") else "",
             f"Status: {r['status']}" if r.get("status") else "", f"Units: {r['units']}" if r.get("units") else "",
             _lines("Unit types", [f"{k} ({v})" for k, v in types.items()]),
             r.get("description") or r.get("text") or ""]
    return Document(id=r["key"], source="wasalt", doc_type="project", title=r.get("title") or "Wasalt project",
                    url=r.get("url"), text="\n".join(l for l in lines if l), meta=meta)


def _wasalt_blog(r: dict) -> list[Document]:
    title = r.get("title") or "Wasalt blog post"
    parts = _chunks(r.get("text") or "")[:6]
    return [Document(id=f"{r['key']}#{i}", source="wasalt", doc_type="blog", title=title, url=r.get("url"),
                     text=f"{title} (Wasalt blog, {(r.get('date') or '')[:10]})\n{p}", meta={"date": r.get("date")})
            for i, p in enumerate(parts)]


# ---- public API -------------------------------------------------------------------------------
def load_documents(data_dir: Path) -> list[Document]:
    docs: list[Document] = []
    files = sorted(Path(data_dir).glob("*.jsonl"))
    if not files:
        log.warning("No data files found in %s", data_dir)
    for path in files:
        n_before = len(docs)
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                r.setdefault("key", r.get("url"))
                src, dt = r.get("source"), r.get("doc_type")
                if src == "darglobal":
                    docs += _darglobal_docs(r)
                elif dt == "listing":
                    docs.append(_wasalt_listing(r))
                elif dt == "project":
                    docs.append(_wasalt_project(r))
                elif dt == "blog":
                    docs += _wasalt_blog(r)
        log.info("Loaded %d documents from %s", len(docs) - n_before, path.name)
    return docs


def dataset_overview(docs: list[Document]) -> dict:
    """Counts used in the system prompt and the /api/stats endpoint (one entry per record, not per chunk)."""
    seen, by_kind, cities, countries = set(), Counter(), Counter(), Counter()
    prices = []
    for d in docs:
        rid = d.id.split("#")[0]
        if rid in seen:
            continue
        seen.add(rid)
        by_kind[f"{d.source} {d.doc_type}"] += 1
        if d.source == "wasalt" and d.doc_type == "listing":
            if d.meta.get("city"):
                cities[d.meta["city"]] += 1
            if d.meta.get("price_sar"):
                prices.append(d.meta["price_sar"])
        if d.source == "darglobal" and d.doc_type == "project" and d.meta.get("country"):
            countries[d.meta["country"]] += 1
    return {
        "records": dict(by_kind),
        "wasalt_listings_by_city": dict(cities.most_common()),
        "darglobal_projects_by_country": dict(countries.most_common()),
        "wasalt_price_range_sar": [min(prices), max(prices)] if prices else None,
    }
