"""Dev CLI to drive the test desktop by hand.

    python -m tools.vnc do "shot"
    python -m tools.vnc do "click 430 125" "key esc" "fill 300 200 Calle 80 # 24-15"
    python -m tools.vnc reset
    python -m tools.vnc check pruebas/E1-crear-cliente.json

Steps: shot | click X Y | rclick X Y | dclick X Y | move X Y | hover X,Y X,Y ... |
       type TEXT | paste TEXT | key COMBO | fill X Y TEXT | scroll X Y up|down N | wait SECONDS
Every step waits for the screen to settle; the final frame goes to shots/last.png.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from agente.admin import DesktopAdmin
from agente.driver import Desktop

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "shots"


async def run_steps(steps: list[str]) -> None:
    t0 = time.monotonic()
    desk = await Desktop.connect(os.environ["DESKTOP_URL"], os.environ["DESKTOP_PASSWORD"])
    print(f"connected '{desk.rfb.name}' {desk.size[0]}x{desk.size[1]} in {time.monotonic() - t0:.2f}s")
    SHOTS.mkdir(exist_ok=True)
    try:
        await desk.wait_stable(no_change=0.5)
        for i, step in enumerate(steps, 1):
            cmd, _, rest = step.partition(" ")
            args = rest.split()
            t = time.monotonic()
            if cmd == "shot":
                pass
            elif cmd in ("click", "rclick", "dclick", "move"):
                x, y = int(args[0]), int(args[1])
                if cmd == "click":
                    await desk.click(x, y)
                elif cmd == "rclick":
                    await desk.click(x, y, button="right")
                elif cmd == "dclick":
                    await desk.double_click(x, y)
                else:
                    await desk.move(x, y)
            elif cmd == "hover":
                await desk.hover_path([tuple(int(v) for v in a.split(",")) for a in args])
            elif cmd == "type":
                await desk.type_text(rest)
            elif cmd == "paste":
                await desk.paste_text(rest)
            elif cmd == "key":
                await desk.key(rest)
            elif cmd == "fill":
                x, y, text = int(args[0]), int(args[1]), rest.split(" ", 2)[2]
                await desk.fill(x, y, text)
            elif cmd == "scroll":
                await desk.scroll(int(args[0]), int(args[1]), args[2], int(args[3]) if len(args) > 3 else 3)
            elif cmd == "wait":
                await asyncio.sleep(float(args[0]))
            else:
                raise SystemExit(f"unknown step: {step}")
            changed = await desk.wait_stable() if cmd not in ("shot", "wait") else None
            bbox = desk.changed_bbox(since=t)
            print(f"[{i}] {step!r}: changed={changed} bbox={bbox} ({time.monotonic() - t:.2f}s)")
            desk.screenshot().save(SHOTS / f"step{i:02d}.png")
        desk.screenshot().save(SHOTS / "last.png")
    finally:
        await desk.close()


def main() -> None:
    load_dotenv(ROOT / ".env")
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    admin = DesktopAdmin(os.environ["DESKTOP_URL"], os.environ["DESKTOP_TOKEN"])
    if cmd == "do":
        asyncio.run(run_steps(sys.argv[2:] or ["shot"]))
    elif cmd == "reset":
        print(admin.reset())
    elif cmd == "check":
        print(json.dumps(admin.check(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))), ensure_ascii=False, indent=1))
    elif cmd == "health":
        print(admin.health())
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
