"""Run perception on a screenshot: python -m tools.see shots/last.png

Writes shots/marks.png (numbered marks) and prints elements and any table found.
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

from agente.perception import extract_table, get_ocr, perceive


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "shots/last.png")
    screen = perceive(Image.open(path))
    out = path.parent / "marks.png"
    screen.annotated.save(out)
    print(f"ocr={get_ocr().name} {len(screen.elements)} elements in {screen.seconds:.2f}s, today={screen.today}, marks -> {out}")
    print(screen.listing())
    table = extract_table(screen.ocr)
    if table:
        print(f"\ntable: {len(table['rows'])} rows, headers={table['headers']}")
        for r in table["rows"][:5]:
            print(" ", json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
