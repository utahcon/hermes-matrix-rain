"""matrix-idle-rain — Matrix rain as a "your turn" beacon for Hermes CLI.

Semantics (inverted screensaver):
  * Agent finishes its turn  -> rain starts after a short delay.
  * You press any key         -> rain dismisses instantly, screen restored.
  * New turn / tool activity  -> rain killed (belt and suspenders).
  * Approval prompt waiting   -> rain starts after a LONGER delay, so the
    prompt is readable first; if you walked away, the rain calls you back.

The rain itself is rendered by ``rain.py``, a detached child process that
draws on the controlling TTY's alternate screen buffer.  It self-dismisses
on any keypress (tty atime change) and self-destructs if the Hermes process
exits (parent-pid watchdog), so it can never orphan-rain over your shell.

Only activates when the Hermes process has a controlling TTY — gateway,
cron, and subagent sessions are untouched.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent
_RAIN_SCRIPT = _PLUGIN_DIR / "rain.py"

# Seconds of grace after turn-end before rain starts. Long enough that you
# never see it while actively reading the response and typing a reply.
_DELAY_TURN_END = 4.0
# Approval prompts must stay readable — only rain if you've walked away.
_DELAY_APPROVAL = 20.0

_state: dict = {"proc": None}


def _tty_path() -> str | None:
    """Controlling TTY of this Hermes process, or None if headless."""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                return os.ttyname(fd)
        except OSError:
            continue
    return None


def _stop_rain(**_kwargs) -> None:
    proc = _state.get("proc")
    if proc is None:
        return
    _state["proc"] = None
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:  # pragma: no cover — defensive
        pass


def _start_rain(delay: float) -> None:
    tty = _tty_path()
    if not tty or not _RAIN_SCRIPT.exists():
        return
    _stop_rain()
    try:
        _state["proc"] = subprocess.Popen(
            [
                sys.executable,
                str(_RAIN_SCRIPT),
                "--tty", tty,
                "--delay", str(delay),
                "--parent-pid", str(os.getpid()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # pragma: no cover — never break the agent
        logger.debug("matrix-idle-rain: failed to spawn rain: %s", exc)
        _state["proc"] = None


def _on_turn_end(**_kwargs) -> None:
    _start_rain(_DELAY_TURN_END)


def _on_activity(**_kwargs) -> None:
    _stop_rain()


def _on_approval_wait(**_kwargs) -> None:
    _start_rain(_DELAY_APPROVAL)


def register(ctx) -> None:
    if not _tty_path():
        logger.debug("matrix-idle-rain: no controlling TTY, staying dormant")
        return
    ctx.register_hook("post_llm_call", _on_turn_end)
    ctx.register_hook("pre_llm_call", _on_activity)
    ctx.register_hook("pre_tool_call", _on_activity)
    ctx.register_hook("pre_approval_request", _on_approval_wait)
    ctx.register_hook("post_approval_response", _on_activity)
    logger.debug("matrix-idle-rain: hooks registered")
