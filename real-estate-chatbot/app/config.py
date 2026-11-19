"""Settings, read from environment variables (see .env.example)."""
import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _list(name: str) -> list[str]:
    return [m.strip() for m in os.getenv(name, "").split(",") if m.strip()]


DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data" / "raw"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# openrouter/free routes each request to a currently available free model, so the app keeps
# working when individual free models are retired. Pin a specific ":free" model here if preferred.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
OPENROUTER_FALLBACK_MODELS = _list("OPENROUTER_FALLBACK_MODELS")
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "900"))

APP_URL = os.getenv("APP_URL", "http://localhost:7860")
APP_TITLE = os.getenv("APP_TITLE", "DarGlobal & Wasalt Property Assistant")

# Protects the shared free OpenRouter quota on a public URL.
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))

TOP_K = int(os.getenv("TOP_K", "8"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "9000"))
