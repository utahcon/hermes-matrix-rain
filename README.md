# matrix-idle-rain

Matrix digital rain as an ambient status indicator for Hermes Agent CLI
sessions. The rain's color IS the agent's state.

## Modes (config.yaml)

### ambient (default)

| Agent state                    | Rain                                  |
|--------------------------------|---------------------------------------|
| Working (thinking/tool calls)  | GREEN rain                            |
| Needs your input (approval)    | RED rain + reverse-video text banner  |
| Turn finished                  | BLUE rain until you press a key       |

Any keypress dismisses the rain for the current state (so you can watch
tool output stream by); it returns on the next state change. The rain
draws on the alternate screen buffer — dismissing it restores your
terminal exactly as it was.

### beacon

No rain while working. Rain only fires as a "your turn" beacon once the
agent finishes (default 4s grace) or an approval prompt has sat unanswered
(default 20s). The original v1.0 behavior.

## Accessibility note

Red vs green is the classic colorblind confusion pair. The input-required
state therefore ALWAYS draws a reverse-video "INPUT REQUIRED - PRESS ANY
KEY" banner in addition to the color change, so the state reads by form
regardless of hue. If red/green look alike to you, swap `colors.approval`
to `magenta` or `white` in config.yaml.

## Install

    git clone https://github.com/utahcon/hermes-matrix-rain.git ~/.hermes/plugins/matrix-idle-rain
    hermes plugins enable matrix-idle-rain

Takes effect on the next CLI session start (plugins load at startup).

Requires only Python stdlib and a POSIX TTY (Linux/macOS). No dependencies.
No-op for non-TTY sessions (gateway, cron, subagents).

## Configure

Edit `config.yaml` in the plugin directory:

    mode: ambient            # or: beacon
    colors:
      working: green         # green|red|blue|magenta|cyan|white
      approval: red
      done: blue
    delays:
      working: 1.5           # seconds of grace before rain per state
      approval: 0.5
      done: 2.0
      beacon_done: 4.0
      beacon_approval: 20.0
    approval_banner: "INPUT REQUIRED - PRESS ANY KEY"

The `delays.working` grace means quick turns never flash rain at you.
Frame rate and glyph set live at the top of `rain.py`.

## Uninstall

    hermes plugins disable matrix-idle-rain
    rm -rf ~/.hermes/plugins/matrix-idle-rain

## How it works

- `__init__.py` registers Hermes lifecycle hooks (`pre_llm_call`,
  `pre_tool_call`, `post_llm_call`, `pre_approval_request`,
  `post_approval_response`) and maps them onto a working/approval/done
  state machine.
- Each state spawns `rain.py`, a detached stdlib-only renderer that writes
  ANSI directly to the controlling TTY device on the alternate screen
  buffer (no curses, no stdin contention with the foreground TUI).
- Dismissal: the renderer polls the TTY's atime every frame — any
  keystroke updates it and the renderer exits (code 3), restoring the
  screen. The plugin remembers the dismissal and won't re-rain until the
  state changes.
- Safety: SIGTERM from the plugin on state change, parent-pid watchdog if
  Hermes exits, and every exit path restores the screen via `\e[?1049l`.

## Files

- `plugin.yaml`   — Hermes plugin manifest
- `config.yaml`   — mode, colors, delays, banner text
- `__init__.py`   — hook wiring + state machine
- `rain.py`       — renderer (colors, banner, FPS, glyphs)
