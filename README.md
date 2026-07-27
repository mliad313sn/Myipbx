# Legacy-to-Modern IPBX Appliance

Turns an older generation machine carrying legacy Digium interface cards into a
modern, browser managed telephony appliance. The interface card drivers are
compiled directly against the running kernel, the telephony engine is built on
top of them, and the whole system is operated from a plain browser dashboard
fed by a persistent bidirectional socket.

Two constraints govern the entire product and are enforced structurally rather
than by convention:

**This appliance assigns no addresses.** It contains no address allocation
service of any kind. It does not ship one, install one, enable one, or tolerate
one — it refuses to start if it finds one on the machine. The appliance is a
guest on someone else's network and behaves like one.

**Every numeral is spelled in full letters.** Every log line, every dashboard
readout, and every operator facing string renders numbers as words. This is not
a style guideline applied by hand; a numeral spelling module exists in the
control plane, in the browser, and in the shell scripts, all three are held
identical by an automated test, and the logging formatter routes every emitted
line through it so no code path can defeat the rule by forgetting it.

The one deliberate boundary: values that a machine reads rather than a person —
the port number in a configuration document, an address in a command you are
meant to paste, a numeric literal in source code — remain numerals, because
spelling them would stop them working. The constraint governs what the
appliance *emits*: its logs, its dashboard, and its interface responses. That
boundary is drawn on purpose and is where the automated tests draw it too.

## Everything is done from the browser

That is the product's central claim, and it is the one worth checking first,
because it is where the free field is weakest. An administrator never needs a
terminal — not for telephony, and not for the operating system underneath it.

From the console: extensions, trunks, ring groups, interactive menus, call
queues, conference rooms, inbound and outbound routes, time conditions and
voicemail; live call monitoring and call history; the
machine's static addressing, host name, time zone and clock; starting and
restarting services; a deny by default **firewall** generated from declared
rules; **compiling the legacy interface card drivers against the running
kernel**; generating span configuration; reading logs; backup and restore; and
restarting or shutting down the machine.

The one procedure that deliberately still needs a terminal is resetting a lost
administrator password, because an interface that could do that without
authentication would not be a security boundary.

How this stays true as the product grows: the console builds its forms from the
same schema the appliance validates against, and system operations are a fixed
vocabulary of verbs on a narrow privileged helper. A field cannot exist in one
place and not the other, and an operation that is not a verb is not an
operation.

## What is here

```
appliance/     the control plane -- standard library only, no dependencies
web/           the console -- vanilla markup and scripting, no build step
iso/           the bootable image build: five stages and a boot test
scripts/       the staging scripts, the exclusion audit, the privileged helper
config/        engine, service, network, privilege, and appliance templates
tests/         the quality assurance suite -- three hundred forty-two tests
docs/          vision, benchmarks, architecture, user guide, runbook, assurance
```

The control plane imports nothing outside the Python standard library. The
socket protocol, the request server, the engine manager client, and the
scheduler are all implemented directly, because the target machines are old,
frequently air gapped, and often have no working package index for their
vintage. A dependency tree is a liability on this hardware.

## The bootable image

The product ships as one file that boots a machine straight into a working
appliance: a Linux operating system, the telephony engine, the legacy interface
card drivers already compiled against the kernel it carries, the control plane,
the console, and the manual. Nothing is downloaded on first boot, because these
machines are frequently on networks that can reach nothing.

```bash
sudo make image              # build it
sudo make image-boot-test    # start the finished image and prove it boots
sudo make image-rehearse     # report what the build would do, changing nothing
```

It boots by both paths — the legacy boot record for older machines and the
firmware boot manager for modern ones — and carries a hybrid partition table,
so the same file can be burned to a disc or written straight to a flash device.

Because the appliance never requests an address and never offers one, it has to
arrive already reachable. It does: a documented static address, printed on the
boot screen in words, changed from the appliance's own console once you can
reach it. That is how physical appliances have always solved this, and it is
the only answer consistent with the exclusion.

The build has five stages, each separately runnable and each recording a
receipt, so a build interrupted during the slow compression resumes rather than
restarting. Full detail is in
[`docs/image-architecture.md`](docs/image-architecture.md).

## Installing onto an existing system

The image is the usual route. This path is for adding the appliance to a Linux
system that already exists.

The installer needs administrative privilege and the source archives for the
interface drivers and the telephony engine. Because these appliances are
frequently air gapped, the archives are expected to be present locally; a
download address may be supplied instead when the machine has a route.

```bash
# see what the installer would do, changing nothing
sudo ./scripts/install-appliance.sh --rehearse

# prove the machine assigns no addresses before anything else
sudo ./scripts/verify-no-dhcp.sh

# install, supplying the site's static addressing
sudo APPLIANCE_INTERFACE=eth0 \
     APPLIANCE_ADDRESS=192.0.2.20 \
     APPLIANCE_GATEWAY=192.0.2.1 \
     APPLIANCE_SOURCE_DIR=/usr/local/src/myipbx \
     ./scripts/install-appliance.sh
```

