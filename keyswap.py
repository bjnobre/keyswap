#!/usr/bin/env python3
"""
keyswap: low-level keyboard combo substitution and text expansion daemon.

What it does:
- Watches one or more input devices through evdev
- Re-emits events through a uinput virtual device
- Supports combo substitutions (e.g. C-nk_minus -> "=")
- Supports text expansions (e.g. ":123" -> "1234567890")
- Decodes typed characters through xkbcommon so layout-dependent text works better

Notes:
- This daemon expects real keyboard devices in the config
- Removable devices may disappear and reappear with different /dev/input/eventN numbers
- Prefer stable paths like /dev/input/by-path/... or custom udev symlinks when possible
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import select
import signal
import sys
import tempfile
import time
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DeviceSelection = list[str] | str

from evdev import InputDevice, UInput, ecodes

# -----------------------------------------------------------------------------
# Configuration and constants
# -----------------------------------------------------------------------------

DEBUG_BEEP_ON_KEY_EVENTS = True
DEBUG_BEEP_ON_REPEAT = True
DEBUG_BEEP_COMMAND = ["gsound-play", "-i", "bell"]

SYSTEM_CONFIG_PATH = Path("/etc/keyswap/config.json")
INPUT_EVENT_GLOB = "/dev/input/event*"
AUTO_DEVICES_MODE = "auto"
AUTO_RESCAN_INTERVAL_SEC = 60.0
AUTO_POLL_TIMEOUT_MS = 500
INPUT_DIR = Path("/dev/input")

# Linux inotify constants. Used to avoid rescanning /dev/input on a timer.
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_ATTRIB = 0x00000004
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000
INOTIFY_EVENT_MASK = IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO | IN_ATTRIB

REAL_KEYBOARD_PROBE_KEYS = {
    ecodes.KEY_A, ecodes.KEY_B, ecodes.KEY_C, ecodes.KEY_D, ecodes.KEY_E,
    ecodes.KEY_F, ecodes.KEY_G, ecodes.KEY_H, ecodes.KEY_I, ecodes.KEY_J,
    ecodes.KEY_K, ecodes.KEY_L, ecodes.KEY_M, ecodes.KEY_N, ecodes.KEY_O,
    ecodes.KEY_P, ecodes.KEY_Q, ecodes.KEY_R, ecodes.KEY_S, ecodes.KEY_T,
    ecodes.KEY_U, ecodes.KEY_V, ecodes.KEY_W, ecodes.KEY_X, ecodes.KEY_Y,
    ecodes.KEY_Z, ecodes.KEY_SPACE,
}

KEYSWAP_UINPUT_NAME = "keyswap-uinput"
PSEUDO_KEYBOARD_NAMES = {
    KEYSWAP_UINPUT_NAME,
    "Power Button",
    "Sleep Button",
    "Video Bus",
    "Intel HID events",
    "Intel HID 5 button array",
    "PC Speaker",
    "Dell WMI hotkeys",
    "HDA Digital PCBeep",
}

# Timing controls for text expansion.
EXPANSION_BACKSPACE_SETTLE_MS = 10
TYPE_CHAR_DELAY_MS = 5
BACKSPACE_DELAY_MS = 1
EXPANSION_BUFFER_MAX_CHARS = 20

# Keep a quiet, bounded history of key traffic. It is only written to the
# journal when an anomalous state is detected, giving intermittent keyboard
# bugs useful context without enabling per-key debug logging all the time.
BUG_TRACE_MAX_EVENTS = 80

# XKB constants.
XKB_CONTEXT_NO_FLAGS = 0
XKB_KEYMAP_COMPILE_NO_FLAGS = 0
XKB_KEY_DOWN = 1
XKB_KEY_UP = 0

# Modifier aliases used in combo config strings.
MODIFIER_KEYCODES = {
    "C": {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL},
    "A": {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT},
    "S": {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT},
    "M": {ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA},
}
ALL_MODIFIER_KEYS = set().union(*MODIFIER_KEYCODES.values())

# Supported key names in config strings.
KEY_ALIASES = {
    "nk_minus": ecodes.KEY_KPMINUS,
    "nk_delete": ecodes.KEY_KPDOT,
    "nk_plus": ecodes.KEY_KPPLUS,
    "nk_enter": ecodes.KEY_KPENTER,
    "nk_0": ecodes.KEY_KP0,
    "nk_1": ecodes.KEY_KP1,
    "nk_2": ecodes.KEY_KP2,
    "nk_3": ecodes.KEY_KP3,
    "nk_4": ecodes.KEY_KP4,
    "nk_5": ecodes.KEY_KP5,
    "nk_6": ecodes.KEY_KP6,
    "nk_7": ecodes.KEY_KP7,
    "nk_8": ecodes.KEY_KP8,
    "nk_9": ecodes.KEY_KP9,
    "minus": ecodes.KEY_MINUS,
    "equal": ecodes.KEY_EQUAL,
    "dot": ecodes.KEY_DOT,
    "comma": ecodes.KEY_COMMA,
    "slash": ecodes.KEY_SLASH,
    "semicolon": ecodes.KEY_SEMICOLON,
    "apostrophe": ecodes.KEY_APOSTROPHE,
    "leftbrace": ecodes.KEY_LEFTBRACE,
    "rightbrace": ecodes.KEY_RIGHTBRACE,
    "backslash": ecodes.KEY_BACKSLASH,
    "grave": ecodes.KEY_GRAVE,
    "space": ecodes.KEY_SPACE,
    "enter": ecodes.KEY_ENTER,
    "tab": ecodes.KEY_TAB,
    "esc": ecodes.KEY_ESC,
    "delete": ecodes.KEY_DELETE,
    "backspace": ecodes.KEY_BACKSPACE,
}

# Characters the daemon can synthesize through uinput.
# This is separate from XKB decoding: XKB is used to understand what the user
# typed; this map is used to emit replacement text.
CHARMAP = {
    "a": (ecodes.KEY_A, False),
    "b": (ecodes.KEY_B, False),
    "c": (ecodes.KEY_C, False),
    "d": (ecodes.KEY_D, False),
    "e": (ecodes.KEY_E, False),
    "f": (ecodes.KEY_F, False),
    "g": (ecodes.KEY_G, False),
    "h": (ecodes.KEY_H, False),
    "i": (ecodes.KEY_I, False),
    "j": (ecodes.KEY_J, False),
    "k": (ecodes.KEY_K, False),
    "l": (ecodes.KEY_L, False),
    "m": (ecodes.KEY_M, False),
    "n": (ecodes.KEY_N, False),
    "o": (ecodes.KEY_O, False),
    "p": (ecodes.KEY_P, False),
    "q": (ecodes.KEY_Q, False),
    "r": (ecodes.KEY_R, False),
    "s": (ecodes.KEY_S, False),
    "t": (ecodes.KEY_T, False),
    "u": (ecodes.KEY_U, False),
    "v": (ecodes.KEY_V, False),
    "w": (ecodes.KEY_W, False),
    "x": (ecodes.KEY_X, False),
    "y": (ecodes.KEY_Y, False),
    "z": (ecodes.KEY_Z, False),
    "A": (ecodes.KEY_A, True),
    "B": (ecodes.KEY_B, True),
    "C": (ecodes.KEY_C, True),
    "D": (ecodes.KEY_D, True),
    "E": (ecodes.KEY_E, True),
    "F": (ecodes.KEY_F, True),
    "G": (ecodes.KEY_G, True),
    "H": (ecodes.KEY_H, True),
    "I": (ecodes.KEY_I, True),
    "J": (ecodes.KEY_J, True),
    "K": (ecodes.KEY_K, True),
    "L": (ecodes.KEY_L, True),
    "M": (ecodes.KEY_M, True),
    "N": (ecodes.KEY_N, True),
    "O": (ecodes.KEY_O, True),
    "P": (ecodes.KEY_P, True),
    "Q": (ecodes.KEY_Q, True),
    "R": (ecodes.KEY_R, True),
    "S": (ecodes.KEY_S, True),
    "T": (ecodes.KEY_T, True),
    "U": (ecodes.KEY_U, True),
    "V": (ecodes.KEY_V, True),
    "W": (ecodes.KEY_W, True),
    "X": (ecodes.KEY_X, True),
    "Y": (ecodes.KEY_Y, True),
    "Z": (ecodes.KEY_Z, True),
    "ç": (ecodes.KEY_SEMICOLON, False),
    "Ç": (ecodes.KEY_SEMICOLON, True),
    "0": (ecodes.KEY_0, False),
    "1": (ecodes.KEY_1, False),
    "2": (ecodes.KEY_2, False),
    "3": (ecodes.KEY_3, False),
    "4": (ecodes.KEY_4, False),
    "5": (ecodes.KEY_5, False),
    "6": (ecodes.KEY_6, False),
    "7": (ecodes.KEY_7, False),
    "8": (ecodes.KEY_8, False),
    "9": (ecodes.KEY_9, False),
    "=": (ecodes.KEY_EQUAL, False),
    "+": (ecodes.KEY_EQUAL, True),
    "-": (ecodes.KEY_MINUS, False),
    "_": (ecodes.KEY_MINUS, True),
    ".": (ecodes.KEY_DOT, False),
    ">": (ecodes.KEY_DOT, True),
    ",": (ecodes.KEY_COMMA, False),
    "<": (ecodes.KEY_COMMA, True),
    "/": (ecodes.KEY_RO, False),
    "?": (ecodes.KEY_RO, True),
    ";": (ecodes.KEY_SEMICOLON, False),
    # Brazilian ABNT2: Shift+/ produces ':'; Shift+; produces 'Ç'.
    ":": (ecodes.KEY_SLASH, True),
    "'": (ecodes.KEY_APOSTROPHE, False),
    '"': (ecodes.KEY_APOSTROPHE, True),
    "[": (ecodes.KEY_LEFTBRACE, False),
    "{": (ecodes.KEY_LEFTBRACE, True),
    "]": (ecodes.KEY_RIGHTBRACE, False),
    "}": (ecodes.KEY_RIGHTBRACE, True),
    "\\": (ecodes.KEY_BACKSLASH, False),
    "|": (ecodes.KEY_BACKSLASH, True),
    "`": (ecodes.KEY_GRAVE, False),
    "~": (ecodes.KEY_GRAVE, True),
    " ": (ecodes.KEY_SPACE, False),
    "\n": (ecodes.KEY_ENTER, False),
    "!": (ecodes.KEY_1, True),
    "@": (ecodes.KEY_2, True),
    "#": (ecodes.KEY_3, True),
    "$": (ecodes.KEY_4, True),
    "%": (ecodes.KEY_5, True),
    "^": (ecodes.KEY_6, True),
    "&": (ecodes.KEY_7, True),
    "*": (ecodes.KEY_8, True),
    "(": (ecodes.KEY_9, True),
    ")": (ecodes.KEY_0, True),
}

# Characters produced with an ABNT2 dead key followed by a base character.
# Keep these separate from CHARMAP because each character requires two taps.
ABNT2_DEAD_KEYS = {
    "acute": (ecodes.KEY_LEFTBRACE, False),
    "grave": (ecodes.KEY_LEFTBRACE, True),
    "tilde": (ecodes.KEY_APOSTROPHE, False),
    "circumflex": (ecodes.KEY_APOSTROPHE, True),
    "diaeresis": (ecodes.KEY_6, True),
}

DEAD_KEY_CHARACTERS = {
    "acute": ("áéíóúýÁÉÍÓÚÝ", "aeiouyAEIOUY"),
    "grave": ("àèìòùÀÈÌÒÙ", "aeiouAEIOU"),
    "tilde": ("ãẽĩõũñÃẼĨÕŨÑ", "aeiounAEIOUN"),
    "circumflex": ("âêîôûÂÊÎÔÛ", "aeiouAEIOU"),
    "diaeresis": ("äëïöüÿÄËÏÖÜŸ", "aeiouyAEIOUY"),
}

DEAD_KEY_CHARMAP: dict[str, tuple[tuple[int, bool], tuple[int, bool]]] = {}
for accent_name, (accented_chars, base_chars) in DEAD_KEY_CHARACTERS.items():
    dead_key = ABNT2_DEAD_KEYS[accent_name]
    for accented_char, base_char in zip(accented_chars, base_chars):
        DEAD_KEY_CHARMAP[accented_char] = (dead_key, CHARMAP[base_char])

CHARMAP_KEYCODES = {keycode for keycode, _needs_shift in CHARMAP.values()}
CHARMAP_KEYCODES.update(
    keycode
    for steps in DEAD_KEY_CHARMAP.values()
    for keycode, _needs_shift in steps
)

# Uinput capabilities cannot be expanded after the virtual device is created.
# Keep navigation keys available even when the keyboard that provides them is
# disconnected at startup and added later by auto-discovery.
NAVIGATION_KEYCODES = {
    ecodes.KEY_HOME,
    ecodes.KEY_UP,
    ecodes.KEY_PAGEUP,
    ecodes.KEY_LEFT,
    ecodes.KEY_RIGHT,
    ecodes.KEY_END,
    ecodes.KEY_DOWN,
    ecodes.KEY_PAGEDOWN,
}

# -----------------------------------------------------------------------------
# Runtime mutable state
# -----------------------------------------------------------------------------

@dataclass
class DeviceKeyState:
    pressed: set[int] = field(default_factory=set)
    forwarded_modifiers: set[int] = field(default_factory=set)
    suppressed_keyups: set[int] = field(default_factory=set)
    triggered_combo_keys: set[int] = field(default_factory=set)
    reported_orphan_repeats: set[int] = field(default_factory=set)


device_states: dict[str, DeviceKeyState] = {}
virtual_key_owners: dict[int, set[str]] = {}
virtual_pressed_keys: set[int] = set()
bug_trace: deque[dict[str, Any]] = deque(maxlen=BUG_TRACE_MAX_EVENTS)

last_debug_beep_time = 0.0
last_poll_wakeup_time = time.monotonic()

typed_buffer = ""

pending_expansion_match: dict[str, Any] | None = None
pending_expansion_run: dict[str, Any] | None = None

open_devices: list[InputDevice] = []
virtual_uinput: UInput | None = None
logger: logging.Logger | None = None
xkb_decoder: "XKBDecoder | None" = None


# -----------------------------------------------------------------------------
# XKB bindings
# -----------------------------------------------------------------------------

class XKBRuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


class XKBDecoder:
    """Minimal xkbcommon wrapper for decoding typed characters."""

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        try:
            self.lib = ctypes.cdll.LoadLibrary("libxkbcommon.so.0")
        except OSError as exc:
            raise RuntimeError("libxkbcommon.so.0 not found. Install libxkbcommon0.") from exc

        self._setup_signatures()

        self.context = self.lib.xkb_context_new(XKB_CONTEXT_NO_FLAGS)
        if not self.context:
            raise RuntimeError("failed to create xkb context")

        names = XKBRuleNames(
            self._encode(cfg.get("rules") or os.environ.get("XKB_DEFAULT_RULES")),
            self._encode(cfg.get("model") or os.environ.get("XKB_DEFAULT_MODEL")),
            self._encode(cfg.get("layout") or os.environ.get("XKB_DEFAULT_LAYOUT") or "us"),
            self._encode(cfg.get("variant") or os.environ.get("XKB_DEFAULT_VARIANT")),
            self._encode(cfg.get("options") or os.environ.get("XKB_DEFAULT_OPTIONS")),
        )

        self.keymap = self.lib.xkb_keymap_new_from_names(
            self.context,
            ctypes.byref(names),
            XKB_KEYMAP_COMPILE_NO_FLAGS,
        )
        if not self.keymap:
            self.lib.xkb_context_unref(self.context)
            raise RuntimeError(
                "failed to create xkb keymap for "
                f"layout={cfg.get('layout') or os.environ.get('XKB_DEFAULT_LAYOUT') or 'us'}"
            )

        self.state = self.lib.xkb_state_new(self.keymap)
        if not self.state:
            self.lib.xkb_keymap_unref(self.keymap)
            self.lib.xkb_context_unref(self.context)
            raise RuntimeError("failed to create xkb state")

        self.layout_info = {
            "rules": cfg.get("rules") or os.environ.get("XKB_DEFAULT_RULES"),
            "model": cfg.get("model") or os.environ.get("XKB_DEFAULT_MODEL"),
            "layout": cfg.get("layout") or os.environ.get("XKB_DEFAULT_LAYOUT") or "us",
            "variant": cfg.get("variant") or os.environ.get("XKB_DEFAULT_VARIANT"),
            "options": cfg.get("options") or os.environ.get("XKB_DEFAULT_OPTIONS"),
        }

    def _setup_signatures(self) -> None:
        self.lib.xkb_context_new.argtypes = [ctypes.c_int]
        self.lib.xkb_context_new.restype = ctypes.c_void_p

        self.lib.xkb_context_unref.argtypes = [ctypes.c_void_p]
        self.lib.xkb_context_unref.restype = None

        self.lib.xkb_keymap_new_from_names.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(XKBRuleNames),
            ctypes.c_int,
        ]
        self.lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p

        self.lib.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
        self.lib.xkb_keymap_unref.restype = None

        self.lib.xkb_state_new.argtypes = [ctypes.c_void_p]
        self.lib.xkb_state_new.restype = ctypes.c_void_p

        self.lib.xkb_state_unref.argtypes = [ctypes.c_void_p]
        self.lib.xkb_state_unref.restype = None

        self.lib.xkb_state_update_key.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
        self.lib.xkb_state_update_key.restype = ctypes.c_int

        self.lib.xkb_state_key_get_utf8.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        self.lib.xkb_state_key_get_utf8.restype = ctypes.c_int

    @staticmethod
    def _encode(value: str | None) -> bytes | None:
        return None if value is None else value.encode("utf-8")

    @staticmethod
    def _to_xkb_keycode(evdev_keycode: int) -> int:
        return evdev_keycode + 8

    def update_key(self, evdev_keycode: int, is_down: bool) -> None:
        self.lib.xkb_state_update_key(
            self.state,
            self._to_xkb_keycode(evdev_keycode),
            XKB_KEY_DOWN if is_down else XKB_KEY_UP,
        )

    def char_for_keydown(self, evdev_keycode: int) -> str | None:
        xkb_keycode = self._to_xkb_keycode(evdev_keycode)
        buf = ctypes.create_string_buffer(64)
        n = self.lib.xkb_state_key_get_utf8(self.state, xkb_keycode, buf, len(buf))
        if n <= 0:
            return None
        text = buf.value.decode("utf-8", errors="ignore")
        return text or None

    def reset_keys(self, pressed_keycodes: set[int] | None = None) -> None:
        """Rebuild transient XKB key state after device loss or resume."""
        new_state = self.lib.xkb_state_new(self.keymap)
        if not new_state:
            raise RuntimeError("failed to reset xkb state")
        old_state = self.state
        self.state = new_state
        if old_state:
            self.lib.xkb_state_unref(old_state)
        for code in sorted(pressed_keycodes or set()):
            self.update_key(code, True)

    def close(self) -> None:
        if getattr(self, "state", None):
            self.lib.xkb_state_unref(self.state)
            self.state = None
        if getattr(self, "keymap", None):
            self.lib.xkb_keymap_unref(self.keymap)
            self.keymap = None
        if getattr(self, "context", None):
            self.lib.xkb_context_unref(self.context)
            self.context = None


# -----------------------------------------------------------------------------
# Logging and config loading
# -----------------------------------------------------------------------------

def resolve_user_config_path() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "keyswap" / "config.json"
    return Path.home() / ".config" / "keyswap" / "config.json"


def setup_logging(log_level: str) -> logging.Logger:
    handlers = [logging.StreamHandler(sys.stdout)]
    level = getattr(logging, log_level.upper())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("keyswap")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keyboard substitution and text-expansion daemon and configuration CLI."
    )
    parser.add_argument("-c", "--config", help="Explicit config path")
    parser.add_argument(
        "--log-level",
        choices=("warning", "info", "debug"),
        default="warning",
        help="Logging detail (default: warning)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Alias for --log-level debug",
    )
    parser.add_argument("--dump-config", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="List configured mappings")
    list_parser.add_argument(
        "category",
        nargs="?",
        default="all",
        choices=("all", "substitutions", "expansions"),
    )

    add_parser = subparsers.add_parser("add", help="Add a configured mapping")
    add_parser.add_argument("mapping_type", choices=("substitution", "expansion"))
    add_parser.add_argument("trigger")
    add_parser.add_argument("replacement")
    add_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing mapping with the same trigger",
    )

    delete_parser = subparsers.add_parser("delete", help="Delete a configured mapping")
    delete_parser.add_argument("mapping_type", choices=("substitution", "expansion"))
    delete_parser.add_argument("trigger")

    subparsers.add_parser("test", help="Validate the configuration")
    service_help = {
        "start": "Start the user service",
        "stop": "Stop the user service",
        "restart": "Restart the user service",
        "status": "Show user service status",
    }
    for command, help_text in service_help.items():
        subparsers.add_parser(command, help=help_text)

    history_parser = subparsers.add_parser(
        "history",
        help="Show logs from the systemd user journal",
    )
    history_parser.add_argument(
        "-n",
        "--lines",
        type=positive_int,
        default=100,
        help="Number of journal lines to show (default: 100)",
    )
    history_parser.add_argument("--since", help="journalctl time expression")
    history_parser.add_argument(
        "--bugs",
        action="store_true",
        help="Show only existing BUG_CONTEXT incident entries",
    )
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def resolve_config_path(explicit_path: str | None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    user_config = resolve_user_config_path()
    if user_config.is_file():
        return user_config.resolve()
    return SYSTEM_CONFIG_PATH


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_argument_parser().parse_args(argv)


def parse_key(name: str) -> int:
    lowered = name.lower()

    if lowered in KEY_ALIASES:
        return KEY_ALIASES[lowered]

    if len(lowered) == 1 and "a" <= lowered <= "z":
        return getattr(ecodes, f"KEY_{lowered.upper()}")

    if len(lowered) == 1 and "0" <= lowered <= "9":
        return getattr(ecodes, f"KEY_{lowered}")

    ec_name = f"KEY_{lowered.upper()}"
    if hasattr(ecodes, ec_name):
        return getattr(ecodes, ec_name)

    raise ValueError(f"unknown key '{name}'")


def parse_combo(combo: str) -> tuple[set[str], int]:
    parts = combo.split("-")
    required_modifiers: set[str] = set()

    for part in parts[:-1]:
        if part not in MODIFIER_KEYCODES:
            raise ValueError(f"unknown modifier '{part}' in combo '{combo}'")
        required_modifiers.add(part)

    return required_modifiers, parse_key(parts[-1])


def load_raw_config(config_path: Path) -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if not isinstance(raw, dict):
        raise ValueError("config root must be an object/map")
    if "substitutions" not in raw:
        raise ValueError("config must contain 'substitutions'")
    if not isinstance(raw["substitutions"], dict):
        raise ValueError("'substitutions' must be an object/map")
    for combo, output in raw["substitutions"].items():
        if not isinstance(combo, str) or not combo:
            raise ValueError("substitution combos must be non-empty strings")
        if not isinstance(output, str):
            raise ValueError(f"substitution output for {combo!r} must be a string")

    if "sequences" in raw:
        raise ValueError(
            "unsupported config key 'sequences'; rename it to 'expansions'"
        )
    expansions = raw.get("expansions", {})
    if not isinstance(expansions, dict):
        raise ValueError("'expansions' must be an object/map")
    for trigger, replacement in expansions.items():
        if not isinstance(trigger, str) or not trigger:
            raise ValueError("expansion triggers must be non-empty strings")
        if not isinstance(replacement, str):
            raise ValueError(f"expansion replacement for {trigger!r} must be a string")
        if len(trigger) > EXPANSION_BUFFER_MAX_CHARS:
            raise ValueError(
                f"expansion trigger {trigger!r} exceeds the "
                f"{EXPANSION_BUFFER_MAX_CHARS}-character limit"
            )
    return raw


def load_config(config_path: Path) -> tuple[DeviceSelection, dict, dict, dict]:
    raw = load_raw_config(config_path)

    if "devices" in raw:
        configured_devices = raw["devices"]
    elif "device" in raw:
        configured_devices = raw["device"]
    else:
        configured_devices = AUTO_DEVICES_MODE

    if configured_devices == AUTO_DEVICES_MODE:
        device_selection: DeviceSelection = AUTO_DEVICES_MODE
    elif isinstance(configured_devices, str):
        device_selection = [configured_devices]
    elif isinstance(configured_devices, list) and configured_devices == [AUTO_DEVICES_MODE]:
        device_selection = AUTO_DEVICES_MODE
    elif isinstance(configured_devices, list) and configured_devices:
        if not all(isinstance(item, str) and item for item in configured_devices):
            raise ValueError("'devices' entries must be non-empty strings")
        device_selection = configured_devices
    else:
        raise ValueError("'devices' must be 'auto' or a non-empty list of paths")

    substitutions = {}
    for combo, output in raw["substitutions"].items():
        required_modifiers, main_key = parse_combo(combo)
        substitutions[(frozenset(required_modifiers), main_key)] = {
            "combo_text": combo,
            "output": output,
        }

    expansions = raw.get("expansions", {})

    xkb_config = raw.get("xkb", {})
    if not isinstance(xkb_config, dict):
        raise ValueError("'xkb' must be an object/map when present")

    return device_selection, substitutions, expansions, xkb_config


def write_config_atomically(config_path: Path, raw: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = config_path.stat().st_mode & 0o777 if config_path.exists() else 0o600
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(existing_mode)
        os.replace(temporary_path, config_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def mapping_section(mapping_type: str) -> str:
    return "substitutions" if mapping_type == "substitution" else "expansions"


def list_config(config_path: Path, category: str) -> None:
    raw = load_raw_config(config_path)
    print(f"config: {config_path}")
    if category in ("all", "substitutions"):
        print("substitutions:")
        for trigger, replacement in raw["substitutions"].items():
            print(f"  {trigger} -> {replacement!r}")
    if category in ("all", "expansions"):
        print("expansions:")
        for trigger, replacement in raw.get("expansions", {}).items():
            print(f"  {trigger!r} -> {replacement!r}")


def add_mapping(
    config_path: Path,
    mapping_type: str,
    trigger: str,
    replacement: str,
    force: bool,
) -> None:
    raw = load_raw_config(config_path)
    section_name = mapping_section(mapping_type)
    section = raw.setdefault(section_name, {})
    if trigger in section and not force:
        raise ValueError(
            f"{mapping_type} {trigger!r} already exists; use --force to replace it"
        )
    section[trigger] = replacement
    validate_raw_config(raw, config_path)
    write_config_atomically(config_path, raw)
    print(f"added {mapping_type} {trigger!r} -> {replacement!r}")


def delete_mapping(config_path: Path, mapping_type: str, trigger: str) -> None:
    raw = load_raw_config(config_path)
    section_name = mapping_section(mapping_type)
    section = raw.get(section_name, {})
    if trigger not in section:
        raise ValueError(f"{mapping_type} {trigger!r} does not exist")
    del section[trigger]
    validate_raw_config(raw, config_path)
    write_config_atomically(config_path, raw)
    print(f"deleted {mapping_type} {trigger!r}")


def validate_raw_config(raw: dict[str, Any], config_path: Path) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(raw, handle)
        load_config(temporary_path)
    except ValueError as exc:
        raise ValueError(f"invalid change for {config_path}: {exc}") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def run_service_command(command: str) -> int:
    args = ["systemctl", "--user", command, "keyswap.service"]
    if command == "status":
        args.append("--no-pager")
    return subprocess.run(args, check=False).returncode


def show_history(lines: int, since: str | None, bugs_only: bool) -> int:
    args = [
        "journalctl",
        "--user",
        "-u",
        "keyswap.service",
        "--no-pager",
        "-n",
        str(lines),
    ]
    if since:
        args.extend(["--since", since])
    if bugs_only:
        args.extend(["--grep", "BUG_CONTEXT"])
    return subprocess.run(args, check=False).returncode


def dump_loaded_config(
    config_path: Path,
    device_selection: DeviceSelection,
    substitutions: dict,
    expansions: dict,
    xkb_config: dict,
) -> None:
    print(f"config: {config_path}")
    print("devices:")
    if device_selection == AUTO_DEVICES_MODE:
        print("  - auto")
    else:
        for path in device_selection:
            print(f"  - {path}")

    print("substitutions:")
    for item in substitutions.values():
        print(f"  - {item['combo_text']} -> {item['output']}")

    print("expansions:")
    for trigger, replacement in expansions.items():
        print(f"  - {trigger!r} -> {replacement!r}")

    print("xkb:")
    for key in ("rules", "model", "layout", "variant", "options"):
        if xkb_config.get(key):
            print(f"  - {key}: {xkb_config[key]}")


# -----------------------------------------------------------------------------
# State helpers
# -----------------------------------------------------------------------------
def record_bug_trace(
    direction: str,
    code: int,
    value: int,
    *,
    device_name: str | None = None,
    reason: str | None = None,
) -> None:
    """Record key metadata only; never record decoded or substituted text."""
    item: dict[str, Any] = {
        "age_clock": round(time.monotonic(), 6),
        "direction": direction,
        "key": diagnostic_key_name(code),
        "value": value_name(value),
    }
    if device_name is not None:
        item["device"] = device_name
    if reason is not None:
        item["reason"] = reason
    bug_trace.append(item)


def dump_bug_context(reason: str, **details: Any) -> None:
    """Flush the recent key flight recorder to the persistent service journal."""
    if logger is None:
        bug_trace.clear()
        return

    now = time.monotonic()
    events = []
    for item in bug_trace:
        event = dict(item)
        event["age_ms"] = round((now - event.pop("age_clock")) * 1000)
        events.append(event)

    logger.warning(
        "BUG_CONTEXT reason=%s details=%s state=%s recent_events=%s",
        reason,
        details,
        diagnostic_state_snapshot(),
        events,
    )
    bug_trace.clear()


def all_pressed_physical_keys() -> set[int]:
    return set().union(*(state.pressed for state in device_states.values())) if device_states else set()


def all_state_codes(attribute: str) -> set[int]:
    return (
        set().union(*(getattr(state, attribute) for state in device_states.values()))
        if device_states
        else set()
    )


def is_physically_pressed(code: int) -> bool:
    return any(code in state.pressed for state in device_states.values())


def reset_transient_text_state(reason: str) -> None:
    global typed_buffer, pending_expansion_match, pending_expansion_run

    typed_buffer = ""
    pending_expansion_match = None
    pending_expansion_run = None
    if xkb_decoder is not None:
        xkb_decoder.reset_keys(all_pressed_physical_keys())
    if logger is not None:
        logger.debug("reset transient text/xkb state reason=%s", reason)


def release_device_keys(device_id: str, reason: str) -> None:
    """Remove one physical device without disturbing keys owned by others."""
    state = device_states.get(device_id)
    if state is None:
        return

    dump_bug_context(reason, device_id=device_id)
    released: list[int] = []
    for code, owners in list(virtual_key_owners.items()):
        if device_id not in owners:
            continue
        owners.discard(device_id)
        if owners:
            continue
        virtual_key_owners.pop(code, None)
        write_key(code, 0, f"device_release({reason})")
        released.append(code)

    if released:
        sync(f"device_release({reason})")

    device_states.pop(device_id, None)
    reset_transient_text_state(reason)
    if logger is not None:
        logger.warning(
            "released removed device state device_id=%s keys=%s remaining_devices=%s",
            device_id,
            [key_name(code) for code in released],
            sorted(device_states),
        )


def release_all_virtual_keys(reason: str) -> None:
    if virtual_uinput is None:
        return

    if reason not in {"startup", "shutdown"}:
        dump_bug_context(reason)

    keys_to_release = sorted({
        *ALL_MODIFIER_KEYS,
        *CHARMAP_KEYCODES,
        *all_pressed_physical_keys(),
        *all_state_codes("forwarded_modifiers"),
        *all_state_codes("suppressed_keyups"),
        *all_state_codes("triggered_combo_keys"),
        *virtual_pressed_keys,
        ecodes.KEY_UP,
        ecodes.KEY_DOWN,
        ecodes.KEY_LEFT,
        ecodes.KEY_RIGHT,
        ecodes.KEY_HOME,
        ecodes.KEY_END,
        ecodes.KEY_PAGEUP,
        ecodes.KEY_PAGEDOWN,
        ecodes.KEY_BACKSPACE,
        ecodes.KEY_SPACE,
    })

    if logger is not None:
        log = logger.debug if reason == "startup" else logger.warning
        log(
            "force releasing virtual keys reason=%s keys=%s",
            reason,
            [key_name(code) for code in keys_to_release],
        )

    for code in keys_to_release:
        send_key(code, 0, f"force_release({reason})")

    virtual_uinput.syn()

    device_states.clear()
    virtual_key_owners.clear()
    virtual_pressed_keys.clear()
    reset_transient_text_state(reason)

def release_stale_virtual_keys_if_idle() -> None:
    if virtual_uinput is None:
        return

    if all_pressed_physical_keys():
        return

    if not virtual_pressed_keys:
        return

    if logger is not None:
        logger.warning(
            "idle safety release for stale virtual keys: %s",
            [key_name(code) for code in sorted(virtual_pressed_keys)],
        )

    release_all_virtual_keys("idle_safety")

def key_name(code: int) -> str:
    return ecodes.KEY.get(code, f"UNKNOWN_{code}")


def diagnostic_key_name(code: int) -> str:
    # A journal is persistent and may be included in bug reports. Preserve the
    # keys relevant to state bugs, but do not leave reconstructable typed text.
    if code in CHARMAP_KEYCODES:
        return "<text-key>"
    return key_name(code)


def value_name(value: int) -> str:
    return {0: "up", 1: "down", 2: "repeat"}.get(value, str(value))

def debug_beep_for_key_event(code: int, value: int) -> None:
    global last_debug_beep_time

    if not DEBUG_BEEP_ON_KEY_EVENTS:
        return

    if logger is None or not logger.isEnabledFor(logging.DEBUG):
        return

    if value == 0:
        return

    if value == 2 and not DEBUG_BEEP_ON_REPEAT:
        return

    now = time.monotonic()
    if now - last_debug_beep_time < 0.05:
        return

    last_debug_beep_time = now

    try:
        subprocess.Popen(
            DEBUG_BEEP_COMMAND,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass

def physical_modifiers() -> set[str]:
    pressed = all_pressed_physical_keys()
    active: set[str] = set()
    for modifier_name, keycodes in MODIFIER_KEYCODES.items():
        if pressed & keycodes:
            active.add(modifier_name)
    return active


def has_non_text_modifier() -> bool:
    return bool(physical_modifiers() - {"S"})


def state_snapshot() -> dict[str, Any]:
    return {
        "pressed": [key_name(code) for code in sorted(all_pressed_physical_keys())],
        "virtual_owners": {
            key_name(code): sorted(owners)
            for code, owners in sorted(virtual_key_owners.items())
        },
        "devices": {
            device_id: {
                "pressed": [key_name(code) for code in sorted(state.pressed)],
                "forwarded_modifiers": [key_name(code) for code in sorted(state.forwarded_modifiers)],
                "suppressed_keyups": [key_name(code) for code in sorted(state.suppressed_keyups)],
                "triggered_keys": [key_name(code) for code in sorted(state.triggered_combo_keys)],
            }
            for device_id, state in sorted(device_states.items())
        },
        "mods": sorted(physical_modifiers()),
        "typed_buffer": typed_buffer[-40:],
        "pending_expansion_match": pending_expansion_match,
    }


def diagnostic_state_snapshot() -> dict[str, Any]:
    """Return persistent-log-safe state without typed or replacement text."""
    snapshot = state_snapshot()
    snapshot["typed_buffer"] = f"<redacted length={len(typed_buffer)}>"
    snapshot["pending_expansion_match"] = pending_expansion_match is not None
    return snapshot


# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------

def track_virtual_key_state(code: int, value: int) -> None:
    if value == 1:
        virtual_pressed_keys.add(code)
    elif value == 0:
        virtual_pressed_keys.discard(code)


def send_key(code: int, value: int, reason: str) -> None:
    record_bug_trace("out", code, value, reason=reason)
    track_virtual_key_state(code, value)

    logger.debug(
        "SEND key %-14s %-6s reason=%s virtual_pressed=%s physical_pressed=%s",
        key_name(code),
        value_name(value),
        reason,
        [key_name(item) for item in sorted(virtual_pressed_keys)],
        [key_name(item) for item in sorted(all_pressed_physical_keys())],
    )

    virtual_uinput.write(ecodes.EV_KEY, code, value)


def write_key(code: int, value: int, reason: str) -> None:
    send_key(code, value, reason)


def sync(reason: str) -> None:
    logger.debug("emit syn reason=%s", reason)
    virtual_uinput.syn()


def tap(code: int, reason: str) -> None:
    write_key(code, 1, reason)
    write_key(code, 0, reason)
    sync(reason)


def send_backspaces(count: int, reason: str) -> None:
    for _ in range(count):
        tap(ecodes.KEY_BACKSPACE, reason)
        time.sleep(BACKSPACE_DELAY_MS / 1000.0)


def forward_owned_key_event(event, device_id: str, device_name: str, reason: str) -> bool:
    """Forward aggregate key transitions while retaining per-device ownership."""
    code = event.code
    owners = virtual_key_owners.get(code)

    if event.value == 1:
        if owners is None:
            owners = set()
            virtual_key_owners[code] = owners
        if device_id in owners:
            return False
        first_owner = not owners
        owners.add(device_id)
        if first_owner:
            forward_event(event, reason, device_name)
        return first_owner

    if event.value == 0:
        if not owners or device_id not in owners:
            logger.debug("ignore unowned keyup key=%s dev=%s", key_name(code), device_name)
            return False
        owners.remove(device_id)
        if owners:
            return False
        virtual_key_owners.pop(code, None)
        forward_event(event, reason, device_name)
        return True

    raise ValueError(f"ownership helper received key value {event.value}")


def release_forwarded_modifiers() -> None:
    to_release = sorted(all_state_codes("forwarded_modifiers"))
    if not to_release:
        return

    logger.debug(
        "release_forwarded_modifiers begin state=%s releasing=%s",
        state_snapshot(),
        [key_name(code) for code in to_release],
    )

    for code in to_release:
        owners = virtual_key_owners.pop(code, set())
        for device_id in owners:
            state = device_states.get(device_id)
            if state is not None:
                state.suppressed_keyups.add(code)
                state.forwarded_modifiers.discard(code)
        if code in virtual_pressed_keys:
            write_key(code, 0, "release_forwarded_modifiers")

    sync("release_forwarded_modifiers")
    logger.debug("release_forwarded_modifiers end state=%s", state_snapshot())


def type_char(ch: str) -> None:
    if ch in DEAD_KEY_CHARMAP:
        logger.debug("type_char ch=%r dead_key_steps", ch)
        for keycode, needs_shift in DEAD_KEY_CHARMAP[ch]:
            type_keycode(keycode, needs_shift, ch)
        return

    if ch not in CHARMAP:
        raise ValueError(f"unsupported output character: {ch!r}")

    keycode, needs_shift = CHARMAP[ch]
    type_keycode(keycode, needs_shift, ch)


def type_keycode(keycode: int, needs_shift: bool, source_char: str) -> None:
    logger.debug(
        "type_char ch=%r key=%s needs_shift=%s",
        source_char,
        key_name(keycode),
        needs_shift,
    )

    shift_pressed = False

    try:
        if needs_shift:
            write_key(ecodes.KEY_LEFTSHIFT, 1, f"type_char({source_char}) shift_down")
            sync(f"type_char({source_char}) shift_down")
            shift_pressed = True

        tap(keycode, f"type_char({source_char})")

    finally:
        if shift_pressed:
            write_key(ecodes.KEY_LEFTSHIFT, 0, f"type_char({source_char}) shift_up")
            sync(f"type_char({source_char}) shift_up")


def type_text(text: str) -> None:
    logger.info("typing substitution text=%r", text)
    for ch in text:
        type_char(ch)
        time.sleep(TYPE_CHAR_DELAY_MS / 1000.0)


def forward_event(event, reason: str, device_name: str) -> None:
    if event.type == ecodes.EV_KEY:
        record_bug_trace(
            "out",
            event.code,
            event.value,
            device_name=device_name,
            reason=reason,
        )
        track_virtual_key_state(event.code, event.value)

        logger.debug(
            "SEND forward key %-14s %-6s dev=%s reason=%s virtual_pressed=%s physical_pressed=%s state=%s",
            key_name(event.code),
            value_name(event.value),
            device_name,
            reason,
            [key_name(item) for item in sorted(virtual_pressed_keys)],
            [key_name(item) for item in sorted(all_pressed_physical_keys())],
            state_snapshot(),
        )

    virtual_uinput.write_event(event)
    virtual_uinput.syn()


def create_virtual_uinput(devices: list[InputDevice]) -> UInput:
    """Create the fixed-capability output device used by current and future inputs."""
    capabilities: dict[int, set[Any]] = {}

    for dev in devices:
        for event_type, event_codes in dev.capabilities().items():
            if event_type in (ecodes.EV_SYN, ecodes.EV_FF):
                continue
            capabilities.setdefault(event_type, set()).update(event_codes)

    capabilities.setdefault(ecodes.EV_KEY, set()).update(NAVIGATION_KEYCODES)
    return UInput(events=capabilities, name=KEYSWAP_UINPUT_NAME)


# -----------------------------------------------------------------------------
# Text expansion helpers
# -----------------------------------------------------------------------------

def update_typed_buffer_from_keydown(code: int) -> None:
    global typed_buffer

    if code == ecodes.KEY_BACKSPACE:
        typed_buffer = typed_buffer[:-1]
        logger.debug("typed_buffer=%r", typed_buffer)
        return

    if code == ecodes.KEY_TAB:
        typed_buffer += "\t"
        typed_buffer = typed_buffer[-EXPANSION_BUFFER_MAX_CHARS:]
        logger.debug("typed_buffer=%r", typed_buffer)
        return

    if code == ecodes.KEY_ENTER:
        typed_buffer += "\n"
        typed_buffer = typed_buffer[-EXPANSION_BUFFER_MAX_CHARS:]
        logger.debug("typed_buffer=%r", typed_buffer)
        return

    if code == ecodes.KEY_ESC:
        typed_buffer = ""
        logger.debug("typed_buffer=%r", typed_buffer)
        return

    if xkb_decoder is None:
        return

    ch = xkb_decoder.char_for_keydown(code)
    if ch:
        typed_buffer += ch
        typed_buffer = typed_buffer[-EXPANSION_BUFFER_MAX_CHARS:]
        logger.debug("typed_buffer=%r", typed_buffer)


def maybe_mark_pending_expansion(expansions: dict[str, str], last_code: int) -> bool:
    global pending_expansion_match

    if pending_expansion_match is not None or not expansions or not typed_buffer:
        return False

    for trigger, replacement in expansions.items():
        if typed_buffer.endswith(trigger):
            pending_expansion_match = {
                "trigger": trigger,
                "replacement": replacement,
                "last_code": last_code,
            }
            logger.info(
                "pending expansion trigger=%r replacement=%r last_key=%s",
                trigger,
                replacement,
                key_name(last_code),
            )
            return True

    return False


def queue_pending_expansion_if_matches(code: int) -> bool:
    global pending_expansion_match, pending_expansion_run

    if pending_expansion_match is None:
        return False
    if pending_expansion_match["last_code"] != code:
        return False

    pending_expansion_run = pending_expansion_match
    pending_expansion_match = None

    logger.info(
        "queued expansion trigger=%r replacement=%r on keyup=%s",
        pending_expansion_run["trigger"],
        pending_expansion_run["replacement"],
        key_name(code),
    )
    return True


def run_queued_expansion_if_any() -> bool:
    global typed_buffer, pending_expansion_run

    if pending_expansion_run is None:
        return False

    trigger = pending_expansion_run["trigger"]
    replacement = pending_expansion_run["replacement"]

    logger.info("running queued expansion trigger=%r replacement=%r", trigger, replacement)

    release_forwarded_modifiers()
    send_backspaces(len(trigger), f"expansion_backspace({trigger})")
    time.sleep(EXPANSION_BACKSPACE_SETTLE_MS / 1000.0)
    type_text(replacement)

    typed_buffer = typed_buffer[:-len(trigger)] + replacement
    typed_buffer = typed_buffer[-EXPANSION_BUFFER_MAX_CHARS:]
    logger.debug("typed_buffer=%r", typed_buffer)

    pending_expansion_run = None
    return True


def should_run_queued_expansion_now() -> bool:
    return pending_expansion_run is not None and not all_pressed_physical_keys()


# -----------------------------------------------------------------------------
# Input event handling
# -----------------------------------------------------------------------------

def handle_key_event(
    event,
    device_id: str,
    device_name: str,
    substitutions: dict,
    expansions: dict,
) -> None:
    global pending_expansion_match

    code = event.code
    value = event.value
    state = device_states.setdefault(device_id, DeviceKeyState())
    record_bug_trace("in", code, value, device_name=device_name)

    logger.debug(
        "physical key %-14s %-6s dev=%s before_state=%s",
        key_name(code),
        value_name(value),
        device_name,
        state_snapshot(),
    )

    debug_beep_for_key_event(code, value)

    if value == 1:  # key down
        was_globally_pressed = is_physically_pressed(code)
        state.pressed.add(code)
        state.reported_orphan_repeats.discard(code)

        if xkb_decoder is not None and not was_globally_pressed:
            xkb_decoder.update_key(code, True)

        if code in ALL_MODIFIER_KEYS:
            forward_owned_key_event(event, device_id, device_name, "modifier_passthrough")
            state.forwarded_modifiers.add(code)
            return

        combo = (frozenset(physical_modifiers()), code)
        matched = substitutions.get(combo)
        if matched:
            state.triggered_combo_keys.add(code)
            logger.info(
                "matched combo=%s dev=%s output=%r",
                matched["combo_text"],
                device_name,
                matched["output"],
            )
            release_forwarded_modifiers()
            type_text(matched["output"])
            return

        forward_owned_key_event(event, device_id, device_name, "normal_key_passthrough")

        if not has_non_text_modifier():
            update_typed_buffer_from_keydown(code)
            maybe_mark_pending_expansion(expansions, code)
        else:
            logger.debug(
                "skip typed_buffer update because non-text modifier is active key=%s mods=%s state=%s",
                key_name(code),
                sorted(physical_modifiers()),
                state_snapshot(),
            )

        return

    if value == 0:  # key up
        state.pressed.discard(code)
        state.reported_orphan_repeats.discard(code)

        if xkb_decoder is not None and not is_physically_pressed(code):
            xkb_decoder.update_key(code, False)

        if code in state.suppressed_keyups:
            logger.debug("suppress physical keyup key=%s state=%s", key_name(code), state_snapshot())
            state.suppressed_keyups.discard(code)
            return

        if code in state.triggered_combo_keys:
            logger.debug("suppress triggered combo keyup key=%s state=%s", key_name(code), state_snapshot())
            state.triggered_combo_keys.discard(code)
            return

        if code in ALL_MODIFIER_KEYS:
            state.forwarded_modifiers.discard(code)

        forward_owned_key_event(event, device_id, device_name, "keyup_passthrough")
        queue_pending_expansion_if_matches(code)
        return

    if value == 2:  # repeat
        logger.debug(
            "repeat detected key=%s dev=%s state=%s",
            key_name(code),
            device_name,
            state_snapshot(),
        )

        if code in state.triggered_combo_keys:
            return

        if device_id not in virtual_key_owners.get(code, set()) or code not in virtual_pressed_keys:
            if code not in state.reported_orphan_repeats:
                state.reported_orphan_repeats.add(code)
                dump_bug_context("orphan_repeat_suppressed", device=device_name, key=key_name(code))
            return

        forward_event(event, "repeat_passthrough", device_name)
        return

    logger.warning(
        "unknown key value key=%s value=%s dev=%s state=%s",
        key_name(code),
        value,
        device_name,
        state_snapshot(),
    )


# -----------------------------------------------------------------------------
# Device setup and poll loop
# -----------------------------------------------------------------------------

def event_path_sort_key(path: str) -> tuple[int, str]:
    name = Path(path).name
    if name.startswith("event") and name[5:].isdigit():
        return int(name[5:]), path
    return sys.maxsize, path


def device_key_codes(dev: InputDevice) -> set[int]:
    try:
        key_codes = dev.capabilities(verbose=False).get(ecodes.EV_KEY, [])
    except OSError:
        return set()

    codes: set[int] = set()
    for item in key_codes:
        if isinstance(item, int):
            codes.add(item)
        elif isinstance(item, tuple) and item and isinstance(item[0], int):
            codes.add(item[0])
    return codes


def should_auto_include_device(dev: InputDevice) -> bool:
    if dev.name in PSEUDO_KEYBOARD_NAMES:
        return False

    key_codes = device_key_codes(dev)
    if not key_codes:
        return False

    # Real typing keyboards expose ordinary text keys. Pseudo keyboard-like
    # devices usually expose only power, brightness, rfkill, audio, or WMI keys.
    return bool(key_codes & REAL_KEYBOARD_PROBE_KEYS)


def discover_keyboard_device_paths() -> list[str]:
    discovered: list[str] = []

    for path in sorted(INPUT_DIR.glob("event*"), key=lambda item: event_path_sort_key(str(item))):
        path_text = str(path)
        dev: InputDevice | None = None
        try:
            dev = InputDevice(path_text)
            if should_auto_include_device(dev):
                discovered.append(path_text)
                logger.info("auto-discovered keyboard path=%s name=%s", path_text, dev.name)
            else:
                logger.debug("auto-skipped input path=%s name=%s", path_text, dev.name)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            logger.debug("auto-scan skipped path=%s: %s", path_text, exc)
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass

    return discovered

def resolve_device_paths(device_selection: DeviceSelection) -> list[str]:
    if device_selection == AUTO_DEVICES_MODE:
        return discover_keyboard_device_paths()
    return list(device_selection)


def open_configured_devices(device_paths: list[str]) -> tuple[list[InputDevice], list[tuple[str, str]]]:
    devices: list[InputDevice] = []
    failures: list[tuple[str, str]] = []

    for path in device_paths:
        try:
            dev = InputDevice(path)
            devices.append(dev)
            logger.info("opening device path=%s name=%s", dev.path, dev.name)
        except FileNotFoundError:
            failures.append((path, "not found"))
            logger.warning("configured device is missing: %s", path)
        except PermissionError:
            failures.append((path, "permission denied"))
            logger.warning("configured device is not accessible: %s", path)
        except OSError as exc:
            failures.append((path, str(exc)))
            logger.warning("failed to open configured device %s: %s", path, exc)

    return devices, failures


def grab_devices(devices: list[InputDevice]) -> list[InputDevice]:
    grabbed: list[InputDevice] = []

    for dev in devices:
        try:
            dev.grab()
            logger.info("grabbed device path=%s name=%s", dev.path, dev.name)
            grabbed.append(dev)
        except OSError as exc:
            logger.warning("failed to grab device path=%s name=%s: %s", dev.path, dev.name, exc)
            try:
                dev.close()
            except Exception:
                pass

    return grabbed


def build_poller(devices: list[InputDevice]) -> tuple[select.poll, dict[int, InputDevice]]:
    poller = select.poll()
    fd_to_device: dict[int, InputDevice] = {}

    for dev in devices:
        poller.register(dev.fd, select.POLLIN)
        fd_to_device[dev.fd] = dev

    return poller, fd_to_device




def setup_input_dir_inotify() -> int | None:
    """Watch /dev/input for event device changes without periodic scanning."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        inotify_init1 = libc.inotify_init1
        inotify_init1.argtypes = [ctypes.c_int]
        inotify_init1.restype = ctypes.c_int
        inotify_add_watch = libc.inotify_add_watch
        inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        inotify_add_watch.restype = ctypes.c_int

        fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            logger.warning("failed to initialize inotify: %s", os.strerror(err))
            return None

        watch = inotify_add_watch(fd, str(INPUT_DIR).encode("utf-8"), INOTIFY_EVENT_MASK)
        if watch < 0:
            err = ctypes.get_errno()
            os.close(fd)
            logger.warning("failed to watch %s with inotify: %s", INPUT_DIR, os.strerror(err))
            return None

        logger.info("watching %s for input device changes via inotify", INPUT_DIR)
        return fd
    except Exception as exc:
        logger.warning("inotify setup failed; falling back to slow periodic rescan: %s", exc)
        return None


