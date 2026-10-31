"""Turn a screenshot into numbered, clickable elements the LLM can refer to.

OCR finds text; OpenCV finds rectangles (inputs, buttons, dropdowns, checkboxes).
Rectangles absorb the text inside them; empty inputs get the nearest label.
Every element gets a mark number drawn on the image, so the LLM answers
"click [17]" and the code clicks the element's exact centre.
"""
import re
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .ocr import OCRBox, get_ocr

DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")


@dataclass
class Element:
    id: int
    kind: str  # text | link | button | input | checkbox
    text: str
    x1: int
    y1: int
    x2: int
    y2: int
    label: str = ""  # for inputs: the caption next to the field

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    def describe(self) -> str:
        parts = [f"[{self.id}] {self.kind}"]
        if self.label:
            parts.append(f"label={self.label!r}")
        if self.text or self.kind != "input":
            parts.append(repr(self.text))
        else:
            parts.append("(empty)")
        parts.append(f"@({self.center[0]},{self.center[1]})")
        return " ".join(parts)


@dataclass
class Screen:
    image: Image.Image
    elements: list[Element]
    ocr: list[OCRBox]
    seconds: float
    today: str | None = None  # ISO date read from the screen, if any
    annotated: Image.Image | None = field(default=None, repr=False)

    def by_id(self, mark: int) -> Element:
        for e in self.elements:
            if e.id == mark:
                return e
        raise KeyError(f"no element [{mark}] on screen")

    def find(self, text: str, exact: bool = False) -> list[Element]:
        t = text.casefold()
        return [e for e in self.elements if (e.text.casefold() == t if exact else t in e.text.casefold())]

    def listing(self) -> str:
        return "\n".join(e.describe() for e in self.elements)


# ---- rectangle detection ---------------------------------------------------

