"""Read on-screen tables into rows, so counts and lookups are done in code, not by the LLM."""
from .ocr import OCRBox


def _rows(boxes: list[OCRBox], tol: int = 7) -> list[list[OCRBox]]:
    rows: list[list[OCRBox]] = []
    for b in sorted(boxes, key=lambda b: b.cy):
        if rows and abs(rows[-1][0].cy - b.cy) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [sorted(r, key=lambda b: b.x1) for r in rows]


def extract_table(boxes: list[OCRBox], header_hint: list[str] | None = None) -> dict | None:
    """Find the header row (>=3 cells, followed by aligned rows) and map cells to columns.

    Returns {"headers": [...], "rows": [{header: value}], "header_y": int} or None.
    """
    rows = _rows(boxes)
    # The header is the row whose column starts best line up with the rows below it.
    header_idx, best = None, 0
    for i, r in enumerate(rows):
        if len(r) < 3:
            continue
        if header_hint and not any(h.casefold() in " ".join(b.text for b in r).casefold() for h in header_hint):
            continue
        aligned = 0
        for f in rows[i + 1:]:
            hits = sum(any(abs(b.x1 - h.x1) <= 12 for b in f) for h in r)
            if hits < 0.6 * len(r):
                break
            aligned += 1
        if aligned > best:
            header_idx, best = i, aligned
    if header_idx is None or best < 2:
        return None  # one aligned row is a menu bar or title, not a table

    header = rows[header_idx]
    # Column i spans from its header's left edge to the next header's left edge.
    starts = [h.x1 - 8 for h in header]
    names = [h.text for h in header]
    out = []
    for r in rows[header_idx + 1:header_idx + 1 + best]:
        rec = {}
        for b in r:
            col = max((i for i, s in enumerate(starts) if b.x1 >= s), default=0)
            rec[names[col]] = (rec.get(names[col], "") + " " + b.text).strip()
        out.append(rec)
    return {"headers": names, "rows": out, "header_y": header[0].cy}
