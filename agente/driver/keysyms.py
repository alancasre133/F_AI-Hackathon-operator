"""X11 keysyms for RFB KeyEvents."""

NAMED = {
    "backspace": 0xFF08, "tab": 0xFF09, "enter": 0xFF0D, "return": 0xFF0D, "esc": 0xFF1B, "escape": 0xFF1B,
    "delete": 0xFFFF, "del": 0xFFFF, "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
    "pageup": 0xFF55, "pagedown": 0xFF56, "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "space": 0x0020, "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3, "alt": 0xFFE9, "super": 0xFFEB,
    **{f"f{i}": 0xFFBD + i for i in range(1, 13)},
}

MODIFIERS = {NAMED["shift"], NAMED["ctrl"], NAMED["alt"], NAMED["super"]}

# Combos that would reload, close or leave the kiosk app and lose the task's work.
BLOCKED_COMBOS = {
    frozenset(c.split("+"))
    for c in [
        "f5", "ctrl+r", "ctrl+shift+r", "ctrl+f5", "ctrl+w", "ctrl+shift+w", "ctrl+q", "alt+f4",
        "ctrl+l", "ctrl+t", "ctrl+n", "ctrl+shift+n", "alt+left", "alt+right", "alt+home", "f11", "f12",
        "ctrl+shift+i", "ctrl+u", "ctrl+p", "ctrl+o", "ctrl+s",
    ]
}


def char_keysym(ch: str) -> int:
    if ch == "\n":
        return NAMED["enter"]
    if ch == "\t":
        return NAMED["tab"]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp  # ASCII and Latin-1 map 1:1 (í=0xED, ñ=0xF1, ¿=0xBF)
    return 0x01000000 + cp


def name_keysym(name: str) -> int:
    name = name.strip().lower()
    if name in NAMED:
        return NAMED[name]
    if len(name) == 1:
        return char_keysym(name)
    raise ValueError(f"unknown key: {name}")


def parse_combo(combo: str) -> list[str]:
    """'ctrl+a' -> ['ctrl', 'a']; a lone '+' is the plus key."""
    if combo == "+":
        return ["+"]
    return [p.strip().lower() for p in combo.split("+") if p.strip()]
