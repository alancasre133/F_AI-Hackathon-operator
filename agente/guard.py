"""Guardrails in code: which clicks may change data, row accounting, and the final summary."""
import difflib
import re
import unicodedata
from datetime import date, datetime


def fold(s: str) -> str:
    """Lowercase without accents, for matching button captions."""
    return "".join(c for c in unicodedata.normalize("NFD", s.casefold()) if unicodedata.category(c) != "Mn")


COMMIT_WORDS = {
    "eliminar": ["eliminar", "borrar", "anular", "desactivar", "inactivar", "dar de baja", "suprimir", "quitar", "remover",
                 "archivar", "delete", "remove"],
    "exportar": ["exportar", "descargar", "generar reporte", "generar informe", "generar", "imprimir", "export", "download"],
    "guardar": ["guardar", "grabar", "salvar", "crear", "registrar", "actualizar", "enviar", "aplicar", "agregar", "anadir",
                "procesar", "save", "submit", "nuevo", "nueva", "alta"],
    "confirmar": ["confirmar", "si", "aceptar", "continuar", "ok"],
}

# Which kinds of data-changing clicks each task type may make.
ALLOWED = {
    "crear": {"guardar", "confirmar"},
    "editar": {"guardar", "confirmar"},
    "exportar": {"exportar", "confirmar"},
    "eliminar": {"eliminar", "confirmar", "guardar"},
    "consultar": set(),
    "otro": {"guardar", "confirmar"},
}
LIMITS = {"guardar": 3, "exportar": 2, "eliminar": 1, "confirmar": 4}


def commit_kind(caption: str) -> str | None:
    words = fold(caption).strip()
    if not words or len(words) > 40:
        return None  # long text is content, not a button caption
    for kind, keys in COMMIT_WORDS.items():
        for k in keys:
            if re.search(rf"(^|\W){re.escape(k)}(\W|$)", words):
                return kind
    return None


class ActionGate:
    def __init__(self, task_type: str):
        self.task_type = task_type if task_type in ALLOWED else "otro"
        self.used: dict[str, int] = {}

    def check(self, caption: str, declared: str | None = None) -> tuple[bool, str | None, str]:
        """Return (allowed, kind, reason). 'Nuevo cliente' style openers count as guardar only for creation.

        `declared` is what the LLM said this turn's important action is; it is the backup when OCR
        can't read the button caption."""
        kind = commit_kind(caption)
        if kind is None and declared in COMMIT_WORDS:
            kind = declared
            caption = caption or declared
        if kind is None:
            return True, None, ""
        f = fold(caption)
        if kind == "guardar" and (f.startswith(("nuevo", "nueva", "alta", "+")) or (f.startswith("crear ") and len(f.split()) > 1)):
            # Opening a blank form changes nothing; tracked apart so it never counts as a save.
            return (self.task_type == "crear"), "abrir", "abrir un formulario de alta solo se permite al crear"
        if kind == "confirmar" and f in ("aceptar", "ok") and not self.used:
            return True, None, ""  # closing an info/error dialog changes nothing
        if kind not in ALLOWED[self.task_type]:
            return False, kind, f"'{caption}' ({kind}) no corresponde a una tarea de tipo {self.task_type}"
        if self.used.get(kind, 0) >= LIMITS[kind]:
            return False, kind, f"límite de '{kind}' alcanzado; verifica antes de repetir"
        return True, kind, ""

    def record(self, kind: str | None) -> None:
        if kind:
            self.used[kind] = self.used.get(kind, 0) + 1

    def missing_effect(self) -> str | None:
        """A create/export task can't be 'terminada' if its data-changing click never happened."""
        if self.task_type == "crear" and not self.used.get("guardar"):
            return "no se guardó ningún registro nuevo"
        if self.task_type == "exportar" and not self.used.get("exportar"):
            return "no se exportó ningún reporte"
        return None


# ---- accumulated table rows -------------------------------------------------

def parse_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class RowStore:
    """Rows read across pages, deduplicated, so counts are computed in code."""

    def __init__(self):
        self.headers: list[str] = []
        self.rows: list[dict] = []
        self._seen: set = set()

    def add(self, table: dict) -> int:
        """Add a page of rows. Columns are matched by position, so OCR jitter in a header
        ("Cliente" vs "Clicnte") doesn't look like a different table and wipe the count."""
        headers = table["headers"]
        if self.headers and not self._same_table(headers):
            self.clear()
        if not self.headers:
            self.headers = list(headers)
        added = 0
        for r in table["rows"]:
            values = [r.get(h, "") for h in headers]
            row = {self.headers[i] if i < len(self.headers) else headers[i]: v for i, v in enumerate(values)}
            key = tuple(" ".join(fold(v).split()) for v in values)
            if key not in self._seen:
                self._seen.add(key)
                self.rows.append(row)
                added += 1
        return added

    def _same_table(self, headers: list[str]) -> bool:
        if abs(len(headers) - len(self.headers)) > 1:
            return False
        hits = sum(any(same_text(h, k) for k in self.headers) for h in headers)
        return len(headers) == len(self.headers) or hits >= 0.6 * len(headers)

    def count(self, filters: list[dict]) -> tuple[int, list[dict]]:
        hits = [r for r in self.rows if all(_match(r, f) for f in filters)]
        return len(hits), hits

    def clear(self) -> None:
        self.headers, self.rows, self._seen = [], [], set()


def _column(row: dict, col: str) -> str:
    for k, v in row.items():
        if fold(k) == fold(col):
            return v
    for k, v in row.items():
        if fold(col) in fold(k):
            return v
    return ""


def _match(row: dict, f: dict) -> bool:
    val = _column(row, f.get("col", ""))
    want = str(f.get("valor", ""))
    op = f.get("op", "eq")
    if op in ("antes", "despues", "desde", "hasta"):
        a, b = parse_date(val), parse_date(want)
        if not a or not b:
            return False
        return {"antes": a < b, "despues": a > b, "desde": a >= b, "hasta": a <= b}[op]
    if op == "contiene":
        return fold(want) in fold(val)
    return same_text(val, want)


def same_text(a: str, b: str) -> bool:
    """Equal after folding, tolerating a 1-2 character OCR slip ("Ferreterfa" for "Ferretería").

    A different name ("Ferretería Ruiz e Hijos" vs "Ferretería Ruiz") still differs: length gap > 2.
    """
    a, b = " ".join(fold(a).split()), " ".join(fold(b).split())
    if a == b:
        return True
    if abs(len(a) - len(b)) > 2 or min(len(a), len(b)) < 5:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


# ---- final summary ------------------------------------------------------------

def build_summary(final: dict, secrets: list[str], is_question: bool = True) -> str:
    estado = final.get("estado")
    lines = []
    respuesta = final.get("respuesta")
    if is_question and estado == "terminada" and respuesta not in (None, "", "null"):
        respuesta = str(respuesta).strip().removeprefix("respuesta:").strip()
        lines.append(f"respuesta: {respuesta}")
    resumen = re.sub(r"(?im)^\s*respuesta\s*:.*$", "", str(final.get("resumen") or "")).strip()
    if resumen:
        lines.append(resumen)
    razones = [str(r) for r in final.get("razones") or [] if str(r).strip()]
    if razones:
        lines.append("Motivo: " + "; ".join(razones))
    text = "\n".join(lines) or ("Tarea completada." if estado == "terminada" else "No se pudo completar la tarea.")
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[oculto]")
    return text
