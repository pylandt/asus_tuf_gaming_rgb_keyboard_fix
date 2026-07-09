#!/usr/bin/env python3
"""
kbd-fix.py - ASUS TUF Gaming keyboard RGB tool (ITE5570, USB id 0b05:19b6)

The RGB keyboard controller on FA*-series laptops must be "armed" with a
vendor HID feature report after every cold power-off (Windows/Armoury Crate
does this; Linux drivers currently do not). This tool reads the keyboard's
HID report descriptor to discover the correct arming report IDs for YOUR
specific model, arms the controller, sets a colour, and can install a systemd
service so the lighting returns automatically on every boot and resume.

USAGE
  Analyse only (read-only - just lists the report IDs for your model):
      sudo python3 kbd-fix.py

  Arm + set colour now (mode string = kbd_rgb_mode format, see below):
      sudo python3 kbd-fix.py --arm "<your mode string>"

  Arm + set colour AND install a permanent boot/resume service:
      sudo python3 kbd-fix.py --arm "<your mode string>" --install

  Remove the installed service:
      sudo python3 kbd-fix.py --uninstall

  Show this help:
      python3 kbd-fix.py --help

MODE STRING (kbd_rgb_mode) = "save mode R G B speed"
  save : 1 = persist the setting
  mode : 0 static, 1 breathe, 2 rainbow/cycle (availability varies by model)
  R G B: 0-255 each
  speed: 0-2
  Examples:
    static white : "1 0 255 255 255 0"
    static green : "1 0 0 255 0 0"
    static red   : "1 0 255 0 0 0"
    breathe blue : "1 1 0 0 255 1"
    slow rainbow : "1 2 0 0 0 0" 

CAVEATS
  - Run with sudo (needs raw HID + sysfs write access).
  - Confirmed on FA608UP, FA608UH & FA808UM. Other variants SHOULD work via
    auto-detection but are untested.
  - Assumes the keyboard is USB id 0b05:19b6. If your keyboard differs, the
    tool will say so and show you how to find your id.
  - One-shot arming does not survive a full power-off; use --install for a
    permanent fix.
"""
import glob, os, sys, fcntl, re, shutil

VID, PID = "0B05", "19B6"
LED_BASE = "/sys/class/leds/asus::kbd_backlight"
SERVICE_NAME = "kbd-rgb.service"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}"
INSTALL_BIN  = "/usr/local/bin/kbd-fix.py"
DEFAULT_MODE = "1 0 255 255 255 0"   # static white

# ---------- helpers ----------
def require_root():
    if os.geteuid() != 0:
        print("This needs root. Re-run with:  sudo python3 kbd-fix.py ...", file=sys.stderr)
        sys.exit(1)

def valid_mode_string(s):
    """A kbd_rgb_mode value: exactly 6 space-separated integers, each 0-255."""
    parts = s.split()
    if len(parts) != 6:
        return False
    for p in parts:
        if not re.fullmatch(r"\d{1,3}", p):
            return False
        if not (0 <= int(p) <= 255):
            return False
    return True

# ---------- device / descriptor ----------
def find_kbd_hidraw():
    nodes = []
    for node in sorted(glob.glob("/dev/hidraw*")):
        name = os.path.basename(node)
        try:
            ue = open(f"/sys/class/hidraw/{name}/device/uevent").read().upper()
        except OSError:
            continue
        if VID in ue and PID in ue:
            nodes.append((node, name))
    return nodes

def read_descriptor(name):
    with open(f"/sys/class/hidraw/{name}/device/report_descriptor", "rb") as f:
        return f.read()