def drain_inotify_events(fd: int) -> None:
    """Drain pending inotify records. Names are not needed; any event triggers a rescan."""
    while True:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            logger.warning("failed reading inotify fd=%s: %s", fd, exc)
            return
        if not data:
            return


def add_auto_discovered_devices(
    poller: select.poll,
    fd_to_device: dict[int, InputDevice],
) -> None:
    active_paths = {dev.path for dev in fd_to_device.values()}

    for path in discover_keyboard_device_paths():
        if path in active_paths:
            continue

        try:
            dev = InputDevice(path)
            dev.grab()
            poller.register(dev.fd, select.POLLIN)
            fd_to_device[dev.fd] = dev
            open_devices.append(dev)
            active_paths.add(path)
            logger.info("auto-added keyboard path=%s name=%s", dev.path, dev.name)
        except PermissionError:
            logger.warning("auto-discovered keyboard is not accessible: %s", path)
        except OSError as exc:
            logger.warning("failed to add auto-discovered keyboard %s: %s", path, exc)


def remove_polled_device(
    poller: select.poll,
    fd_to_device: dict[int, InputDevice],
    fd: int,
    reason: str,
) -> bool:
    """Remove a failed device and report whether auto-discovery should retry."""
    global open_devices

    dev = fd_to_device.pop(fd, None)

    try:
        poller.unregister(fd)
    except Exception:
        pass

    if dev is None:
        return False

    # Release only keys owned by this device. Other keyboards may legitimately
    # be holding the same key and must keep their virtual ownership.
    release_device_keys(
        getattr(dev, "path", f"fd:{fd}"),
        f"device_removed({getattr(dev, 'name', '?')})",
    )

    open_devices = [item for item in open_devices if item.fd != fd]

    logger.warning(
        "removing device fd=%s path=%s name=%s: %s",
        fd,
        getattr(dev, "path", "?"),
        getattr(dev, "name", "?"),
        reason,
    )

    try:
        dev.ungrab()
    except Exception:
        pass

    try:
        dev.close()
    except Exception:
        pass

    # An inotify notification and a device POLLHUP can arrive in the same poll
    # batch. If the notification is handled first, discovery still sees this
    # device in fd_to_device and skips it. Tell the main loop to rescan again
    # after the failed descriptor has been removed.
    return True


