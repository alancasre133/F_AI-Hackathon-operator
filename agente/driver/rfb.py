"""Minimal async RFB (VNC, RFC 6143) client over WebSocket.

Handles the handshake (VNC auth), keeps a live framebuffer from Raw/CopyRect
updates and exposes the two input messages: PointerEvent and KeyEvent.
"""
import asyncio
import collections
import struct
import time

import numpy as np
from Crypto.Cipher import DES
from PIL import Image
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

ENC_RAW = 0
ENC_COPYRECT = 1
ENC_DESKTOP_SIZE = -223
ENC_CURSOR = -239  # server sends the cursor shape apart instead of drawing it into frames

SEC_NONE = 1
SEC_VNC_AUTH = 2


class RFBError(Exception):
    pass


def vnc_des_key(password: str) -> bytes:
    """VNC auth uses the password (8 bytes, zero padded) with each byte's bits reversed as the DES key."""
    raw = password.encode("latin-1")[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in raw)


class RFBClient:
    def __init__(self, url: str, password: str, shared: bool = True):
        self.url = url
        self.password = password
        self.shared = shared
        self.width = 0
        self.height = 0
        self.name = ""
        self.fb: np.ndarray | None = None  # (h, w, 4) uint8, BGRX
        self.last_change = 0.0  # monotonic time of the last update that changed pixels
        self.updates = 0
        self.dirty = collections.deque(maxlen=500)  # (t, x, y, w, h) of changed rects
        self.connected = False
        self.error: Exception | None = None
        self._ws = None
        self._buf = bytearray()
        self._reader: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._first_frame = asyncio.Event()

    # ---- connection -------------------------------------------------------

    async def connect(self, timeout: float = 20.0) -> None:
        self._ws = await asyncio.wait_for(
            connect(self.url, subprotocols=["binary"], max_size=None, ping_interval=20, open_timeout=timeout),
            timeout,
        )
        await asyncio.wait_for(self._handshake(), timeout)
        self.connected = True
        self._reader = asyncio.create_task(self._read_loop())
        await self._request_update(incremental=False)
        await asyncio.wait_for(self._first_frame.wait(), timeout)

    async def close(self) -> None:
        self.connected = False
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()

    async def _handshake(self) -> None:
        version = await self._read(12)
        if not version.startswith(b"RFB "):
            raise RFBError(f"not an RFB server: {version!r}")
        server_minor = (int(version[4:7]), int(version[8:11]))
        legacy = server_minor < (3, 7)
        await self._ws.send(b"RFB 003.003\n" if legacy else b"RFB 003.008\n")

        if legacy:
            (sec,) = struct.unpack(">I", await self._read(4))
            if sec == 0:
                raise RFBError(await self._read_reason())
        else:
            n = (await self._read(1))[0]
            if n == 0:
                raise RFBError(await self._read_reason())
            offered = await self._read(n)
            if SEC_VNC_AUTH in offered:
                sec = SEC_VNC_AUTH
            elif SEC_NONE in offered:
                sec = SEC_NONE
            else:
                raise RFBError(f"no supported security type in {list(offered)}")
            await self._ws.send(bytes([sec]))

        if sec == SEC_VNC_AUTH:
            challenge = await self._read(16)
            response = DES.new(vnc_des_key(self.password), DES.MODE_ECB).encrypt(challenge)
            await self._ws.send(response)
        if sec == SEC_VNC_AUTH or not legacy:
            (result,) = struct.unpack(">I", await self._read(4))
            if result != 0:
                raise RFBError("authentication failed" + ("" if legacy else f": {await self._read_reason()}"))

        await self._ws.send(bytes([1 if self.shared else 0]))  # ClientInit

        self.width, self.height = struct.unpack(">HH", await self._read(4))
        await self._read(16)  # server pixel format; we override it below
        (name_len,) = struct.unpack(">I", await self._read(4))
        self.name = (await self._read(name_len)).decode("latin-1", "replace")
        self.fb = np.zeros((self.height, self.width, 4), np.uint8)

        # 32bpp little-endian true colour: bytes arrive as B, G, R, X.
        await self._ws.send(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        encodings = [ENC_COPYRECT, ENC_RAW, ENC_DESKTOP_SIZE, ENC_CURSOR]
        await self._ws.send(struct.pack(">BxH", 2, len(encodings)) + b"".join(struct.pack(">i", e) for e in encodings))

    async def _read_reason(self) -> str:
        (n,) = struct.unpack(">I", await self._read(4))
        return (await self._read(n)).decode("latin-1", "replace")

    # ---- incoming ---------------------------------------------------------

    async def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            msg = await self._ws.recv()
            self._buf += msg if isinstance(msg, (bytes, bytearray)) else msg.encode("latin-1")
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _read_loop(self) -> None:
        try:
            while True:
                msg_type = (await self._read(1))[0]
                if msg_type == 0:
                    await self._on_framebuffer_update()
                elif msg_type == 1:  # SetColourMapEntries (unused in true colour)
                    _, n = struct.unpack(">xHH", await self._read(5))
                    await self._read(n * 6)
                elif msg_type == 2:  # Bell
                    pass
                elif msg_type == 3:  # ServerCutText
                    (n,) = struct.unpack(">xxxI", await self._read(7))
                    await self._read(n)
                else:
                    raise RFBError(f"unknown server message type {msg_type}")
        except (asyncio.CancelledError, ConnectionClosed):
            pass
        except Exception as exc:  # surfaced to callers through .error
            self.error = exc
        finally:
            self.connected = False

    async def _on_framebuffer_update(self) -> None:
        (n_rects,) = struct.unpack(">xH", await self._read(3))
        now = time.monotonic()
        changed = False
        resized = False
        for _ in range(n_rects):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self._read(12))
            if enc == ENC_RAW:
                pixels = np.frombuffer(await self._read(w * h * 4), np.uint8).reshape(h, w, 4)
                region = self.fb[y:y + h, x:x + w]
                if not np.array_equal(region, pixels):
                    region[...] = pixels
                    changed = True
                    self.dirty.append((now, x, y, w, h))
            elif enc == ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", await self._read(4))
                self.fb[y:y + h, x:x + w] = self.fb[sy:sy + h, sx:sx + w].copy()
                changed = True
                self.dirty.append((now, x, y, w, h))
            elif enc == ENC_CURSOR:
                await self._read(w * h * 4 + (w + 7) // 8 * h)  # shape + mask; not needed
            elif enc == ENC_DESKTOP_SIZE:
                self.width, self.height = w, h
                self.fb = np.zeros((h, w, 4), np.uint8)
                resized = changed = True
            else:
                raise RFBError(f"unsupported encoding {enc}")
        if changed:
            self.last_change = now
        self.updates += 1
        self._first_frame.set()
        await self._request_update(incremental=not resized)

    async def _request_update(self, incremental: bool = True) -> None:
        await self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    # ---- outgoing ---------------------------------------------------------

    async def _send(self, data: bytes) -> None:
        if self.error:
            raise RFBError(f"connection failed: {self.error}")
        async with self._send_lock:
            await self._ws.send(data)

    async def pointer(self, x: int, y: int, mask: int = 0) -> None:
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        await self._send(struct.pack(">BBHH", 5, mask, x, y))

    async def key(self, keysym: int, down: bool) -> None:
        await self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    async def cut_text(self, text: str) -> None:
        data = text.encode("latin-1", "replace")
        await self._send(struct.pack(">BxxxI", 6, len(data)) + data)

    # ---- frames -----------------------------------------------------------

    def screenshot(self) -> Image.Image:
        return Image.fromarray(np.ascontiguousarray(self.fb[..., [2, 1, 0]]), "RGB")
