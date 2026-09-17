"""Console output helpers shared by every module."""
import os
import sys
from datetime import datetime

# Windows gives a redirected stdout the locale codec (cp1252), which cannot
# encode the box/arrow glyphs the reports use: force UTF-8 so `python -m mlo >
# out.txt` writes a log instead of dying with UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _is_console():
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


# ANSI colours are for an interactive console: NO_COLOR, or a pipe/redirect,
# keeps escape codes out of log files.
_COLOR = not os.environ.get("NO_COLOR") and _is_console()

class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    GREY = "\033[90m"


def c(text, color_code):
    return f"{color_code}{text}{Color.RESET}" if _COLOR else str(text)


def clear_screen():
    """Clear an attached console; a redirect is left untouched."""
    if not _is_console():
        return
    print("\033[2J\033[H", end="", flush=True)


def log(msg, color=None):
    timestamp = datetime.now().strftime("%H:%M:%S")
    colored_msg = c(msg, color) if color else msg
    print(f"[{c(timestamp, Color.GREY)}] {colored_msg}", flush=True)


def fmt_size(b):
    try:
        b = int(b)
    except Exception:
        b = 0
    return f"{b:,} bytes ({b / 1024.0:,.2f} KB / {b / (1024.0 * 1024.0):,.2f} MB)"


def fmt_short_bytes(b):
    """Compact byte size: '1,234 B' / '12.4 KB' / '3.45 MB'."""
    try:
        b = int(b)
    except Exception:
        b = 0
    if abs(b) >= 1024 * 1024:
        return f"{b / (1024 * 1024):,.2f} MB"
    if abs(b) >= 1024:
        return f"{b / 1024:,.2f} KB"
    return f"{b:,} B"


def print_separator():
    print(c("-" * 60, Color.GREY), flush=True)


def print_header(title):
    """Banner line, exactly 60 columns wide like print_separator()."""
    print(c(f"── {title} ", Color.CYAN) + c("─" * max(2, 56 - len(title)), Color.GREY),
          flush=True)


def pause_for_input():
    """Wait for Enter; Ctrl+C and EOF propagate so the caller can abort."""
    input(c("\nPress Enter to continue...", Color.GREY))


def _short_val(v, n=28):
    if v is None:
        return "MISS"
    s = str(v).replace("\n", " ").replace("\r", " ").strip()
    if not s:
        return "MISS"
    return s if len(s) <= n else s[: n - 1] + "…"

