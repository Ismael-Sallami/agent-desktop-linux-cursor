# agent-desktop-linux-cursor

An agent skill and command-line tool (`adc`) that lets an AI agent operate Linux desktop
applications on its own isolated desktop, with a visible agent cursor, while the user keeps
working undisturbed on another workspace.

It builds on [agent-desktop](https://github.com/vercel-labs/agent-desktop), which drives
applications through the accessibility tree. On GNOME/X11, agent-desktop's cursor overlay is not
rendered and newly launched windows appear on the user's workspace. `adc` addresses both issues
and extends the tool into a complete, isolated working environment for an agent.

![The agent drawing in Excalidraw on its own private display, with the agent cursor and intent labels](docs/demo.gif)

*Recorded with `examples/excalidraw-demo.sh`: the agent draws on its private display while the
user's pointer and workspace remain untouched.*

## Features

- **Dedicated workspace.** The agent works on its own GNOME workspace. Windows are created there
  directly, without appearing on the user's workspace and without taking focus.
- **Private display (nested mode).** A Xephyr display embedded in a window on the agent
  workspace gives the agent its own pointer, keyboard focus and clipboard. Physical input such as
  dragging, shortcuts and Enter is safe because it never reaches the user's session.
- **Isolation.** Nested applications run on a private D-Bus session restricted to an allow-list
  of services (accessibility, dconf, gvfs), a private accessibility bus, and isolated XDG
  configuration directories. Single-instance applications and notifications therefore cannot
  leak onto the user's desktop, and applications never restore the user's open documents.
- **Agent cursor.** A click-through overlay shows the agent's pointer, an intent label, click
  ripples, element outlines and a typing caret, with a distinct colour for each agent.
- **Vision and human-like input.** `adc see` captures the private display with a labelled
  coordinate grid. Pointer movements are eased and slightly curved; text input supports
  accented characters and line breaks.
- **Persistent browser profiles.** Chrome runs with an agent-owned profile that keeps site
  sessions across runs.
- **User hand-off.** `adc ask-user` raises a system notification when a sign-in, captcha or
  two-factor step is required. The user is taken to the agent workspace, completes the step with
  their own keyboard, and is returned to their previous workspace.
- **Focus guard (shared mode).** If an agent-launched window takes focus or switches the user's
  workspace, the change is reverted immediately.
- **Recording.** Sessions on the private display can be recorded to MP4 without the user
  watching the agent workspace.

## Requirements

- Linux with an X11 session (tested on Ubuntu 24.04, GNOME Shell 46). Wayland is not supported.
- [agent-desktop](https://github.com/vercel-labs/agent-desktop) (`npm install -g agent-desktop`).
- Packages: `xserver-xephyr`, `openbox`, `xdotool`, `x11-utils`, `x11-xserver-utils`, `xclip`,
  `imagemagick`, `ffmpeg`, `libnotify-bin`, `python3-gi`, and `picom`.
- Google Chrome or Chromium for `adc browser`.

```bash
sudo apt install xserver-xephyr openbox xdotool x11-utils x11-xserver-utils xclip \
  imagemagick ffmpeg libnotify-bin python3-gi picom
npm install -g agent-desktop
```

## Installation

Clone the repository into a skills directory recognised by your agent and expose `adc` on the
`PATH`:

```bash
git clone https://github.com/Ismael-Sallami/agent-desktop-linux-cursor.git \
  ~/.claude/skills/agent-desktop-linux-cursor
ln -s ~/.claude/skills/agent-desktop-linux-cursor/scripts/adc ~/.local/bin/adc
```

The agent reads `SKILL.md`, which describes when and how to use each command.

## Usage

```bash
adc start --nested --record ~/Videos/session.mp4
adc browser "https://www.linkedin.com/jobs/" --name linkedin
adc ask-user "Please sign in to LinkedIn"        # only needed once per profile
adc snapshot --app chrome -i --compact
adc click @s1abc:e12 --label "Open job offer"
adc see --region 600,130,1000,260 --scale 0.5
adc run gnome-text-editor
adc write "Summary of the results" --target "Text Editor"
adc key ctrl+s
adc stop
```

Run `adc --help` for the complete command list. Any command that `adc` does not define is
passed to agent-desktop unchanged, within the active session and environment.

A recorded demonstration is available in `examples/excalidraw-demo.sh`.

## Modes

| | Nested (`adc start --nested`) | Shared (`adc start`) |
|---|---|---|
| Display | Private Xephyr display on the agent workspace | User's X server, agent workspace |
| Input | Real pointer and keyboard of the private display | Headless accessibility actions only |
| Isolation | Complete | Focus guard |
| Vision and recording | Yes | No |
| Existing user applications | Not reachable | Reachable |

## Safety

- The agent never uses the user's pointer, keyboard or clipboard, and never switches the user's
  workspace.
- In shared mode, physical input, focus changes and clipboard access are refused unless the user
  explicitly allows them with `ADC_ALLOW_PHYSICAL=1`.
- Credentials are always entered by the user through `adc ask-user`.
- Agent browser profiles are stored in `~/.local/share/adc/browsers` with mode `0700`. Cookies
  are encrypted with Chrome's basic password store rather than the system keyring, so the
  protection of these profiles relies on file permissions.

## License

MIT. See [LICENSE](LICENSE).
