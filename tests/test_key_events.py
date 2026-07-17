import importlib.util
import io
import json
import logging
import sys
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
sys.modules[SPEC.name] = keyswap
SPEC.loader.exec_module(keyswap)


class FakeUInput:
    def __init__(self):
        self.events = []
        self.syncs = 0

    def write_event(self, event):
        self.events.append((event.type, event.code, event.value))

    def write(self, event_type, code, value):
        self.events.append((event_type, code, value))

    def syn(self):
        self.syncs += 1


class KeyRepeatTests(unittest.TestCase):
    def setUp(self):
        keyswap.logger = logging.getLogger("keyswap-test")
        keyswap.xkb_decoder = None
        keyswap.virtual_uinput = FakeUInput()
        keyswap.device_states.clear()
        keyswap.virtual_key_owners.clear()
        keyswap.virtual_pressed_keys.clear()
        keyswap.bug_trace.clear()
        keyswap.typed_buffer = ""
        keyswap.pending_sequence_match = None
        keyswap.pending_sequence_run = None

    @staticmethod
    def event(value):
        return SimpleNamespace(type=ecodes.EV_KEY, code=ecodes.KEY_F, value=value)

    def handle(self, value, device_id="/dev/input/test", device_name="test keyboard"):
        keyswap.handle_key_event(self.event(value), device_id, device_name, {}, {})

    def test_valid_physical_repeat_is_forwarded_after_matching_keydown(self):
        self.handle(1)
        self.handle(2)
        self.handle(0)

        self.assertEqual(
            [value for _type, _code, value in keyswap.virtual_uinput.events],
            [1, 2, 0],
        )

    def test_repeat_without_tracked_keydown_is_suppressed(self):
        self.handle(2)

        self.assertEqual(keyswap.virtual_uinput.events, [])

    def test_repeat_after_state_clear_is_suppressed(self):
        self.handle(1)
        keyswap.device_states.clear()
        keyswap.virtual_key_owners.clear()
        keyswap.virtual_pressed_keys.clear()

        self.handle(2)

        self.assertEqual(
            [value for _type, _code, value in keyswap.virtual_uinput.events],
            [1],
        )

    def test_repeat_from_second_device_without_ownership_is_suppressed(self):
        self.handle(1, "/dev/input/keyboard-a", "keyboard A")
        self.handle(2, "/dev/input/keyboard-b", "keyboard B")

        self.assertEqual(
            [value for _type, _code, value in keyswap.virtual_uinput.events],
            [1],
        )

    def test_incident_dump_includes_recent_key_flight_recorder(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        keyswap.logger.addHandler(handler)
        keyswap.logger.setLevel(logging.WARNING)
        try:
            self.handle(1)
            keyswap.dump_bug_context("test_incident", device="test keyboard")
        finally:
            keyswap.logger.removeHandler(handler)

        output = stream.getvalue()
        self.assertIn("BUG_CONTEXT reason=test_incident", output)
        self.assertIn("<text-key>", output)
        self.assertIn("recent_events=", output)
        self.assertEqual(list(keyswap.bug_trace), [])


class MultiDeviceOwnershipTests(unittest.TestCase):
    def setUp(self):
        keyswap.logger = logging.getLogger("keyswap-multi-device-test")
        keyswap.xkb_decoder = None
        keyswap.virtual_uinput = FakeUInput()
        keyswap.device_states.clear()
        keyswap.virtual_key_owners.clear()
        keyswap.virtual_pressed_keys.clear()
        keyswap.bug_trace.clear()
        keyswap.typed_buffer = ""
        keyswap.pending_sequence_match = None
        keyswap.pending_sequence_run = None

    @staticmethod
    def event(code, value):
        return SimpleNamespace(type=ecodes.EV_KEY, code=code, value=value)

    def handle(self, device_id, name, code, value):
        keyswap.handle_key_event(self.event(code, value), device_id, name, {}, {})

    def values(self, code):
        return [value for _type, event_code, value in keyswap.virtual_uinput.events if event_code == code]

    def test_shared_key_stays_down_until_last_keyboard_releases_it(self):
        code = ecodes.KEY_RIGHT
        self.handle("kbd-a", "keyboard A", code, 1)
        self.handle("kbd-b", "keyboard B", code, 1)
        self.handle("kbd-a", "keyboard A", code, 0)
        self.handle("kbd-b", "keyboard B", code, 0)

        self.assertEqual(self.values(code), [1, 0])

    def test_disconnect_releases_only_removed_devices_exclusive_keys(self):
        self.handle("kbd-a", "keyboard A", ecodes.KEY_LEFT, 1)
        self.handle("kbd-b", "keyboard B", ecodes.KEY_RIGHT, 1)

        keyswap.release_device_keys("kbd-b", "test_disconnect")

        self.assertEqual(self.values(ecodes.KEY_RIGHT), [1, 0])
        self.assertEqual(self.values(ecodes.KEY_LEFT), [1])
        self.assertEqual(keyswap.virtual_key_owners[ecodes.KEY_LEFT], {"kbd-a"})
        self.assertIn(ecodes.KEY_LEFT, keyswap.virtual_pressed_keys)

    def test_disconnect_does_not_release_a_shared_key_owned_by_other_keyboard(self):
        code = ecodes.KEY_BACKSPACE
        self.handle("kbd-a", "keyboard A", code, 1)
        self.handle("kbd-b", "keyboard B", code, 1)

        keyswap.release_device_keys("kbd-b", "test_disconnect")

        self.assertEqual(self.values(code), [1])
        self.assertEqual(keyswap.virtual_key_owners[code], {"kbd-a"})
        self.handle("kbd-a", "keyboard A", code, 0)
        self.assertEqual(self.values(code), [1, 0])

    def test_reconnected_device_can_press_and_release_navigation_key(self):
        code = ecodes.KEY_RIGHT
        self.handle("old-path", "K250", code, 1)
        keyswap.release_device_keys("old-path", "test_disconnect")
        self.handle("new-path", "K250", code, 1)
        self.handle("new-path", "K250", code, 0)

        self.assertEqual(self.values(code), [1, 0, 1, 0])

    def test_shared_modifier_survives_one_keyboard_disconnect(self):
        code = ecodes.KEY_LEFTCTRL
        self.handle("kbd-a", "keyboard A", code, 1)
        self.handle("kbd-b", "keyboard B", code, 1)

        keyswap.release_device_keys("kbd-b", "test_disconnect")

        self.assertEqual(self.values(code), [1])
        self.assertEqual(keyswap.virtual_key_owners[code], {"kbd-a"})
        self.handle("kbd-a", "keyboard A", code, 0)
        self.assertEqual(self.values(code), [1, 0])

    def test_xkb_receives_only_aggregate_transitions_and_is_rebuilt_on_disconnect(self):
        decoder = SimpleNamespace(updates=[], resets=[])
        decoder.update_key = lambda code, down: decoder.updates.append((code, down))
        decoder.reset_keys = lambda codes: decoder.resets.append(set(codes))
        decoder.char_for_keydown = lambda _code: None
        keyswap.xkb_decoder = decoder
        code = ecodes.KEY_RIGHT

        self.handle("kbd-a", "keyboard A", code, 1)
        self.handle("kbd-b", "keyboard B", code, 1)
        self.handle("kbd-a", "keyboard A", code, 0)
        keyswap.release_device_keys("kbd-b", "test_disconnect")

        self.assertEqual(decoder.updates, [(code, True)])
        self.assertEqual(decoder.resets, [set()])


class VirtualDeviceCapabilityTests(unittest.TestCase):
    def test_virtual_device_preserves_physical_repeat_capability(self):
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
        self.assertIn(ecodes.EV_REP, capabilities)
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
