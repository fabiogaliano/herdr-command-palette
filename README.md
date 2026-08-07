# herdr-command-palette

A fuzzy command palette for [Herdr](https://herdr.dev).

Press one key to open a popup. The popup lists every Herdr action. Each row shows the keyboard shortcut next to the action name. Select a row and press Enter to run it.

![Command Palette screenshot](https://raw.githubusercontent.com/fabiogaliano/herdr-command-palette/main/screenshot.png)

## What it does

- Shows **live agents** at the top, sorted by status: blocked → working → done → idle.
- Shows all pane, tab, workspace, worktree, and session actions.
- Shows your **custom commands** from `[[keys.command]]` with their shortcuts.
- Shows actions from **other installed plugins** automatically.
- Actions that need an argument (rename, switch, focus) open a second picker.
- Destructive actions (close workspace, remove worktree) ask for confirmation.

### Key-only rows

Some Herdr actions are client-side terminal modes. The palette cannot run them from a popup. These rows show the shortcut so you know which key to press. Examples: copy mode, resize mode, edit scrollback.

## Requirements

- Herdr 0.7.5 or later
- `fzf` on `PATH`
- Python 3.9 or later

## Install

```sh
herdr plugin install fabiogaliano/herdr-command-palette
```

Then bind it in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+space"
type = "shell"
command = "herdr plugin pane open --plugin palette --entrypoint palette"
description = "Command palette"
```

Reload the config:

```sh
herdr server reload-config
```

### Install from a local checkout

```sh
git clone https://github.com/fabiogaliano/herdr-command-palette.git
herdr plugin link ./herdr-command-palette
```

## How it works

A shell trampoline (`bin/herdr-palette`) starts Python with `-S -E` flags to skip site processing. This cuts interpreter startup time.

The Python script does three things in parallel at startup:
1. Fetches the agent list.
2. Fetches the workspace list.
3. Fetches other plugins' actions.

It reads your `config.toml` for keybindings and theme colors, then hands everything to `fzf`. Filtering runs server-side: each keystroke re-renders the rows in Python rather than using fzf's built-in matcher, so group headings stay with their rows.

Total time from keypress to first paint is approximately 45 ms.

## Configuration

The palette works without configuration. It reads your `[keys]` and `[theme.custom]` sections from `config.toml` automatically.

To customize the palette itself, create a `config.toml` in the plugin config directory. Herdr sets this path at runtime as `HERDR_PLUGIN_CONFIG_DIR`. Find it with:

```sh
herdr plugin list
```

All settings are optional. Defaults apply when a setting is absent.

```toml
# Glyph shown for the prefix key in shortcut hints.
# Default: the rendered prefix chord (e.g. "^;" for ctrl+semicolon).
# Set a glyph to use a fixed symbol instead.
leader = "✦"

# How modifier keys are displayed: "symbol" (^⌥⇧⌘) or "text" (Ctrl+Alt+Shift+Cmd).
# Default: "symbol" on macOS, "text" on Linux.
modifier_style = "symbol"

# Show actions the palette cannot run (copy mode, resize mode, etc.).
# These rows display the shortcut so you know which key to press.
# Default: true.
show_key_only = true

# Order of groups in the palette. Omit a group to hide it.
# Default: ["agents", "actions", "custom", "plugins"].
group_order = ["agents", "actions", "custom", "plugins"]

# Agent status glyphs. Override any or all.
[glyphs]
working = "●"
done = "✓"
idle = "◌"
blocked = "▲"
unknown = "·"
```

### Theme integration

Row text colors (shortcut hints, group headings, dim text) are derived from your `[theme.custom]` section:

| Theme token  | Used for           |
|------------- |------------------- |
| `accent`     | Shortcut hints     |
| `overlay0`   | Group headings     |
| `subtext0`   | Dim / secondary    |

If no custom theme is set, the palette falls back to default terminal colors.

## Tracing

Set `HERDR_PALETTE_TRACE` to a file path to log startup timing:

```sh
export HERDR_PALETTE_TRACE=/tmp/palette-trace.log
```

## License

[MIT](LICENSE)
