#!/usr/bin/env python3
"""
kbd-fix.py - ASUS TUF Gaming keyboard RGB tool

Restore keyboard RGB control on Linux for HID LampArray keyboards.

PROBLEM
    Modern ASUS TUF/ROG keyboards (ITE controllers on I2C-HID, invisible to
    lsusb) implement HID LampArray, the standard behind Windows Dynamic
    Lighting.  Its AutonomousMode flag -- "Lighting And Illumination" usage
    page 0x59, usage 0x71 -- decides who owns the lamps:
 
        0 = the host owns them exclusively; nothing else may change them
        1 = the device drives its own lamps (embedded effects / EC)
 
    The controller powers up with AutonomousMode = 0, waiting for a Dynamic
    Lighting host that does not exist on Linux.  So the keys stay dark, and
    writes to /sys/class/leds/*kbd_backlight succeed in the kernel but are
    ignored by the device.  Booting Windows clears it -- Windows restores
    AutonomousMode when it releases the device -- until the next power-off.
 
FIX
    Write AutonomousMode = 1: one feature report, one byte.  That releases
    the host lock, after which the kernel colour interface works normally.
    The report ID is read from the device's own HID descriptor, so no
    per-model table is needed.  The setting is lost on power-off, hence
    --install.
 
    The opposite approach is equally valid: write AutonomousMode = 0 to keep
    the lock and drive the lamps directly with LampRangeUpdate reports.  That
    gives host-side colour control without asus-wmi, but a userspace process
    must then own the lighting for as long as it should stay lit.  This tool
    takes the other route -- hand control back to the firmware -- so that
    existing tools keep working and nothing has to stay resident.

USAGE
    Analyse (read-only):
    sudo ./kbd-fix.py
    
    Release lock, set colour:
    sudo ./kbd-fix.py --arm "<your mode string>"
    
    Release lock, set colour AND install a permanent boot/resume service:
    sudo ./kbd-fix.py --arm "<your mode string>" --install
    
    Remove the installed service:
    sudo ./kbd-fix.py --uninstall

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
"""

from __future__ import annotations
 
import argparse
import contextlib
import fcntl
import glob
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
 
# HID "Lighting And Illumination" page and the usages within it.
# Vendors may mirror the same collection onto a private page (>= 0xFF00).
LIGHTING_PAGE = 0x59
# These are *field* usages, not the collection usages that introduce their
# reports (0x70 / 0x02): a usage before a Collection belongs to the
# collection, so only the inner field usages reach the Feature item.
USAGE_AUTONOMOUS_MODE = 0x71   # sole field of LampArrayControlReport
# LampCount alone is ambiguous -- it also appears in LampMultiUpdateReport --
# so the attributes report is identified by LampCount plus a bounding-box
# field, which only it declares.  LampCount is its first field.
ATTRIBUTE_USAGES = {0x03, 0x04}
 
LED_GLOB = "/sys/class/leds/*kbd_backlight"
SERVICE_NAME = "kbd-rgb.service"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}"
INSTALL_BIN = "/usr/local/bin/kbd-fix.py"
DEFAULT_MODE = "1 0 255 255 255 0"  # static white
DEFAULT_BRIGHTNESS = 3
 
MODE_RE = re.compile(r"\A\d{1,3}(?: \d{1,3}){5}\Z")
 
 
# ---------------- HID plumbing ----------------
 
def _hid_ioctl(nr: int, size: int) -> int:
    """_IOC(_IOC_WRITE|_IOC_READ, 'H', nr, size) -- the HIDIOC* macro form."""
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr
 
 
@contextlib.contextmanager
def _hid_open(path: str):
    fd = os.open(path, os.O_RDWR)
    try:
        yield fd
    finally:
        os.close(fd)
 
 
def _get_feature(fd: int, report_id: int, size: int) -> bytes | None:
    """HIDIOCGFEATURE. Returns the payload without its ID byte, or None.
 
    The ioctl yields the byte count actually read; anything at or below the
    report-ID byte means the device did not answer.  Distinguishing that from
    a genuine zero matters, because some controllers accept the write but do
    not report state back.
    """
    buf = bytearray(size + 1)
    buf[0] = report_id
    try:
        read = fcntl.ioctl(fd, _hid_ioctl(0x07, len(buf)), buf, True)
    except OSError:
        return None
    return bytes(buf[1:read]) if isinstance(read, int) and read > 1 else None
 
 
def _set_feature(fd: int, report_id: int, payload: bytes) -> bool:
    """HIDIOCSFEATURE. Writes exactly the report's declared length."""
    buf = bytes((report_id,)) + payload
    try:
        fcntl.ioctl(fd, _hid_ioctl(0x06, len(buf)), buf)
    except OSError as exc:
        print(f"    write to report {report_id:#04x} failed: {exc}")
        return False
    return True
 
 
