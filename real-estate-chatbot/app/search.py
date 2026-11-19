"""Retrieval: BM25 keyword ranking combined with structured filters parsed from the question.

"3 bedroom apartments in Riyadh under 1.5M" -> filters {bedrooms: 3, property_type: apartment,
city: Riyadh, max_price: 1_500_000} applied to listing metadata, then ranked by BM25.
If the filters match nothing, they are relaxed step by step so the user still gets the closest results.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi

from .data import Document

STOPWORDS = set("""a an the and or of in on at to for from with by is are was were be been being it its this that these
those what which who whom whose how when where why can could would should do does did i me my you your we our they
their there here any some show find give list tell about please want looking need get much many under over than
between above below less more near me available""".split())

TYPE_SYNONYMS = {
    "apartment": ("apartment", "apartments", "flat", "flats", "condo"),
    "villa": ("villa", "villas", "house", "houses", "mansion", "mansions"),
    "land": ("land", "plot", "plots", "lands"),
    "penthouse": ("penthouse", "penthouses"),
    "duplex": ("duplex", "duplexes"),
    "floor": ("floor", "floors"),
    "building": ("building", "buildings"),
    "studio": ("studio", "studios"),
    "townhouse": ("townhouse", "townhouses"),
}
CITY_SYNONYMS = {
    "Riyadh": ("riyadh",), "Jeddah": ("jeddah", "jedda", "jiddah"), "Taif": ("taif",), "Makkah": ("makkah", "mecca"),
    "Madinah": ("madinah", "medina"), "Dammam": ("dammam",), "Khobar": ("khobar",), "Dubai": ("dubai",),
    "Ras Al Khaimah": ("ras al khaimah", "rak", "al marjan"), "Muscat": ("muscat",), "Doha": ("doha",),
    "London": ("london",),
}
COUNTRY_SYNONYMS = {
    "UAE": ("uae", "emirates", "united arab emirates"), "Saudi Arabia": ("saudi", "ksa", "kingdom"),
    "Qatar": ("qatar",), "Oman": ("oman",), "Spain": ("spain", "marbella", "spanish"),
    "United Kingdom": ("uk", "united kingdom", "england", "britain"),
}
_AMOUNT = r"(\d+(?:[.,]\d+)*)\s*(k|m|mn|million|thousand|mil)?"


def _amount(num: str, unit: str | None) -> float:
    value = float(num.replace(",", "")) if num.count(",") != 1 or len(num.split(",")[1]) == 3 else float(num.replace(",", "."))
    unit = (unit or "").lower()
    if unit in ("k", "thousand"):
        value *= 1_000
    elif unit in ("m", "mn", "million", "mil"):
        value *= 1_000_000
    return value


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if t not in STOPWORDS and len(t) > 1 or t.isdigit()]


@dataclass
class Filters:
    source: str | None = None
    city: str | None = None
    country: str | None = None
    property_type: str | None = None
    purpose: str | None = None
    bedrooms: int | None = None
    min_price: float | None = None
    max_price: float | None = None
    sort: str | None = None          # price_asc | price_desc
    relaxed: list[str] = field(default_factory=list)

    def active(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, []) and k != "relaxed"}


def parse_filters(q: str) -> Filters:
    low = " " + q.lower() + " "
    f = Filters()
    if re.search(r"\bdar ?global\b", low):
        f.source = "darglobal"
    elif re.search(r"\bwasalt\b", low):
        f.source = "wasalt"
    for city, words in CITY_SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
            f.city = city
            break
    if not f.city:
        for country, words in COUNTRY_SYNONYMS.items():
            if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
                f.country = country
                break
    for ptype, words in TYPE_SYNONYMS.items():
        if any(re.search(rf"\b{w}\b", low) for w in words):
            f.property_type = ptype
            break
    if re.search(r"\b(rent|rental|renting|lease|leasing)\b", low):
        f.purpose = "rent"
    elif re.search(r"\b(buy|buying|purchase|for sale|sale)\b", low):
        f.purpose = "sale"
    if m := re.search(r"\b(\d+)\s*-?\s*(?:bed(?:room)?s?|br|bhk|bd)\b", low):
        f.bedrooms = int(m.group(1))
    if m := re.search(rf"\bbetween\s+(?:sar\s*)?{_AMOUNT}\s*(?:sar)?\s*(?:and|to|-)\s*(?:sar\s*)?{_AMOUNT}", low):
        f.min_price, f.max_price = _amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))
    else:
        if m := re.search(rf"\b(?:under|below|less than|up to|upto|max(?:imum)?|within|cheaper than|budget(?: of)?)\s+(?:sar\s*)?{_AMOUNT}", low):
            f.max_price = _amount(m.group(1), m.group(2))
        if m := re.search(rf"\b(?:over|above|more than|at least|min(?:imum)?|starting from)\s+(?:sar\s*)?{_AMOUNT}", low):
            f.min_price = _amount(m.group(1), m.group(2))
    # Small bare numbers ("under 5") are not prices: treat values below 10,000 SAR as noise.
    for attr in ("min_price", "max_price"):
        if getattr(f, attr) is not None and getattr(f, attr) < 10_000:
            setattr(f, attr, None)
    if re.search(r"\b(cheapest|lowest price|most affordable|affordable|budget|least expensive)\b", low):
        f.sort = "price_asc"
    elif re.search(r"\b(most expensive|priciest|highest price|luxurious|premium)\b", low):
        f.sort = "price_desc"
    return f


def _matches(d: Document, f: Filters) -> bool:
    m = d.meta
    if f.source and d.source != f.source:
        return False
    if f.city and (m.get("city") or "").lower() != f.city.lower() and f.city.lower() not in (m.get("location") or "").lower():
        return False
    if f.country and m.get("country") != f.country:
        return False
    if f.property_type:
        types = m.get("types") or []
        if d.doc_type not in ("listing", "project") or (f.property_type not in types and f.property_type not in d.title.lower()):
            return False
    if f.purpose and d.doc_type == "listing" and m.get("purpose") not in (f.purpose, None):
        return False
    if f.bedrooms is not None and m.get("bedrooms") != f.bedrooms:
        return False
    price = m.get("price_sar")
    if f.max_price is not None and (price is None or price > f.max_price):
        return False
    if f.min_price is not None and (price is None or price < f.min_price):
        return False
    return True


# Relaxation order: drop the most specific numeric constraints first, keep place/type longest.
RELAX_ORDER = ("bedrooms", "min_price", "max_price", "purpose", "property_type", "city", "country", "source")


class SearchIndex:
    def __init__(self, docs: list[Document]):
        self.docs = docs
        self.bm25 = BM25Okapi([tokenize(d.text) for d in docs]) if docs else None

    def search(self, query: str, k: int = 8, filters: Filters | None = None) -> tuple[list[Document], Filters]:
        if not self.docs:
            return [], Filters()
        f = filters or parse_filters(query)
        scores = self.bm25.get_scores(tokenize(query))

        candidates = [i for i, d in enumerate(self.docs) if _matches(d, f)]
        for attr in RELAX_ORDER:  # loosen filters until something matches
            if candidates or not f.active():
                break
            if getattr(f, attr) is not None:
                f.relaxed.append(f"{attr}={getattr(f, attr)}")
                setattr(f, attr, None)
                candidates = [i for i, d in enumerate(self.docs) if _matches(d, f)]
        if not candidates:
            candidates = list(range(len(self.docs)))

        has_filters = bool(f.active())
        if f.sort in ("price_asc", "price_desc"):
            priced = [i for i in candidates if self.docs[i].meta.get("price_sar")]
            if priced:
                candidates = sorted(priced, key=lambda i: self.docs[i].meta["price_sar"], reverse=f.sort == "price_desc")
                ranked = candidates
            else:
                ranked = sorted(candidates, key=lambda i: -scores[i])
        else:
            ranked = sorted(candidates, key=lambda i: -scores[i])
            if not has_filters:
                ranked = [i for i in ranked if scores[i] > 0] or ranked

        # One result per page: up to two of its best chunks are merged, so each source gets one citation number.
        merged: dict[str, Document] = {}
        counts: dict[str, int] = {}
        for i in ranked:
            d = self.docs[i]
            rid = d.id.split("#")[0]
            if rid in merged:
                if counts[rid] < 2:
                    merged[rid] = Document(id=rid, source=d.source, doc_type=d.doc_type, title=merged[rid].title,
                                           url=d.url, text=merged[rid].text + "\n" + d.text, meta=merged[rid].meta)
                    counts[rid] += 1
                continue
            if len(merged) >= k:
                continue
            merged[rid] = d
            counts[rid] = 1
        return list(merged.values()), f