def parse_feature_report_ids(desc):
    """Walk the HID report descriptor; return {usage_page: set(report_ids)}
    for every report id that has a FEATURE main item."""
    i, up, rid, fids = 0, None, None, {}
    n = len(desc)
    while i < n:
        b = desc[i]
        if b == 0xFE:                       # long item: 0xFE, bDataSize, bTag, data...
            if i + 1 < n:
                i += 3 + desc[i+1]
            else:
                break
            continue
        tag, size = b & 0xFC, b & 0x03
        dlen = {0:0, 1:1, 2:2, 3:4}[size]
        if i + 1 + dlen > n:                # truncated/garbage - stop safely
            break
        data = desc[i+1:i+1+dlen]
        val = int.from_bytes(data, "little") if data else 0
        if tag == 0x04:    up = val         # Usage Page (global)
        elif tag == 0x84:  rid = val        # Report ID (global)
        elif tag == 0xB0:                   # Feature (main)
            if up is not None and rid is not None:
                fids.setdefault(up, set()).add(rid)
        i += 1 + dlen
    return fids

def is_vendor_page(up):
    if up is None:
        return False
    if up >= 0xFF00:        # standard vendor-defined range
        return True
    if up in (0x59,):       # ASUS Aura page seen on FA* keyboards
        return True
    return False

def candidate_arm_ids(name):
    fids = parse_feature_report_ids(read_descriptor(name))
    vendor = []
    for up in sorted(fids):
        if is_vendor_page(up):
            vendor += sorted(fids[up])
    return sorted(set(vendor)), fids

# ---------- actions ----------
def send_feature(node, report_id, length=64):
    if length < 2:
        length = 2
    buf = bytes([report_id & 0xFF, 0x01] + [0x00] * (length - 2))
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError as e:
        print(f"    cannot open {node}: {e}")
        return False
    try:
        ioc = (3 << 30) | (len(buf) << 16) | (ord('H') << 8) | 0x06  # HIDIOCSFEATURE
        fcntl.ioctl(fd, ioc, bytes(buf))
        return True
    except OSError as e:
        print(f"    arm id {hex(report_id)} failed: {e}")
        return False
    finally:
        os.close(fd)

def set_colour(mode_str):
    if not os.path.isdir(LED_BASE):
        print(f"  {LED_BASE} not found - kernel kbd_backlight interface missing; "
              "cannot set colour (controller may still be armed).")
        return False
    ok = True
    for fname, value in (("kbd_rgb_state", "1 1 1 1 1"),
                         ("kbd_rgb_mode",  mode_str),
                         ("brightness",    "3")):
        path = f"{LED_BASE}/{fname}"
        try:
            with open(path, "w") as f:
                f.write(value)
        except OSError as e:
            print(f"  write {path} failed: {e}")
            ok = False
    return ok

def do_arm(mode_str):
    nodes = find_kbd_hidraw()
    if not nodes:
        print(f"No keyboard matched USB id {VID}:{PID}.")
        print("Find your keyboard's id with:")
        print("  for n in /dev/hidraw*; do echo $n; "
              "cat /sys/class/hidraw/$(basename $n)/device/uevent | grep HID_; done")
        return 1
    armed_any = False
    for node, name in nodes:
        try:
            ids, _ = candidate_arm_ids(name)
        except OSError as e:
            print(f"{node}: cannot read descriptor: {e}")
            continue
        if not ids:
            print(f"{node}: no vendor-page feature report IDs found")
            continue
        print(f"{node}: arming report IDs {[hex(x) for x in ids]}")
        for rid in ids:
            send_feature(node, rid)
        armed_any = True
    if not armed_any:
        return 1
    if mode_str:
        if set_colour(mode_str):
            print(f"set colour/mode: '{mode_str}'")
    else:
        print("controller armed (no colour given). Set one with --arm \"<mode>\" "
              "or write to " + LED_BASE)
    return 0

