#!/usr/bin/env python3
"""Fuzzy command palette for Herdr: search every action, see its shortcut, run it."""

from __future__ import annotations

# Kept deliberately small: every import here is paid on every popup, and on
# every keystroke too (fzf reloads by respawning this script). shutil, tempfile,
# dataclasses and typing between them cost more than the rest of startup.
import json
import os
import re
import shlex
import subprocess
import sys
import threading  # free: subprocess has already imported it


def terminal_width(default: int = 100) -> int:
    # COLUMNS first, like shutil does: the --rows reload runs with no tty of its
    # own, and fzf exports the popup's width into the child's environment.
    try:
        return int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        pass
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default


def which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate
    return None


HERDR = os.environ.get("HERDR_BIN_PATH") or which("herdr") or "herdr"

# Set HERDR_PALETTE_TRACE=/tmp/some.log to time startup; the trampoline writes
# to the same file, so the log spans from pane spawn to first paint.
TRACE = os.environ.get("HERDR_PALETTE_TRACE")


def trace(label: str) -> None:
    if not TRACE:
        return
    import time

    start = float(os.environ.get("HERDR_PALETTE_T0") or time.time())
    try:
        with open(TRACE, "a") as fh:
            fh.write(f"{time.time():.3f} +{(time.time() - start) * 1000:7.1f}ms  {label}\n")
    except OSError:
        pass


CONFIG = os.path.expanduser("~/.config/herdr/config.toml")

RESET = "\x1b[0m"
# Defaults; overridden in main() from [theme.custom] when available.
DIM = "\x1b[2m"
KEY = "\x1b[35m"
CAT = "\x1b[36m"

# Glyph and colour per agent_status, so a blocked agent is findable at a glance.
# Mutable: main() merges user overrides from [glyphs] in plugin config.
AGENT_STATUS = {
    "working": ("●", "\x1b[33m"),
    "done": ("✓", "\x1b[32m"),
    "idle": ("◌", DIM),
    "blocked": ("▲", "\x1b[31m"),
}
AGENT_STATUS_UNKNOWN = ("·", DIM)

DEFAULT_GLYPHS = {
    "working": "●",
    "done": "✓",
    "idle": "◌",
    "blocked": "▲",
    "unknown": "·",
}


def hex_to_ansi(hex_color: str) -> str:
    """Convert #rrggbb to a 24-bit ANSI foreground sequence."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return ""
    try:
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    except ValueError:
        return ""
    return f"\x1b[38;2;{r};{g};{b}m"


def load_plugin_config() -> dict:
    """Read the plugin's own config from HERDR_PLUGIN_CONFIG_DIR/config.toml."""
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        return {}
    path = os.path.join(config_dir, "config.toml")
    try:
        import tomllib
    except ModuleNotFoundError:
        return _parse_plugin_config_fallback(path)
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def _parse_plugin_config_fallback(path: str) -> dict:
    """Minimal reader for plugin config on Python < 3.11."""
    result: dict[str, object] = {}
    section: str | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            if section not in result:
                result[section] = {}
            continue
        if "=" not in line:
            continue
        name, _, raw = line.partition("=")
        name = name.strip()
        raw = raw.strip()
        if raw.startswith("["):
            values = re.findall(r'"([^"]*)"', raw)
            value: object = values
        elif raw.startswith('"'):
            value = raw.strip('"')
        elif raw.lower() in ("true", "false"):
            value = raw.lower() == "true"
        else:
            value = raw
        if section:
            result.setdefault(section, {})[name] = value  # type: ignore[union-attr]
        else:
            result[name] = value
    return result


def apply_theme_colors(herdr_config: dict) -> None:
    """Override KEY/CAT/DIM from [theme.custom] so row text matches the theme."""
    global KEY, CAT, DIM
    custom = (herdr_config.get("theme") or {}).get("custom") or {}
    accent = custom.get("accent")
    if accent:
        KEY = hex_to_ansi(accent) or KEY
    overlay = custom.get("overlay0")
    if overlay:
        CAT = hex_to_ansi(overlay) or CAT
    subtext = custom.get("subtext0")
    if subtext:
        DIM = hex_to_ansi(subtext) or DIM


def apply_glyph_config(plugin_config: dict) -> None:
    """Merge user glyph overrides into AGENT_STATUS."""
    glyphs = plugin_config.get("glyphs")
    if not glyphs or not isinstance(glyphs, dict):
        return
    global AGENT_STATUS, AGENT_STATUS_UNKNOWN
    for status in ("working", "done", "idle", "blocked"):
        glyph = glyphs.get(status)
        if glyph and status in AGENT_STATUS:
            _, color = AGENT_STATUS[status]
            AGENT_STATUS[status] = (glyph, color)
    unknown_glyph = glyphs.get("unknown")
    if unknown_glyph:
        AGENT_STATUS_UNKNOWN = (unknown_glyph, AGENT_STATUS_UNKNOWN[1])

