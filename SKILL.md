---
name: agent-desktop-linux-cursor
description: >
  Linux (X11/GNOME) desktop agent that operates applications like a person on its own machine
  without interfering with the user: a dedicated workspace, a private X display (own pointer,
  keyboard focus and clipboard), a private D-Bus and accessibility bus, a visible agent cursor
  with labels, screen inspection with a coordinate grid, human-like mouse and keyboard input,
  persistent browser profiles, user hand-off notifications for sign-ins, and screen recording.
  Wraps the agent-desktop CLI through `adc`. Use for any desktop GUI task on Linux, requests such
  as "use the computer", "work on another desktop or workspace", "do not touch my mouse",
  "record the screen", "agent cursor", multi-agent cursors, or whenever agent-desktop alone would
  take the user's focus.
---

# adc: the agent's own desktop on Linux

`adc` offers two modes. **Use nested mode by default.** Use shared mode only when the task
requires an application instance or window that already exists on the user's desktop.

| | Nested (`adc start --nested`) | Shared (`adc start`) |
|---|---|---|
| Location | Private Xephyr display inside a window on the agent workspace | User's X server, agent workspace |
| Input | Real pointer and keyboard of the private display: click, drag, hover, shortcuts, Enter | Headless AT-SPI actions only |
| Isolation | Complete: own focus, clipboard, D-Bus (allow-list: accessibility, dconf, gvfs), accessibility bus, XDG configuration | Focus guard reverts focus theft; brief flicker possible |
| Vision | `adc see` screenshot with coordinate grid | Not available (window is unmapped) |
| Recording | `--record` or `adc record` | Not available |
| Applications | Fresh instances; persistent agent browser profiles | Can reach running applications (`adopt`) |

Requirements: X11, `agent-desktop`, `xdotool`, `xprop`, `xclip`, ImageMagick, `ffmpeg`,
`Xephyr`, `picom`, `openbox` (or another window manager), `notify-send`, python3 with PyGObject
(GTK 3). Without a window manager, agent-desktop rejects nested windows
(`native_window_unproven`) and only `see`, `*-xy`, `key` and `write` remain usable.

## Nested workflow (observe, act, verify)

```bash
adc start --nested [--size 1600x900] [--record ~/Videos/task.mp4]
adc browser "https://example.com" --name work       # persistent agent browser profile
adc run gnome-text-editor                           # any application, private D-Bus
adc see [--scale 0.5] [--grid 100] [--region x,y,w,h]   # returns data.path; read the PNG
adc click-xy 640 312 --label "Open menu"            # grid labels are real coordinates
adc write "text" --target "Text Editor"             # returns data.focused; verify it
adc key Return ; adc key ctrl+l
adc drag-xy 100 200 400 200 ; adc scroll-xy 800 500 down 5 ; adc double-xy | right-xy | move
adc snapshot --app chrome -i --compact ; adc click @ref    # accessibility path
adc ask-user "Please sign in to LinkedIn"           # hand-off notification, blocks until answered
adc stop                                            # closes apps gracefully, saves the recording
```

- **Prefer accessibility references** when the application exposes them: they are exact and
  inexpensive. Use `see` with `*-xy` for canvases, games, poorly labelled interfaces, and visual
  verification. Keep `see` inexpensive with `--region` and `--scale 0.4` to `0.6`; `--grid 0`
  removes the grid.
- In nested mode, reference actions first move the real pointer to the element, then perform the
  semantic action. `--headed` and all physical commands are permitted, since the display is
  private.
- **Sign-ins, captchas, two-factor authentication, and anything only the user may do: run
  `adc ask-user "<what and where>"`.** A critical system notification appears on the user's
  desktop with "Go now" and "Not now". "Go now" brings the user to the agent workspace with the
  agent window focused, so the user's own keyboard types into it. "I'm done" returns the user to
  the previous workspace and window. The result is `data.answer`: `done`, `later`, `closed` or
  `timeout`. Never type user credentials. Notification language follows `ADC_LANG` or `LANG`
  (English and Spanish are included).
- Browser profiles belong to the agent and persist (`--name`, default `default`, stored under
  `~/.local/share/adc/browsers`). After one sign-in through `ask-user`, the site remains signed in
  across sessions. `--profile "<Chrome profile directory>"` copies a user profile for settings and
  bookmarks; site sessions generally do not survive the copy.
- Nested applications use an isolated XDG configuration, data, state and cache directory
  (`~/.local/share/adc/agent-home`), so they never restore or modify the user's open documents.
  Files in `$HOME` are shared, so saved files appear where the user expects them.
- `key` and `write` return `data.focused`, the window that received the input. If it is not the
  intended window, stop and refocus (`adc focus "<title>"` or a click). Never type into an
  unidentified window.
- Shortcuts are application specific (GNOME Text Editor: `ctrl+t` opens a tab, `ctrl+n` a
  window). When a shortcut has no visible effect, inspect with `see` and use the visible control.
- Close nested applications with `adc close-app <app>` (private accessibility bus). Never use
  `pkill` or `pgrep -x <app>`: they would also match the user's own instance.
- The nested display inherits the user's keyboard layout at start-up.

## Shared workflow

```bash
adc start [--desktop N]                        # overlay and focus guard on the agent workspace
adc launch APP [agent-desktop launch options]  # opens on the agent workspace without focus
adc snapshot ... ; adc click @ref --adc-label "..." ; adc type @ref "..."
adc stop
```

Commands refused with `ADC_POLICY`: `--headed`, focus changes, pointer and keyboard input,
`scroll`, clipboard access and shell surfaces. Set `ADC_ALLOW_PHYSICAL=1` only with the user's
explicit consent. `adc adopt --app <class>` moves a user's window to the agent workspace; ask
first.

## Rules

- Never switch the user's workspace and never use the user's pointer, keyboard or clipboard.
- Ask before irreversible or outward-facing actions (send, submit, apply, purchase, delete, post).
- Hand sign-ins, captchas and two-factor authentication to the user with `adc ask-user`.
- Multiple agents: share one session and set `ADC_AGENT=<id>` per agent for a distinct colour
  and label.
- Terminate helper processes by PID; never `pkill -f` a pattern that also appears in your own
  command line.

## Troubleshooting

| Symptom | Resolution |
|---|---|
| `native_window_unproven` in nested mode | Install `openbox`, then `adc stop` and `adc start --nested` |
| Application window takes about 25 s to appear | It requested a desktop portal; the D-Bus allow-list prevents this. Report the application |
| `see` returns a grey or black image | The overlay lacks alpha: `picom` is missing or not running |
| Chrome web content missing from the tree | Start it with `adc browser` (adds `--force-renderer-accessibility`) |
| Signed-in site asks to sign in again | Always end with `adc stop`, which closes the browser gracefully so cookies are written |
| `ADC_ALREADY_RUNNING` | Reuse the session or run `adc stop` |

## Files

- `scripts/adc`: command-line interface.
- `scripts/adc_nested.py`: nested display, isolation, input, vision, browser and hand-off.
- `scripts/adc-host.py`: host window created directly on the agent workspace, embedding Xephyr.
- `scripts/adc-overlay.py`: agent cursor, labels, effects and focus guard.
- `examples/excalidraw-demo.sh`: recorded drawing demonstration.