# ---------------- descriptor parsing ----------------
 
@dataclass
class Report:
    """Feature-item facts for one report ID."""
 
    usages: set[tuple[int, int]] = field(default_factory=set)  # (page, usage)
    bits: int = 0
 
    @property
    def size(self) -> int:
        return max(1, (self.bits + 7) // 8)
 
 
def parse_descriptor(desc: bytes) -> dict[int, Report]:
    """Collect FEATURE items from a HID report descriptor, keyed by report ID.
 
    Global items (usage page, report ID/size/count) persist until changed and
    are saved/restored by Push/Pop; local items (usage) are consumed by the
    next main item.  Usage *ranges* are ignored -- LampArray always declares
    AutonomousMode as a single usage.  Malformed input ends the walk rather
    than raising.
    """
    state = {"page": None, "id": 0, "size": 0, "count": 0}
    stack: list[dict] = []
    usages: list[tuple[int, int]] = []
    reports: dict[int, Report] = {}
    pos, end = 0, len(desc)
 
    while pos < end:
        prefix = desc[pos]
 
        if prefix == 0xFE:  # long item: prefix, data length, tag, data...
            if pos + 1 >= end:
                break
            pos += 3 + desc[pos + 1]
            continue
 
        tag = prefix & 0xFC
        size_code = prefix & 0x03
        length = 4 if size_code == 3 else size_code
        if pos + 1 + length > end:
            break
        value = int.from_bytes(desc[pos + 1:pos + 1 + length], "little")
 
        if tag == 0x04:                        # Usage Page (global)
            state["page"] = value
        elif tag == 0x84:                      # Report ID (global)
            state["id"] = value
        elif tag == 0x74:                      # Report Size (global)
            state["size"] = value
        elif tag == 0x94:                      # Report Count (global)
            state["count"] = value
        elif tag == 0xA4:                      # Push
            stack.append(dict(state))
        elif tag == 0xB4:                      # Pop
            if stack:
                state = stack.pop()
        elif tag == 0x08:                      # Usage (local)
            # A 4-byte usage carries its own page in the high half.
            usages.append((value >> 16, value & 0xFFFF) if length == 4
                          else (state["page"], value))
        elif tag == 0xB0:                      # Feature (main)
            report = reports.setdefault(state["id"], Report())
            report.bits += state["size"] * state["count"]
            report.usages.update(usages)
            usages.clear()
        elif tag in (0x80, 0x90, 0xA0, 0xC0):  # Input/Output/Collection/End
            usages.clear()                     # other main items also consume
 
        pos += 1 + length
 
    return reports
 
 
# ---------------- devices ----------------
 
@dataclass
class Control:
    """A LampArray feature report located in the descriptor."""
 
    report_id: int
    page: int
    size: int
    label: str = "control"
 
    @property
    def standard(self) -> bool:
        return self.page == LIGHTING_PAGE
 
    def __str__(self) -> str:
        kind = "spec page" if self.standard else "vendor mirror"
        return (f"{self.label} report {self.report_id:#04x} "
                f"page {self.page:#06x} ({kind}), {self.size}B")
 
 
@dataclass
class Device:
    path: str
    name: str
    hid_id: str
    controls: list[Control]          # AutonomousMode -- written by --arm
    attributes: Control | None = None  # LampArrayAttributes -- read only
 
 
def _find(reports: dict[int, Report], required: set[int],
          label: str) -> list[Control]:
    """Reports whose fields include every usage in `required`.
 
    Matching on a set matters: LampCount appears in both the attributes and
    the multi-update report, so a single usage would be ambiguous.  Only the
    lighting page and vendor mirrors of it are considered, and the standard
    page is returned first.
    """
    found = []
    for report_id, report in sorted(reports.items()):
        pages = {page for page, _ in report.usages
                 if page == LIGHTING_PAGE or (page or 0) >= 0xFF00}
        for page in sorted(pages):
            declared = {usage for pg, usage in report.usages if pg == page}
            if required <= declared:
                found.append(Control(report_id, page, report.size, label))
    return sorted(found, key=lambda control: not control.standard)
 
 
def _controls(reports: dict[int, Report]) -> list[Control]:
    """Reports carrying AutonomousMode -- the ones this tool writes."""
    return _find(reports, {USAGE_AUTONOMOUS_MODE}, "control")
 
 
def discover() -> list[Device]:
    """LampArray devices, found by descriptor content rather than USB id.
 
    Matching on the descriptor is what makes this vendor and model neutral,
    and it is the only option for I2C-HID keyboards, which never appear in
    lsusb.
    """
    devices = []
    for path in sorted(glob.glob("/dev/hidraw*")):
        sysfs = f"/sys/class/hidraw/{os.path.basename(path)}/device"
        try:
            with open(f"{sysfs}/report_descriptor", "rb") as handle:
                reports = parse_descriptor(handle.read())
        except OSError:
            continue
        controls = _controls(reports)
        if not controls:
            continue
        attributes = next(iter(_find(reports, ATTRIBUTE_USAGES, "attributes")),
                          None)
        info: dict[str, str] = {}
        with contextlib.suppress(OSError), open(f"{sysfs}/uevent") as handle:
            info = dict(line.split("=", 1)
                        for line in handle.read().splitlines() if "=" in line)
        devices.append(Device(path, info.get("HID_NAME", "?"),
                              info.get("HID_ID", "?"), controls, attributes))
    return devices
 
 
def read_lamp_count(fd: int, attributes: Control | None) -> int | None:
    """LampCount from LampArrayAttributesReport: a u16 at the start.
 
    Unlike AutonomousMode, this report is answerable on the controllers seen
    so far, so it doubles as proof that the LampArray channel really works.
    """
    if attributes is None:
        return None
    data = _get_feature(fd, attributes.report_id, attributes.size)
    return int.from_bytes(data[:2], "little") if data and len(data) >= 2 else None
 
 
def _fmt(value: int | None) -> str:
    return {None: "unreadable", 0: "host-locked",
            1: "device-controlled"}.get(value, str(value))
 
 
def read_mode(fd: int, control: Control) -> int | None:
    data = _get_feature(fd, control.report_id, control.size)
    return data[0] if data else None
 
 
def release_lock(device: Device) -> bool:
    """Set AutonomousMode = 1 on each control report.
 
    Success is whether the write was accepted, not whether the read-back
    agrees: some controllers apply the setting yet never answer state
    queries, so the read-back is diagnostic only.  A confirmed report
    short-circuits the rest, so a vendor mirror is written only when the
    standard report does not confirm.
    """
    written_any = False
    with _hid_open(device.path) as fd:
        for control in device.controls:
            before = read_mode(fd, control)
            written = _set_feature(fd, control.report_id,
                                   b"\x01".ljust(control.size, b"\0"))
            after = read_mode(fd, control)
            written_any |= written
            if not written:
                note = "  [write rejected]"
            elif after is None:
                note = "  [accepted; state not readable]"
            elif after != 1:
                note = "  [accepted but still reported as locked]"
            else:
                note = ""
            print(f"  {control}: {_fmt(before)} -> {_fmt(after)}{note}")
            if after == 1:
                break
    return written_any
 
 
# ---------------- colour ----------------
 
RGB_ATTR = "kbd_rgb_mode"
# Written in this order: enable all power states, set the effect, then level.
LED_ATTRS = ("kbd_rgb_state", RGB_ATTR, "brightness")
 
 
def led_node() -> str | None:
    """Prefer an RGB-capable kbd_backlight node over a brightness-only one."""
    nodes = sorted(glob.glob(LED_GLOB))
    return next((node for node in nodes
                 if os.path.exists(f"{node}/{RGB_ATTR}")),
                nodes[0] if nodes else None)
 
 
def led_attrs(node: str) -> list[str]:
    """Control files the node actually exposes, in write order."""
    return [name for name in LED_ATTRS if os.path.exists(f"{node}/{name}")]
 
 
def apply_colour(mode: str, brightness: int) -> bool:
    node = led_node()
    if not node:
        print("  no *kbd_backlight node found; colour not set "
              "(the host lock is still released)")
        return False
    values = {"kbd_rgb_state": "1 1 1 1 1", RGB_ATTR: mode,
              "brightness": str(brightness)}
    available = led_attrs(node)
    if RGB_ATTR not in available:
        print(f"  {node} exposes no {RGB_ATTR}; setting brightness only")
    ok = True
    for name in available:
        try:
            with open(f"{node}/{name}", "w") as handle:
                handle.write(values[name])
        except OSError as exc:
            print(f"  write {node}/{name} failed: {exc}")
            ok = False
    if ok:
        print(f"  {node}: mode '{mode}', brightness {brightness}")
    return ok
 
 
# ---------------- commands ----------------
 
def cmd_analyse() -> int:
    devices = discover()
    if not devices:
        print("No HID device exposes a LampArray AutonomousMode field.\n"
              "This tool does not apply here: if your keyboard is dark, its\n"
              "lighting uses a different mechanism.")
        return 1
 
    for device in devices:
        print(f"{device.path}  {device.hid_id}  {device.name}")
        # Read everything in one open, then report outside it.
        with _hid_open(device.path) as fd:
            lamps = read_lamp_count(fd, device.attributes)
            states = [(control, read_mode(fd, control))
                      for control in device.controls]
        if device.attributes:
            counted = f"{lamps} lamp{'' if lamps == 1 else 's'}" if lamps \
                else "no answer"
            zones = " (single zone; per-key not available)" if lamps == 1 else ""
            print(f"  {device.attributes}: {counted}{zones}")
        for control, state in states:
            print(f"  {control}: {_fmt(state)}")
        if all(state is None for _, state in states):
            print("  AutonomousMode is not readable on this controller, so the\n"
                  "  current lock state is unknown; --arm writes regardless")
 
    node = led_node()
    if not node:
        print("\nLED node: none found -- colour cannot be set, but releasing\n"
              "the lock may still restore the keyboard's own lighting.")
    else:
        available = led_attrs(node)
        print(f"\nLED node: {node} [{', '.join(available) or 'no controls'}]")
        if RGB_ATTR not in available:
            print(f"  no {RGB_ATTR} here -- brightness only, no colour control")
 
    print(f'\nRelease the lock:  sudo {sys.argv[0]} --arm "{DEFAULT_MODE}"')
    print("Add --install to reapply it on boot and resume.")
    return 0
 
 
def cmd_apply(mode: str, brightness: int) -> int:
    devices = discover()
    if not devices:
        print("No LampArray device found. Run without arguments for details.")
        return 1
    released = False
    for device in devices:
        print(f"{device.path} ({device.name}):")
        released |= release_lock(device)
    if not released:
        return 1
    if mode:
        apply_colour(mode, brightness)
    else:
        print(f'  lock released; no colour given (try --arm "{DEFAULT_MODE}")')
    return 0
 
 
def cmd_install(mode: str, brightness: int) -> int:
    try:
        source = os.path.abspath(__file__)
    except NameError:  # executed from stdin
        print("--install needs the script saved to a file.", file=sys.stderr)
        return 1
    # mode and brightness are validated as plain integers, so the values
    # embedded in the unit file cannot inject additional directives.
    unit = f"""\
[Unit]
Description=Release HID LampArray host lock and restore keyboard RGB
After=multi-user.target suspend.target hibernate.target hybrid-sleep.target
 
[Service]
Type=oneshot
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/env python3 {INSTALL_BIN} --arm "{mode}" --brightness {brightness}
 
[Install]
WantedBy=multi-user.target suspend.target hibernate.target hybrid-sleep.target
"""
    try:
        if source != INSTALL_BIN:
            shutil.copy(source, INSTALL_BIN)
        os.chmod(INSTALL_BIN, 0o755)
        with open(SERVICE_PATH, "w") as handle:
            handle.write(unit)
    except OSError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    os.system("systemctl daemon-reload")
    os.system(f"systemctl enable --now {SERVICE_NAME}")
    print(f"installed {SERVICE_PATH} (mode '{mode}', brightness {brightness})")
    print(f"  remove with: sudo {sys.argv[0]} --uninstall")
    return 0
 
 
def cmd_uninstall() -> int:
    os.system(f"systemctl disable --now {SERVICE_NAME} 2>/dev/null")
    for path in (SERVICE_PATH, INSTALL_BIN):
        try:
            os.remove(path)
            print(f"removed {path}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"could not remove {path}: {exc}")
    os.system("systemctl daemon-reload")
    return 0
 
 
# ---------------- main ----------------
 
def valid_mode(mode: str) -> bool:
    """kbd_rgb_mode value: six integers 0-255, single-space separated."""
    return bool(MODE_RE.match(mode)) and all(
        int(part) <= 255 for part in mode.split())
 
 
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Release the HID LampArray host lock so the keyboard "
                    "backlight works on Linux.",
        epilog='example: sudo ./kbd-fix.py --arm "1 0 0 255 0 0" --install')
    parser.add_argument(
        "--arm", "--release", nargs="?", const="", metavar="MODE",
        help='release the lock; optional colour "save mode R G B speed", '
             'e.g. "1 0 255 255 255 0"')
    parser.add_argument(
        "--brightness", type=int, choices=range(4), default=DEFAULT_BRIGHTNESS,
        metavar="0-3", help="LED brightness (default: 3)")
    parser.add_argument("--install", action="store_true",
                        help="also install a boot/resume systemd service")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the systemd service")
    return parser.parse_args(argv)
 
 
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
 
    if os.geteuid() != 0:
        print("Needs root: sudo ./kbd-fix.py ...", file=sys.stderr)
        return 1
 
    if args.uninstall:
        return cmd_uninstall()
 
    if args.arm is None and not args.install:
        return cmd_analyse()
 
    mode = args.arm or ""
    if mode and not valid_mode(mode):
        print(f'Invalid mode "{mode}": expected six integers 0-255, '
              'e.g. "1 0 255 255 255 0".', file=sys.stderr)
        return 1
 
    status = cmd_apply(mode, args.brightness) if args.arm is not None else 0
    if status == 0 and args.install:
        status = cmd_install(mode or DEFAULT_MODE, args.brightness)
    return status
 
 
if __name__ == "__main__":
    sys.exit(main())
 
