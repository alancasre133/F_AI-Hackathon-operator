"""The agent: understand the task, then observe -> decide -> act -> verify until done or out of time."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .driver import BlockedAction, Desktop
from .guard import ActionGate, RowStore, build_summary
from .guard import fold
from .llm import LLM, MODEL_FAST, MODEL_SMART, image_part
from .memory import describe as memory_describe
from .memory import fingerprint, remember_route
from .perception import Screen, extract_table, perceive
from .prompts import PLAN_SYSTEM, STEP_SYSTEM

log = logging.getLogger("agente")

TASK_BUDGET = 280  # seconds of work; the evaluator stops polling at 300
HARD_LIMIT = 292
FINISH_MARGIN = 22  # below this many seconds left, give a verdict from what is on screen


@dataclass
class TaskResult:
    estado: str = "en_curso"
    resumen: str = ""
    steps: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class Agent:
    def __init__(self, tarea: str, url: str, password: str, on_update: Callable[[TaskResult], None] | None = None, shots_dir=None):
        self.tarea = tarea
        self.url = url
        self.password = password
        self.on_update = on_update or (lambda r: None)
        self.shots_dir = shots_dir
        self.result = TaskResult()
        self.llm = LLM()
        self.rows = RowStore()
        self.history: list[str] = []
        self.started = time.monotonic()
        self.desk: Desktop | None = None
        self.intent: dict = {}

    # ---- entry point ----------------------------------------------------

    async def run(self) -> TaskResult:
        try:
            final = await asyncio.wait_for(self._run(), HARD_LIMIT - (time.monotonic() - self.started))
        except asyncio.TimeoutError:
            final = {"estado": "fallida", "resumen": "Se agotó el tiempo antes de verificar el resultado.", "razones": self._progress()}
        except Exception as exc:  # never leave a task hanging in en_curso
            log.exception("task crashed")
            final = {"estado": "fallida", "resumen": f"Error del agente: {type(exc).__name__}.", "razones": self._progress()}
        finally:
            if self.desk:
                try:
                    await self.desk.close()  # stop touching the desktop the moment we report
                except Exception:
                    pass
            await self.llm.close()
        self.result.estado = final.get("estado") if final.get("estado") in ("terminada", "fallida") else "fallida"
        # Only questions carry a "respuesta:" line; elsewhere it could be misread by the evaluator.
        self.result.resumen = build_summary(final, [self.password, self.url], is_question=bool(self.intent.get("pregunta")))
        self.result.usage = dict(self.llm.usage, seconds_total=round(time.monotonic() - self.started, 1))
        self.on_update(self.result)
        return self.result

    def _remaining(self) -> float:
        return TASK_BUDGET - (time.monotonic() - self.started)

    def _note(self, text: str) -> None:
        self.history.append(text)
        self.result.steps.append(text)
        log.info(text)
        self.on_update(self.result)

    def _progress(self) -> list[str]:
        return [h for h in self.history[-4:]]

    # ---- main loop --------------------------------------------------------

    async def _run(self) -> dict:
        self.desk = await Desktop.connect(self.url, self.password)
        await self.desk.wait_stable(no_change=0.6)
        screen = await self._see()
        screen = await self._dismiss_startup_notice(screen)
        self.app = fingerprint(screen)
        self.knowledge = memory_describe(self.app)
        intent = self.intent = await self._plan(screen)
        self.gate = ActionGate(intent.get("tipo", "otro"))
        self._note(f"plan: {json.dumps(intent, ensure_ascii=False)}")

        step = 0
        pending_verify = False  # a data-changing click happened and its result hasn't been seen yet
        while True:
            step += 1
            if self._remaining() < FINISH_MARGIN:
                return await self._verdict(screen, intent)
            decision = await self._decide(screen, intent, step)
            actions = decision.get("acciones") or []
            declared = str(decision.get("accion_importante") or "").strip().lower()
            clicks = [a for a in actions if isinstance(a, dict) and a.get("a") in ("click", "doble_click", "click_xy")]
            if clicks:
                clicks[-1]["_declarado"] = declared  # the batch's last click is the one that commits
            if not actions:
                self._note(f"#{step} sin acciones")
                screen = await self._see()
                continue

            for act in actions[:4]:
                if act.get("a") == "terminar":
                    if pending_verify and act.get("estado") == "terminada":
                        self._note(f"#{step} terminar rechazado: falta ver el resultado del último guardado")
                        break
                    return act
                outcome, committed = await self._execute(act, screen)
                self._note(f"#{step} {outcome}")
                if committed:
                    pending_verify = True
                if outcome.startswith(("BLOQUEADO", "ERROR")):
                    break
            else:
                pass
            screen = await self._see()
            if pending_verify:
                pending_verify = False  # the next decision sees the post-commit screen

    # ---- perception & LLM -------------------------------------------------

    async def _see(self, save: bool = True) -> Screen:
        # OCR takes ~2 s; run it off the event loop so the API and the VNC reader stay responsive.
        screen = await asyncio.to_thread(perceive, self.desk.screenshot(), True, self.desk.changed_bbox())
        if self.shots_dir and save:
            screen.annotated.save(self.shots_dir / f"step{len(self.history):03d}.png")
        return screen

    async def _plan(self, screen: Screen) -> dict:
        content = [
            {"type": "text", "text": f"Tarea: {self.tarea}\nHoy (según la app): {screen.today or 'desconocido'}\n\nElementos visibles:\n{screen.listing()}"},
            image_part(screen.annotated),
        ]
        try:
            return await self.llm.json(PLAN_SYSTEM, content, model=MODEL_SMART, max_tokens=900)
        except Exception as exc:
            log.warning("plan failed: %s", exc)
            return {"tipo": "otro", "plan": []}

    async def _decide(self, screen: Screen, intent: dict, step: int) -> dict:
        table = extract_table(screen.ocr)
        table_txt = ""
        if table:
            rows = "\n".join(json.dumps(r, ensure_ascii=False) for r in table["rows"][:30])
            table_txt = f"\nTabla visible ({len(table['rows'])} filas):\n{rows}\n"
        acc = f"\nFilas acumuladas con leer_tabla: {len(self.rows.rows)}\n" if self.rows.rows else ""
        hist = "\n".join(self.history[-14:])
        text = (
            f"TAREA: {self.tarea}\n"
            f"PLAN: {json.dumps(intent, ensure_ascii=False)}\n"
            f"HOY (app): {screen.today or 'desconocido'} | tiempo restante: {int(self._remaining())} s | paso {step}\n"
            f"HISTORIAL:\n{hist}\n"
            f"{table_txt}{acc}\n"
            f"ELEMENTOS:\n{screen.listing()}"
        )
        model = MODEL_SMART if self._needs_smart() else MODEL_FAST
        return await self.llm.json(STEP_SYSTEM, [{"type": "text", "text": text}, image_part(screen.annotated)], model=model)

    NEUTRAL = {"entendido", "cerrar", "omitir", "ahora no", "mas tarde", "cancelar", "no gracias", "x", "×", "continuar sin cambios"}

    async def _dismiss_startup_notice(self, screen: Screen) -> Screen:
        """A notice open before we did anything (e.g. "Novedades de la versión 4.2") is not part of the task:
        close it in code with its neutral button, saving the LLM steps. Never "Aceptar"/"Actualizar"."""
        for _ in range(2):
            hits = [e for e in screen.elements if fold(e.text).strip(" []()") in self.NEUTRAL]
            if not hits:
                return screen
            el = max(hits, key=lambda e: e.y1)  # the dialog's button row sits below its text
            await self.desk.click(*el.center)
            changed = await self.desk.wait_stable()
            if not changed:
                await self.desk.key("esc")
                await self.desk.wait_stable()
            self._note(f"aviso inicial cerrado con '{el.text}'")
            screen = await self._see()
        return screen

    async def _verdict(self, screen: Screen, intent: dict) -> dict:
        """Out of time: judge from the screen and history instead of failing blindly.

        Often the work is done (saved, exported, counted) and only the final check was missing."""
        hist = "\n".join(self.history[-20:])
        text = (
            f"TAREA: {self.tarea}\nPLAN: {json.dumps(intent, ensure_ascii=False)}\nHISTORIAL:\n{hist}\n"
            f"Filas acumuladas con leer_tabla: {len(self.rows.rows)}\n\nELEMENTOS:\n{screen.listing()}\n\n"
            "SE ACABÓ EL TIEMPO: ya no puedes actuar. Decide el resultado con lo que ves y el historial. "
            "\"terminada\" SOLO si la pantalla o el historial muestran evidencia de que lo pedido quedó hecho "
            "(mensaje de guardado/exportado, registro con los valores pedidos) o, en una pregunta, si ya tienes el dato completo (en un conteo, solo si leíste TODAS las páginas). "
            "Si no hay evidencia, \"fallida\" con las razones. Responde SOLO con la acción terminar en JSON: "
            '{"a":"terminar","estado":"...","resumen":"...","respuesta":"... o null","razones":[...]}'
        )
        try:
            act = await asyncio.wait_for(
                self.llm.json(STEP_SYSTEM, [{"type": "text", "text": text}, image_part(screen.annotated)], model=MODEL_FAST, max_tokens=600),
                timeout=max(5.0, self._remaining() + 12),
            )
            if act.get("a") == "terminar" or "estado" in act:
                self._note(f"veredicto por tiempo: {act.get('estado')}")
                return act
        except Exception as exc:
            log.warning("verdict failed: %s", exc)
        return {"estado": "fallida", "resumen": "Se agotó el tiempo de la tarea sin poder verificar el resultado.", "razones": self._progress()}

    def _needs_smart(self) -> bool:
        """Escalate to the stronger model after blocks, errors or actions that changed nothing."""
        recent = self.history[-3:]
        return any(("BLOQUEADO" in h or "ERROR" in h or "sin cambios" in h) for h in recent)

    # ---- acting -----------------------------------------------------------

    @staticmethod
    def _text_at(screen: Screen, x: int, y: int) -> str:
        """Caption under a raw click point, so click_xy goes through the action gate too."""
        for e in screen.elements:
            if e.x1 - 4 <= x <= e.x2 + 4 and e.y1 - 4 <= y <= e.y2 + 4 and e.text:
                return e.text
        near = [b for b in screen.ocr if abs(b.cy - y) <= 12 and b.x1 - 30 <= x <= b.x2 + 30]
        return min(near, key=lambda b: abs(b.cx - x)).text if near else ""

    @staticmethod
    def _find(screen: Screen, name: str, near: tuple[int, int] | None):
        """Element whose text matches `name` (exact first, then contained), closest to the last menu opened."""
        want = fold(name).strip()
        cands = [e for e in screen.elements if fold(e.text).strip(" >»▸▶") == want] or                 [e for e in screen.elements if want and want in fold(e.text)]
        if not cands:
            return None
        if near:
            cands.sort(key=lambda e: abs(e.center[0] - near[0]) + abs(e.center[1] - near[1]))
        return cands[0]

    async def _find_in_dropdown(self, name: str, near: tuple[int, int]):
        """Re-read only the area where a dropdown or submenu opens (below / right of the item just opened)."""
        from .perception.ocr import get_ocr
        from .perception.screen import Element

        engine = get_ocr()
        if not hasattr(engine, "region"):
            return None
        img = self.desk.screenshot()
        box = (max(0, near[0] - 60), max(0, near[1] - 15), min(img.width, near[0] + 460), min(img.height, near[1] + 380))
        want = fold(name).strip()
        boxes = await asyncio.to_thread(engine.region, img, box)
        hits = [b for b in boxes if fold(b.text).strip(" >»▸▶") == want] or [b for b in boxes if want in fold(b.text)]
        if not hits:
            return None
        b = min(hits, key=lambda b: abs(b.cx - near[0]) + abs(b.cy - near[1]))
        return Element(id=0, kind="text", text=b.text, x1=b.x1, y1=b.y1, x2=b.x2, y2=b.y2)

    async def _menu(self, route: list[str]) -> str:
        """Walk a menu route by text: click the top item, hover submenus, click the final item."""
        if not route:
            return "ERROR menu: ruta vacía"
        d = self.desk
        near = None
        for i, name in enumerate(route):
            screen = await self._see(save=False)
            el = self._find(screen, name, near)
            if not el:
                await asyncio.sleep(0.8)  # dropdowns can paint >1 s after the click
                screen = await self._see(save=False)
                el = self._find(screen, name, near)
            if not el and near:
                el = await self._find_in_dropdown(name, near)
            if not el:
                return f"ERROR menu: no encontré '{name}' (nivel {i + 1} de {' > '.join(route)})"
            nxt = self._find(screen, route[i + 1], el.center) if i < len(route) - 1 else None
            if nxt and nxt.x1 >= el.x1 - 15 and nxt.y1 >= el.y1 - 5 and nxt.y1 - el.y2 < 400:
                near = el.center  # already open (its item shows in the dropdown); clicking again would close it
                continue
            ok, kind, why = self.gate.check(el.text)
            if not ok:
                return f"BLOQUEADO menu '{name}': {why}"
            last = i == len(route) - 1
            if i == 0:
                await d.click(*el.center)
            else:
                # From the menu bar go down first; inside dropdowns go sideways first, so menus stay open.
                await d.hover_path([el.center], horizontal_first=i > 1)
                if last:
                    await d.click(*el.center)
            self.gate.record(kind)
            changed = await d.wait_stable(no_change=1.6)
            if not last and not changed and i > 0:
                await d.click(*el.center)  # submenu that opens on click, not hover
                await d.wait_stable(no_change=1.6)
            near = el.center
        remember_route(self.app, route)
        return f"menu {' > '.join(route)} -> ok"

    async def _execute(self, act: dict, screen: Screen) -> tuple[str, bool]:
        a = act.get("a")
        d = self.desk
        try:
            el = screen.by_id(int(act["id"])) if "id" in act and act["id"] is not None else None
        except (KeyError, ValueError, TypeError):
            return f"ERROR: el elemento [{act.get('id')}] no existe en pantalla", False
        label = f"[{el.id}] '{el.text or el.label}'" if el else ""

        committed = False
        if a in ("click", "doble_click", "click_derecho", "click_xy"):
            if el:
                caption = el.text
            else:
                caption = self._text_at(screen, int(act["x"]), int(act["y"]))
                label = f"'{caption}'" if caption else ""
            declared = act.get("_declarado")
            ok, kind, why = self.gate.check(caption, declared if declared in ("guardar", "exportar", "eliminar") else None)
            if not ok:
                return f"BLOQUEADO {a} {label}: {why}", False
            x, y = el.center if el else (int(act["x"]), int(act["y"]))
            if a == "doble_click":
                await d.double_click(x, y)
            elif a == "click_derecho":
                await d.click(x, y, button="right")
            else:
                await d.click(x, y)
            self.gate.record(kind)
            committed = kind is not None and kind != "abrir"
        elif a == "menu":
            return await self._menu([str(x) for x in act.get("ruta") or []]), False
        elif a == "hover":
            await d.hover_path([el.center])
        elif a == "llenar":
            await d.fill(*el.center, str(act.get("texto", "")))
        elif a == "seleccionar":
            await d.click(*el.center)
            await d.wait_stable(no_change=0.5)
            await d.type_text(str(act.get("opcion", "")))
            await d.key("enter")
        elif a == "escribir":
            await d.type_text(str(act.get("texto", "")))
        elif a == "tecla":
            try:
                await d.key(str(act.get("combo", "")))
            except (BlockedAction, ValueError) as exc:
                return f"BLOQUEADO tecla {act.get('combo')}: {exc}", False
        elif a == "scroll":
            x, y = el.center if el else (640, 450)
            await d.scroll(x, y, act.get("dir", "down"), int(act.get("n", 3)))
        elif a == "esperar":
            await asyncio.sleep(min(float(act.get("s", 1)), 3))
            return "esperar", False
        elif a == "leer_tabla":
            table = extract_table(screen.ocr)
            if not table:
                return "leer_tabla: no se encontró tabla en pantalla", False
            added = self.rows.add(table)
            return f"leer_tabla: {len(table['rows'])} filas visibles, {added} nuevas, {len(self.rows.rows)} acumuladas", False
        elif a == "contar":
            n, _ = self.rows.count(act.get("filtros") or [])
            return f"contar {json.dumps(act.get('filtros'), ensure_ascii=False)} sobre {len(self.rows.rows)} filas -> {n}", False
        else:
            return f"ERROR: acción desconocida {a}", False

        changed = await d.wait_stable()
        desc = {k: v for k, v in act.items() if k not in ("a", "id", "_declarado")}
        return f"{a} {label} {json.dumps(desc, ensure_ascii=False) if desc else ''} -> {'cambió la pantalla' if changed else 'sin cambios'}".replace("  ", " "), committed
