"""OCR backends returning text segments with pixel boxes.

Tesseract (spa+eng) is preferred: it keeps accents, which this kind of app
treats as significant ("Ramírez" != "Ramirez"). RapidOCR is the fallback when
the Tesseract binary is missing; its boxes are good but it drops accents.
"""
import os
import shutil
from dataclasses import dataclass

import numpy as np
from PIL import Image


@dataclass
class OCRBox:
    text: str
    x1: int
    y1: int
    x2: int
    y2: int
    score: float

    @property
    def cx(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y1 + self.y2) // 2


class TesseractOCR:
    name = "tesseract"

    def __init__(self, lang: str = "spa+eng", scale: int = 3):
        import pytesseract

        cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
        if not cmd:
            raise RuntimeError("tesseract binary not found")
        pytesseract.pytesseract.tesseract_cmd = cmd
        self.tess = pytesseract
        self.lang = lang
        self.scale = scale

    def __call__(self, img: Image.Image, psm: int = 3) -> list[OCRBox]:
        s = self.scale
        big = Image.fromarray(dark_on_light(np.asarray(img.convert("L")))).resize((img.width * s, img.height * s), Image.LANCZOS)
        # psm 3 (automatic layout) at 3x scale read accents best on SIGECOM screens.
        d = self.tess.image_to_data(big, lang=self.lang, config=f"--psm {psm} --oem 1", output_type=self.tess.Output.DICT)
        words = []
        for i in range(len(d["text"])):
            text = d["text"][i].strip().strip("|[]")
            if not text or float(d["conf"][i]) <= 30 or set(text) <= set("|Il!_-—"):
                continue  # empty, unsure, or a field border read as a glyph
            words.append((d["block_num"][i], d["par_num"][i], d["line_num"][i], d["left"][i] // s, d["top"][i] // s,
                          (d["left"][i] + d["width"][i]) // s, (d["top"][i] + d["height"][i]) // s, text, float(d["conf"][i])))
        return _group_words(words)

    def region(self, img: Image.Image, box: tuple[int, int, int, int]) -> list[OCRBox]:
        """OCR one area on its own (sparse text mode). Overlays such as dropdowns break full-page layout analysis."""
        x1, y1, x2, y2 = box
        out = self(img.crop(box), psm=11)
        for b in out:
            b.x1 += x1; b.x2 += x1; b.y1 += y1; b.y2 += y1
        return out


def dark_on_light(gray: np.ndarray) -> np.ndarray:
    """Invert regions with a dark background (title bars, filled buttons) so all text is dark on light."""
    import cv2

    # Median, not mean: it keeps edges sharp, so text right next to a dark highlight isn't half-inverted.
    local = cv2.medianBlur(gray, 31)
    return np.where(local < 128, 255 - gray, gray).astype(np.uint8)


def _group_words(words) -> list[OCRBox]:
    """Join words of one OCR line into segments; a wide gap starts a new segment (table cells, menu items)."""
    lines: dict[tuple, list] = {}
    for w in words:
        lines.setdefault(w[:3], []).append(w)
    out = []
    for ws in lines.values():
        ws.sort(key=lambda w: w[3])
        seg = [ws[0]]
        for w in ws[1:]:
            height = max(seg[-1][6] - seg[-1][4], 8)
            if w[3] - seg[-1][5] > 1.6 * height:
                out.append(_segment(seg))
                seg = [w]
            else:
                seg.append(w)
        out.append(_segment(seg))
    return out


def _segment(seg) -> OCRBox:
    return OCRBox(
        text=" ".join(w[7] for w in seg),
        x1=min(w[3] for w in seg), y1=min(w[4] for w in seg),
        x2=max(w[5] for w in seg), y2=max(w[6] for w in seg),
        score=sum(w[8] for w in seg) / len(seg) / 100,
    )


class RapidOCRBackend:
    name = "rapidocr"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self.engine = RapidOCR()

    def __call__(self, img: Image.Image) -> list[OCRBox]:
        res, _ = self.engine(np.asarray(img.convert("RGB")))
        out = []
        for box, text, score in res or []:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append(OCRBox(text.strip(), int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)), float(score)))
        return [b for b in out if b.text]


_engine = None


def get_ocr():
    global _engine
    if _engine is None:
        try:
            _engine = TesseractOCR()
        except Exception:
            _engine = RapidOCRBackend()
    return _engine
