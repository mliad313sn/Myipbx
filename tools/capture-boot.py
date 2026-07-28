"""Boot the finished image in an emulator and photograph the screen.

Every frame this writes came off the emulated machine's own framebuffer while
the appliance was starting: the bootloader menu, the kernel coming up, the
services starting, the login prompt. Nothing is drawn or reconstructed. Where
the screen was blank at the moment a frame was taken -- which happens between
the bootloader handing over and the first console output -- the frame is
dropped rather than kept as evidence of nothing.

The emulator's monitor is driven over a Unix socket, and its ``screendump``
writes a portable pixmap that is converted here. The serial console is captured
alongside, so the text of the boot is on record as well as the picture of it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
IMAGE = Path(
    os.environ.get("MYIPBX_IMAGE", "/var/tmp/myipbx-image/output/myipbx-appliance.iso")
)
FIRMWARE = Path("/usr/share/ovmf/OVMF.fd")


class Monitor:
    """The emulator's own command channel, over a Unix socket."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: socket.socket | None = None

    def open(self, attempts: int = 60) -> None:
        for _ in range(attempts):
            if self.path.exists():
                try:
                    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    connection.settimeout(10.0)
                    connection.connect(str(self.path))
                    self.connection = connection
                    self.drain()
                    return
                except OSError:
                    pass
            time.sleep(0.5)
        raise RuntimeError(f"the emulator's monitor never appeared at {self.path}")

    def drain(self) -> str:
        assert self.connection is not None
        collected = b""
        self.connection.settimeout(0.4)
        try:
            while True:
                chunk = self.connection.recv(65536)
                if not chunk:
                    break
                collected += chunk
        except (TimeoutError, socket.timeout, OSError):
            pass
        return collected.decode("utf-8", "replace")

    def command(self, text: str) -> str:
        assert self.connection is not None
        self.connection.sendall((text + "\n").encode("utf-8"))
        time.sleep(0.4)
        return self.drain()

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass


def convert(source: Path, destination: Path) -> tuple[bool, str]:
    """Turn one pixmap into a picture, and say whether it shows anything.

    A frame taken while the screen is blank is a black rectangle. Keeping it
    would pad the record with pictures of nothing, so it is reported and
    dropped by the caller.
    """
    with Image.open(source) as image:
        picture = image.convert("RGB")
        extrema = picture.convert("L").getextrema()
        picture.save(destination, "PNG", optimize=True)
    lightest = extrema[1]
    return lightest > 12, f"lightest pixel {lightest}"


def capture(mode: str, destination: Path, schedule: list[tuple[float, str]]) -> int:
    """Boot once and take a frame at each moment in the schedule."""
    destination.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp/myipbx-boot-capture") / mode
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    monitor_path = work / "monitor.sock"
    serial_path = destination / f"{mode}-serial-console.txt"

    command = [
        "qemu-system-x86_64",
        "-m", "2048",
        "-smp", "2",
        "-cdrom", str(IMAGE),
        "-boot", "d",
        "-vga", "std",
        "-display", "none",
        "-monitor", f"unix:{monitor_path},server,nowait",
        "-serial", f"file:{serial_path}",
        "-net", "none",
    ]
    if mode == "firmware":
        if not FIRMWARE.is_file():
            print(f"  the firmware image at {FIRMWARE} is absent; skipping this path")
            return 0
        command += ["-bios", str(FIRMWARE)]

    print(f"  booting the image, {mode} path")
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )

    monitor = Monitor(monitor_path)
    kept = 0
    started = time.monotonic()
    try:
        monitor.open()
        for at, name in schedule:
            wait = at - (time.monotonic() - started)
            if wait > 0:
                time.sleep(wait)
            if process.poll() is not None:
                print("  the emulator exited early")
                break
            pixmap = work / f"{name}.ppm"
            monitor.command(f"screendump {pixmap}")
            for _ in range(20):
                if pixmap.exists() and pixmap.stat().st_size > 0:
                    break
                time.sleep(0.2)
            if not pixmap.exists():
                print(f"  no frame came back at {int(at)} seconds")
                continue
            picture = destination / f"{mode}-{name}.png"
            try:
                lit, note = convert(pixmap, picture)
            except Exception as error:  # noqa: BLE001
                print(f"  the frame at {int(at)} seconds could not be converted: {error}")
                continue
            if lit:
                kept += 1
                print(f"  captured {picture.name} at {int(at)} seconds ({note})")
            else:
                picture.unlink(missing_ok=True)
                print(f"  the screen was blank at {int(at)} seconds ({note}); dropped")
    finally:
        monitor.close()
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(work, ignore_errors=True)
    return kept


def main() -> int:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "captures" / "boot"
    if not IMAGE.is_file():
        print(f"there is no image at {IMAGE}")
        return 1

    print(f"the image is {IMAGE} ({IMAGE.stat().st_size // 1048576} mebibytes)")

    #: When to look, and what that moment is. Chosen against what a boot of
    #: this image actually does rather than at even intervals: the bootloader
    #: menu waits, the kernel is quick, and the services take the longest.
    #: The emulator here has no hardware assistance, so guest time runs at
    #: roughly a fifteenth of wall clock: the bootloader's ten second menu took
    #: about a hundred and fifty seconds to count down. The moments below are
    #: wall clock and were calibrated against that, not guessed at even
    #: intervals. The repository's own boot test allows six hundred seconds for
    #: the same reason.
    schedule = [
        (5.0, "01-bootloader-menu"),
        (120.0, "02-bootloader-menu-counting-down"),
        (200.0, "03-handing-over-to-the-kernel"),
        (320.0, "04-starting"),
        (450.0, "05-starting-services"),
        (580.0, "06-further-in"),
        (700.0, "07-login-prompt"),
        (820.0, "08-settled"),
    ]

    total = 0
    for mode in ("legacy", "firmware"):
        total += capture(mode, destination, schedule)
    print(f"kept {total} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
