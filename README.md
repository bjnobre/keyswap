# keyswap

`keyswap` is a Linux keyboard substitution and text-expansion daemon built on top of `evdev`, `uinput`, and `xkbcommon`.

It listens to one or more input devices, forwards normal key events through a virtual keyboard, detects configured key combinations or typed sequences, and injects replacement text system-wide.

The current runtime model is intentionally simple:

- runs as the **logged-in user**
- uses a **user-level systemd service**
- loads user config from `~/.config/keyswap/config.json`
- falls back to `/etc/keyswap/config.json`
- relies on Unix groups and `udev` rules for device access

This repository is both:

- a normal Git repo for local development
- a Debian-package source tree

---

## Current status

This is a working prototype, not a finished product.

What currently works:

- multiple configured input devices
- combo-to-text substitutions
- typed sequence expansions
- XKB-based typed character decoding
- user config overriding fallback config
- user-service runtime model
- Debian packaging scaffolding
- tolerant startup when some configured devices are missing
- removal of dead/hung-up input fds from the poll loop
- automatic keyboard discovery with `"devices": "auto"`
- hotplug/re-discovery for keyboards that disappear and later return, using `/dev/input` notifications when available

What is still rough:

- auto-discovery depends on kernel/udev keyboard metadata and intentionally excludes known pseudo-keyboard devices
- fallback re-discovery is slower if `/dev/input` notifications are unavailable
- output character support is still limited to the built-in `CHARMAP`
- sequence expansion is still timing-sensitive under some fast typing patterns
- devices are accepted from config without strong keyboard-capability validation yet
- no helper CLI yet
- no formal release process yet

---

## Configuration

Runtime configuration is loaded in this order:

1. `--config /path/to/config.json`
2. `~/.config/keyswap/config.json`
3. `/etc/keyswap/config.json`

The recommended device configuration is automatic keyboard discovery:

```json
{
  "devices": "auto"
}
```

With `"devices": "auto"`, `keyswap` listens to real keyboard devices detected through `/dev/input/event*`. This includes the internal laptop keyboard and connected USB/Bluetooth keyboards. It excludes its own virtual output device, mouse devices, and common pseudo-keyboards such as power buttons, video bus devices, hotkey-only devices, and PC speaker inputs.

Static device paths are still supported when you need explicit control:

```json
{
  "devices": [
    "/dev/input/event0"
  ]
}
```

Static `/dev/input/eventN` paths can change after reconnects. Prefer `"devices": "auto"` unless you have a specific reason to pin devices manually.

Do not judge typing latency while running with `--verbose`; debug logging is intentionally noisy and can affect interactive testing.

The systemd service uses `--log-level info`. This records startup, device
changes, matched substitutions, and sequence expansions in the journal without
logging every key event or the continuously updated typed buffer. Follow it with:

```bash
journalctl --user -u keyswap.service -f
```

For full per-key diagnostics, use `--log-level debug` (or its `--verbose`
alias). For errors and unusual conditions only, use `--log-level warning`.

---

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0).

See the `LICENSE` file for the full license text.

---

## Repository layout

```text
.
├── keyswap.py
├── config/
│   └── config.json
├── systemd/
│   └── user/
│       └── keyswap.service
├── udev/
│   └── 70-keyswap.rules
└── debian/
    ├── changelog
    ├── control
    ├── install
    ├── keyswap.postinst
    ├── keyswap.postrm
    ├── rules
    └── source/
        └── format
