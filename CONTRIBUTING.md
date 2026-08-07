# Contributing

Thanks for improving the Herdr command palette. This guide keeps contributions
fast to review and safe to ship.

## Set up for development

1. Clone the repo.
2. Link it into Herdr as a local plugin:

   ```sh
   herdr plugin link /path/to/herdr-command-palette
   ```

3. Herdr now runs your working copy instead of an installed release.

## Run and test

There is no separate test suite. The palette runs as a Herdr popup, so:

1. Open a Herdr session.
2. Trigger the palette (default binding, or your configured `prefix+space`).
3. Confirm your change: check the rows, the shortcuts, and the colors.
4. Repeat after every edit. Startup is fast, so this loop stays fast too.

## Trace performance

Set `HERDR_PALETTE_TRACE` to a file path before you open the palette:

```sh
HERDR_PALETTE_TRACE=/tmp/palette-trace.log herdr
```

Open the palette, then read the log. It records a timestamped line for every
stage, from pane spawn to first paint. Use it to catch regressions before you
ship them.

## Pull request guidelines

- Keep the plugin pure standard library. Do not add third-party dependencies.
  Every import you add is paid on every popup open, and on every keystroke
  during fuzzy search.
- Keep startup fast. Profile with `HERDR_PALETTE_TRACE` before and after your
  change. If a change adds noticeable latency, find a cheaper way first.
- Test in a real Herdr session before you open the PR. Do not rely on reading
  the code alone; run it.
- Describe the performance impact of your change in the PR description, even
  if it is "none".

## `DEFAULT_KEYS`

`DEFAULT_KEYS` mirrors Herdr's built-in default keybindings. Herdr's own docs
define the source of truth. Nothing keeps the two in sync automatically: if
Herdr adds, removes, or renames a default binding, update `DEFAULT_KEYS` by
hand in the same PR. Check both against the Herdr docs before you ship a
keybinding-related change.
