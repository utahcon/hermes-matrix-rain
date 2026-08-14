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
state therefore ALWAYS draws a reverse-video "INPUT NEEDED - PRESS CTRL+L TO
VIEW" banner in addition to the color change, so the state reads by form
regardless of hue. If red/green look alike to you, swap `colors.approval`
to `magenta` or `white` in config.yaml.

## Install

    git clone https://github.com/utahcon/hermes-matrix-rain.git ~/.hermes/plugins/matrix-idle-rain
    hermes plugins enable matrix-idle-rain

Takes effect on the next CLI session start (plugins load at startup).

Requires only Python stdlib and a POSIX TTY (Linux/macOS). No dependencies.
No-op for non-TTY sessions (gateway, cron, subagents).

## Output window (split mode)

By default (`output_window: 0.10`) the bottom 10% of the screen stays a
LIVE output window: a scroll region confines the session's streaming
output there, under a separator line, so output never fights the
animation. Set `output_window: 0` for fullscreen rain on the alternate
screen buffer (pixel-perfect restore on dismiss; split mode instead
clears the rain area and leaves the output window's text in place).

Note on dismissal: keypress detection watches the TTY's atime, which
updates when the foreground app reads input. The Hermes TUI reads stdin
continuously so dismissal is immediate; under a program that never reads
stdin the rain persists until the next read (or state change).

## Configure

Edit `config.yaml` in the plugin directory:

    mode: ambient            # or: beacon
    output_window: 0.10      # bottom fraction kept for live output; 0 = fullscreen
    direction: down          # down|up|left|right|down-left|down-right|up-left|up-right
    signature:
      enabled: true          # after `after` seconds, left-side drops spell
      text: utahcon          # `text` in dark orange along their trail
      after: 30
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
    approval_banner: "INPUT NEEDED - PRESS CTRL+L TO VIEW"

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
