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

# Halfwidth katakana (single-cell width — fullwidth kana are 2 cells wide
# and spill into neighbours, chewing the signature and banner) + latin +
# digits. Halfwidth kana are also what the film actually used.
GLYPHS = [chr(c) for c in range(0xFF66, 0xFF9E)] + list(
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
                 initial: bool = False):
        self.rows = rows
        self.cols = cols
        self.dx, self.dy = d
        self.travel = (rows if self.dy else 0) + (cols if self.dx else 0)
        self.reset(initial=initial)

    def reset(self, initial: bool = False, stagger: float = 0.0) -> None:
        self.x = float(random.randint(1, self.cols))
        self.y = float(random.randint(1, self.rows))
        # Pull the head back behind its entry edge so drops stream in.
        # ``stagger`` widens the spawn spread (in multiples of travel) so
        # arrivals spread over time — used by wash mode for a gradual sweep.
        span = self.travel * (2 if initial else 1) + self.travel * stagger
        back = random.uniform(0.2 * self.travel, 0.2 * self.travel + span)
        self.x -= self.dx * back
        self.y -= self.dy * back
        self.speed = random.choice((1, 1, 1, 2))
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


class SigWord:
    """The signature: the word, stacked vertically in a fixed column,
    falling top-to-bottom as one intact unit. Spawned once every
    ``interval`` seconds after ``after`` seconds of rain."""

    STEP_EVERY = 2  # frames per row of fall — slower than the rain, readable

    def __init__(self, text: str, col: int):
        self.text = text
        self.col = col
        self.active = False
        self.head = 0  # row of the LAST letter (bottom of the word)
        self._frame = 0

    def spawn(self) -> None:
        self.active = True
        self.head = 0  # word starts fully above the screen
        self._frame = 0

    def render(self, rows: int, cols: int,
               ban_cells: set[tuple[int, int]]) -> str:
        """Advance (every STEP_EVERY frames) and return the ANSI to draw
        the word plus an eraser above it. Deactivates once fully off."""
        if not self.active or self.col > cols:
            return ""
        self._frame += 1
        if self._frame % self.STEP_EVERY:
            advance = False
        else:
            advance = True
            self.head += 1
        L = len(self.text)
        top = self.head - L + 1
        if top > rows:
            self.active = False
            return ""
        if not advance:
            pass  # still redraw every frame so rain can't chew the word
        buf = []
        for i, ch in enumerate(self.text):
            y = top + i
            if 1 <= y <= rows and (y, self.col) not in ban_cells:
                buf.append(f"\x1b[{y};{self.col}H{SIG_BODY}{ch}")
        # Eraser: the cell the word's top just vacated.
        if advance and 1 <= top - 1 <= rows \
                and (top - 1, self.col) not in ban_cells:
            buf.append(f"\x1b[{top - 1};{self.col}H ")
        return "".join(buf)


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
    ap.add_argument("--color", default="green",
                    choices=sorted(RAMPS) + ["rainbow"])
    ap.add_argument("--direction", default="down",
                    choices=sorted(DIRECTIONS) + ["random"])
    ap.add_argument("--banner", default="")
    ap.add_argument("--sig-text", default="")
    ap.add_argument("--sig-after", type=float, default=30.0)
    ap.add_argument("--sig-interval", type=float, default=30.0)
    ap.add_argument("--sig-col", type=int, default=5)
    ap.add_argument("--output-window", type=float, default=0.0,
                    help="Fraction of rows (bottom) left as a live output "
                         "window. 0 = fullscreen rain on the alt buffer.")
    ap.add_argument("--control-file", default="",
                    help="Path to a live-state file: line 1 = color, "
                         "line 2 = banner text. Polled every few frames; "
                         "changes apply without restarting the animation.")
    ap.add_argument("--wash", action="store_true",
                    help="Split mode: don't clear the rain area at start — "
                         "the drops wash away the existing frame as they "
                         "fall. (No effect in fullscreen: the alternate "
                         "screen starts blank.)")
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

    ramp_pool = list(RAMPS.values())

    def assign_ramp(drop) -> None:
        drop.ramp = random.choice(ramp_pool)

    # Color/banner are LIVE state: initialized from CLI, then updated from
    # the control file mid-animation (no frame reset on state change).
    cur_color = args.color
    cur_banner = args.banner
    rainbow = cur_color == "rainbow"
    head = bright = dim = faint = ""
    if not rainbow:
        head, bright, dim, faint = RAMPS[cur_color]
    ctl_mtime = 0  # 0 = any existing control file is read on first poll

    if args.direction == "random":
        d = DIRECTIONS[random.choice(list(DIRECTIONS))]
    else:
        d = DIRECTIONS[args.direction]
    atime0 = atime()
    rows, cols = term_size(fd)

    # Split mode: reserve the bottom fraction as a live output window.
    # Fullscreen (output_window 0) uses the alt screen but STILL reserves a
    # minimal 2-row sink: the foreground session writes to the shared TTY
    # regardless of buffers, so without a confined scroll region its output
    # stomps the animation. The sink is the only real fix.
    split = 0.0 < args.output_window < 0.9

    def region(rows_total: int) -> int:
        """Rows available to the rain (excludes separator + output sink)."""
        if split:
            out_rows = max(2, int(round(rows_total * args.output_window)))
        else:
            out_rows = 2  # minimal sink on the alt screen
        return max(4, rows_total - out_rows - 1)  # -1 separator row

    def sep_line(rain_rows_: int, cols_: int) -> str:
        return f"\x1b[{rain_rows_ + 1};1H\x1b[2;37m" + "─" * cols_ + RESET

    rain_rows = region(rows)
    n_drops = cols if d[1] else rain_rows
    if d[0] and d[1]:
        n_drops = (rain_rows + cols) // 2 + cols // 2
    # Wash mode: drops all start BEHIND the entry edge, staggered widely,
    # so they sweep in over several seconds and consume the old frame
    # progressively. Otherwise scatter drops across the screen so full
    # rain appears instantly.
    wash = split and args.wash
    drops = [Drop(rain_rows, cols, d, initial=not wash)
             for _ in range(n_drops)]
    if wash:
        for _dr in drops:
            _dr.reset(stagger=10.0)
    if rainbow:
        for _dr in drops:
            assign_ramp(_dr)
    ban_str, ban_cells = banner_cells(cur_banner, rain_rows, cols)

    def poll_control() -> bool:
        """Re-read the control file if changed. Returns True when the
        banner changed (caller must rebuild banner cells + clear its old
        box)."""
        nonlocal cur_color, cur_banner, rainbow, head, bright, dim, faint
        nonlocal ctl_mtime
        if not args.control_file:
            return False
        try:
            st = os.stat(args.control_file)
        except OSError:
            return False
        if st.st_mtime_ns == ctl_mtime:
            return False
        ctl_mtime = st.st_mtime_ns
        try:
            with open(args.control_file) as fh:
                lines = fh.read().splitlines()
        except OSError:
            return False
        new_color = (lines[0].strip() if lines else "") or cur_color
        new_banner = lines[1].strip() if len(lines) > 1 else ""
        if new_color in RAMPS or new_color == "rainbow":
            cur_color = new_color
            rainbow = cur_color == "rainbow"
            if rainbow:
                for _dr in drops:
                    if not hasattr(_dr, "ramp"):
                        assign_ramp(_dr)
            else:
                head, bright, dim, faint = RAMPS[cur_color]
        if new_banner != cur_banner:
            cur_banner = new_banner
            return True
        return False

    # Signature word: falls intact down a fixed column, periodically.
    sig = SigWord(args.sig_text, max(1, args.sig_col)) if args.sig_text else None
    sig_last = -1e9  # so the first spawn happens right at sig_after

    dismissed = False
    started = time.monotonic()
    # Both modes confine the session's output to a bottom scroll region so
    # streaming text never enters the rain area. Fullscreen additionally
    # switches to the alt buffer first (scrollback stays untouched).
    setup = "" if split else ALT_ON
    if split and args.wash:
        # Wash mode: leave the existing frame in place — the rain's
        # erasers consume it as drops pass. Only set the regions.
        clear_part = ""
    else:
        clear_part = f"\x1b[1;{rain_rows}r\x1b[1;1H\x1b[2J"
    w(
        setup
        + CUR_HIDE
        + clear_part
        + f"\x1b[{rain_rows + 2};{rows}r"          # scroll = output sink
        + f"\x1b[{rows};1H"                        # park cursor in sink
        + sep_line(rain_rows, cols)
    )
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
                    if rainbow:
                        for _dr in drops:
                            assign_ramp(_dr)
                    ban_str, ban_cells = banner_cells(cur_banner, rain_rows, nc)
                    if split:
                        w(f"\x1b[{rain_rows + 2};{rows}r" + CLEAR
                          + sep_line(rain_rows, cols))
                    else:
                        w(f"\x1b[1;{rain_rows}r\x1b[1;1H\x1b[2J"
                          + f"\x1b[{rain_rows + 2};{rows}r"
                          + f"\x1b[{rows};1H"
                          + sep_line(rain_rows, cols))

            if tick % 3 == 0 and poll_control():
                # Banner changed: wipe the old box, rebuild for the new.
                for (by, bx) in ban_cells:
                    if 1 <= by <= rain_rows:
                        w(f"\x1b[{by};{bx}H ")
                ban_str, ban_cells = banner_cells(cur_banner, rain_rows, cols)

            if sig and not sig.active and (
                time.monotonic() - started >= args.sig_after
                and time.monotonic() - sig_last >= args.sig_interval
            ):
                sig.spawn()
                sig_last = time.monotonic()

            buf: list[str] = []
            if split:
                buf.append("\x1b7")  # save foreground cursor
            # Render the signature FIRST so its post-advance cells mask the
            # rain this frame; paint it after the drops so it stays on top.
            sig_str = ""
            sig_cells: set[tuple[int, int]] = set()
            if sig and sig.active:
                sig_str = sig.render(rain_rows, cols, ban_cells)
                L = len(sig.text)
                top = sig.head - L + 1
                sig_cells = {(top + i, sig.col) for i in range(L)}
            for drop in drops:
                if drop.step():
                    drop.reset()
                    if rainbow:
                        assign_ramp(drop)
                hx, hy = drop.x, drop.y

                if rainbow:
                    head, bright, dim, faint = drop.ramp
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
                            and (y, x) not in ban_cells \
                            and (y, x) not in sig_cells:
                        if color is None:
                            buf.append(f"\x1b[{y};{x}H ")
                        else:
                            g = random.choice(GLYPHS)
                            buf.append(f"\x1b[{y};{x}H{color}{g}")
            if sig_str:
                buf.append(sig_str)
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
                # Reset the scroll region and clear. We cannot restore what
                # the rain painted over (no alt screen in split mode), so
                # force the foreground app to repaint: jiggle the tty size
                # (rows-1, then back) — a REAL winsize change is the only
                # thing readline/Ink/prompt frameworks reliably redraw on
                # (plain SIGWINCH with unchanged size is ignored).
                os.write(fd, (
                    RESET + "\x1b[r" + CLEAR + CUR_SHOW
                    + f"\x1b[{rows};1H"
                ).encode())
                try:
                    ws = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
                    r, c, xp, yp = struct.unpack("HHHH", ws)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", max(r - 1, 2), c, xp, yp))
                    time.sleep(0.05)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", r, c, xp, yp))
                except OSError:
                    pass
            else:
                # Fullscreen: reset the scroll region BEFORE leaving the
                # alt buffer (DECSTBM persists across buffer switches in
                # most emulators), then restore the original screen.
                os.write(fd, (RESET + "\x1b[r" + CLEAR + CUR_SHOW
                              + ALT_OFF).encode())
            os.close(fd)
        except OSError:
            pass
        if split and args.parent_pid:
            # Belt and suspenders for apps that catch WINCH without
            # tracking size.
            try:
                os.kill(args.parent_pid, signal.SIGWINCH)
            except OSError:
                pass
    return EXIT_DISMISSED if dismissed else 0


if __name__ == "__main__":
    sys.exit(main())
