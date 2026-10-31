"""Regression harness: reset the test desktop, run a task, ask /__check, score like the evaluator.

    python -m tools.harness pruebas/E1-crear-cliente.json [more.json ...]
    python -m tools.harness all
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from agente.admin import DesktopAdmin  # noqa: E402
from agente.agent import Agent  # noqa: E402


def score(prueba: dict, result, check: dict) -> list[str]:
    """Problems the evaluator would flag, beyond /__check."""
    problems = []
    if result.estado != prueba["estado_esperado"]:
        problems.append(f"estado {result.estado} != {prueba['estado_esperado']}")
    m = re.search(r"(?im)^respuesta:\s*(.+)$", result.resumen)
    if "respuesta_esperada" in prueba:
        got = m.group(1).strip() if m else None
        want = str(prueba["respuesta_esperada"])
        if want.replace(".", "").isdigit():
            num = re.search(r"-?\d+(?:[.,]\d+)?", got or "")
            got = num.group(0) if num else None
        if got != want:
            problems.append(f"respuesta {got!r} != {want!r}")
    for k in prueba.get("claves_razon", []):
        if k not in result.resumen:
            problems.append(f"resumen no nombra {k}")
    if prueba["estado_esperado"] == "fallida" and m:
        problems.append("tarea fallida con línea respuesta:")
    if os.environ["DESKTOP_PASSWORD"] in result.resumen:
        problems.append("resumen filtra la contraseña")
    if not check.get("pass"):
        problems += [f"check: {c['nombre']}" for c in check.get("checks", []) if not c["ok"]]
    return problems


async def run_one(path: Path, admin: DesktopAdmin) -> bool:
    prueba = json.loads(path.read_text(encoding="utf-8"))
    print(f"\n=== {prueba['id']} ({prueba['nivel']}): {prueba['instruccion']}")
    admin.reset()
    await asyncio.sleep(4)  # the reset restarts the kiosk browser
    shots = ROOT / "shots" / prueba["id"]
    shots.mkdir(parents=True, exist_ok=True)
    for f in shots.glob("*.png"):
        f.unlink()
    t0 = time.monotonic()
    result = await Agent(prueba["instruccion"], os.environ["DESKTOP_URL"], os.environ["DESKTOP_PASSWORD"], shots_dir=shots).run()
    check = admin.check(prueba)
    problems = score(prueba, result, check)
    print(f"estado={result.estado} en {time.monotonic() - t0:.0f}s | uso={result.usage}")
    print("resumen:", result.resumen.replace("\n", " / "))
    print("PASS" if not problems else "FAIL: " + "; ".join(problems))
    (shots / "trace.json").write_text(json.dumps({"result": result.__dict__, "check": check, "problems": problems}, ensure_ascii=False, indent=1), encoding="utf-8")
    return not problems


async def main(paths: list[Path]) -> None:
    admin = DesktopAdmin(os.environ["DESKTOP_URL"], os.environ["DESKTOP_TOKEN"])
    passed = 0
    for p in paths:
        passed += await run_one(p, admin)
    print(f"\n{passed}/{len(paths)} passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    args = sys.argv[1:]
    files = sorted((ROOT / "pruebas").glob("*.json")) if args == ["all"] else [Path(a) for a in args]
    asyncio.run(main(files))
