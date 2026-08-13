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
        keyswap.pending_expansion_match = None
        keyswap.pending_expansion_run = None

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
        keyswap.pending_expansion_match = None
        keyswap.pending_expansion_run = None

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
    def test_virtual_device_does_not_enable_second_repeat_source(self):
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

    def test_ydotool_virtual_device_is_excluded_from_auto_discovery(self):
        virtual_device = SimpleNamespace(name="ydotoold virtual device")

        self.assertFalse(keyswap.should_auto_include_device(virtual_device))


class DeviceReconnectTests(unittest.TestCase):
    def test_removing_polled_device_requests_post_removal_rescan(self):
        device = SimpleNamespace(
            fd=21,
            path="/dev/input/event21",
            name="Logi K250",
            ungrab=lambda: None,
            close=lambda: None,
        )
        poller = SimpleNamespace(unregister=lambda _fd: None)
        fd_to_device = {device.fd: device}
        original_open_devices = keyswap.open_devices
        original_logger = keyswap.logger
        try:
            keyswap.open_devices = [device]
            keyswap.logger = logging.getLogger("keyswap-reconnect-test")
            with patch.object(keyswap, "release_device_keys"):
                should_rescan = keyswap.remove_polled_device(
                    poller,
                    fd_to_device,
                    device.fd,
                    "invalid poll mask=24",
                )
        finally:
            keyswap.open_devices = original_open_devices
            keyswap.logger = original_logger

        self.assertTrue(should_rescan)
        self.assertEqual(fd_to_device, {})


class DroppedEventRecoveryTests(unittest.TestCase):
    def setUp(self):
        keyswap.logger = logging.getLogger("keyswap-syn-dropped-test")
        keyswap.xkb_decoder = None
        keyswap.virtual_uinput = FakeUInput()
        keyswap.device_states.clear()
        keyswap.virtual_key_owners.clear()
        keyswap.virtual_pressed_keys.clear()
        keyswap.bug_trace.clear()

    def test_resync_releases_stale_key_and_rebuilds_active_keys(self):
        path = "/dev/input/event0"
        stale_code = ecodes.KEY_F
        active_code = ecodes.KEY_LEFTSHIFT
        state = keyswap.DeviceKeyState()
        state.pressed.add(stale_code)
        keyswap.device_states[path] = state
        keyswap.virtual_key_owners[stale_code] = {path}
        keyswap.virtual_pressed_keys.add(stale_code)
        device = SimpleNamespace(
            path=path,
            name="AT Translated Set 2 keyboard",
            active_keys=lambda: [active_code],
        )

        active_keys = keyswap.resync_device_keys(device, "syn_dropped")

        self.assertEqual(active_keys, {active_code})
        self.assertEqual(keyswap.device_states[path].pressed, {active_code})
        self.assertNotIn(stale_code, keyswap.virtual_key_owners)
        self.assertEqual(keyswap.virtual_key_owners[active_code], {path})
        self.assertIn((ecodes.EV_KEY, stale_code, 0), keyswap.virtual_uinput.events)
        self.assertIn((ecodes.EV_KEY, active_code, 1), keyswap.virtual_uinput.events)


class BugTracePrivacyTests(unittest.TestCase):
    def test_text_keys_are_redacted_but_navigation_keys_are_named(self):
        self.assertEqual(keyswap.diagnostic_key_name(ecodes.KEY_A), "<text-key>")
        self.assertEqual(keyswap.diagnostic_key_name(ecodes.KEY_LEFT), "KEY_LEFT")

    def test_persistent_state_snapshot_redacts_typed_text(self):
        original_buffer = keyswap.typed_buffer
        original_pending = keyswap.pending_expansion_match
        try:
            keyswap.typed_buffer = "private text"
            keyswap.pending_expansion_match = {
                "trigger": "secret",
                "replacement": "also secret",
                "last_code": ecodes.KEY_T,
            }
            snapshot = keyswap.diagnostic_state_snapshot()
        finally:
            keyswap.typed_buffer = original_buffer
            keyswap.pending_expansion_match = original_pending

        self.assertEqual(snapshot["typed_buffer"], "<redacted length=12>")
        self.assertIs(snapshot["pending_expansion_match"], True)
        self.assertNotIn("private", repr(snapshot))
        self.assertNotIn("secret", repr(snapshot))


class ExpansionBufferTests(unittest.TestCase):
    def test_expansion_buffer_limit_is_twenty_characters(self):
        self.assertEqual(keyswap.EXPANSION_BUFFER_MAX_CHARS, 20)

    def test_config_rejects_trigger_longer_than_expansion_buffer(self):
        config = {
            "devices": "auto",
            "substitutions": {},
            "expansions": {"x" * 21: "replacement"},
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
            json.dump(config, handle)
            handle.flush()
            with self.assertRaisesRegex(ValueError, "20-character limit"):
                keyswap.load_config(Path(handle.name))

    def test_typed_buffer_is_trimmed_to_twenty_characters(self):
        original_buffer = keyswap.typed_buffer
        original_decoder = keyswap.xkb_decoder
        original_logger = keyswap.logger
        try:
            keyswap.typed_buffer = ""
            keyswap.xkb_decoder = SimpleNamespace(char_for_keydown=lambda _code: "x")
            keyswap.logger = logging.getLogger("keyswap-expansion-buffer-test")
            for _ in range(25):
                keyswap.update_typed_buffer_from_keydown(ecodes.KEY_X)
            result = keyswap.typed_buffer
        finally:
            keyswap.typed_buffer = original_buffer
            keyswap.xkb_decoder = original_decoder
            keyswap.logger = original_logger

        self.assertEqual(result, "x" * 20)


if __name__ == "__main__":
    unittest.main()
