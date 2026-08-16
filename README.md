# ASUS TUF Gaming Keyboard RGB Fix

On modern ASUS TUF/ROG laptops the keyboard backlight stays dark on Linux after
a cold boot, and `asusctl` or direct sysfs writes appear to succeed while
nothing lights up.

The cause is not a missing driver. These keyboards implement **HID LampArray**
(HID usage page `0x59`, "Lighting And Illumination") — the standard behind
Windows Dynamic Lighting. Its `AutonomousMode` flag decides who owns the lamps:

| Value | Meaning |
|-------|---------|
| `0`   | The **host** owns the lamps exclusively. Nothing else may change them — not the on-board effects, not the embedded controller. |
| `1`   | The **device** may drive its own lamps again (firmware effects / EC control). |

The controller powers up with `AutonomousMode = 0`, waiting for a Dynamic
Lighting host that does not exist on Linux. The keys stay dark, and writes to
`/sys/class/leds/*kbd_backlight` succeed in the kernel but are ignored by the
device. Booting Windows clears it — Windows restores `AutonomousMode` when it
releases the device — until the next power-off.

**The fix is one feature report, one byte:** write `AutonomousMode = 1` to
release the host lock. Afterwards the normal kernel colour interface works.
This tool reads the report ID from your keyboard's own HID descriptor, so no
per-model table is needed.

You can watch this happen: on a cold boot the keyboard runs a colour sweep,
goes dark the moment Linux enumerates the device, then lights when the fix
runs. That is `device-controlled → host-locked → device-controlled`. On a
*warm* reboot there is no sweep, because the controller never lost power.

## Usage

### 1. Analyse (read-only — shows the control reports and LED node)
```bash
sudo ./kbd-fix.py
```

### 2. Release the lock and set a colour
```bash
sudo ./kbd-fix.py --arm "<mode string>"
```

### 3. Release the lock, set a colour and install a systemd service so it reapplies on boot and resume
```bash
sudo ./kbd-fix.py --arm "<mode string>" --install
```

### 4. Remove the service
```bash
sudo ./kbd-fix.py --uninstall
```

`--release` is an alias for `--arm`. `--brightness 0-3` sets the level
(default 3).

## Mode String Format

`"save mode R G B speed"` — the kernel's `kbd_rgb_mode` format.

- **save**: 1 = persist the setting
- **mode**: 0 = static, 1 = breathe, 2 = rainbow/cycle (availability varies by model)
- **R G B**: 0-255 each
- **speed**: 0-2

### Examples
- Static white: `"1 0 255 255 255 0"`
- Static green: `"1 0 0 255 0 0"`
- Static red: `"1 0 255 0 0 0"`
- Breathe blue: `"1 1 0 0 255 1"`
- Slow rainbow: `"1 2 0 0 0 0"`

## Quick Start

```bash
# See what your machine exposes:
sudo ./kbd-fix.py

# Release the lock and set static white:
sudo ./kbd-fix.py --arm "1 0 255 255 255 0"

# Make it permanent:
sudo ./kbd-fix.py --arm "1 0 255 255 255 0" --install
```

## Requirements

- Linux with a `hidraw` interface
- Python 3.9+ (standard library only)
- systemd, for `--install`
- Root privileges — raw HID access and sysfs writes

## Compatibility

Detection is by HID descriptor content, not by model or USB ID, so any device
implementing LampArray should work. Colour control additionally needs an ASUS
`*kbd_backlight` sysfs node; on other vendors' hardware the lock release still
applies but colour will not.

Note that these keyboards are **I²C-HID** and do **not** appear in `lsusb`.
If your ASUS keyboard shows up as an "N-Key Device" in `lsusb`, it uses the
older Aura protocol and this tool does not apply.

## How It Works

1. Scans `/dev/hidraw*` and parses each HID report descriptor.
2. Finds the feature report carrying usage `0x71` (`AutonomousMode`) on page
   `0x59`, or on a vendor page mirroring it.
3. Writes `AutonomousMode = 1` to that report, at its declared length.
4. Sets your colour via the `kbd_rgb_mode` / `kbd_rgb_state` / `brightness`
   sysfs interface.
5. With `--install`, writes a systemd unit that repeats this on boot and resume.

### Why release the lock rather than take it

Writing `AutonomousMode = 0` and driving the lamps directly with
`LampRangeUpdate` reports also works, and is the approach used in
[asusctl#284](https://github.com/OpenGamingCollective/asusctl/issues/284).
It gives host-side colour control without `asus-wmi`, but a userspace process
must then own the lighting continuously. This tool takes the other route —
handing control back to the firmware — so existing tools keep working and
nothing stays resident.

## Caveats

- Needs root.
- The lock release does not survive a power-off — use `--install`.
- **`AutonomousMode` is write-only on these controllers** — the write is
  accepted but a `GET_FEATURE` on that report goes unanswered, so the lock
  state cannot be verified. This is report-specific, not device-wide: the
  attributes report *is* answerable, and the tool reads it to confirm the
  channel works and report `LampCount`.
- These keyboards report **`LampCount == 1`** — single zone. Static colour and
  host-driven effects are possible; per-key is not.
- Verified on cold boot, warm reboot and suspend/resume. **Hibernate is
  untested** and likely needs more: S4 removes controller power, so the lock
  returns during resume, after the service has already run on the way down.
- This is a workaround. The real fix is kernel/daemon-side LampArray support —
  see [asusctl#284](https://github.com/OpenGamingCollective/asusctl/issues/284),
  which proposes descriptor-keyed detection rather than a per-model allowlist.
  (PR #147 was closed over authorship concerns, not a technical defect.)
  Two separate gaps keep these keyboards dark: `hid-asus` never claims the
  device because its alias is USB-only (`hid:b0003g*v00000B05p000019B6`), so an
  I²C-HID keyboard falls through to `hid-generic` with no LED device registered;
  and `asusd` only probes USB parents, so it never sees the keyboard at all —
  which is why `asusctl aura` reports `Did not find xyz.ljones.Aura`.

## Background

Discussed in the `asusctl` repository:
[OpenGamingCollective/asusctl#119](https://github.com/OpenGamingCollective/asusctl/issues/119).

Earlier versions of this tool broadcast feature reports to every vendor-page
report ID and described the operation as "arming" the controller. That worked,
but only because report `0x46` happened to be in the blast radius. The other
writes were inert: `0x5F` is unrelated ASUS Aura and is declared as 50 bytes,
so a 64-byte write was malformed and always a no-op. Reports `0x44`/`0x45` are
genuine lamp-update reports.

The widely shared `hidapitester --send-feature 70,1` is the same write:
hidapitester takes **decimal** arguments, and 70 = `0x46`. Every model
reported so far uses that same report — there is no per-model magic number.

## Support

If the keyboard stays dark:

1. Run `sudo ./kbd-fix.py` and include the full output in your report.
2. Confirm a LampArray device was found. If none is listed, your keyboard uses
   a different lighting mechanism and this tool does not apply.
3. Report results here or on
   [asusctl#119](https://github.com/OpenGamingCollective/asusctl/issues/119).

## License

MIT License
