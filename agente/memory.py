"""Per-app memory: menu routes that worked, keyed by an app fingerprint (its title bar text).

The evaluation runs several tasks on the same unseen app, so what the agent learns
navigating it in one task makes the next task faster.
"""
import json
import os
import re
import threading
from pathlib import Path

from .guard import fold

PATH = Path(os.environ.get("MEMORY_PATH", Path(__file__).resolve().parent.parent / "data" / "memory.json"))
_lock = threading.Lock()


def fingerprint(screen) -> str:
    """Top-most OCR line (title bar), normalised; good enough to tell apps apart."""
    top = sorted((b for b in screen.ocr if b.y2 < 40), key=lambda b: b.x1)
    text = " ".join(b.text for b in top[:3]) or "desconocida"
    text = re.sub(r"\d{1,2}/\d{1,2}/\d{4}.*", "", text)  # drop the date/user part
    return fold(text)[:60].strip()


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def routes(app: str) -> list[list[str]]:
    return _load().get(app, {}).get("rutas", [])


def remember_route(app: str, route: list[str]) -> None:
    with _lock:
        data = _load()
        known = data.setdefault(app, {}).setdefault("rutas", [])
        if route not in known:
            known.append(route)
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def describe(app: str) -> str:
    rs = routes(app)
    if not rs:
        return ""
    return "Rutas de menú que ya funcionaron en esta app: " + " | ".join(" > ".join(r) for r in rs)
