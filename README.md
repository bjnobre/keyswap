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

What is still rough:

- no hotplug/re-discovery yet for keyboards that disappear and later return
- removable keyboards using `/dev/input/eventN` are fragile unless you use stable paths or custom symlinks
- output character support is still limited to the built-in `CHARMAP`
- sequence expansion is still timing-sensitive under some fast typing patterns
- devices are accepted from config without strong keyboard-capability validation yet
- no helper CLI yet
- no formal release process yet

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
