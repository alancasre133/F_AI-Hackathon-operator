# Agente operador sin API

Agente que opera aplicaciones de escritorio heredadas **sin API, solo por pantalla** (VNC), como lo haría un empleado nuevo,
y ejecuta tareas en lenguaje natural en apps que nunca ha visto. Reto 2 · Frontier Hackathon.

## Cómo funciona

```
POST /tasks ─► gestor de tareas (watchdog 4:30) ─► agente
                                                    │  1 comprender  2 orientarse  3 localizar
                                                    │  4 actuar      5 verificar   6 reportar
                         LLM con visión (OpenRouter) ◄┤  guardrails en código: compuerta de acciones,
                                                    │  anti-duplicados, resumen determinista, sin secretos
          percepción (Tesseract spa + OpenCV) ◄──── driver RFB propio ◄──► escritorio noVNC (1280×800)
```

- **Driver RFB propio** (`agente/driver/`): protocolo VNC sobre WebSocket, autenticación DES, modo compartido, framebuffer
  vivo. Clics sostenidos 150 ms, `Ctrl+A` + borrar antes de escribir, menús con trayectoria en L, espera de estabilidad
  usando los rectángulos que cambian, atajos peligrosos bloqueados (F5, Ctrl+W, Alt+F4...).
- **Percepción** (`agente/perception/`): OCR Tesseract en español (las tildes importan), detección de rectángulos por
  líneas rectas, elementos numerados (“clic [17]”), etiquetas de campos, enlaces, lectura de tablas a filas y fecha de “hoy”.
  Tras cada acción se re-lee aparte la zona que cambió (menús, ventanas emergentes).
- **Agente** (`agente/agent.py`): plan inicial (Opus 5.5) y bucle observar → decidir → actuar (Sonnet 5.5, escala a Opus
  tras errores). Los conteos se hacen en código sobre filas leídas en todas las páginas, nunca a ojo.
- **Guardrails** (`agente/guard.py`): Guardar / Exportar / Eliminar / Confirmar solo si corresponden al tipo de tarea y con
  límite; no se acepta `terminada` sin ver el resultado del último guardado; resumen con `respuesta:` solo en preguntas;
  contraseña y URL del escritorio nunca salen en el resumen.

## API (contrato)

Todas las llamadas llevan `Authorization: Bearer <TEAM_TOKEN>`.

| Ruta | Respuesta |
| --- | --- |
| `GET /health` | `{"ok": true}` |
| `POST /tasks` `{"tarea", "escritorio": {"url", "password"}}` | `{"task_id"}` en < 1 s |
| `GET /tasks/{id}` | `{"estado": "en_curso" \| "terminada" \| "fallida", "resumen"}` |
| `GET /tasks/{id}/trace` | pasos del agente y uso de tokens (depuración) |

Una tarea nueva sobre el mismo escritorio cancela la anterior, para que nada caiga en la tarea siguiente.

## Correr

```bash
docker build -t operario .
docker run -p 8000:8000 -e TEAM_TOKEN=... -e OPENROUTER_API_KEY=... operario
```

Variables: `TEAM_TOKEN`, `OPENROUTER_API_KEY`, opcionales `MODEL_FAST` (por defecto `anthropic/claude-sonnet-5.5`),
`MODEL_SMART` (`anthropic/claude-opus-5.5`), `MAX_PARALLEL` (6).

Desarrollo local (necesita Tesseract con `spa`):

```bash
python -m tools.vnc do "shot"                       # captura del escritorio de pruebas
python -m tools.see shots/last.png                  # qué percibe el agente
python -m tools.harness all                         # reset → tarea → /__check para E1–E5
```

## Resultados en la app de pruebas

5/5 tareas de ejemplo pasan `/__check` y las reglas de evaluación, en 51–113 s, con ~55k tokens de entrada por tarea
(~$0.16 por tarea con Sonnet + Opus).
