import importlib.util
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evdev import ecodes


MODULE_PATH = Path(__file__).resolve().parents[1] / "keyswap.py"
SPEC = importlib.util.spec_from_file_location("keyswap_under_test", MODULE_PATH)
keyswap = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(keyswap)


class FakeUInput:
    def __init__(self):
        self.events = []
        self.syncs = 0

    def write_event(self, event):
        self.events.append((event.type, event.code, event.value))

    def syn(self):
        self.syncs += 1


class KeyRepeatTests(unittest.TestCase):
    def setUp(self):
        keyswap.logger = logging.getLogger("keyswap-test")
        keyswap.xkb_decoder = None
        keyswap.virtual_uinput = FakeUInput()
        keyswap.pressed_physical_keys.clear()
        keyswap.forwarded_modifier_keys.clear()
        keyswap.suppressed_keyup_codes.clear()
        keyswap.triggered_combo_keys.clear()
        keyswap.virtual_pressed_keys.clear()
        keyswap.reported_orphan_repeat_codes.clear()
        keyswap.bug_trace.clear()

    @staticmethod
    def event(value):
        return SimpleNamespace(type=ecodes.EV_KEY, code=ecodes.KEY_F, value=value)

    def handle(self, value):
        keyswap.handle_key_event(self.event(value), "test keyboard", {}, {})

    def test_valid_physical_repeat_is_forwarded_after_matching_keydown(self):
        self.handle(1)
        self.handle(2)
        self.handle(0)

        self.assertEqual(
            [value for _type, _code, value in keyswap.virtual_uinput.events],
            [1, 2, 0],
        )

    def test_orphan_repeat_is_suppressed(self):
        self.handle(2)

        self.assertEqual(keyswap.virtual_uinput.events, [])
        self.assertEqual(keyswap.virtual_uinput.syncs, 0)

    def test_repeat_after_forced_state_clear_is_suppressed(self):
        self.handle(1)
        keyswap.pressed_physical_keys.clear()
        keyswap.virtual_pressed_keys.clear()

        self.handle(2)

        self.assertEqual(
            [value for _type, _code, value in keyswap.virtual_uinput.events],
            [1],
        )

    def test_orphan_repeat_dumps_recent_key_flight_recorder(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        keyswap.logger.addHandler(handler)
        keyswap.logger.setLevel(logging.WARNING)
        try:
            self.handle(1)
            keyswap.pressed_physical_keys.clear()
            keyswap.virtual_pressed_keys.clear()
            self.handle(2)
        finally:
            keyswap.logger.removeHandler(handler)

        output = stream.getvalue()
        self.assertIn("BUG_CONTEXT reason=orphan_repeat", output)
        self.assertIn("<text-key>", output)
        self.assertIn("recent_events=", output)
        self.assertEqual(list(keyswap.bug_trace), [])


class VirtualDeviceCapabilityTests(unittest.TestCase):
    def test_virtual_device_does_not_enable_a_second_repeat_source(self):
        physical_device = SimpleNamespace(
            capabilities=lambda: {
                ecodes.EV_SYN: [ecodes.SYN_REPORT],
                ecodes.EV_KEY: [ecodes.KEY_A],
                ecodes.EV_REP: [ecodes.REP_DELAY, ecodes.REP_PERIOD],
            }
        )

        with patch.object(keyswap, "UInput") as uinput:
            keyswap.create_virtual_uinput([physical_device])

        capabilities = uinput.call_args.kwargs["events"]
        self.assertNotIn(ecodes.EV_SYN, capabilities)
        self.assertNotIn(ecodes.EV_REP, capabilities)
        self.assertIn(ecodes.KEY_A, capabilities[ecodes.EV_KEY])


class BugTracePrivacyTests(unittest.TestCase):
    def test_text_keys_are_redacted_but_navigation_keys_are_named(self):
        self.assertEqual(keyswap.diagnostic_key_name(ecodes.KEY_A), "<text-key>")
        self.assertEqual(keyswap.diagnostic_key_name(ecodes.KEY_LEFT), "KEY_LEFT")

    def test_persistent_state_snapshot_redacts_typed_text(self):
        original_buffer = keyswap.typed_buffer
        original_pending = keyswap.pending_sequence_match
        try:
            keyswap.typed_buffer = "private text"
            keyswap.pending_sequence_match = {
                "trigger": "secret",
                "replacement": "also secret",
                "last_code": ecodes.KEY_T,
            }
            snapshot = keyswap.diagnostic_state_snapshot()
        finally:
            keyswap.typed_buffer = original_buffer
            keyswap.pending_sequence_match = original_pending

        self.assertEqual(snapshot["typed_buffer"], "<redacted length=12>")
        self.assertIs(snapshot["pending_sequence_match"], True)
        self.assertNotIn("private", repr(snapshot))
        self.assertNotIn("secret", repr(snapshot))


class SequenceBufferTests(unittest.TestCase):
    def test_sequence_buffer_limit_is_twenty_characters(self):
        self.assertEqual(keyswap.SEQUENCE_BUFFER_MAX_CHARS, 20)

    def test_config_rejects_trigger_longer_than_sequence_buffer(self):
        config = {
            "devices": "auto",
            "substitutions": {},
            "sequences": {"x" * 21: "replacement"},
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
            json.dump(config, handle)
            handle.flush()
            with self.assertRaisesRegex(ValueError, "20-character limit"):
                keyswap.load_config(Path(handle.name))

    def test_typed_buffer_is_trimmed_to_twenty_characters(self):
        original_buffer = keyswap.typed_buffer
        original_decoder = keyswap.xkb_decoder
        try:
            keyswap.typed_buffer = ""
            keyswap.xkb_decoder = SimpleNamespace(char_for_keydown=lambda _code: "x")
            for _ in range(25):
                keyswap.update_typed_buffer_from_keydown(ecodes.KEY_X)
            result = keyswap.typed_buffer
        finally:
            keyswap.typed_buffer = original_buffer
            keyswap.xkb_decoder = original_decoder

        self.assertEqual(result, "x" * 20)


if __name__ == "__main__":
    unittest.main()
