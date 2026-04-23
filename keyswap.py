#!/usr/bin/env python3

import argparse
import ctypes
import json
import logging
import os
import select
import signal
import sys
import time
from pathlib import Path

from evdev import InputDevice, UInput, ecodes

SYSTEM_CONFIG_PATH = Path("/etc/keyswap/config.json")

pending_sequence_run = None

SEQUENCE_BACKSPACE_SETTLE_MS = 20
TYPE_CHAR_DELAY_MS = 20

MODIFIERS = {
    "C": {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL},
    "A": {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT},
    "S": {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT},
    "M": {ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA},
}

ALL_MODIFIER_KEYS = set().union(*MODIFIERS.values())

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

CHARMAP = {
    "a": (ecodes.KEY_A, False), "b": (ecodes.KEY_B, False), "c": (ecodes.KEY_C, False),
    "d": (ecodes.KEY_D, False), "e": (ecodes.KEY_E, False), "f": (ecodes.KEY_F, False),
    "g": (ecodes.KEY_G, False), "h": (ecodes.KEY_H, False), "i": (ecodes.KEY_I, False),
    "j": (ecodes.KEY_J, False), "k": (ecodes.KEY_K, False), "l": (ecodes.KEY_L, False),
    "m": (ecodes.KEY_M, False), "n": (ecodes.KEY_N, False), "o": (ecodes.KEY_O, False),
    "p": (ecodes.KEY_P, False), "q": (ecodes.KEY_Q, False), "r": (ecodes.KEY_R, False),
    "s": (ecodes.KEY_S, False), "t": (ecodes.KEY_T, False), "u": (ecodes.KEY_U, False),
    "v": (ecodes.KEY_V, False), "w": (ecodes.KEY_W, False), "x": (ecodes.KEY_X, False),
    "y": (ecodes.KEY_Y, False), "z": (ecodes.KEY_Z, False),

    "A": (ecodes.KEY_A, True), "B": (ecodes.KEY_B, True), "C": (ecodes.KEY_C, True),
    "D": (ecodes.KEY_D, True), "E": (ecodes.KEY_E, True), "F": (ecodes.KEY_F, True),
    "G": (ecodes.KEY_G, True), "H": (ecodes.KEY_H, True), "I": (ecodes.KEY_I, True),
    "J": (ecodes.KEY_J, True), "K": (ecodes.KEY_K, True), "L": (ecodes.KEY_L, True),
    "M": (ecodes.KEY_M, True), "N": (ecodes.KEY_N, True), "O": (ecodes.KEY_O, True),
    "P": (ecodes.KEY_P, True), "Q": (ecodes.KEY_Q, True), "R": (ecodes.KEY_R, True),
    "S": (ecodes.KEY_S, True), "T": (ecodes.KEY_T, True), "U": (ecodes.KEY_U, True),
    "V": (ecodes.KEY_V, True), "W": (ecodes.KEY_W, True), "X": (ecodes.KEY_X, True),
    "Y": (ecodes.KEY_Y, True), "Z": (ecodes.KEY_Z, True),

    "0": (ecodes.KEY_0, False), "1": (ecodes.KEY_1, False), "2": (ecodes.KEY_2, False),
    "3": (ecodes.KEY_3, False), "4": (ecodes.KEY_4, False), "5": (ecodes.KEY_5, False),
    "6": (ecodes.KEY_6, False), "7": (ecodes.KEY_7, False), "8": (ecodes.KEY_8, False),
    "9": (ecodes.KEY_9, False),

    "=": (ecodes.KEY_EQUAL, False),
    "+": (ecodes.KEY_EQUAL, True),
    "-": (ecodes.KEY_MINUS, False),
    "_": (ecodes.KEY_MINUS, True),
    ".": (ecodes.KEY_DOT, False),
    ">": (ecodes.KEY_DOT, True),
    ",": (ecodes.KEY_COMMA, False),
    "<": (ecodes.KEY_COMMA, True),
    "/": (ecodes.KEY_SLASH, False),
    "?": (ecodes.KEY_SLASH, True),
    ";": (ecodes.KEY_SEMICOLON, False),
    ":": (ecodes.KEY_SEMICOLON, True),
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

pressed_physical = set()
forwarded_modifiers = set()
suppressed_keyups = set()
triggered_keys = set()

typed_buffer = ""
max_sequence_len = 0
pending_sequence_match = None

devices = []
ui = None
log = None
xkb_decoder = None


class XKBRuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


class XKBDecoder:
    XKB_CONTEXT_NO_FLAGS = 0
    XKB_KEYMAP_COMPILE_NO_FLAGS = 0
    XKB_KEY_DOWN = 1
    XKB_KEY_UP = 0

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        try:
            self.lib = ctypes.cdll.LoadLibrary("libxkbcommon.so.0")
        except OSError as exc:
            raise RuntimeError(
                "libxkbcommon.so.0 not found. Install libxkbcommon0."
            ) from exc

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

        self.context = self.lib.xkb_context_new(self.XKB_CONTEXT_NO_FLAGS)
        if not self.context:
            raise RuntimeError("failed to create xkb context")

        names = XKBRuleNames(
            self._enc(cfg.get("rules") or os.environ.get("XKB_DEFAULT_RULES")),
            self._enc(cfg.get("model") or os.environ.get("XKB_DEFAULT_MODEL")),
            self._enc(cfg.get("layout") or os.environ.get("XKB_DEFAULT_LAYOUT") or "us"),
            self._enc(cfg.get("variant") or os.environ.get("XKB_DEFAULT_VARIANT")),
            self._enc(cfg.get("options") or os.environ.get("XKB_DEFAULT_OPTIONS")),
        )

        self.keymap = self.lib.xkb_keymap_new_from_names(
            self.context,
            ctypes.byref(names),
            self.XKB_KEYMAP_COMPILE_NO_FLAGS,
        )
        if not self.keymap:
            self.lib.xkb_context_unref(self.context)
            raise RuntimeError(
                f"failed to create xkb keymap for layout={cfg.get('layout') or os.environ.get('XKB_DEFAULT_LAYOUT') or 'us'}"
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

    @staticmethod
    def _enc(value):
        if value is None:
            return None
        return value.encode("utf-8")

    @staticmethod
    def _to_xkb_keycode(evdev_keycode: int) -> int:
        return evdev_keycode + 8

    def update_key(self, evdev_keycode: int, is_down: bool):
        self.lib.xkb_state_update_key(
            self.state,
            self._to_xkb_keycode(evdev_keycode),
            self.XKB_KEY_DOWN if is_down else self.XKB_KEY_UP,
        )

    def char_for_keydown(self, evdev_keycode: int) -> str | None:
        xkb_keycode = self._to_xkb_keycode(evdev_keycode)
        buf = ctypes.create_string_buffer(64)
        n = self.lib.xkb_state_key_get_utf8(self.state, xkb_keycode, buf, len(buf))
        if n <= 0:
            return None
        text = buf.value.decode("utf-8", errors="ignore")
        if not text:
            return None
        return text

    def close(self):
        if getattr(self, "state", None):
            self.lib.xkb_state_unref(self.state)
            self.state = None
        if getattr(self, "keymap", None):
            self.lib.xkb_keymap_unref(self.keymap)
            self.keymap = None
        if getattr(self, "context", None):
            self.lib.xkb_context_unref(self.context)
            self.context = None


def resolve_user_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "keyswap" / "config.json"
    return Path.home() / ".config" / "keyswap" / "config.json"


def setup_logging(verbose_stdout: bool):
    handlers = [logging.StreamHandler(sys.stdout)]
    level = logging.DEBUG if verbose_stdout else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("keyswap")


def key_name(code: int) -> str:
    return ecodes.KEY.get(code, f"UNKNOWN_{code}")


def value_name(value: int) -> str:
    return {0: "up", 1: "down", 2: "repeat"}.get(value, str(value))


def get_paths():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", help="Explicit config path")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dump-config", action="store_true")
    args = parser.parse_args()

    if args.config:
        return Path(args.config).expanduser().resolve(), args.verbose, args.dump_config

    user_cfg = resolve_user_config_path()
    if user_cfg.is_file():
        return user_cfg.resolve(), args.verbose, args.dump_config

    return SYSTEM_CONFIG_PATH, args.verbose, args.dump_config


def parse_combo(combo: str):
    parts = combo.split("-")
    required_mods = set()

    for p in parts[:-1]:
        if p not in MODIFIERS:
            raise ValueError(f"unknown modifier '{p}' in combo '{combo}'")
        required_mods.add(p)

    main_key = parse_key(parts[-1])
    return required_mods, main_key


def parse_key(name: str):
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


def load_config(config_path: Path):
    global max_sequence_len

    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if "substitutions" not in raw:
        raise ValueError("config must contain 'substitutions'")

    if "devices" in raw:
        dev_paths = raw["devices"]
        if not isinstance(dev_paths, list) or not dev_paths:
            raise ValueError("'devices' must be a non-empty list")
    elif "device" in raw:
        dev_paths = [raw["device"]]
    else:
        raise ValueError("config must contain 'device' or 'devices'")

    compiled = {}
    for combo, output in raw["substitutions"].items():
        required_mods, main_key = parse_combo(combo)
        compiled[(frozenset(required_mods), main_key)] = {
            "combo_text": combo,
            "output": output,
        }

    sequences = raw.get("sequences", {})
    if not isinstance(sequences, dict):
        raise ValueError("'sequences' must be an object/map")

    xkb_cfg = raw.get("xkb", {})
    if not isinstance(xkb_cfg, dict):
        raise ValueError("'xkb' must be an object/map when present")

    max_sequence_len = max((len(k) for k in sequences), default=0)

    return dev_paths, compiled, sequences, xkb_cfg


def physical_modifiers():
    active = set()
    for mod_name, keys in MODIFIERS.items():
        if pressed_physical & keys:
            active.add(mod_name)
    return active


def state_snapshot():
    return {
        "pressed": [key_name(k) for k in sorted(pressed_physical)],
        "forwarded_modifiers": [key_name(k) for k in sorted(forwarded_modifiers)],
        "suppressed_keyups": [key_name(k) for k in sorted(suppressed_keyups)],
        "triggered_keys": [key_name(k) for k in sorted(triggered_keys)],
        "mods": sorted(physical_modifiers()),
        "typed_buffer": typed_buffer[-40:],
        "pending_sequence_match": pending_sequence_match,
    }


def write_key(code: int, value: int, reason: str):
    log.debug("emit key %-14s %-6s reason=%s", key_name(code), value_name(value), reason)
    ui.write(ecodes.EV_KEY, code, value)


def sync(reason: str):
    log.debug("emit syn reason=%s", reason)
    ui.syn()


def tap(code: int, reason: str):
    write_key(code, 1, reason)
    write_key(code, 0, reason)
    sync(reason)


def send_backspaces(count: int, reason: str):
    for _ in range(count):
        tap(ecodes.KEY_BACKSPACE, reason)
        time.sleep(0.002)


def release_forwarded_modifiers():
    to_release = list(forwarded_modifiers)
    if not to_release:
        return

    log.debug(
        "release_forwarded_modifiers begin state=%s releasing=%s",
        state_snapshot(),
        [key_name(k) for k in to_release],
    )

    for code in to_release:
        write_key(code, 0, "release_forwarded_modifiers")
        suppressed_keyups.add(code)

    sync("release_forwarded_modifiers")
    forwarded_modifiers.clear()

    log.debug("release_forwarded_modifiers end state=%s", state_snapshot())


def type_char(ch: str):
    if ch not in CHARMAP:
        raise ValueError(f"unsupported output character: {ch!r}")

    keycode, needs_shift = CHARMAP[ch]
    log.debug("type_char ch=%r key=%s needs_shift=%s", ch, key_name(keycode), needs_shift)

    if needs_shift:
        write_key(ecodes.KEY_LEFTSHIFT, 1, f"type_char({ch}) shift_down")
        sync(f"type_char({ch}) shift_down")

    tap(keycode, f"type_char({ch})")

    if needs_shift:
        write_key(ecodes.KEY_LEFTSHIFT, 0, f"type_char({ch}) shift_up")
        sync(f"type_char({ch}) shift_up")


def type_text(text: str):
    log.info("typing substitution text=%r", text)
    for ch in text:
        type_char(ch)
        time.sleep(TYPE_CHAR_DELAY_MS / 1000.0)

def forward_event(event, reason: str, dev_name: str):
    if event.type == ecodes.EV_KEY:
        log.debug(
            "forward key %-14s %-6s dev=%s reason=%s state=%s",
            key_name(event.code),
            value_name(event.value),
            dev_name,
            reason,
            state_snapshot(),
        )
    ui.write_event(event)
    ui.syn()


def cleanup_and_exit(*_args):
    global devices, ui, xkb_decoder
    if log is not None:
        log.info("stopping")

    for dev in devices:
        try:
            dev.ungrab()
            if log is not None:
                log.info("released device path=%s name=%s", dev.path, dev.name)
        except Exception:
            pass

    try:
        if ui is not None:
            ui.close()
    except Exception:
        pass

    try:
        if xkb_decoder is not None:
            xkb_decoder.close()
    except Exception:
        pass

    sys.exit(0)


def update_typed_buffer_from_keydown(code: int):
    global typed_buffer

    if code == ecodes.KEY_BACKSPACE:
        typed_buffer = typed_buffer[:-1]
        log.info("typed_buffer=%r", typed_buffer)
        return

    if code == ecodes.KEY_TAB:
        typed_buffer += "\t"
        typed_buffer = typed_buffer[-max(max_sequence_len, 200):]
        log.info("typed_buffer=%r", typed_buffer)
        return

    if code == ecodes.KEY_ENTER:
        typed_buffer += "\n"
        typed_buffer = typed_buffer[-max(max_sequence_len, 200):]
        log.info("typed_buffer=%r", typed_buffer)
        return

    if code == ecodes.KEY_ESC:
        typed_buffer = ""
        log.info("typed_buffer=%r", typed_buffer)
        return

    if xkb_decoder is None:
        return

    ch = xkb_decoder.char_for_keydown(code)
    if ch:
        typed_buffer += ch
        typed_buffer = typed_buffer[-max(max_sequence_len, 200):]
        log.info("typed_buffer=%r", typed_buffer)


def maybe_mark_pending_sequence(sequences, last_code: int):
    global pending_sequence_match

    if pending_sequence_match is not None:
        return False

    if not sequences or not typed_buffer:
        return False

    for trigger, replacement in sequences.items():
        if typed_buffer.endswith(trigger):
            pending_sequence_match = {
                "trigger": trigger,
                "replacement": replacement,
                "last_code": last_code,
            }
            log.info(
                "pending sequence trigger=%r replacement=%r last_key=%s",
                trigger,
                replacement,
                key_name(last_code),
            )
            return True

    return False


def queue_pending_sequence_if_matches(code: int):
    global pending_sequence_match, pending_sequence_run

    if pending_sequence_match is None:
        return False

    if pending_sequence_match["last_code"] != code:
        return False

    pending_sequence_run = pending_sequence_match
    pending_sequence_match = None

    log.info(
        "queued sequence trigger=%r replacement=%r on keyup=%s",
        pending_sequence_run["trigger"],
        pending_sequence_run["replacement"],
        key_name(code),
    )
    return True

def run_queued_sequence_if_any():
    global typed_buffer, pending_sequence_run

    if pending_sequence_run is None:
        return False

    trigger = pending_sequence_run["trigger"]
    replacement = pending_sequence_run["replacement"]

    log.info("running queued sequence trigger=%r replacement=%r", trigger, replacement)

    release_forwarded_modifiers()
    send_backspaces(len(trigger), f"sequence_backspace({trigger})")
    time.sleep(SEQUENCE_BACKSPACE_SETTLE_MS / 1000.0)
    type_text(replacement)

    typed_buffer = typed_buffer[:-len(trigger)] + replacement
    typed_buffer = typed_buffer[-max(max_sequence_len, 200):]
    log.info("typed_buffer=%r", typed_buffer)

    pending_sequence_run = None
    return True


def should_run_queued_sequence_now():
    # Wait until the user has fully released all physical keys.
    return pending_sequence_run is not None and len(pressed_physical) == 0


def handle_key_event(event, dev_name: str, substitutions, sequences):
    global pending_sequence_match

    code = event.code
    value = event.value

    log.debug(
        "physical key %-14s %-6s dev=%s before_state=%s",
        key_name(code),
        value_name(value),
        dev_name,
        state_snapshot(),
    )

    if value == 1:
        pressed_physical.add(code)

        if xkb_decoder is not None:
            xkb_decoder.update_key(code, True)

        if code in ALL_MODIFIER_KEYS:
            forward_event(event, "modifier_passthrough", dev_name)
            forwarded_modifiers.add(code)
            return

        combo = (frozenset(physical_modifiers()), code)
        matched = substitutions.get(combo)

        if matched:
            triggered_keys.add(code)
            log.info("matched combo=%s dev=%s output=%r", matched["combo_text"], dev_name, matched["output"])
            release_forwarded_modifiers()
            type_text(matched["output"])
            return

        forward_event(event, "normal_key_passthrough", dev_name)
        update_typed_buffer_from_keydown(code)
        maybe_mark_pending_sequence(sequences, code)
        return

    if value == 0:
        pressed_physical.discard(code)

        if xkb_decoder is not None:
            xkb_decoder.update_key(code, False)

        if code in suppressed_keyups:
            suppressed_keyups.discard(code)
            return

        if code in triggered_keys:
            triggered_keys.discard(code)
            return

        if code in ALL_MODIFIER_KEYS:
            forwarded_modifiers.discard(code)

        forward_event(event, "keyup_passthrough", dev_name)

        queue_pending_sequence_if_matches(code)
        return

    if value == 2:
        if code in triggered_keys:
            return

        forward_event(event, "repeat_passthrough", dev_name)
        return


def main():
    global devices, ui, log, xkb_decoder

    config_path, verbose, dump_config = get_paths()
    log = setup_logging(verbose)
    log.info("using config: %s", config_path)

    dev_paths, substitutions, sequences, xkb_cfg = load_config(config_path)

    if dump_config:
        print(f"config: {config_path}")
        print("devices:")
        for d in dev_paths:
            print(f"  - {d}")
        print("substitutions:")
        for v in substitutions.values():
            print(f"  - {v['combo_text']} -> {v['output']}")
        print("sequences:")
        for trigger, replacement in sequences.items():
            print(f"  - {trigger!r} -> {replacement!r}")
        print("xkb:")
        for k in ("rules", "model", "layout", "variant", "options"):
            if xkb_cfg.get(k):
                print(f"  - {k}: {xkb_cfg[k]}")
        return

    xkb_decoder = XKBDecoder(xkb_cfg)
    log.info("xkb layout info: %s", xkb_decoder.layout_info)
    log.info("watching devices: %s", dev_paths)
    log.info("watching substitutions: %s", [v["combo_text"] for v in substitutions.values()])
    log.info("watching sequences: %s", list(sequences.keys()))

    devices = [InputDevice(path) for path in dev_paths]
    for dev in devices:
        log.info("opening device path=%s name=%s", dev.path, dev.name)

    ui = UInput.from_device(*devices, name="keyswap-uinput")

    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)

    for dev in devices:
        dev.grab()
        log.info("grabbed device path=%s name=%s", dev.path, dev.name)

    poller = select.poll()
    fd_to_dev = {}

    for dev in devices:
        poller.register(dev.fd, select.POLLIN)
        fd_to_dev[dev.fd] = dev

    while True:
        ready = poller.poll()

        for fd, mask in ready:
            if not (mask & select.POLLIN):
                continue

            dev = fd_to_dev[fd]

            try:
                for event in dev.read():
                    if event.type != ecodes.EV_KEY:
                        forward_event(event, "non_key_passthrough", dev.name)
                        continue

                    handle_key_event(event, dev.name, substitutions, sequences)

            except BlockingIOError:
                continue

        if should_run_queued_sequence_now():
            more_ready = poller.poll(0)
            if not more_ready:
                run_queued_sequence_if_any()


if __name__ == "__main__":
    main()
