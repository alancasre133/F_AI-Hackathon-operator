"""Client for the test desktop's admin endpoints (/__reset, /__check). Development only."""
from urllib.parse import urlparse

import httpx


def base_url(ws_url: str) -> str:
    u = urlparse(ws_url)
    scheme = "https" if u.scheme == "wss" else "http"
    return f"{scheme}://{u.netloc}"


class DesktopAdmin:
    def __init__(self, ws_url: str, token: str):
        self.base = base_url(ws_url)
        self.headers = {"Authorization": f"Bearer {token}"}

    def health(self) -> dict:
        return httpx.get(f"{self.base}/health", timeout=15).json()

    def reset(self) -> dict:
        r = httpx.post(f"{self.base}/__reset", headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json() if r.content else {}

    def check(self, prueba: dict) -> dict:
        r = httpx.post(f"{self.base}/__check", headers=self.headers, json=prueba, timeout=60)
        r.raise_for_status()
        return r.json()