The installation runs in five ordered stages. Every stage is idempotent,
records a receipt when it completes, and honours rehearsal mode, so a failed
installation is resumed by running the command again rather than restarted.

| Stage | What it does |
| --- | --- |
| one | build toolchain, kernel headers, service account, directory tree |
| two | static network configuration and the address allocation audit |
| three | interface card drivers, compiled against the running kernel |
| four | the telephony engine |
| five | the control plane, the dashboard, and the service unit |

Individual stages can be run alone with `--from-stage` and `--to-stage`, and a
completed stage can be forced to repeat with `--force`.

The installer prints the initial administrator password exactly once, on the
console, and never writes it to a log. There are no default credentials.

## Operating

Open the dashboard in a browser on the address and port the installer reported.
The dashboard shows live call activity, trunk registration state, detected
interface cards and spans, configuration reconciliation, automated tasks, and
the enforcement state of both constraints.

The link indicator in the header distinguishes three conditions that a polling
dashboard cannot tell apart: **live**, **reconnecting**, and **stale** with the
age of the last confirmed update. A frozen screen is never allowed to look like
a quiet night.

Full operating procedures are in [`docs/operations-runbook.md`](docs/operations-runbook.md).

## The three usability defects this product exists to overcome

Identified by benchmarking the open source field. Detailed in
[`docs/market-benchmark.md`](docs/market-benchmark.md).

**One — an ambiguous configuration source of truth.** The common pattern is a
web front end owning a database that regenerates engine files, while the engine
still reads those files directly. The two diverge silently, and an emergency
edit made at three o'clock in the morning evaporates the next time somebody
presses apply.

Here, one structured document is the source of truth and every engine file is a
rendered artefact of it. Before rendering, the appliance compares each file's
digest against what it last wrote. A mismatch means a human edited a generated
file, so the appliance refuses to overwrite and raises the divergence on the
dashboard as an explicit choice: adopt the file on disk, or regenerate over it.
Drift is reported, never resolved by ambush.

**Two — polling shaped visibility.** Graphical layers ask the engine for a
snapshot on a timer, so anything shorter than the interval is invisible and a
dead session reads as silence.

Here, a persistent bidirectional socket is fed directly from the engine's own
event stream, so state transitions are pushed as they occur. A dual layer
heartbeat — a protocol ping plus an application heartbeat carrying the state
sequence and the age of the last engine event — is what makes stale
distinguishable from quiet.

**Three — unguided legacy hardware provisioning.** Bringing up an interface card
is a sequence of console incantations recalled from a decade old forum thread,
and the graphical layers only become useful after the hardware already works.

Here, guided bring up is a first class feature: the peripheral bus is
enumerated, cards are named in plain language, drivers are compiled and loaded
as an observable staged operation with plain language failure reasons, and
detected spans are mapped to logical trunks by choosing rather than typing.

## Development

```bash
make help              # every target
make test              # the complete suite
make check             # syntax gates for shell, control plane, and dashboard
make test-constraints  # only the two absolute constraint suites
make test-concurrency  # only the one hundred concurrent session scenarios
make audit             # the address allocation audit against this machine
make lint              # static analysis of the console sources
make test-browser      # drive the real console in a real browser
make run               # run the control plane in the foreground
```

The suite needs nothing beyond the standard library interpreter. Where a
browser scripting runtime is present it does two things more: it proves the
console's numeral implementation agrees with the control plane's, and it drives
the real console in a real browser against a real appliance — signing in,
walking every section, creating and deleting an extension through the generated
forms, and watching a pushed update arrive. Without that runtime those tests
report themselves skipped and everything else runs normally.

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/product-vision.md`](docs/product-vision.md) | what the product is and why the constraints exist |
| [`docs/image-architecture.md`](docs/image-architecture.md) | what is in the bootable image and how it is built |
| [`docs/market-benchmark.md`](docs/market-benchmark.md) | the field survey and the three defects to overcome |
| [`docs/system-architecture.md`](docs/system-architecture.md) | the three layers and how each constraint is enforced |
| [`docs/operations-runbook.md`](docs/operations-runbook.md) | installing, operating, diagnosing, and recovering |
| [`docs/competitive-benchmark.md`](docs/competitive-benchmark.md) | measured against the free field, with sources and honest gaps |
| [`docs/user-guide.md`](docs/user-guide.md) | how to run the appliance, task by task |
| [`docs/quality-assurance-report.md`](docs/quality-assurance-report.md) | what was tested, what failed, and what was repaired |
