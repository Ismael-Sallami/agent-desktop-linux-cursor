# Changelog

## 2.0.0

- Built-in AT-SPI accessibility engine (`scripts/adc_a11y.py`): `snapshot`, `find`, `wait`, `get`,
  `is`, `click`, `right-click`, `type`, `set-value`, `clear`, `select`, `toggle`, `check`,
  `uncheck`, `expand`, `collapse`, `focus`, `scroll-to`, `list-apps`, `close-app`. The
  agent-desktop CLI and Node.js are no longer required.
- Correct screen coordinates for GTK 4 applications; labels for GTK 4 menu items; popovers listed
  under their expanded button.
- Nested mode types with the real keyboard, scrolls off-screen elements into view before acting,
  and falls back to a real click for elements without an accessible action.
- `select` no longer deselects an option that was already chosen and closes dropdowns it opened.
- `close-app --force`; a hint when an application keeps a dialog open.
- Fix: the private accessibility bus took over the user's own accessibility socket
  (`$XDG_RUNTIME_DIR/at-spi/bus_<n>`); each nested display now gets its own.
- `adc doctor` and `adc version`.
- Packaged as a Claude Code plugin (`.claude-plugin/plugin.json`, `bin/adc`).

## 1.0.0

- First release: nested and shared modes, agent cursor, vision, hand-off, recording.
