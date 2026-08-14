"""matrix-idle-rain — Matrix rain status indicator for Hermes CLI.

Two modes (see config.yaml):

ambient (default)
  * Agent working        -> rain in ``colors.working``   (default green)
  * Input required       -> rain in ``colors.approval``  (default red)
                            plus a reverse-video text banner, so the state
                            reads by form as well as hue (colorblind-safe)
  * Turn finished        -> rain in ``colors.done``      (default blue)
                            until you press a key
  * Any keypress dismisses the rain for the current state; it returns on
    the next state change.

beacon
  * No rain while working. Rain only when the agent is done (your-turn
    beacon) or an approval prompt has been sitting unanswered.

The rain is rendered by ``rain.py``, a detached child process drawing on
the controlling TTY's alternate screen buffer. It self-dismisses on any
keypress (tty atime change) and self-destructs if the Hermes process exits
(parent-pid watchdog). Restoring the alt screen brings back the original
terminal contents untouched.

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
_CONFIG_PATH = _PLUGIN_DIR / "config.yaml"

# Renderer exit code meaning "user pressed a key".
_EXIT_DISMISSED = 3

_DEFAULTS = {
    "mode": "ambient",
    "direction": "down",
    "colors": {"working": "green", "approval": "red", "done": "blue"},
    "delays": {
        "working": 1.5,
        "approval": 0.5,
        "done": 2.0,
        "beacon_done": 4.0,
        "beacon_approval": 20.0,
    },
    "approval_banner": "INPUT REQUIRED - PRESS ANY KEY",
    "signature": {"enabled": True, "text": "utahcon", "after": 30.0},
    "output_window": 0.10,
}


def _load_config() -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}
    try:
        import yaml

        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        for key, val in raw.items():
            if key in ("colors", "delays", "signature") and isinstance(val, dict):
                cfg[key].update(val)
            elif key in cfg:
                cfg[key] = val
    except FileNotFoundError:
        pass
    except Exception as exc:  # bad YAML must never break the agent
        logger.warning("matrix-idle-rain: config.yaml unreadable (%s), using defaults", exc)
    return cfg


_CFG = _load_config()

_state: dict = {
    "proc": None,        # Popen of the live renderer, or None
    "phase": "idle",     # idle | working | approval | done
    "proc_phase": None,  # phase the live renderer was spawned for
    "dismissed_phase": None,  # phase the user dismissed via keypress
}


def _tty_path() -> str | None:
    """Controlling TTY of this Hermes process, or None if headless."""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                return os.ttyname(fd)
        except OSError:
            continue
    return None


def _note_dismissal() -> None:
    """If the renderer exited because the user pressed a key, remember it
    so we don't respawn rain into their face during the same phase."""
    proc = _state.get("proc")
    if proc is not None and proc.poll() == _EXIT_DISMISSED:
        _state["dismissed_phase"] = _state["proc_phase"]
        _state["proc"] = None
        _state["proc_phase"] = None


def _stop_rain() -> None:
    _note_dismissal()
    proc = _state.get("proc")
    _state["proc"] = None
    _state["proc_phase"] = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:  # pragma: no cover — defensive
        pass


def _spawn_rain(phase: str, color: str, delay: float, banner: str = "") -> None:
    tty = _tty_path()
    if not tty or not _RAIN_SCRIPT.exists():
        return
    _note_dismissal()
    # User already dismissed rain in this phase — respect that.
    if _state["dismissed_phase"] == phase:
        return
    # Same-phase renderer already running (e.g. repeated pre_tool_call) —
    # leave it alone to avoid restart flicker.
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None and _state["proc_phase"] == phase:
        return
    _stop_rain()
    cmd = [
        sys.executable, str(_RAIN_SCRIPT),
        "--tty", tty,
        "--delay", str(delay),
        "--parent-pid", str(os.getpid()),
        "--color", color,
        "--direction", str(_CFG.get("direction", "down")),
        "--output-window", str(_CFG.get("output_window", 0.0)),
    ]
    sig = _CFG.get("signature") or {}
    if sig.get("enabled") and sig.get("text"):
        cmd += ["--sig-text", str(sig["text"]),
                "--sig-after", str(sig.get("after", 30.0))]
    if banner:
        cmd += ["--banner", banner]
    try:
        _state["proc"] = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _state["proc_phase"] = phase
    except Exception as exc:  # pragma: no cover — never break the agent
        logger.debug("matrix-idle-rain: failed to spawn rain: %s", exc)
        _state["proc"] = None
        _state["proc_phase"] = None


def _enter_phase(phase: str) -> None:
    _note_dismissal()
    if phase != _state["phase"]:
        _state["phase"] = phase
        _state["dismissed_phase"] = None


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------

def _on_turn_start(**_kw) -> None:
    _enter_phase("working")
    if _CFG["mode"] == "ambient":
        _spawn_rain("working", _CFG["colors"]["working"], _CFG["delays"]["working"])
    else:
        _stop_rain()


def _on_tool_activity(**_kw) -> None:
    _enter_phase("working")
    if _CFG["mode"] == "ambient":
        # Re-ensure rain (no-op if already running or user dismissed it).
        _spawn_rain("working", _CFG["colors"]["working"], _CFG["delays"]["working"])
    else:
        _stop_rain()


def _on_turn_end(**_kw) -> None:
    _enter_phase("done")
    if _CFG["mode"] == "ambient":
        _spawn_rain("done", _CFG["colors"]["done"], _CFG["delays"]["done"])
    else:
        _spawn_rain("done", _CFG["colors"]["done"], _CFG["delays"]["beacon_done"])


def _on_approval_wait(**_kw) -> None:
    _enter_phase("approval")
    delay = (
        _CFG["delays"]["approval"]
        if _CFG["mode"] == "ambient"
        else _CFG["delays"]["beacon_approval"]
    )
    _spawn_rain(
        "approval",
        _CFG["colors"]["approval"],
        delay,
        banner=_CFG["approval_banner"],
    )


def _on_approval_done(**_kw) -> None:
    _enter_phase("working")
    if _CFG["mode"] == "ambient":
        _spawn_rain("working", _CFG["colors"]["working"], _CFG["delays"]["working"])
    else:
        _stop_rain()


def register(ctx) -> None:
    if not _tty_path():
        logger.debug("matrix-idle-rain: no controlling TTY, staying dormant")
        return
    ctx.register_hook("pre_llm_call", _on_turn_start)
    ctx.register_hook("pre_tool_call", _on_tool_activity)
    ctx.register_hook("post_llm_call", _on_turn_end)
    ctx.register_hook("pre_approval_request", _on_approval_wait)
    ctx.register_hook("post_approval_response", _on_approval_done)
    logger.debug("matrix-idle-rain: hooks registered (mode=%s)", _CFG["mode"])
