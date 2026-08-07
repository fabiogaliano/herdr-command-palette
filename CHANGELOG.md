# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

### Added

- Plugin configuration via `$HERDR_PLUGIN_CONFIG_DIR/config.toml`.
- Leader symbol defaults to the rendered prefix chord; configurable via `leader`.
- Row text colors now derive from `[theme.custom]` in the Herdr config.
- Agent status glyphs configurable via a `[glyphs]` section.
- Modifier key style auto-detects the platform; configurable via `modifier_style`.
- Group ordering configurable via `group_order`.
- `show_key_only` option to hide teach-only actions.

### Fixed

- Self-exclusion filter now matches on plugin id instead of command substring.

## [0.1.0]

### Added

- Initial release: fuzzy command palette with live agents, pane/tab/workspace/worktree
  actions, custom commands, and other plugins' actions.
