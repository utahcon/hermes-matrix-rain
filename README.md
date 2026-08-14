# matrix-idle-rain

Matrix digital rain as a "your turn" beacon for Hermes CLI sessions.

While the agent is working: nothing. The moment it finishes its turn and
is waiting on you, green katakana rain takes over the terminal (after a
4-second grace period so you can start reading/typing without ever seeing
it). Press any key and the rain vanishes instantly — the alternate screen
buffer means your scrollback and the agent's response come back untouched.

Also rains (after 20s) when a dangerous-command approval prompt has been
sitting unanswered, so a walked-away-from approval gate calls you back.

## Behavior matrix

| Event                              | Effect                     |
|------------------------------------|----------------------------|
| Agent finishes turn (post_llm_call) | Rain starts after 4s       |
| You press any key                   | Rain dismissed, screen restored |
| New turn starts (pre_llm_call)      | Rain killed                |
| Tool activity (pre_tool_call)       | Rain killed                |
| Approval prompt waiting             | Rain starts after 20s      |
| Approval answered                   | Rain killed                |
| Hermes process exits                | Rain self-destructs (pid watchdog) |
| No controlling TTY (gateway/cron)   | Plugin stays dormant       |

## Files

- `plugin.yaml`   — manifest
- `__init__.py`   — hook wiring; spawns/kills the renderer
- `rain.py`       — stdlib-only ANSI renderer, writes directly to the TTY
                    device on the alternate screen buffer (no curses, no
                    stdin contention with the foreground TUI)

## Tuning

Edit `__init__.py`:
- `_DELAY_TURN_END` (default 4.0s) — grace before rain on turn end
- `_DELAY_APPROVAL` (default 20.0s) — grace before rain on approval wait

Edit `rain.py`: `FPS` (default 14), `GLYPHS`, color codes.

## Install

    git clone https://github.com/utahcon/hermes-matrix-rain.git ~/.hermes/plugins/matrix-idle-rain
    hermes plugins enable matrix-idle-rain

Takes effect on the next CLI session start (plugins load at startup).

Requires only Python stdlib and a POSIX TTY (Linux/macOS). No dependencies.

## Uninstall

    hermes plugins disable matrix-idle-rain
    rm -rf ~/.hermes/plugins/matrix-idle-rain

## Dismissal mechanics

The renderer polls the TTY's atime each frame — any keystroke on the
terminal updates it, which the renderer treats as "user is here" and exits.
SIGTERM from the plugin (new turn / tool call) and parent-pid death are the
other two exits. All paths restore the screen via `\e[?1049l`.
