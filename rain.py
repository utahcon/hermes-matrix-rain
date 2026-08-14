"""Matrix digital rain on a TTY's alternate screen buffer.

Spawned by the matrix-idle-rain Hermes plugin. Not meant to be run by hand,
but you can:

    python3 rain.py --tty $(tty) --delay 1 --parent-pid $$ --color blue \
        --direction up --banner "HELLO" --sig-text utahcon --sig-after 30

Lifecycle:
  1. Sleep ``--delay`` seconds. If killed during the nap, exit silently.
  2. Snapshot the tty's atime, switch to the alternate screen, hide cursor.
  3. Rain until dismissed by ANY of:
       * SIGTERM/SIGINT (plugin killed us — state change)     -> exit 0
       * tty atime changed (user pressed a key)               -> exit 3
       * parent pid vanished (Hermes exited)                  -> exit 0
  4. Restore: leave alt screen, show cursor.

Exit code 3 tells the plugin the USER dismissed the rain, so it won't
respawn rain for the same phase.

Directions: down (classic), up, left, right, down-left, down-right,
up-left, up-right. Drops are velocity-vector particles, so all directions
share one code path.

Signature: after ``--sig-after`` seconds of visible rain, a fraction of
drops spawning in the left third of the screen render their trail as the
letters of ``--sig-text`` in dark orange instead of random glyphs.

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

# Dark orange for the signature (256-color): head 208, body 166.
SIG_HEAD = "\x1b[38;5;208m"
SIG_BODY = "\x1b[38;5;166m"

DIRECTIONS: dict[str, tuple[int, int]] = {
    "down": (0, 1),
    "up": (0, -1),
    "right": (1, 0),
    "left": (-1, 0),
    "down-right": (1, 1),
    "down-left": (-1, 1),
    "up-right": (1, -1),
    "up-left": (-1, -1),
}

FPS = 14
SIG_FRACTION = 0.35  # chance a left-third respawn becomes a signature drop

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


class Drop:
    """A rain particle moving along a fixed unit direction vector."""

    def __init__(self, rows: int, cols: int, d: tuple[int, int],
                 sig_text: str = "", sig_ok: bool = False,
                 initial: bool = False):
        self.rows = rows
        self.cols = cols
        self.dx, self.dy = d
        self.travel = (rows if self.dy else 0) + (cols if self.dx else 0)
        self.reset(sig_text, sig_ok, initial=initial)

    def reset(self, sig_text: str = "", sig_ok: bool = False,
              initial: bool = False) -> None:
        self.x = float(random.randint(1, self.cols))
        self.y = float(random.randint(1, self.rows))
        # Pull the head back behind its entry edge so drops stream in.
        span = self.travel * (2 if initial else 1)
        back = random.uniform(0.2 * self.travel, 0.2 * self.travel + span)
        self.x -= self.dx * back
        self.y -= self.dy * back
        self.speed = random.choice((1, 1, 1, 2))
        # Signature drops must spawn aimed at the left third of the screen.
        self.sig = bool(
            sig_text and sig_ok
            and random.random() < SIG_FRACTION
            and (self.dx != 0 or self.x <= self.cols / 3)
        )
        if self.sig:
            self.text = sig_text
            self.length = len(sig_text)
        else:
            self.length = random.randint(4, max(5, self.travel - 2))

    def step(self) -> bool:
        """Advance; return True when the whole trail has left the screen."""
        self.x += self.dx * self.speed
        self.y += self.dy * self.speed
        tx = self.x - self.dx * self.length
        ty = self.y - self.dy * self.length
        if self.dy > 0 and ty > self.rows:
            return True
        if self.dy < 0 and ty < 1:
            return True
        if self.dx > 0 and tx > self.cols:
            return True
        if self.dx < 0 and tx < 1:
            return True
        return False


def banner_cells(banner: str, rows: int, cols: int) -> tuple[str, set[tuple[int, int]]]:
    """Precompute the banner draw string and the cells it occupies."""
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
    ap.add_argument("--direction", default="down", choices=sorted(DIRECTIONS))
    ap.add_argument("--banner", default="")
    ap.add_argument("--sig-text", default="")
    ap.add_argument("--sig-after", type=float, default=30.0)
    ap.add_argument("--output-window", type=float, default=0.0,
                    help="Fraction of rows (bottom) left as a live output "
                         "window. 0 = fullscreen rain on the alt buffer.")
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
    d = DIRECTIONS[args.direction]
    atime0 = atime()
    rows, cols = term_size(fd)

    # Split mode: reserve the bottom fraction as a live output window.
    split = 0.0 < args.output_window < 0.9

    def region(rows_total: int) -> int:
        """Rows available to the rain (excludes separator + output window)."""
        if not split:
            return rows_total
        out_rows = max(2, int(round(rows_total * args.output_window)))
        return max(4, rows_total - out_rows - 1)  # -1 separator row

    def sep_line(rain_rows_: int, cols_: int) -> str:
        return f"\x1b[{rain_rows_ + 1};1H\x1b[2;37m" + "─" * cols_ + RESET

    rain_rows = region(rows)
    n_drops = cols if d[1] else rain_rows
    if d[0] and d[1]:
        n_drops = (rain_rows + cols) // 2 + cols // 2
    drops = [Drop(rain_rows, cols, d, initial=True) for _ in range(n_drops)]
    ban_str, ban_cells = banner_cells(args.banner, rain_rows, cols)

    dismissed = False
    started = time.monotonic()
    if split:
        # No alt screen: the bottom window must show the REAL session
        # output. Restrict scrolling to the output window so streaming
        # text never enters the rain area, and clear the rain canvas.
        w(
            CUR_HIDE
            + f"\x1b[1;{rain_rows}r\x1b[1;1H\x1b[2J"  # clear via temp region
            + f"\x1b[{rain_rows + 2};{rows}r"          # scroll = output win
            + f"\x1b[{rows};1H"                        # park cursor in window
            + sep_line(rain_rows, cols)
        )
    else:
        w(ALT_ON + CUR_HIDE + CLEAR)
    frame_t = 1.0 / FPS
    tick = 0
    try:
        while _running:
            tick += 1
            if atime() != atime0:
                dismissed = True
                break
            if tick % FPS == 0 and not parent_alive():
                break
            if tick % (FPS * 5) == 0:
                nr, nc = term_size(fd)
                if (nr, nc) != (rows, cols):
                    rows, cols = nr, nc
                    rain_rows = region(rows)
                    n_drops = nc if d[1] else rain_rows
                    if d[0] and d[1]:
                        n_drops = (rain_rows + nc) // 2 + nc // 2
                    drops = [Drop(rain_rows, nc, d, initial=True)
                             for _ in range(n_drops)]
                    ban_str, ban_cells = banner_cells(args.banner, rain_rows, nc)
                    if split:
                        w(f"\x1b[{rain_rows + 2};{rows}r" + CLEAR
                          + sep_line(rain_rows, cols))
                    else:
                        w(CLEAR)

            sig_ok = bool(args.sig_text) and (
                time.monotonic() - started >= args.sig_after
            )

            buf: list[str] = []
            if split:
                buf.append("\x1b7")  # save foreground cursor
            for drop in drops:
                if drop.step():
                    drop.reset(args.sig_text, sig_ok)
                hx, hy = drop.x, drop.y

                if drop.sig:
                    # Full word along the trail, dark orange, plus eraser.
                    L = drop.length
                    for i in range(L):
                        x = round(hx - drop.dx * i)
                        y = round(hy - drop.dy * i)
                        if 1 <= y <= rain_rows and 1 <= x <= cols \
                                and (y, x) not in ban_cells:
                            ch = drop.text[L - 1 - i]
                            color = SIG_HEAD if i == 0 else SIG_BODY
                            buf.append(f"\x1b[{y};{x}H{color}{ch}")
                    ex = round(hx - drop.dx * L)
                    ey = round(hy - drop.dy * L)
                    if 1 <= ey <= rain_rows and 1 <= ex <= cols \
                            and (ey, ex) not in ban_cells:
                        buf.append(f"\x1b[{ey};{ex}H ")
                    continue

                cells = (
                    (0, head),
                    (1, bright),
                    (2, dim),
                    (drop.length // 2, faint),
                    (drop.length, None),  # eraser
                )
                for i, color in cells:
                    x = round(hx - drop.dx * i)
                    y = round(hy - drop.dy * i)
                    if 1 <= y <= rain_rows and 1 <= x <= cols \
                            and (y, x) not in ban_cells:
                        if color is None:
                            buf.append(f"\x1b[{y};{x}H ")
                        else:
                            g = random.choice(GLYPHS)
                            buf.append(f"\x1b[{y};{x}H{color}{g}")
            buf.append(RESET)
            if ban_str:
                buf.append(ban_str)
            if split:
                if tick % FPS == 0:  # re-assert separator ~1/sec
                    buf.append(sep_line(rain_rows, cols))
                buf.append("\x1b8")  # restore foreground cursor
            w("".join(buf))
            time.sleep(frame_t)
    finally:
        try:
            if split:
                # Reset scroll region, wipe the rain area, and park the
                # cursor at the separator row so the session continues
                # right below where output was flowing.
                os.write(fd, (
                    RESET + f"\x1b[r"
                    + f"\x1b[1;1H\x1b[{rain_rows + 1};{cols}H\x1b[1J"
                    + CUR_SHOW + f"\x1b[{rows};1H"
                ).encode())
            else:
                os.write(fd, (RESET + CLEAR + CUR_SHOW + ALT_OFF).encode())
            os.close(fd)
        except OSError:
            pass
    return EXIT_DISMISSED if dismissed else 0


if __name__ == "__main__":
    sys.exit(main())
