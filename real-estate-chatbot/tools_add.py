"""Upsert manually collected records into a JSONL file (same schema as the scraper output).
Usage: python3 tools_add.py [path] < records.json   (default path: data/raw/darglobal.jsonl)"""
import json, sys
from pathlib import Path
from datetime import datetime, timezone
p = Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/darglobal.jsonl")
p.parent.mkdir(parents=True, exist_ok=True)
existing = {}
if p.exists():
    for l in p.open(encoding="utf-8"):
        if l.strip():
            r = json.loads(l); existing[r["key"]] = r
for r in json.load(sys.stdin):
    r.setdefault("source", "wasalt" if "wasalt" in p.name else "darglobal")
    r.setdefault("doc_type", "project")
    r["key"] = r.get("key") or r["url"]
    r.setdefault("scraped_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    r.setdefault("collected_via", "web_fetch")
    existing[r["key"]] = r   # new/detailed version replaces the old one
with p.open("w", encoding="utf-8") as fh:
    for r in existing.values():
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"{p}: {len(existing)} records")
