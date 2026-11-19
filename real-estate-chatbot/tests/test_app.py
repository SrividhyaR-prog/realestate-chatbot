"""Run with: pytest -q  (no network or API key needed: the model is mocked)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import config, llm, main
from app.search import parse_filters


@pytest.fixture()
def client(monkeypatch):
    with TestClient(main.app) as c:
        yield c


def events(resp):
    return [json.loads(l) for l in resp.text.splitlines() if l.strip()]


def test_parse_filters():
    f = parse_filters("3 bedroom apartments in Riyadh under 1.5M SAR")
    assert (f.city, f.property_type, f.bedrooms, f.max_price) == ("Riyadh", "apartment", 3, 1_500_000)
    f = parse_filters("villas between 500k and 700k in jeddah for rent")
    assert (f.city, f.property_type, f.min_price, f.max_price, f.purpose) == ("Jeddah", "villa", 500_000, 700_000, "rent")
    assert parse_filters("DarGlobal projects in Oman").source == "darglobal"


def test_health_and_stats(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["documents"] > 0
    s = client.get("/api/stats").json()
    assert s["records"]["darglobal project"] >= 1


def test_chat_streams_answer_with_sources(client, monkeypatch):
    captured = {}

    async def fake_stream(messages):
        captured["messages"] = messages
        for piece in ["Here are ", "some options [1]."]:
            yield piece

    monkeypatch.setattr(main, "stream_chat", fake_stream)
    r = client.post("/api/chat", json={"message": "3 bedroom apartments in Riyadh under 1M"})
    ev = events(r)
    assert ev[0]["type"] == "sources" and ev[0]["items"]
    assert all(i["price_sar"] <= 1_000_000 for i in ev[0]["items"])
    assert "".join(e["text"] for e in ev if e["type"] == "delta") == "Here are some options [1]."
    assert ev[-1]["type"] == "done"
    assert "CONTEXT:" in captured["messages"][-1]["content"]


def test_fallback_when_no_api_key(client, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    ev = events(client.post("/api/chat", json={"message": "Which DarGlobal projects are in Dubai?"}))
    text = "".join(e["text"] for e in ev if e["type"] == "delta")
    assert text == ""
    assert any(e["type"] == "notice" and "no API key" in e["message"] for e in ev)
    source_events = [e for e in ev if e["type"] == "sources"]
    assert source_events[-1]["items"] == []


def test_follow_up_inherits_filters(client, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    hist = [{"role": "user", "content": "villas for sale in Riyadh"}, {"role": "assistant", "content": "..."}]
    ev = events(client.post("/api/chat", json={"message": "what about Jeddah?", "history": hist}))
    items = ev[0]["items"]
    assert items and all("Jeddah" in (i["location"] or "") for i in items)
    assert ev[0]["filters"]["property_type"] == "villa"


def test_validation_and_rate_limit(client, monkeypatch):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    main._hits.clear()
    codes = [client.post("/api/chat", json={"message": "villas"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    main._hits.clear()