def detect_rects(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Axis-aligned UI rectangles (x1, y1, x2, y2).

    Keeps only long straight edge runs (borders, fill edges), which glyphs do not
    produce, then takes the closed shapes they form.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 15, 45)
    horiz = cv2.morphologyEx(edges, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (18, 1)))
    vert = cv2.morphologyEx(edges, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 9)))
    lines = cv2.dilate(horiz | vert, np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(lines, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rects = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (10 <= h <= 48 and 10 <= w <= 760):
            continue
        if len(cv2.approxPolyDP(c, 4, True)) > 10:
            continue
        rects.append((x, y, x + w, y + h))
    return _dedupe(rects)


def _grid_cells(rects) -> set:
    """Rects that butt against a same-height neighbour: table cells, not inputs or buttons."""
    cells = set()
    for a in rects:
        for b in rects:
            if a is b:
                continue
            same_row = abs(a[1] - b[1]) <= 3 and abs(a[3] - b[3]) <= 3
            touching = abs(a[2] - b[0]) <= 4 or abs(b[2] - a[0]) <= 4
            if same_row and touching:
                cells.add(a)
                break
    return cells


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def _dedupe(rects):
    rects = sorted(rects, key=lambda r: -(r[2] - r[0]) * (r[3] - r[1]))
    kept = []
    for r in rects:
        if all(_iou(r, k) < 0.7 for k in kept):
            kept.append(r)
    return kept


def _inside(b: OCRBox, r, pad: int = 3) -> bool:
    return b.x1 >= r[0] - pad and b.y1 >= r[1] - pad and b.x2 <= r[2] + pad and b.y2 <= r[3] + pad


def _fill_color(img: np.ndarray, r) -> np.ndarray:
    x1, y1, x2, y2 = r
    inner = img[y1 + 3:y2 - 2, x1 + 3:x2 - 2]
    if inner.size == 0:
        return np.array([255, 255, 255])
    return np.median(inner.reshape(-1, 3), axis=0)


def _is_link(img: np.ndarray, b: OCRBox) -> bool:
    """Blue text on a light background (hyperlink-like)."""
    crop = img[b.y1:b.y2, b.x1:b.x2].reshape(-1, 3).astype(int)
    if crop.size == 0:
        return False
    bg = np.median(crop, axis=0)
    if bg.mean() < 190:
        return False  # light text on a dark bar is a title, not a link
    glyph = crop[np.abs(crop - bg).sum(axis=1) > 120]
    if len(glyph) < 5:
        return False
    r, g, bl = glyph.mean(axis=0)
    return bl > r + 60 and bl > g + 30


# ---- assembly --------------------------------------------------------------

def perceive(image: Image.Image, annotate: bool = True, focus: tuple[int, int, int, int] | None = None) -> Screen:
    """`focus`: the area that changed with the last action (e.g. a dropdown); it is re-read on its own."""
    t0 = time.monotonic()
    img = np.asarray(image.convert("RGB"))
    engine = get_ocr()
    ocr = engine(image)
    if focus and hasattr(engine, "region"):
        fx1, fy1, fx2, fy2 = focus
        if 0 < (fx2 - fx1) * (fy2 - fy1) < 0.4 * image.width * image.height:
            box = (max(0, fx1 - 4), max(0, fy1 - 4), min(image.width, fx2 + 4), min(image.height, fy2 + 4))
            ocr = [b for b in ocr if not (box[0] <= b.cx <= box[2] and box[1] <= b.cy <= box[3])] + engine.region(image, box)
    rects = detect_rects(img)

    cells = _grid_cells(rects)
    raw: list[dict] = []
    used = set()
    for r in rects:
        if r in cells:
            continue  # table cells: their text stays as plain text/link elements
        inner = [i for i, b in enumerate(ocr) if _inside(b, r)]
        w, h = r[2] - r[0], r[3] - r[1]
        fill = _fill_color(img, r)
        light = fill.mean() > 235
        if w <= 20 and h <= 20 and abs(w - h) <= 4:
            if inner or any(_overlaps(r, b) for b in ocr):
                continue
            kind = "checkbox"
        elif light and h >= 16 and (not inner or _left_aligned(r, [ocr[i] for i in inner])):
            kind = "input"  # white box with left-aligned or no text
        elif inner:
            kind = "button"
        else:
            continue  # empty decorative frame
        used.update(inner)
        text = " ".join(ocr[i].text for i in sorted(inner, key=lambda i: ocr[i].x1))
        raw.append(dict(kind=kind, text=text, box=r))

    # Drop container rects that hold several other elements (panels, table frames).
    raw = [e for e in raw if sum(_contains(e["box"], o["box"]) for o in raw if o is not e) == 0]

    for i, b in enumerate(ocr):
        if i in used:
            continue
        kind = "link" if _is_link(img, b) else "text"
        raw.append(dict(kind=kind, text=b.text, box=(b.x1, b.y1, b.x2, b.y2)))

    texts = [e for e in raw if e["kind"] in ("text", "link")]
    for e in raw:
        if e["kind"] == "input":
            e["label"] = _nearest_label(e["box"], texts)

    raw.sort(key=lambda e: (round(e["box"][1] / 8), e["box"][0]))
    elements = [
        Element(id=i + 1, kind=e["kind"], text=e["text"], x1=e["box"][0], y1=e["box"][1], x2=e["box"][2], y2=e["box"][3], label=e.get("label", ""))
        for i, e in enumerate(raw)
    ]
    screen = Screen(image=image, elements=elements, ocr=ocr, seconds=time.monotonic() - t0, today=_find_today(ocr))
    if annotate:
        screen.annotated = draw_marks(image, elements)
    return screen


def _left_aligned(r, boxes: list[OCRBox]) -> bool:
    """Input values hug the left edge; button captions sit in the middle."""
    x1 = min(b.x1 for b in boxes)
    x2 = max(b.x2 for b in boxes)
    centred = abs((x1 + x2) / 2 - (r[0] + r[2]) / 2) < 6
    return x1 - r[0] < 14 and not centred


def _overlaps(r, b: OCRBox) -> bool:
    return not (b.x2 < r[0] or b.x1 > r[2] or b.y2 < r[1] or b.y1 > r[3])


def _contains(outer, inner) -> bool:
    return outer != inner and outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _nearest_label(box, texts) -> str:
    """Caption directly above the field, or on the same row to its left."""
    best, best_d = "", 1e9
    for t in texts:
        tx1, ty1, tx2, ty2 = t["box"]
        above = ty2 <= box[1] + 2 and box[1] - ty2 < 24 and tx1 < box[2] and tx2 > box[0] - 4
        left = tx2 <= box[0] + 2 and box[0] - tx2 < 160 and abs((ty1 + ty2) / 2 - (box[1] + box[3]) / 2) < 10
        if above or left:
            d = (box[1] - ty2) if above else (box[0] - tx2)
            if d < best_d:
                best, best_d = t["text"], d
    return best


def _find_today(ocr: list[OCRBox]) -> str | None:
    for b in ocr:
        if "hoy" in b.text.casefold():
            m = DATE_RE.search(b.text)
            if m:
                d, mo, y = m.groups()
                return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


# ---- drawing ---------------------------------------------------------------

COLORS = {"input": (0, 150, 70), "button": (220, 90, 0), "link": (30, 90, 220), "checkbox": (150, 0, 150), "text": (200, 30, 60)}


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_marks(image: Image.Image, elements: list[Element]) -> Image.Image:
    out = image.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    font = _font(11)
    for e in elements:
        c = COLORS.get(e.kind, (200, 30, 60))
        d.rectangle([e.x1 - 1, e.y1 - 1, e.x2 + 1, e.y2 + 1], outline=c, width=1)
        tag = str(e.id)
        tw = d.textlength(tag, font=font)
        tx, ty = max(0, e.x1 - 2), max(0, e.y1 - 12)
        d.rectangle([tx, ty, tx + tw + 4, ty + 12], fill=c)
        d.text((tx + 2, ty), tag, fill=(255, 255, 255), font=font)
    return out
