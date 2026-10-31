"""High-level desktop actions built on the two RFB input messages.

Encodes what we know about the target desktops: clicks must be held >=100 ms,
the screen repaints up to ~1 s after an action, and typing appends to existing text.
"""
import asyncio
import math
import time

from PIL import Image

from .keysyms import BLOCKED_COMBOS, MODIFIERS, char_keysym, name_keysym, parse_combo
from .rfb import RFBClient

BUTTONS = {"left": 1, "middle": 2, "right": 4}
WHEEL = {"up": 8, "down": 16, "left": 32, "right": 64}


class BlockedAction(Exception):
    pass


class Desktop:
    click_hold = 0.15  # the app ignores presses shorter than 100 ms
    move_step_px = 14
    move_step_delay = 0.006
    type_delay = 0.02

    def __init__(self, rfb: RFBClient):
        self.rfb = rfb
        self.x: int | None = None  # the real cursor position is unknown until our first move
        self.y: int | None = None
        self.last_action = time.monotonic()
        self._held_keys: set[int] = set()

    @classmethod
    async def connect(cls, url: str, password: str) -> "Desktop":
        rfb = RFBClient(url, password, shared=True)
        await rfb.connect()
        return cls(rfb)

    async def close(self) -> None:
        try:
            await self.release_all()
        finally:
            await self.rfb.close()

    @property
    def size(self) -> tuple[int, int]:
        return self.rfb.width, self.rfb.height

    def _mark(self) -> None:
        """Called when an action starts: changes after this point are its effect."""
        self.last_action = time.monotonic()

    # ---- pointer ----------------------------------------------------------

    async def move(self, x: int, y: int, mask: int = 0) -> None:
        """Glide in small steps so hover effects fire the way they do for a person."""
        if self.x is None:
            # Unknown start: jump, so we don't sweep across menus from a guessed origin.
            await self.rfb.pointer(x, y, mask)
            self.x, self.y = x, y
            self._mark()
            return
        dist = math.hypot(x - self.x, y - self.y)
        steps = max(1, int(dist / self.move_step_px))
        for i in range(1, steps + 1):
            await self.rfb.pointer(round(self.x + (x - self.x) * i / steps), round(self.y + (y - self.y) * i / steps), mask)
            await asyncio.sleep(self.move_step_delay)
        self.x, self.y = x, y
        self._mark()

    async def hover_path(self, points: list[tuple[int, int]], horizontal_first: bool = True) -> None:
        """L-shaped moves between points so nested menus stay open."""
        for px, py in points:
            if horizontal_first and self.x is not None:
                await self.move(px, self.y)
            await self.move(px, py)

    async def click(self, x: int, y: int, button: str = "left", hold: float | None = None) -> None:
        mask = BUTTONS[button]
        await self.move(x, y)
        self._mark()
        await self.rfb.pointer(x, y, mask)
        await asyncio.sleep(self.click_hold if hold is None else hold)
        await self.rfb.pointer(x, y, 0)

    async def double_click(self, x: int, y: int) -> None:
        await self.click(x, y, hold=0.12)
        await asyncio.sleep(0.08)
        await self.click(x, y, hold=0.12)

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        await self.move(x1, y1)
        self._mark()
        await self.rfb.pointer(x1, y1, 1)
        await asyncio.sleep(self.click_hold)
        await self.move(x2, y2, mask=1)
        await asyncio.sleep(0.1)
        await self.rfb.pointer(x2, y2, 0)

    async def scroll(self, x: int, y: int, direction: str = "down", notches: int = 3) -> None:
        await self.move(x, y)
        self._mark()
        for _ in range(notches):
            await self.rfb.pointer(x, y, WHEEL[direction])
            await self.rfb.pointer(x, y, 0)
            await asyncio.sleep(0.05)

    # ---- keyboard ---------------------------------------------------------

    async def key(self, combo: str) -> None:
        """Press a combo like 'ctrl+a' or 'tab': keys down in order, up in reverse."""
        parts = parse_combo(combo)
        if frozenset(parts) in BLOCKED_COMBOS:
            raise BlockedAction(f"blocked key combo: {combo}")
        syms = [name_keysym(p) for p in parts]
        self._mark()
        for s in syms:
            await self.rfb.key(s, True)
            self._held_keys.add(s)
            await asyncio.sleep(0.02)
        for s in reversed(syms):
            await self.rfb.key(s, False)
            self._held_keys.discard(s)
            await asyncio.sleep(0.02)

    async def type_text(self, text: str) -> None:
        self._mark()
        for ch in text:
            sym = char_keysym(ch)
            await self.rfb.key(sym, True)
            await self.rfb.key(sym, False)
            await asyncio.sleep(self.type_delay)

    async def paste_text(self, text: str) -> None:
        """Fallback for characters the server keymap cannot type: remote clipboard + Ctrl+V."""
        await self.rfb.cut_text(text)
        await asyncio.sleep(0.15)
        await self.key("ctrl+v")

    async def fill(self, x: int, y: int, text: str) -> None:
        """Replace a field's content: click, select all, delete (typing appends), type. Empty text clears it."""
        await self.click(x, y)
        await asyncio.sleep(0.1)
        await self.key("ctrl+a")
        await self.key("backspace")
        if text:
            await self.type_text(text)

    async def release_all(self) -> None:
        """Never leave a button or modifier held into the next task."""
        if not self.rfb.connected:
            return
        if self.x is not None:
            await self.rfb.pointer(self.x, self.y, 0)
        for s in list(self._held_keys | MODIFIERS):
            await self.rfb.key(s, False)
        self._held_keys.clear()

    # ---- observation ------------------------------------------------------

    def _is_cursor_rect(self, x: int, y: int, w: int, h: int) -> bool:
        """Small repaint around the pointer: the cursor sprite, not app content."""
        if self.x is None or w > 64 or h > 64:
            return False
        return x - 40 <= self.x <= x + w + 40 and y - 40 <= self.y <= y + h + 40

    def _last_content_change(self, since: float) -> float:
        times = [t for t, x, y, w, h in self.rfb.dirty if t > since and not self._is_cursor_rect(x, y, w, h)]
        return max(times, default=0.0)

    async def wait_stable(self, quiet: float = 0.4, no_change: float = 2.0, timeout: float = 5.0) -> bool:
        """Wait until the screen settles after the last action.

        Returns True if app content changed since the action, False if nothing
        changed within `no_change` seconds (the action probably missed).
        Repaints of the cursor sprite are ignored.
        """
        since = self.last_action
        start = time.monotonic()
        while True:
            now = time.monotonic()
            last = self._last_content_change(since)
            changed = last > since
            if changed and now - last >= quiet:
                return True
            if not changed and now - since >= no_change:
                return False
            if now - start >= timeout:
                return changed
            if self.rfb.error:
                raise self.rfb.error
            await asyncio.sleep(0.05)

    def changed_bbox(self, since: float | None = None) -> tuple[int, int, int, int] | None:
        """Bounding box (x1, y1, x2, y2) of everything that changed since `since`."""
        since = self.last_action if since is None else since
        rects = [(x, y, x + w, y + h) for t, x, y, w, h in self.rfb.dirty if t > since and not self._is_cursor_rect(x, y, w, h)]
        if not rects:
            return None
        return min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)

    def screenshot(self) -> Image.Image:
        return self.rfb.screenshot()
