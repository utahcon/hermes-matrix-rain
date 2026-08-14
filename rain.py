"""Matrix digital rain on a TTY's alternate screen buffer.

Spawned by the matrix-idle-rain Hermes plugin. Not meant to be run by hand,
but you can: ``python3 rain.py --tty $(tty) --delay 1 --parent-pid $$``

Lifecycle:
  1. Sleep ``--delay`` seconds. If killed during the nap, exit silently.
  2. Snapshot the tty's atime, switch to the alternate screen, hide cursor.
  3. Rain until dismissed by ANY of:
       * SIGTERM/SIGINT (plugin killed us — new turn started)
       * tty atime changed (user pressed a key)
       * parent pid vanished (Hermes exited)
  4. Restore: leave alt screen, show cursor. The original screen contents
     come back exactly as they were.

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

# Green ramp, brightest at the head. (Matrix rain is canonically green;
# the effect reads by motion and brightness, not hue.)
HEAD = "\x1b[97m"          # white head
BRIGHT = "\x1b[92m"        # bright green
DIM = "\x1b[32m"           # normal green
FAINT = "\x1b[2;32m"       # faint green tail

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tty", required=True)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--parent-pid", type=int, default=0)
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

    atime0 = atime()
    rows, cols = term_size(fd)
    columns = [Column(rows) for _ in range(cols)]

    w(ALT_ON + CUR_HIDE + CLEAR)
    frame_t = 1.0 / FPS
    tick = 0
    try:
        while _running:
            tick += 1
            # Dismissal checks (atime every frame is a cheap stat).
            if atime() != atime0:
                break
            if tick % FPS == 0 and not parent_alive():
                break
            if tick % (FPS * 5) == 0:
                nr, nc = term_size(fd)
                if (nr, nc) != (rows, cols):
                    rows, cols = nr, nc
                    columns = [Column(rows) for _ in range(cols)]
                    w(CLEAR)

            buf: list[str] = []
            for x, col in enumerate(columns):
                col.step()
                head_y = col.y
                # Draw head, one bright trailer, and erase the tail end.
                cells = (
                    (head_y, HEAD),
                    (head_y - 1, BRIGHT),
                    (head_y - 2, DIM),
                    (head_y - col.length // 2, FAINT),
                    (head_y - col.length, None),  # eraser
                )
                for y, color in cells:
                    if 1 <= y <= rows:
                        if color is None:
                            buf.append(f"\x1b[{y};{x + 1}H ")
                        else:
                            g = random.choice(GLYPHS)
                            buf.append(f"\x1b[{y};{x + 1}H{color}{g}")
            buf.append(RESET)
            w("".join(buf))
            time.sleep(frame_t)
    finally:
        try:
            os.write(fd, (RESET + CLEAR + CUR_SHOW + ALT_OFF).encode())
            os.close(fd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
