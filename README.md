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

No plugin-specific configuration is needed. The palette reads your existing `[keys]` and `[theme.custom]` sections from `config.toml`.

If you set custom keybindings, the palette shows your bindings. If you do not, it shows Herdr's defaults.

## Tracing

Set `HERDR_PALETTE_TRACE` to a file path to log startup timing:

```sh
export HERDR_PALETTE_TRACE=/tmp/palette-trace.log
```

## License

[MIT](LICENSE)