# -----------------------------------------------------------------------------
# Shutdown
# -----------------------------------------------------------------------------

def cleanup_and_exit(*_args) -> None:
    global open_devices, virtual_uinput, xkb_decoder

    if logger is not None:
        logger.info("stopping")

    for dev in open_devices:
        try:
            dev.ungrab()
            if logger is not None:
                logger.info("released device path=%s name=%s", dev.path, dev.name)
        except Exception:
            pass

    try:
        release_all_virtual_keys("shutdown")
    except Exception:
        pass

    try:
        if virtual_uinput is not None:
            virtual_uinput.close()
    except Exception:
        pass

    try:
        if xkb_decoder is not None:
            xkb_decoder.close()
    except Exception:
        pass

    sys.exit(0)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    global open_devices, virtual_uinput, logger, xkb_decoder, last_poll_wakeup_time

    args = parse_args(argv)
    config_path = resolve_config_path(args.config)
    log_level = "debug" if args.verbose else args.log_level

    try:
        if args.command == "list":
            list_config(config_path, args.category)
            return 0
        if args.command == "add":
            add_mapping(
                config_path,
                args.mapping_type,
                args.trigger,
                args.replacement,
                args.force,
            )
            return 0
        if args.command == "delete":
            delete_mapping(config_path, args.mapping_type, args.trigger)
            return 0
        if args.command == "test":
            load_config(config_path)
            print(f"config is valid: {config_path}")
            return 0
        if args.command in ("start", "stop", "restart", "status"):
            return run_service_command(args.command)
        if args.command == "history":
            return show_history(args.lines, args.since, args.bugs)
    except (OSError, ValueError) as exc:
        print(f"keyswap: error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(log_level)
    logger.info("using config: %s", config_path)

    device_selection, substitutions, expansions, xkb_config = load_config(config_path)

    if args.dump_config:
        dump_loaded_config(config_path, device_selection, substitutions, expansions, xkb_config)
        return 0

    xkb_decoder = XKBDecoder(xkb_config)
    logger.info("xkb layout info: %s", xkb_decoder.layout_info)
    logger.info("watching devices: %s", device_selection)
    logger.info("watching substitutions: %s", [item["combo_text"] for item in substitutions.values()])
    logger.info("watching expansions: %s", list(expansions.keys()))

    auto_discovery_enabled = device_selection == AUTO_DEVICES_MODE
    device_paths = resolve_device_paths(device_selection)
    devices, failures = open_configured_devices(device_paths)
    if not devices:
        raise RuntimeError(
            "no input devices could be opened: "
            + ", ".join(f"{path} ({reason})" for path, reason in failures)
        )

    if failures:
        logger.warning(
            "some configured devices are unavailable at startup: %s",
            ", ".join(f"{path} ({reason})" for path, reason in failures),
        )

    virtual_uinput = create_virtual_uinput(devices)
    release_all_virtual_keys("startup")

    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)

    open_devices = grab_devices(devices)
    if not open_devices:
        raise RuntimeError("configured input devices were opened, but none could be grabbed")

    poller, fd_to_device = build_poller(open_devices)
    last_auto_rescan = time.monotonic()
    input_dir_inotify_fd = setup_input_dir_inotify() if auto_discovery_enabled else None
    pending_auto_rescan = False
    if input_dir_inotify_fd is not None:
        poller.register(input_dir_inotify_fd, select.POLLIN)

    while True:
        poll_timeout = 100
        ready = poller.poll(poll_timeout)

        now = time.monotonic()
        if now - last_poll_wakeup_time > 5.0:
            logger.warning(
                "long poll gap detected gap=%.3fs; possible suspend/resume; state=%s",
                now - last_poll_wakeup_time,
                state_snapshot(),
            )
            release_all_virtual_keys("long_poll_gap_possible_resume")
        last_poll_wakeup_time = now

        for fd, mask in ready:
            if input_dir_inotify_fd is not None and fd == input_dir_inotify_fd:
                if mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                    logger.warning("input directory inotify watch failed; falling back to slow periodic rescan")
                    try:
                        poller.unregister(input_dir_inotify_fd)
                    except Exception:
                        pass
                    try:
                        os.close(input_dir_inotify_fd)
                    except Exception:
                        pass
                    input_dir_inotify_fd = None
                    last_auto_rescan = time.monotonic()
                    continue

                if mask & select.POLLIN:
                    drain_inotify_events(fd)
                    pending_auto_rescan = True
                continue

            if mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                if remove_polled_device(
                    poller,
                    fd_to_device,
                    fd,
                    f"invalid poll mask={mask}",
                ):
                    pending_auto_rescan = auto_discovery_enabled
                continue

            if not (mask & select.POLLIN):
                continue

            dev = fd_to_device.get(fd)
            if dev is None:
                continue

            try:
                for event in dev.read():
                    if event.type != ecodes.EV_KEY:
                        # Keyswap only remaps keyboard keys. Forwarding EV_SYN/MSC
                        # from grabbed physical devices adds needless work and can
                        # make typing feel delayed on busy devices.
                        continue

                    handle_key_event(event, dev.path, dev.name, substitutions, expansions)

            except BlockingIOError:
                continue
            except OSError as exc:
                if remove_polled_device(
                    poller,
                    fd_to_device,
                    fd,
                    f"device read failed: {exc}",
                ):
                    pending_auto_rescan = auto_discovery_enabled

        if auto_discovery_enabled:
            if pending_auto_rescan:
                add_auto_discovered_devices(poller, fd_to_device)
                pending_auto_rescan = False
                last_auto_rescan = time.monotonic()
            elif time.monotonic() - last_auto_rescan >= AUTO_RESCAN_INTERVAL_SEC:
                add_auto_discovered_devices(poller, fd_to_device)
                last_auto_rescan = time.monotonic()

        if not fd_to_device:
            if auto_discovery_enabled:
                logger.warning("no input devices remain in poll set; waiting for auto-discovery")
                if input_dir_inotify_fd is None:
                    time.sleep(AUTO_RESCAN_INTERVAL_SEC)
                else:
                    pending_auto_rescan = True
                continue

            logger.error("no input devices remain in poll set; exiting")
            break

        if should_run_queued_expansion_now():
            run_queued_expansion_if_any()

        release_stale_virtual_keys_if_idle()

    return 0


if __name__ == "__main__":
    sys.exit(main())
