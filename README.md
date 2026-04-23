# keyswap

`keyswap` is a small Linux keyboard substitution daemon for `evdev`/`uinput`.

It listens to one or more keyboard event devices, detects configured key combinations, suppresses the trigger key, and injects replacement text system-wide.

The current packaging model is intentionally simple:

- runtime as the **logged-in user**, not as a system service
- user config in `~/.config/keyswap/config.json`
- fallback config in `/etc/keyswap/config.json`
- user-level `systemd` unit
- device permissions handled through Unix groups and `udev`

This repo is structured as a Git repository that can also be built into a Debian package.

## Current status

This is a working prototype with Debian packaging scaffolding.

What it does well:

- handles multiple configured input devices
- supports combo-to-text substitutions
- prefers user config over fallback config
- runs as a user service
- ships packaging assets for Debian-based systems

What is still rough:

- output character support is limited to the built-in `CHARMAP`
- modifiers are tracked globally across configured devices
- no hotplug support yet
- no uninstall helper or management CLI yet
- no formal release process yet

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
```

## Configuration

Config precedence is:

1. explicit `--config /path/to/config.json`
2. `~/.config/keyswap/config.json`
3. `/etc/keyswap/config.json`

Example config:

```json
{
  "devices": [
    "/dev/input/event0",
    "/dev/input/event19"
  ],
  "substitutions": {
    "C-nk_minus": "=",
    "C-nk_delete": ".",
    "A-C-S-g": "great!!!"
  },
  "sequences": {
    "hell...": "Hello World!!!"
  },
  "xkb": {
    "layout": "br"
  }
}
```

### Combo syntax

Modifiers:

- `C` = Control
- `A` = Alt
- `S` = Shift
- `M` = Meta / Super

Examples:

- `C-g`
- `A-C-S-g`
- `C-nk_minus`
- `C-nk_delete`

Some key aliases currently supported:

- `nk_minus`
- `nk_delete`
- `nk_plus`
- `nk_enter`
- `nk_0` through `nk_9`
- `minus`, `equal`, `dot`, `comma`, `slash`
- `enter`, `tab`, `esc`, `delete`, `backspace`, `space`

## Finding keyboard devices

You usually need to identify the right `/dev/input/eventX` devices first.

Useful commands:

```bash
grep -E '^(N: Name=|H: Handlers=)' /proc/bus/input/devices
```

```bash
for e in /dev/input/event*; do
  name=$(cat "/sys/class/input/$(basename "$e")/device/name" 2>/dev/null)
  printf '%-18s %s\n' "$e" "$name"
done
```

## Running manually

Install dependency:

```bash
sudo apt install python3-evdev
```

Run directly for testing:

```bash
python3 ./keyswap.py --verbose
```

If it fails with permission errors, that means your user still lacks access to the input devices or `/dev/uinput`.

## Running as a user service

The intended runtime model is a `systemd --user` service.

After installation:

```bash
systemctl --user daemon-reload
systemctl --user enable --now keyswap.service
```

Logs:

```bash
journalctl --user -u keyswap.service -f
```

## Permissions model

`keyswap` needs:

- read access to `/dev/input/event*`
- read/write access to `/dev/uinput`

The intended approach is:

- user belongs to groups `input` and `uinput`
- `udev` rule assigns `0660` and the correct group to those device nodes

The packaged `udev` rule in this repo is:

```udev
KERNEL=="event*", SUBSYSTEM=="input", GROUP="input", MODE="0660"
KERNEL=="uinput", GROUP="uinput", MODE="0660", OPTIONS+="static_node=uinput"
```

You usually need to log out and back in after group membership changes.

## Debian packaging

This repo includes a Debian package skeleton.

Typical build dependencies:

- `debhelper-compat (= 13)`
- `dh-python`
- `python3`
- `python3-evdev`

Example build flow:

```bash
dpkg-buildpackage -us -uc
```

This should produce a `.deb` from the repository root once the package metadata is finalized.

### Packaging intent

The Debian package is designed to:

- install `keyswap.py`
- install fallback config in `/etc/keyswap/config.json`
- install a user service unit
- install the `udev` rule
- leave the user-level enablement step to the user

That is deliberate. The package should not guess which desktop user to enable the service for.

## Flatpak

Flatpak is not a good target for this application.

`keyswap` needs low-level host input-device access. That is exactly the sort of thing Flatpak sandboxes are bad at by design. A native package is the better route.

## Development notes

A few design choices are intentional:

- The app runs as the user, not as a privileged daemon.
- The user config overrides the global fallback.
- `/etc/keyswap/config.json` is a fallback/template, not the primary config in normal usage.
- The current repo keeps packaging files in-tree so the same source tree can be used for local hacking and package building.

## Next useful steps

- add a small `keyswapctl` helper
- expand key alias coverage
- add hotplug support
- isolate modifier state per device if needed
- write a man page
- test package build on a clean Debian system

## License

No license file has been added yet.
Add one before publishing or redistributing the package.
