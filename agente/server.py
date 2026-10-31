"""HTTP entrypoint (evaluation contract).

    GET  /health            -> 200 when ready
    POST /tasks             -> {"task_id"} immediately; the task runs in the background
    GET  /tasks/{task_id}   -> {"estado": en_curso|terminada|fallida, "resumen"}

Every call carries Authorization: Bearer <team token>.
"""
import asyncio
import logging
import os
import secrets
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .agent import Agent, TaskResult  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("server")

TEAM_TOKEN = os.environ.get("TEAM_TOKEN", "")
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "6"))
KEEP_TASKS = 500

app = FastAPI(title="Agente operador sin API")
tasks: dict[str, dict] = {}  # task_id -> {"result": TaskResult, "job": asyncio.Task, "desktop": url, "created": t}
by_desktop: dict[str, str] = {}  # desktop url -> running task_id
slots = asyncio.Semaphore(MAX_PARALLEL)


def auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not TEAM_TOKEN or not secrets.compare_digest(token, TEAM_TOKEN):
        raise HTTPException(status_code=401, detail="token inválido")


class Escritorio(BaseModel):
    url: str
    password: str


class TaskIn(BaseModel):
    tarea: str
    escritorio: Escritorio


@app.get("/health")
async def health() -> dict:
    # Open on purpose: a readiness probe should not fail on auth. Task routes require the token.
    return {"ok": True, "version": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "local")[:7]}


@app.post("/tasks", dependencies=[Depends(auth)])
async def create_task(body: TaskIn) -> dict:
    task_id = uuid.uuid4().hex
    result = TaskResult()

    # A new task on a desktop means the evaluator moved on: stop anything still running there,
    # or its actions would land in the new task.
    previous = by_desktop.get(body.escritorio.url)
    if previous and previous in tasks and not tasks[previous]["job"].done():
        tasks[previous]["job"].cancel()

    job = asyncio.create_task(_run(task_id, body, result))
    tasks[task_id] = {"result": result, "job": job, "desktop": body.escritorio.url, "created": time.time(), "tarea": body.tarea}
    by_desktop[body.escritorio.url] = task_id
    _trim()
    log.info("task %s created: %s", task_id, body.tarea[:120])
    return {"task_id": task_id}


@app.get("/tasks", dependencies=[Depends(auth)])
async def list_tasks() -> list[dict]:
    """Debug view: recent tasks, newest first, to review an evaluation run afterwards."""
    out = []
    for tid, e in sorted(tasks.items(), key=lambda kv: -kv[1]["created"]):
        r: TaskResult = e["result"]
        out.append({"task_id": tid, "creada": e["created"], "tarea": e.get("tarea", ""), "estado": r.estado,
                    "resumen": r.resumen, "segundos": (r.usage or {}).get("seconds_total")})
    return out


@app.get("/tasks/{task_id}", dependencies=[Depends(auth)])
async def get_task(task_id: str) -> dict:
    entry = tasks.get(task_id)
    if not entry:
        raise HTTPException(status_code=404, detail="tarea no encontrada")
    r: TaskResult = entry["result"]
    return {"estado": r.estado, "resumen": r.resumen}


@app.get("/tasks/{task_id}/trace", dependencies=[Depends(auth)])
async def get_trace(task_id: str) -> dict:
    """Debug view: every step the agent took."""
    entry = tasks.get(task_id)
    if not entry:
        raise HTTPException(status_code=404, detail="tarea no encontrada")
    r: TaskResult = entry["result"]
    return {"estado": r.estado, "resumen": r.resumen, "pasos": r.steps, "uso": r.usage}


async def _run(task_id: str, body: TaskIn, result: TaskResult) -> None:
    def update(r: TaskResult) -> None:
        result.estado, result.resumen, result.steps, result.usage = r.estado, r.resumen, r.steps, r.usage

    try:
        async with slots:
            agent = Agent(body.tarea, body.escritorio.url, body.escritorio.password, on_update=update)
            await agent.run()
    except asyncio.CancelledError:
        if result.estado == "en_curso":
            result.estado = "fallida"
            result.resumen = "Tarea interrumpida: llegó una nueva tarea para el mismo escritorio."
    except Exception as exc:
        log.exception("task %s failed", task_id)
        result.estado = "fallida"
        result.resumen = f"Error del agente: {type(exc).__name__}."
    finally:
        if by_desktop.get(body.escritorio.url) == task_id:
            by_desktop.pop(body.escritorio.url, None)
        log.info("task %s %s", task_id, result.estado)


def _trim() -> None:
    if len(tasks) > KEEP_TASKS:
        for tid in sorted(tasks, key=lambda t: tasks[t]["created"])[: len(tasks) - KEEP_TASKS]:
            if tasks[tid]["job"].done():
                tasks.pop(tid)
