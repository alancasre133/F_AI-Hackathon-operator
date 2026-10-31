"""Test the deployed entrypoint like the evaluator does.

    python -m tools.remote pruebas/E5-homonimos.json [more.json ...]

Reads PROD_URL and PROD_TEAM_TOKEN from .env, resets the test desktop, POSTs the task,
polls every 5 s, then asks /__check and scores the result with the harness rules.
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from agente.admin import DesktopAdmin  # noqa: E402
from tools.harness import score  # noqa: E402


class Result:
    def __init__(self, estado: str, resumen: str):
        self.estado, self.resumen = estado, resumen


def run(path: Path, url: str, headers: dict, admin: DesktopAdmin) -> bool:
    prueba = json.loads(path.read_text(encoding="utf-8"))
    print(f"\n=== {prueba['id']}: {prueba['instruccion']}")
    admin.reset()
    time.sleep(4)
    body = {"tarea": prueba["instruccion"], "escritorio": {"url": os.environ["DESKTOP_URL"], "password": os.environ["DESKTOP_PASSWORD"]}}
    t0 = time.time()
    r = httpx.post(f"{url}/tasks", headers=headers, json=body, timeout=10)
    print(f"POST /tasks -> {r.status_code} in {time.time() - t0:.2f}s {r.text[:80]}")
    r.raise_for_status()
    tid = r.json()["task_id"]
    while time.time() - t0 < 300:
        time.sleep(5)
        g = httpx.get(f"{url}/tasks/{tid}", headers=headers, timeout=10).json()
        if g["estado"] != "en_curso":
            break
    print(f"estado={g['estado']} after {time.time() - t0:.0f}s")
    print("resumen:", g["resumen"].replace("\n", " / "))
    problems = score(prueba, Result(g["estado"], g["resumen"]), admin.check(prueba))
    print("PASS" if not problems else "FAIL: " + "; ".join(problems))
    return not problems


def main() -> None:
    url = os.environ["PROD_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['PROD_TEAM_TOKEN']}"}
    probe = httpx.get(f"{url}/tasks/probe", headers=headers, timeout=10)
    print(f"auth probe: {probe.status_code} (404 = token accepted, 401 = token mismatch or TEAM_TOKEN unset on Railway)")
    if probe.status_code != 404:
        raise SystemExit(1)
    admin = DesktopAdmin(os.environ["DESKTOP_URL"], os.environ["DESKTOP_TOKEN"])
    files = sorted((ROOT / "pruebas").glob("*.json")) if sys.argv[1:] == ["all"] else [Path(a) for a in sys.argv[1:]]
    passed = sum(run(p, url, headers, admin) for p in files)
    print(f"\n{passed}/{len(files)} passed")


if __name__ == "__main__":
    main()