def do_analyse():
    nodes = find_kbd_hidraw()
    if not nodes:
        print(f"No /dev/hidraw* matched USB id {VID}:{PID}.")
        print("Find your keyboard's id with:")
        print("  for n in /dev/hidraw*; do echo $n; "
              "cat /sys/class/hidraw/$(basename $n)/device/uevent | grep HID_; done")
        return 1
    for node, name in nodes:
        print(f"=== {node} ({name}) ===")
        try:
            ids, fids = candidate_arm_ids(name)
        except OSError as e:
            print(f"  cannot read descriptor: {e}")
            continue
        print("  Feature report IDs by usage page:")
        for up in sorted(fids):
            mark = "  <-- VENDOR (arm these)" if is_vendor_page(up) else ""
            print(f"    page {hex(up)}: {[hex(x) for x in sorted(fids[up])]}{mark}")
        print(f"  >> Candidate ARM report IDs: {[hex(x) for x in ids]}")
    print("\nArm + set colour now, e.g. static white:")
    print(f'  sudo python3 kbd-fix.py --arm "{DEFAULT_MODE}"')
    print("Add --install to make it permanent across reboots.")
    return 0

# ---------- service install ----------
def do_install(mode_str):
    if not mode_str:
        mode_str = DEFAULT_MODE
    if not valid_mode_string(mode_str):
        print(f"Refusing to install: invalid mode string '{mode_str}' "
              "(need 6 integers 0-255, e.g. \"1 0 255 255 255 0\").")
        return 1
    try:
        src = os.path.abspath(__file__)
    except NameError:
        print("Cannot determine script path (piped input?). Save the script to a "
              "file and run it from there to use --install.")
        return 1
    try:
        if os.path.abspath(src) != os.path.abspath(INSTALL_BIN):
            shutil.copy(src, INSTALL_BIN)
        os.chmod(INSTALL_BIN, 0o755)
    except OSError as e:
        print(f"could not copy to {INSTALL_BIN}: {e}")
        return 1
    # mode_str is validated above (digits/spaces only) so it is safe to embed.
    service = f"""[Unit]
Description=ASUS keyboard RGB arm and colour
After=multi-user.target suspend.target hibernate.target hybrid-sleep.target

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/env python3 {INSTALL_BIN} --arm "{mode_str}"

[Install]
WantedBy=multi-user.target suspend.target hibernate.target hybrid-sleep.target
"""
    try:
        with open(SERVICE_PATH, "w") as f:
            f.write(service)
    except OSError as e:
        print(f"could not write {SERVICE_PATH}: {e}")
        return 1
    if os.system("systemctl daemon-reload") != 0:
        print("warning: systemctl daemon-reload failed (is this a systemd system?)")
    os.system(f"systemctl enable --now {SERVICE_NAME}")
    print(f"installed {SERVICE_PATH}")
    print(f"  arms + sets mode '{mode_str}' on every boot and resume")
    print(f"  remove with: sudo python3 kbd-fix.py --uninstall")
    return 0

def do_uninstall():
    os.system(f"systemctl disable --now {SERVICE_NAME} 2>/dev/null")
    for p in (SERVICE_PATH, INSTALL_BIN):
        try:
            os.remove(p)
            print(f"removed {p}")
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"could not remove {p}: {e}")
    os.system("systemctl daemon-reload")
    return 0

# ---------- arg handling ----------
def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    if "--uninstall" in args:
        require_root()
        return do_uninstall()

    if "--arm" in args:
        require_root()
        idx = args.index("--arm")
        mode_str = ""
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            mode_str = args[idx + 1]
        if mode_str and not valid_mode_string(mode_str):
            print(f"Invalid mode string '{mode_str}'. Need 6 integers 0-255, "
                  'e.g. "1 0 255 255 255 0" (save mode R G B speed).')
            return 1
        rc = do_arm(mode_str)
        if "--install" in args and rc == 0:
            return do_install(mode_str)
        return rc

    if "--install" in args:
        require_root()
        return do_install("")

    # default: analyse (read-only, but descriptor read still needs root on most systems)
    require_root()
    return do_analyse()

if __name__ == "__main__":
    sys.exit(main())
