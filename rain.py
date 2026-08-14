"""Matrix digital rain on a TTY's alternate screen buffer.

Spawned by the matrix-idle-rain Hermes plugin. Not meant to be run by hand,
but you can:

    python3 rain.py --tty $(tty) --delay 1 --parent-pid $$ --color blue \
        --banner "HELLO"

Lifecycle:
  1. Sleep ``--delay`` seconds. If killed during the nap, exit silently.
  2. Snapshot the tty's atime, switch to the alternate screen, hide cursor.
  3. Rain until dismissed by ANY of:
       * SIGTERM/SIGINT (plugin killed us — state change)     -> exit 0
       * tty atime changed (user pressed a key)               -> exit 3
       * parent pid vanished (Hermes exited)                  -> exit 0
  4. Restore: leave alt screen, show cursor. The original screen contents
     come back exactly as they were.

Exit code 3 tells the plugin the USER dismissed the rain, so it won't
respawn rain for the same phase.

Pure stdlib. No curses — we write ANSI directly to the tty device so we
don't fight the foreground process for stdin.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import struct
import sys
import time

try:
    import fcntl
    import termios
except ImportError:  # non-POSIX — plugin never spawns us there
    sys.exit(0)

EXIT_DISMISSED = 3

# Katakana + latin + digits, the classic mix.
GLYPHS = [chr(c) for c in range(0x30A0, 0x30FF)] + list(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789*+-<>"
)

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
CUR_HIDE = "\x1b[?25l"
CUR_SHOW = "\x1b[?25h"
CLEAR = "\x1b[2J"
RESET = "\x1b[0m"

# Per-color ramps: (head, bright, mid, faint). Head is always white so the
# drop leaders pop regardless of body color.
RAMPS: dict[str, tuple[str, str, str, str]] = {
    "green":   ("\x1b[97m", "\x1b[92m", "\x1b[32m", "\x1b[2;32m"),
    "red":     ("\x1b[97m", "\x1b[91m", "\x1b[31m", "\x1b[2;31m"),
    "blue":    ("\x1b[97m", "\x1b[94m", "\x1b[34m", "\x1b[2;34m"),
    "magenta": ("\x1b[97m", "\x1b[95m", "\x1b[35m", "\x1b[2;35m"),
    "cyan":    ("\x1b[97m", "\x1b[96m", "\x1b[36m", "\x1b[2;36m"),
    "white":   ("\x1b[97m", "\x1b[97m", "\x1b[37m", "\x1b[2;37m"),
}

FPS = 14

_running = True


def _bail(*_a) -> None:
    global _running
    _running = False


def term_size(fd: int) -> tuple[int, int]:
    try:
        rows, cols, _xp, _yp = struct.unpack(
            "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        )
        return max(rows, 5), max(cols, 10)
    except OSError:
        return 24, 80


class Column:
    def __init__(self, rows: int):
        self.rows = rows
        self.reset(initial=True)

    def reset(self, initial: bool = False) -> None:
        self.y = -random.randint(0, self.rows * 2 if initial else self.rows)
        self.speed = random.choice((1, 1, 1, 2))
        self.length = random.randint(4, max(5, self.rows - 2))

    def step(self) -> None:
        self.y += self.speed
        if self.y - self.length > self.rows:
            self.reset()


def banner_cells(banner: str, rows: int, cols: int) -> tuple[str, set[tuple[int, int]]]:
    """Precompute the banner draw string and the cells it occupies.

    Reverse-video box, centered. Returns (ansi_string, occupied_cells) —
    the rain skips occupied cells so the banner stays crisp.
    """
    if not banner:
        return "", set()
    text = f"  {banner}  "
    if len(text) > cols - 2:
        text = text[: cols - 2]
    y = rows // 2
    x0 = max(1, (cols - len(text)) // 2)
    occupied = set()
    for dy in (-1, 0, 1):
        for dx in range(len(text)):
            occupied.add((y + dy, x0 + dx))
    blank = " " * len(text)
    s = (
        f"\x1b[{y - 1};{x0}H\x1b[7m{blank}\x1b[0m"
        f"\x1b[{y};{x0}H\x1b[7;1m{text}\x1b[0m"
        f"\x1b[{y + 1};{x0}H\x1b[7m{blank}\x1b[0m"
    )
    return s, occupied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tty", required=True)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--color", default="green", choices=sorted(RAMPS))
    ap.add_argument("--banner", default="")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _bail)
    signal.signal(signal.SIGINT, _bail)
    signal.signal(signal.SIGHUP, _bail)

    # Grace nap — dismissable.
    deadline = time.monotonic() + args.delay
    while _running and time.monotonic() < deadline:
        time.sleep(0.1)
    if not _running:
        return 0

    try:
        fd = os.open(args.tty, os.O_WRONLY)
    except OSError:
        return 0

    def w(s: str) -> None:
        try:
            os.write(fd, s.encode())
        except OSError:
            _bail()

    def atime() -> float:
        try:
            return os.stat(args.tty).st_atime
        except OSError:
            return 0.0

    def parent_alive() -> bool:
        if not args.parent_pid:
            return True
        try:
            os.kill(args.parent_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True  # e.g. EPERM — process exists, not ours

    head, bright, dim, faint = RAMPS[args.color]
    atime0 = atime()
    rows, cols = term_size(fd)
    columns = [Column(rows) for _ in range(cols)]
    ban_str, ban_cells = banner_cells(args.banner, rows, cols)

    dismissed = False
    w(ALT_ON + CUR_HIDE + CLEAR)
    frame_t = 1.0 / FPS
    tick = 0
    try:
        while _running:
            tick += 1
            # Dismissal checks (atime every frame is a cheap stat).
            if atime() != atime0:
                dismissed = True
                break
            if tick % FPS == 0 and not parent_alive():
                break
            if tick % (FPS * 5) == 0:
                nr, nc = term_size(fd)
                if (nr, nc) != (rows, cols):
                    rows, cols = nr, nc
                    columns = [Column(rows) for _ in range(cols)]
                    ban_str, ban_cells = banner_cells(args.banner, rows, cols)
                    w(CLEAR)

            buf: list[str] = []
            for x, col in enumerate(columns):
                col.step()
                head_y = col.y
                # Draw head, trailers, and erase the tail end.
                cells = (
                    (head_y, head),
                    (head_y - 1, bright),
                    (head_y - 2, dim),
                    (head_y - col.length // 2, faint),
                    (head_y - col.length, None),  # eraser
                )
                for y, color in cells:
                    if 1 <= y <= rows and (y, x + 1) not in ban_cells:
                        if color is None:
                            buf.append(f"\x1b[{y};{x + 1}H ")
                        else:
                            g = random.choice(GLYPHS)
                            buf.append(f"\x1b[{y};{x + 1}H{color}{g}")
            buf.append(RESET)
            if ban_str:
                buf.append(ban_str)
            w("".join(buf))
            time.sleep(frame_t)
    finally:
        try:
            os.write(fd, (RESET + CLEAR + CUR_SHOW + ALT_OFF).encode())
            os.close(fd)
        except OSError:
            pass
    return EXIT_DISMISSED if dismissed else 0


if __name__ == "__main__":
    sys.exit(main())
