# Captures

Photographs and transcripts of this appliance being installed and being used.
Nothing here is drawn, mocked or reconstructed. Every picture came off a real
screen — the emulated machine's own framebuffer while the image was booting, or
a real browser driving the real console over a real socket — and every
transcript is the output of a command that was actually run, at the time
stamped on it.

Three sets, in the order a technician meets them.

## `installation/` — putting the operating system on the machine

Text, because this part of the product has no pictures: it runs on a console
before there is a browser to look at.

| File | What it is |
| --- | --- |
| `01-preflight-check.txt` | `scripts/preflight-check.sh` run on this machine. It reports what is satisfied, what is worth knowing, and what blocks the installation, and it refuses to go on while anything blocks. Note the line confirming the machine runs no address allocation service — that check is part of the installer, not a claim made about it. |
| `02-disk-installer-rehearsal.txt` | `iso/installer/crossbar-install-to-disk.sh --rehearse --disk /dev/vda`, which reads the disks, works out exactly what it would do to the chosen one, and prints every command without running any of them. |
| `03-image-build-usage.txt` | `iso/build-iso.sh --help`: the five stages and the options. |
| `04-image-build-rehearsal.txt` | `iso/build-iso.sh --rehearse`, each of the five stages reporting what it would do. |

## `boot/` — the image starting on a machine

The finished image booted in an emulator, photographed at each stage of the
boot. Both firmware paths were captured: `legacy-*` is the BIOS path through
isolinux, `firmware-*` is the UEFI path through GRUB.

`legacy-serial-console.txt` and `firmware-serial-console.txt` are the boot's own
text, taken off the serial line at the same time, so the words are on record as
well as the pictures.

The frames run from the bootloader menu, through the kernel taking over and the
services starting, to the login prompt — which is the screen that tells whoever
is standing in front of the machine where to point a browser.

**What taking them found.** These frames are of the corrected image. Taking the
first set found four defects on the two screens a technician reads before there
is a browser to read one in, and all four are fixed; they are worth stating
because they are the reason the captures exist.

- **The firmware path did not start the machine at all.** GRUB reported
  `configfile.mod not found` and stopped at a bare `grub>` prompt, with nothing
  on screen saying why. Every machine sold for years boots this way, so the
  image started only on old hardware. A standalone bootloader keeps its modules
  in a memory disk inside itself and finds them through its prefix; the
  configuration it carried reset that prefix to a directory on the image, which
  has never held a module. The prefix is left alone now, the menu is carried
  inside the bootloader rather than fetched off the image, and the modules the
  boot needs are named rather than left to a default.
- **A build that could not produce a firmware bootloader warned and carried
  on**, which is how the above shipped. All three failure points stop the build.
- **Both screens spelled the console's port** — *"port number eight thousand
  eighty-eight"* — which is the one thing on them somebody has to type into a
  browser. They spelled each value on its own, and a bare `8088` carries no
  context that makes it an identifier. Spelling the sentence gets the digits
  back.
- **The bootloader menu was cutting its lines off.** It draws inside a box
  narrower than the screen; at the shipped geometry an entry got fifty-six
  characters, so "no power management" arrived as "no power manageme" and the
  note naming the console lost its port entirely.

`tests/test_first_screens.py` pins all four, and the frames here were taken off
the image rebuilt with them. They also carry the name: the bootloader menu, the
login screen and the host name are all Crossbar, and the appliance's own
services start under that name with nothing failing on the way up.

One thing to be plain about: this machine has no route to a package mirror, so
the image these frames came from was re-mastered from the existing build tree
rather than bootstrapped from nothing. The bootloaders, the boot menu, the
login screen, the compressed root filesystem and the appliance payload inside
it were all rebuilt from the current source; the base operating system and its
packages are from the original bootstrap.

## `console/` — setting up the IPBX through the interface

Forty-nine screenshots of a real browser driving a real appliance from an
empty configuration to a working telephone system: sign in, look at the
hardware, add extensions, declare a trunk, route calls in and out, build a ring
group, a menu, a queue, a conference and a time condition, then the firewall,
the transport security, the audit journal, backup, the tasks the appliance runs
for itself and the constraint audit.

`captures.json` carries a caption for each file.

Five of them are the **reports** section, drawn from a real call record file and
a real queue log the appliance under test was given — a week of calls with a
working day's shape, and a week of a switchboard — and aggregated through the
same readers the console uses. The summary, the hour-of-day distribution, the
per-extension breakdown, the queues, and the period control with its two date
boxes.

One is the **portal**: the same appliance signed in to by an extension's owner
rather than the administrator, showing its own calls and nothing else. That is a
real sign in as a real scoped account, so what is photographed is the
authorisation boundary working rather than a page with its links hidden.

The last five are the same console under different conditions rather than
different pages: the dark theme, the high contrast theme, both together as a
low-vision operator would have them, the console at telephone width, and a
confirmation dialogue holding focus.

Three defects were found by taking these and fixed before the set was kept:
every form said *"add a extension"*; the action buttons in tables inherited a
margin meant for form buttons, which doubled the height of every row; and the
capture script found the "add" button by the words on it, which after the
sortable headings landed went and clicked the column headed *carrier address*
instead. The buttons are named for what they do now.

## How to take them again

    python3 tools/capture-console.py        # starts an appliance, drives it, writes captures/console
    python3 tools/capture-boot.py           # boots the image in an emulator, writes captures/boot

The boot capture wants an image; it looks in the build tree's output directory,
or wherever `CROSSBAR_IMAGE` points. Emulation without hardware assistance runs at
roughly a fifteenth of real speed, so a full boot takes about a quarter of an
hour per firmware path.
