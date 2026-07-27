# Appliance Image Architecture

Author: Agent Three, Chief Technology Officer role, third pass.
Subject: the bootable image — what is in it, how it is built, and why each
choice was made.

## What the image is

One file that boots a machine into a working telephony appliance. It carries a
Linux operating system, the telephony engine, the legacy interface card
drivers, the appliance control plane, and the manual. Nothing is downloaded on
first boot, because the machines this product targets are frequently on
networks that cannot reach anything.

It boots by two paths — the legacy boot record for older machines and the
firmware boot manager for modern ones — and carries a hybrid partition table so
that the same file can be burned to a disc or written straight to a flash
device.

## The base, and why it is not what was first intended

The blueprint called for a particular distribution. The build environment could
not reach that distribution's package mirror; the proxy refused the connection.
Rather than pretend otherwise, the build was based on the long term support
release that **was** reachable, and the base was made a build time choice so
that either can be produced on a host that can reach it:

```
BASE_DISTRIBUTION   which distribution to bootstrap
BASE_SUITE          which release of it
BASE_MIRROR         where to fetch it from
BASE_COMPONENTS     which parts of the archive to enable
```

This is recorded rather than hidden because it is the sort of substitution that
matters to whoever operates the result. A long term support release is the
correct class of base for an appliance either way: it is supported for years,
which is the timescale a telephone system lives on.

## The five build stages

Each is separately runnable, records a receipt when it finishes, and honours a
rehearsal mode that mutates nothing. A build interrupted twenty minutes into
compressing resumes rather than restarting, which matters because compressing
is the slow part.

### Stage one — the base operating system

Bootstraps a minimal root filesystem carrying only the essential set, then adds
a deliberately short list: the service manager, the device manager, the network
tools, the firewall tool, the interpreter, and the certificate store.

Two things happen here that are easy to overlook. Package installation is
configured to ask nothing and to start nothing, because there is nobody to
answer a question and nothing should run inside a filesystem that is being
built. And the base is audited: if any address allocation service arrived as a
dependency of something else, the stage fails rather than shipping it.

### Stage two — the payload

The kernel, the live boot machinery, the telephony engine, the interface card
drivers, the supplementary services, and the control plane.

**The generic kernel is chosen deliberately over a smaller one.** A virtual
machine kernel boots faster and carries far fewer drivers. An appliance meant
for unknown older hardware needs the drivers more than it needs the seconds.

**The interface card drivers are built into the image** against the kernel the
image ships, so a card works on first boot with nothing to compile. The driver
source and the kernel headers stay in the image as well, so the appliance can
rebuild them from its own console after a kernel upgrade — which, as the
competitive benchmark records, is a case the released driver archives no longer
cover.

The stage ends by verifying its own work: the engine is present, the control
plane is present, a kernel is present, and — the check worth having — the
control plane **imports cleanly using the interpreter inside the image**. That
is the only way to know before boot that the two are compatible.

### Stage three — becoming an appliance

A root filesystem containing the right software is not yet an appliance. This
stage gives it an identity, a service account, a static address, a decision
about what runs, and a boot screen.

**The static address is the interesting one.** This appliance never requests an
address and never offers one. An appliance that does neither cannot be reached
the first time unless it arrives with an address already written into it, so it
does: a documented default, printed on the boot screen in words, changed from
the appliance's own console once an operator can reach it. This is how physical
appliances have always solved the problem, and it is the only solution
consistent with the exclusion.

Every address requesting client is masked, and every allocation service unit
name is masked whether or not anything installed one — so nothing can enable
one later either.

The stage finishes by removing what must not ship: the build time restriction
on starting services, the package caches, the machine identity, and the host
keys. An image that many machines boot must not give them all the same identity
or the same keys.

### Stage four — compression

The root filesystem becomes one compressed file that the live boot machinery
mounts read only. The kernel and its initial ram filesystem are lifted out
beside it, because the bootloader must reach them before anything is mounted.

Compression is a real trade here rather than a default. On the older machines
this appliance targets, the processor that decompresses is slow — but the
optical or flash medium it reads from is slower still. The stronger compression
wins, and it is a build time setting for anyone who disagrees.

### Stage five — making it bootable

Both boot paths are installed. The legacy path gets a boot menu carrying the
appliance's address in words. The firmware path gets a bootloader with its
configuration embedded inside it, placed in a small filesystem image where the
firmware knows to look.

Both boot entries carry a serial console. A rack mounted appliance with no
monitor still has to be recoverable — and it is also what makes the build able
to verify its own image by starting it.

The image is then mastered with a hybrid boot record, and a checksum is written
beside it.

## Verification

Three layers, because each can pass while the next fails.

**The payload is verified inside the image**, before it is compressed: the
right files are present and the control plane imports with the image's own
interpreter.

**The image is verified after mastering**: it declares a boot record, and it
carries a kernel, an initial ram filesystem, and a root filesystem.

**The image is verified by being started.** An image that masters cleanly is
not an image that boots; the only way to know is to boot it. The build can run
the finished image in an emulator with a serial console, watch what comes out,
and require evidence that the kernel started and that the appliance's own
identity reached userspace — while failing on any panic or mount failure.

That last check also observes Constraint One rather than asserting it: the boot
transcript is searched for any mention of address allocation, and a mention is
a failure.

## Performance across old and new machines

The mandate asks for reliability on both legacy hardware and modern
environments. Concretely:

- **the generic kernel**, so that an unknown older chipset has a driver;
- **boot entries for awkward hardware** — one disabling mode setting, power
  management and the advanced interrupt controller, which is the combination
  that gets stubborn older machines to boot at all;
- **a verbose entry**, because an appliance that fails silently on unknown
  hardware cannot be diagnosed;
- **both boot paths**, so neither an older machine nor a modern one is excluded;
- **a serial console**, for machines with no usable display;
- **strong compression**, trading processor time against the slower medium;
- **no graphical environment at all**, because the console is a browser
  somewhere else and a desktop would cost memory the appliance should spend on
  calls.

## What the image deliberately does not contain

- any address allocation service, in any form, at any layer;
- any credential — the administrator password is generated on first boot and
  printed once, so that an image many people can download carries no secret;
- any machine identity or host key, for the same reason;
- a graphical desktop;
- a package index cache, which would only be stale.

## Building it

```bash
sudo ./iso/build-iso.sh --rehearse      # change nothing, report what would happen
sudo ./iso/build-iso.sh                 # build the image
sudo ./iso/build-iso.sh --boot-test     # build it, then start it and prove it boots
sudo ./iso/boot-test.sh                 # start an image that already exists
```

Individual stages run with `--from-stage` and `--to-stage`; a completed stage
repeats with `--force`. The finished image and its checksum are written to the
output directory inside the build tree.