# Sorted by how much the agent wants from you: blocked is waiting on an answer,
# working may block soon, done is finished, idle wants nothing.
AGENT_STATUS_ORDER = {"blocked": 0, "working": 1, "done": 2, "idle": 3}
AGENT_STATUS_ORDER_LAST = len(AGENT_STATUS_ORDER)


# --- herdr CLI -------------------------------------------------------------


class HerdrError(RuntimeError):
    pass


def herdr(*args: str) -> dict:
    proc = subprocess.run(
        [HERDR, *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise HerdrError((proc.stderr or proc.stdout).strip() or f"herdr {' '.join(args)} failed")
    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out).get("result", {})
    except json.JSONDecodeError:
        return {}


_PENDING: dict[tuple, threading.Thread] = {}
_FETCHED: dict[tuple, dict] = {}


def prefetch(*args: str) -> None:
    """Start a read in the background; herdr_quiet collects it later.

    The startup reads don't depend on each other, so they overlap with config
    parsing and with one another instead of running as four serial round-trips.
    """

    def run() -> None:
        try:
            _FETCHED[args] = herdr(*args)
        except HerdrError:
            _FETCHED[args] = {}

    thread = threading.Thread(target=run, daemon=True)
    _PENDING[args] = thread
    thread.start()


def herdr_quiet(*args: str) -> dict:
    """Best-effort call for optional data (plugin lists, agent lists)."""
    thread = _PENDING.pop(args, None)
    if thread:
        thread.join()
        return _FETCHED.pop(args, {})
    try:
        return herdr(*args)
    except HerdrError:
        return {}


# --- context ---------------------------------------------------------------


class Context:
    def __init__(
        self,
        pane_id: str | None = None,
        tab_id: str | None = None,
        workspace_id: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self.pane_id = pane_id
        self.tab_id = tab_id
        self.workspace_id = workspace_id
        self.cwd = cwd


def plugin_context() -> dict:
    """Opened as a plugin pane, the invoking pane arrives as JSON in the env."""
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_context() -> Context:
    env = os.environ.get
    plugin = plugin_context()
    ctx = Context(
        # Three launch paths, three shapes: custom keybindings export
        # HERDR_ACTIVE_*, plugin actions export the shorter HERDR_* names, and a
        # plugin pane gets HERDR_PLUGIN_CONTEXT_JSON instead of either.
        pane_id=env("HERDR_ACTIVE_PANE_ID") or env("HERDR_PANE_ID") or plugin.get("pane_id"),
        tab_id=env("HERDR_ACTIVE_TAB_ID") or env("HERDR_TAB_ID") or plugin.get("tab_id"),
        workspace_id=env("HERDR_ACTIVE_WORKSPACE_ID") or env("HERDR_WORKSPACE_ID")
        or plugin.get("workspace_id"),
        cwd=env("HERDR_ACTIVE_PANE_CWD") or plugin.get("workspace_cwd"),
    )
    if not ctx.pane_id:
        pane = herdr_quiet("pane", "current").get("pane") or {}
        ctx.pane_id = pane.get("pane_id")
        ctx.tab_id = ctx.tab_id or pane.get("tab_id")
        ctx.workspace_id = ctx.workspace_id or pane.get("workspace_id")
    return ctx


# --- config + keymap -------------------------------------------------------

DEFAULT_KEYS = {
    "prefix": "ctrl+b",
    "help": "prefix+?",
    "settings": "prefix+s",
    "goto": "prefix+g",
    "detach": "prefix+q",
    "reload_config": "prefix+shift+r",
    "open_notification_target": "prefix+o",
    "new_workspace": "prefix+shift+n",
    "rename_workspace": "prefix+shift+w",
    "close_workspace": "prefix+shift+d",
    "workspace_picker": "prefix+w",
    "new_worktree": "prefix+shift+g",
    "new_tab": "prefix+c",
    "rename_tab": "prefix+shift+t",
    "previous_tab": "prefix+p",
    "next_tab": "prefix+n",
    "switch_tab": "prefix+1..9",
    "close_tab": "prefix+shift+x",
    "split_vertical": "prefix+v",
    "split_horizontal": "prefix+minus",
    "close_pane": "prefix+x",
    "zoom": "prefix+z",
    "rename_pane": "prefix+shift+p",
    "copy_mode": "prefix+[",
    "edit_scrollback": "prefix+e",
    "resize_mode": "prefix+r",
    "toggle_sidebar": "prefix+b",
    "cycle_pane_next": "prefix+tab",
    "cycle_pane_previous": "prefix+shift+tab",
    "focus_pane_left": "prefix+h",
    "focus_pane_down": "prefix+j",
    "focus_pane_up": "prefix+k",
    "focus_pane_right": "prefix+l",
    "swap_pane_left": "prefix+shift+h",
    "swap_pane_down": "prefix+shift+j",
    "swap_pane_up": "prefix+shift+k",
    "swap_pane_right": "prefix+shift+l",
}


def load_config() -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:
        return parse_keys_fallback()
    try:
        with open(CONFIG, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def parse_keys_fallback() -> dict:
    """Minimal [keys] reader for Pythons older than 3.11 (no tomllib)."""
    keys: dict[str, object] = {}
    section = None
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        if section != "keys" or "=" not in line:
            continue
        name, _, raw = line.partition("=")
        raw = raw.strip()
        if raw.startswith("["):
            values = re.findall(r'"([^"]*)"', raw)
            keys[name.strip()] = values
        else:
            keys[name.strip()] = raw.strip('"')
    return {"keys": keys}


SYMBOL_MODS = {
    "ctrl": "^",
    "control": "^",
    "alt": "⌥",
    "opt": "⌥",
    "option": "⌥",
    "shift": "⇧",
    "cmd": "⌘",
    "super": "⌘",
    "meta": "⌘",
}
TEXT_MODS = {
    "ctrl": "Ctrl+",
    "control": "Ctrl+",
    "alt": "Alt+",
    "opt": "Alt+",
    "option": "Alt+",
    "shift": "Shift+",
    "cmd": "Cmd+",
    "super": "Super+",
    "meta": "Meta+",
}

# Set in main() based on plugin config / platform detection.
MOD_SYMBOLS = SYMBOL_MODS

NAMED_KEYS = {
    "semicolon": ";",
    "comma": ",",
    "period": ".",
    "minus": "-",
    "plus": "+",
    "backslash": "\\",
    "slash": "/",
    "quote": "'",
    "backtick": "`",
    "equal": "=",
}


def resolve_modifier_style(plugin_config: dict) -> dict:
    """Pick symbol or text modifier glyphs from config, falling back to platform."""
    style = plugin_config.get("modifier_style", "")
    if style == "text":
        return TEXT_MODS
    if style == "symbol":
        return SYMBOL_MODS
    # Auto-detect: macOS uses symbols, everything else uses text.
    return SYMBOL_MODS if sys.platform == "darwin" else TEXT_MODS


def render_chord(chord: str) -> str:
    parts = chord.split("+")
    # A shifted letter reads better as the capital you actually type.
    if any(p.lower() == "shift" for p in parts) and len(parts[-1]) == 1:
        parts = [p for p in parts if p.lower() != "shift"][:-1] + [parts[-1].upper()]
    out = []
    for part in parts:
        low = part.lower()
        if low in MOD_SYMBOLS:
            out.append(MOD_SYMBOLS[low])
        elif low in NAMED_KEYS:
            out.append(NAMED_KEYS[low])
        elif low in ("enter", "tab", "space", "esc", "escape", "up", "down", "left", "right"):
            out.append(low)
        elif len(part) == 1:
            out.append(part)
        else:
            out.append(part)
    return "".join(out)


class Keymap:
    def __init__(self, config: dict, leader: str = "") -> None:
        keys = config.get("keys", {}) or {}
        self.raw = {**DEFAULT_KEYS, **{k: v for k, v in keys.items() if isinstance(v, (str, list))}}
        prefix = self.raw.get("prefix", "ctrl+b")
        self.prefix = render_chord(prefix if isinstance(prefix, str) else prefix[0])
        # Empty string = use the rendered prefix chord; any other value = use it.
        self.leader = leader if leader else self.prefix

    def chord(self, name: str | None) -> str:
        if not name:
            return ""
        value = self.raw.get(name)
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, str) or not value:
            return ""
        return self.render(value)

    def render(self, value: str) -> str:
        if value.startswith("prefix+"):
            return f"{self.leader} {render_chord(value[len('prefix+'):])}"
        if value == "prefix":
            return self.leader
        return render_chord(value)


# --- prompts ---------------------------------------------------------------


def ask(prompt: str, default: str = "") -> str | None:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return value or default or None


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def pick(rows: list[tuple[str, str]], header: str) -> str | None:
    """Second-stage picker; rows are (value, display)."""
    if not rows:
        print(f"{DIM}nothing to pick{RESET}")
        return None
    return run_fzf(rows, header)


FZF_COLORS: list[str] = []


def fzf_theme(config: dict) -> list[str]:
    """Match fzf to [theme.custom] so the popup reads as one surface.

    Without this fzf paints its own background over Herdr's popup panel, which
    shows up as a band of the wrong navy inside the border.
    """
    custom = (config.get("theme") or {}).get("custom") or {}
    bg = custom.get("panel_bg") or custom.get("surface0")
    if not bg:  # no custom theme, or the pre-3.11 fallback parser
        return []
    spec = {
        "bg": bg,
        "bg+": custom.get("surface1"),
        "fg": custom.get("text"),
        "fg+": custom.get("text"),
        "hl": custom.get("accent"),
        "hl+": custom.get("accent"),
        "prompt": custom.get("overlay0"),
        "info": custom.get("subtext0"),
        "border": bg,
        "pointer": custom.get("accent"),
        "marker": custom.get("mauve"),
        # The rail matches the popup background so only the pointer shows; the
        # scrollbar stays one step up, visible but not a stripe.
        "gutter": bg,
        "scrollbar": custom.get("surface1", bg),
    }
    pairs = [f"{name}:{value}" for name, value in spec.items() if value]
    return [f"--color={','.join(pairs)}"]


def encode_rows(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"{value}\t{display}" for value, display in rows)


def run_fzf(
    rows: list[tuple[str, str]],
    header: str,
    specs: dict | list | None = None,
    focus_id: str | None = None,
) -> str | None:
    payload = encode_rows(rows)
    swap: list[str] = []
    env = {**os.environ, "FZF_DEFAULT_OPTS": "", "FZF_DEFAULT_OPTS_FILE": ""}
    # Herdr gives the plugin a state dir; using it (rather than tempfile) keeps
    # one more module out of startup, and the pid keeps concurrent popups apart.
    state = os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp"
    cache_path = os.path.join(state, f"specs-{os.getpid()}.json") if specs else None
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(specs, fh)
        env["HERDR_PALETTE_SPECS"] = cache_path
        # fzf's own matcher would drop the group headings along with the rows
        # they label, so matching happens here instead and headings are redrawn
        # for whatever survives.
        # sys.executable, not the trampoline: the reload runs on every keystroke,
        # and re-resolving `env python3` through PATH each time is pure waste.
        # -S -E for the same reason the trampoline uses them: this respawns the
        # interpreter on every keystroke, so its startup is the typing latency.
        script = f"{shlex.quote(sys.executable)} -S -E {shlex.quote(os.path.abspath(__file__))}"
        # When the invoking pane is a live agent, pre-select its row so the
        # palette opens on "you are here" rather than always the top.
        initial_pos = 3
        if focus_id:
            for i, (rid, _) in enumerate(rows, 1):
                if rid == focus_id:
                    initial_pos = i
                    break
        swap = [
            "--disabled",
            f"--bind=change:reload({script} --rows {{q}})",
            # `result` fires once the new list is ready; on reload (typing) the
            # position resets to the top so filtered results stay predictable.
            f"--bind=result:pos({initial_pos})",
        ]
    proc = subprocess.run(
        [
            "fzf",
            "--ansi",
            "--delimiter=\t",
            "--with-nth=2..",
            "--layout=reverse",
            "--info=inline",
            "--no-multi",
            "--height=100%",
            # Herdr's popup already draws a frame; a second one from fzf reads as a bug.
            "--border=none",
            "--margin=0",
            "--padding=0",
            *([f"--header={header}"] if header else []),
            *FZF_COLORS,
            *swap,
            "--prompt=› ",
        ],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        # A user's FZF_DEFAULT_OPTS (--border, --height 40%) would fight the flags above.
        env=env,
    )
    if cache_path:
        try:
            os.unlink(cache_path)
        except OSError:
            pass
    trace("fzf returned")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split("\t", 1)[0]


# --- actions ---------------------------------------------------------------


class Action:
    def __init__(
        self,
        id: str,
        title: str,
        category: str,
        key: str | None = None,  # config key name this action is bound to
        run=None,
        chord: str = "",  # pre-rendered, for dynamic rows with no config key
        chord_color: str = "",  # override for the right-hand column's colour
        note: str = "",  # dim trailing text, already coloured
        raw_title: bool = False,  # keep the title verbatim, don't strip the category
    ) -> None:
        self.id = id
        self.title = title
        self.category = category
        self.key = key
        self.run = run
        self.chord = chord
        self.chord_color = chord_color
        self.note = note
        self.raw_title = raw_title

    @property
    def teach_only(self) -> bool:
        return self.run is None


def need(value: str | None, what: str) -> str:
    if not value:
        raise HerdrError(f"no {what} in context")
    return value


def relative_focus(kind: str, offset: int, ctx: Context) -> None:
    """Focus the previous/next tab or workspace by position in the live list."""
    if kind == "tab":
        items = herdr("tab", "list").get("tabs", [])
        key, focus = "tab_id", ("tab", "focus")
    else:
        items = herdr("workspace", "list").get("workspaces", [])
        key, focus = "workspace_id", ("workspace", "focus")
    if not items:
        raise HerdrError(f"no {kind}s")
    index = next((i for i, item in enumerate(items) if item.get("focused")), 0)
    target = items[(index + offset) % len(items)]
    herdr(*focus, target[key])


def pick_workspace(header: str) -> str | None:
    rows = [
        (ws["workspace_id"], f"{ws.get('number', '')}  {ws.get('label', ws['workspace_id'])}")
        for ws in herdr("workspace", "list").get("workspaces", [])
    ]
    return pick(rows, header)


def build_actions() -> list[Action]:
    A = Action
    actions: list[Action] = [
        # Panes
        # --focus follows the split: you asked for a new pane, so you want to be in it.
        A("split_right", "Split pane right", "pane", "split_vertical",
          lambda c: herdr("pane", "split", "--current", "--direction", "right", "--focus")),
        A("split_down", "Split pane down", "pane", "split_horizontal",
          lambda c: herdr("pane", "split", "--current", "--direction", "down", "--focus")),
        A("zoom", "Toggle pane zoom", "pane", "zoom",
          lambda c: herdr("pane", "zoom", "--pane", need(c.pane_id, "pane"))),
        A("close_pane", "Close pane", "pane", "close_pane",
          lambda c: herdr("pane", "close", need(c.pane_id, "pane"))),
        A("rename_pane", "Rename pane…", "pane", "rename_pane", rename_pane),
        *[
            A(f"focus_pane_{d}", f"Focus pane {d}", "pane", f"focus_pane_{d}",
              (lambda d: lambda c: herdr("pane", "focus", "--current", "--direction", d))(d))
            for d in ("left", "down", "up", "right")
        ],
        *[
            A(f"swap_pane_{d}", f"Swap pane {d}", "pane", f"swap_pane_{d}",
              (lambda d: lambda c: herdr("pane", "swap", "--current", "--direction", d))(d))
            for d in ("left", "down", "up", "right")
        ],
        A("copy_mode", "Copy mode", "pane", "copy_mode"),
        A("resize_mode", "Resize mode", "pane", "resize_mode"),
        A("edit_scrollback", "Edit scrollback", "pane", "edit_scrollback"),
        A("cycle_pane_next", "Cycle to next pane", "pane", "cycle_pane_next"),
        A("cycle_pane_previous", "Cycle to previous pane", "pane", "cycle_pane_previous"),
        A("last_pane", "Last pane", "pane", "last_pane"),
        # Tabs
        A("new_tab", "New tab", "tab", "new_tab",
          lambda c: herdr("tab", "create", "--focus")),
        A("rename_tab", "Rename tab…", "tab", "rename_tab", rename_tab),
        A("close_tab", "Close tab", "tab", "close_tab",
          lambda c: herdr("tab", "close", need(c.tab_id, "tab"))),
        A("switch_tab", "Switch to tab…", "tab", "switch_tab", switch_tab),
        A("previous_tab", "Previous tab", "tab", "previous_tab",
          lambda c: relative_focus("tab", -1, c)),
        A("next_tab", "Next tab", "tab", "next_tab",
          lambda c: relative_focus("tab", 1, c)),
        # Workspaces
        A("new_workspace", "New workspace…", "workspace", "new_workspace", new_workspace),
        A("rename_workspace", "Rename workspace…", "workspace", "rename_workspace", rename_workspace),
        A("switch_workspace", "Switch to workspace…", "workspace", "workspace_picker", switch_workspace),
        A("previous_workspace", "Previous workspace", "workspace", "previous_workspace",
          lambda c: relative_focus("workspace", -1, c)),
        A("next_workspace", "Next workspace", "workspace", "next_workspace",
          lambda c: relative_focus("workspace", 1, c)),
        A("close_workspace", "Close workspace", "workspace", "close_workspace", close_workspace),
        # Worktrees
        A("new_worktree", "New worktree…", "worktree", "new_worktree", new_worktree),
        A("open_worktree", "Open worktree…", "worktree", "open_worktree", open_worktree),
        A("remove_worktree", "Remove worktree", "worktree", "remove_worktree", remove_worktree),
        # Agents
        A("focus_agent", "Focus agent…", "agent", "focus_agent", focus_agent),
        A("previous_agent", "Previous agent", "agent", "previous_agent"),
        A("next_agent", "Next agent", "agent", "next_agent"),
        # Session
        A("goto", "Session navigator", "session", "goto"),
        A("help", "Keybinding help", "session", "help"),
        A("settings", "Settings", "session", "settings"),
        A("toggle_sidebar", "Toggle sidebar", "session", "toggle_sidebar"),
        A("detach", "Detach session", "session", "detach"),
        A("open_notification_target", "Open notification target", "session", "open_notification_target"),
        A("reload_config", "Reload config", "session", "reload_config",
          lambda c: herdr("server", "reload-config")),
    ]
    return actions


def rename_pane(ctx: Context) -> None:
    label = ask("New pane name")
    if label:
        herdr("pane", "rename", need(ctx.pane_id, "pane"), label)


def rename_tab(ctx: Context) -> None:
    label = ask("New tab name")
    if label:
        herdr("tab", "rename", need(ctx.tab_id, "tab"), label)


def switch_tab(ctx: Context) -> None:
    rows = [
        (tab["tab_id"], f"{tab.get('number', '')}  {tab.get('label', tab['tab_id'])}")
        for tab in herdr("tab", "list").get("tabs", [])
    ]
    tab_id = pick(rows, "switch to tab")
    if tab_id:
        herdr("tab", "focus", tab_id)


def new_workspace(ctx: Context) -> None:
    label = ask("Workspace name (blank for default)")
    args = ["workspace", "create", "--focus"]
    if label:
        args += ["--label", label]
    herdr(*args)


def rename_workspace(ctx: Context) -> None:
    label = ask("New workspace name")
    if label:
        herdr("workspace", "rename", need(ctx.workspace_id, "workspace"), label)


def switch_workspace(ctx: Context) -> None:
    workspace_id = pick_workspace("switch to workspace")
    if workspace_id:
        herdr("workspace", "focus", workspace_id)


def close_workspace(ctx: Context) -> None:
    workspace_id = need(ctx.workspace_id, "workspace")
    if confirm(f"Close workspace {workspace_id}?"):
        herdr("workspace", "close", workspace_id)


def new_worktree(ctx: Context) -> None:
    branch = ask("Branch name")
    if not branch:
        return
    args = ["worktree", "create", "--branch", branch, "--focus"]
    if ctx.workspace_id:
        args += ["--workspace", ctx.workspace_id]
    herdr(*args)


def worktree_rows(ctx: Context) -> list[tuple[str, str]]:
    args = ["worktree", "list", "--json"]
    if ctx.workspace_id:
        args += ["--workspace", ctx.workspace_id]
    trees = herdr(*args).get("worktrees", [])
    rows = []
    for tree in trees:
        branch = tree.get("branch") or tree.get("path", "")
        rows.append((branch, f"{branch}  {DIM}{tree.get('path', '')}{RESET}"))
    return rows


def open_worktree(ctx: Context) -> None:
    branch = pick(worktree_rows(ctx), "open worktree")
    if not branch:
        return
    args = ["worktree", "open", "--branch", branch, "--focus"]
    if ctx.workspace_id:
        args += ["--workspace", ctx.workspace_id]
    herdr(*args)


def remove_worktree(ctx: Context) -> None:
    workspace_id = need(ctx.workspace_id, "workspace")
    if confirm(f"Remove worktree checkout for {workspace_id}?"):
        herdr("worktree", "remove", "--workspace", workspace_id)


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    return f"~{path[len(home):]}" if path.startswith(home + os.sep) else path


def agent_entries() -> list[tuple[str, str, str, str, str]]:
    """Live agents as (pane_id, workspace number, label, cwd, status)."""
    # agent list reports workspace_id only; the human-readable number and label
    # live on the workspace record.
    workspaces = {
        ws["workspace_id"]: ws
        for ws in herdr_quiet("workspace", "list").get("workspaces", [])
    }
    entries = []
    for agent in herdr_quiet("agent", "list").get("agents", []):
        pane_id = agent.get("pane_id")
        if not pane_id:
            continue
        workspace_id = agent.get("workspace_id", "")
        ws = workspaces.get(workspace_id, {})
        entries.append(
            (
                pane_id,
                str(ws.get("number", "")),
                ws.get("label") or workspace_id,
                tilde(agent.get("foreground_cwd") or agent.get("cwd", "")),
                agent.get("agent_status") or "unknown",
            )
        )

    def rank(entry: tuple[str, str, str, str, str]) -> tuple:
        number = entry[1]
        # Workspace order within a status, so a row doesn't move unless its
        # status actually changed.
        return (
            AGENT_STATUS_ORDER.get(entry[4], AGENT_STATUS_ORDER_LAST),
            int(number) if number.isdigit() else 1 << 30,
            entry[2],
        )

    return sorted(entries, key=rank)


def focus_agent(ctx: Context) -> None:
    entries = agent_entries()
    number_w = max((len(e[1]) for e in entries), default=0)
    label_w = max((len(e[2]) for e in entries), default=0)
    cwd_w = max((len(e[3]) for e in entries), default=0)

    rows = []
    for pane_id, number, label, cwd, status in entries:
        glyph, colour = AGENT_STATUS.get(status, AGENT_STATUS_UNKNOWN)
        rows.append(
            (
                pane_id,
                f"{number:<{number_w}}  {label:<{label_w}}  "
                f"{DIM}{cwd:<{cwd_w}}{RESET}  {colour}{glyph} {status}{RESET}",
            )
        )
    target = pick(rows, "focus agent")
    if target:
        herdr("agent", "focus", target)


# --- dynamic rows ----------------------------------------------------------


def live_agent_actions() -> list[Action]:
    """One row per live agent, so focusing one is a single pick, not two."""
    entries = agent_entries()
    number_w = max((len(e[1]) for e in entries), default=0)
    label_w = max((len(e[2]) for e in entries), default=0)
    rows = []
    for pane_id, number, label, cwd, status in entries:
        glyph, colour = AGENT_STATUS.get(status, AGENT_STATUS_UNKNOWN)
        rows.append(
            Action(
                id=f"agent:{pane_id}",
                # Padded here rather than by render_rows: the title column is
                # sized across every action, and these two sub-columns should
                # line up with each other, not with "Split pane right".
                title=f"{glyph} {number:<{number_w}}  {label:<{label_w}}",
                category="agents",
                run=(lambda p: lambda c: herdr("agent", "focus", p))(pane_id),
                chord=status,
                chord_color=colour,
                note=f"{DIM}{cwd}{RESET}",
                raw_title=True,
            )
        )
    return rows


def plugin_actions() -> list[Action]:
    """Every action contributed by other installed plugins, for free."""
    rows = []
    for item in herdr_quiet("plugin", "action", "list", "--json").get("actions", []):
        plugin_id = item.get("plugin_id", "")
        action_id = item.get("action_id", "")
        if plugin_id == "palette":
            continue
        rows.append(
            Action(
                id=f"plugin:{plugin_id}:{action_id}",
                title=item.get("title", action_id),
                category=f"plugin:{plugin_id}",
                run=(lambda p, a: lambda c: herdr("plugin", "action", "invoke", a, "--plugin", p))(
                    plugin_id, action_id
                ),
            )
        )
    return rows


def make_shell_runner(command: str):
    def run(ctx: Context) -> None:
        subprocess.Popen(
            command,
            shell=True,
            cwd=ctx.cwd or None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    return run


def custom_commands(config: dict, keymap: Keymap) -> list[Action]:
    entries = ((config.get("keys") or {}).get("command")) or []
    rows = []
    for index, entry in enumerate(entries):
        command = entry.get("command", "")
        if not command or "--plugin palette" in command or "--plugin=palette" in command:
            continue
        kind = entry.get("type", "shell")
        title = entry.get("description") or command
        chord = keymap.render(entry.get("key", "")) if entry.get("key") else ""
        # Only `shell` commands are safe to run from inside the palette popup;
        # popup/pane types need the client to own the terminal, which it can't
        # while this popup is on screen.
        run = make_shell_runner(command) if kind == "shell" else None
        rows.append(Action(f"custom:{index}", title, f"custom:{kind}", run=run, chord=chord))
    return rows


# --- palette ---------------------------------------------------------------


SEPARATOR = "__group__"


def strip_category(title: str, category: str) -> str:
    """"Split pane right" under the "pane" heading reads as "Split right"."""
    stem = category.rstrip("s") or category
    out = re.sub(rf"\b{re.escape(stem)}s?\b", "", title, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([…:.])", r"\1", out).strip()
    if not out:  # a title that is only its category, e.g. "Workspaces"
        return title
    return out[0].upper() + out[1:]


def row_specs(actions: list[Action], keymap: Keymap) -> list[dict]:
    """Flatten actions to plain data so re-rendering per keystroke stays cheap."""
    return [
        {
            "id": action.id,
            "cat": action.category,
            "title": action.title if action.raw_title
            else strip_category(action.title, action.category),
            "keys": action.chord or keymap.chord(action.key),
            "keycolor": action.chord_color,
            "note": action.note,
            "teach": action.teach_only,
        }
        for action in actions
    ]


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def matches(query: str, haystack: str) -> bool:
    """Substring per term, falling back to subsequence, AND across terms."""
    haystack = haystack.lower()
    for term in query.lower().split():
        if term in haystack:
            continue
        chars = iter(haystack)
        if not all(char in chars for char in term):
            return False
    return True


def render_rows(specs: list[dict], query: str = "") -> list[tuple[str, str]]:
    width = terminal_width()
    # Column widths come from the full set so they don't jitter while typing.
    title_w = max(24, min(46, max((len(s["title"]) for s in specs), default=24) + 2))
    key_w = 16
    rows: list[tuple[str, str]] = []
    group = None
    for spec in specs:
        haystack = f"{spec['cat']} {spec['title']} {spec['keys']} {strip_ansi(spec.get('note', ''))}"
        if query and not matches(query, haystack):
            continue
        if spec["cat"] != group:
            rows.append((SEPARATOR, ""))
            group = spec["cat"]
            rule = "─" * max(4, title_w + key_w - len(group) - 4)
            rows.append((SEPARATOR, f"{CAT}── {group} {rule}{RESET}"))
        keys = spec["keys"] or "—"
        keys_color = spec.get("keycolor") or (KEY if spec["keys"] else DIM)
        note = f"{DIM} key only{RESET}" if spec["teach"] else spec.get("note", "")
        display = f"{spec['title']:<{title_w}}{keys_color}{keys:<{key_w}}{RESET}{note}"
        rows.append((spec["id"], display[: width + 64]))
    return rows


def emit_rows(query: str) -> int:
    """`--rows <query>`: what fzf reloads on every keystroke."""
    cache = os.environ.get("HERDR_PALETTE_SPECS")
    if not cache:
        return 1
    try:
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 1
    # The cache is either a bare list (v0.1) or a dict with colors (v0.2+).
    if isinstance(data, dict):
        specs = data.get("specs", [])
        colors = data.get("colors", {})
        global KEY, DIM, CAT
        KEY = colors.get("key", KEY)
        DIM = colors.get("dim", DIM)
        CAT = colors.get("cat", CAT)
    else:
        specs = data
    print(encode_rows(render_rows(specs, query)))
    return 0


GROUP_BUILDERS = {
    "agents": lambda config, keymap: live_agent_actions(),
    "actions": lambda config, keymap: build_actions(),
    "custom": lambda config, keymap: custom_commands(config, keymap),
    "plugins": lambda config, keymap: plugin_actions(),
}
DEFAULT_GROUP_ORDER = ["agents", "actions", "custom", "plugins"]


def fatal(problem: str, remedy: str) -> int:
    """A popup pane is torn down the moment this process exits, and stderr goes
    with it, so returning 1 here reads as the palette flashing and vanishing
    rather than as an error. Hold the frame until a keypress instead.
    """
    print(f"\n  {problem}\n\n  {remedy}\n")
    print(f"  PATH={os.environ.get('PATH', '')}\n")
    if sys.stdin.isatty():
        print("  Press Enter to close.")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    return 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--rows":
        return emit_rows(sys.argv[2] if len(sys.argv) > 2 else "")

    if not which("fzf"):
        return fatal(
            "herdr-palette requires fzf on PATH.",
            "Install fzf, or start the Herdr server from a login shell so it "
            "inherits your PATH.",
        )

    # HERDR falls back to a bare "herdr" when PATH is too thin to resolve it,
    # which would otherwise fail later as an empty palette rather than an error.
    if not which(HERDR) and not os.access(HERDR, os.X_OK):
        return fatal(
            "herdr-palette cannot find the herdr binary.",
            "Set HERDR_BIN_PATH to its full path, or start the Herdr server "
            "from a login shell so it inherits your PATH.",
        )

    trace("main entered")
    prefetch("agent", "list")
    prefetch("workspace", "list")
    prefetch("plugin", "action", "list", "--json")
    config = load_config()
    pcfg = load_plugin_config()

    # Apply resolved settings to module globals before anything renders.
    global FZF_COLORS, MOD_SYMBOLS
    FZF_COLORS = fzf_theme(config)
    apply_theme_colors(config)
    apply_glyph_config(pcfg)
    MOD_SYMBOLS = resolve_modifier_style(pcfg)

    leader = pcfg.get("leader", "")
    keymap = Keymap(config, leader=leader)
    ctx = load_context()
    trace("config + context")

    show_key_only = pcfg.get("show_key_only", True)
    group_order = pcfg.get("group_order", DEFAULT_GROUP_ORDER)
    if isinstance(group_order, str):
        group_order = DEFAULT_GROUP_ORDER

    actions: list[Action] = []
    for group in group_order:
        builder = GROUP_BUILDERS.get(group)
        if builder:
            actions.extend(builder(config, keymap))

    if not show_key_only:
        actions = [a for a in actions if not a.teach_only]

    index = {action.id: action for action in actions}

    trace("actions built")
    specs = row_specs(actions, keymap)
    # Bake resolved colors into the cache so the --rows reload path uses them.
    cache_data = {
        "specs": specs,
        "colors": {"key": KEY, "dim": DIM, "cat": CAT},
    }
    rows = render_rows(specs)
    trace("rows rendered — handing off to fzf")
    # Pre-select the row for the currently focused agent, if any.
    active_agent = f"agent:{ctx.pane_id}" if ctx.pane_id else None
    # Group headings are selectable in fzf; treat picking one as "keep looking".
    while True:
        chosen = run_fzf(rows, "", specs=cache_data, focus_id=active_agent)
        if not chosen:
            return 0
        if chosen != SEPARATOR:
            break

    action = index.get(chosen)
    if not action:
        return 1

    if action.teach_only:
        chord = action.chord or keymap.chord(action.key)
        if chord:
            print(f"{action.title} is a client-side action — press {KEY}{chord}{RESET}")
        else:
            print(f"{action.title} is a client-side action with no binding configured.")
        input(f"{DIM}enter to close{RESET}")
        return 0

    try:
        action.run(ctx)
    except HerdrError as err:
        print(f"\x1b[31m{err}{RESET}", file=sys.stderr)
        input(f"{DIM}enter to close{RESET}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
