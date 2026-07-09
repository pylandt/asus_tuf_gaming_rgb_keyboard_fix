# ASUS TUF Gaming A16 Keyboard RGB Controller Fix

On ASUS TUF Gaming A16 FA608* models, the keyboard RGB controller (ITE5570, USB id 0b05:19b6) must be "armed" on cold boot by sending HID feature reports to the keyboard. Windows/Armoury Crate handles this automatically, but Linux drivers currently do not.

This tool reads your keyboard's HID report descriptor to discover the correct arming report IDs for YOUR specific model, arms the controller, sets a colour, and can install a systemd service so the lighting returns automatically on every boot and resume.

## Usage

### 1. Analyse only (read-only - just lists the report IDs for your model):
```bash
sudo python3 fa608-kbd.py
```

### 2. Arm + set colour now (mode string = kbd_rgb_mode format):
```bash
sudo python3 fa608-kbd.py --arm "<your mode string>"
```

### 3. Arm + set colour AND install a permanent boot/resume service:
```bash
sudo python3 fa608-kbd.py --arm "<your mode string>" --install
```

### 4. Remove the installed service:
```bash
sudo python3 fa608-kbd.py --uninstall
```

## Mode String Format

`MODE STRING = "save mode R G B speed"`

- **save**: 1 = persist the setting
- **mode**: 0 = static, 1 = breathe, 2 = rainbow/cycle (availability varies by model)
- **R G B**: 0-255 each
- **speed**: 0-2

### Examples:
- Static white: `"1 0 255 255 255 0"`
- Static green: `"1 0 0 255 0 0"`
- Static red: `"1 0 255 0 0 0"`
- Breathe blue: `"1 1 0 0 255 1"`
- Slow rainbow: `"1 2 0 0 0 0"`

## Quick Start

```bash
# Check your model's report IDs:
sudo python3 fa608-kbd.py

# Arm and set static white lighting:
sudo python3 fa608-kbd.py --arm "1 0 255 255 255 0"

# Make it permanent (automatically arm on every boot/resume):
sudo python3 fa608-kbd.py --arm "1 0 255 255 255 0" --install
```

## Requirements

- Linux OS
- Python 3.x
- Root/sudo privileges (required for HID access and sysfs writes)

## Compatibility

- ASUS TUF Gaming A16 FA608* (confirmed working for FA608UP, FA608UH)
- Other FA608 variants should work via auto-detection but may need testing - please report results

## How It Works

1. The tool scans `/dev/hidraw*` to find your keyboard (USB id 0b05:19b6)
2. It parses the HID report descriptor to discover vendor-page FEATURE report IDs
3. It sends 64-byte feature reports to arm the RGB controller
4. It then sets your desired colour via the Linux kbd_backlight sysfs interface
5. Optionally, it installs a systemd service that automatically repeats this on boot and resume

## Caveats

- Requires sudo (needs raw HID access and sysfs write permissions)
- One-shot arming does not survive a full power-off; use `--install` for a permanent fix
- If your keyboard's USB ID differs from 0b05:19b6, the tool will tell you how to find it

## Background

This script was created to address the issue discussed in the official `asusctl` repository:
- [OpenGamingCollective/asusctl#119](https://github.com/OpenGamingCollective/asusctl/issues/119#issuecomment-4783713758)

The issue documents the RGB controller arming problem on FA608* models and the solution implemented in this tool.

## Support

If you encounter any issues:
1. Run `sudo python3 fa608-kbd.py` to see your device's detected report IDs
2. Check that your keyboard is detected (USB id 0b05:19b6)
3. Report results on this repository or the [asusctl issue](https://github.com/OpenGamingCollective/asusctl/issues/119)

## License

MIT License
