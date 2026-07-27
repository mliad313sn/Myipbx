# Competitive Benchmark

Author: Agent Two, Product Management role, second pass.
Supersedes nothing: the three usability defects in `market-benchmark.md` remain
the product thesis. This document is the evidence behind them, and the honest
account of where this appliance is behind the field as well as ahead of it.

## How to read this, and how much to trust it

Claims below carry a marker. This matters more than the comparison itself,
because a benchmark that cannot say how it knows something is marketing.

| Marker | Means |
| --- | --- |
| **[source]** | read in the project's own shipping source code, with the file named |
| **[docs]** | read in the project's official documentation or its documentation repository |
| **[index]** | a search result summary only; the page itself was never opened |
| **[unverified]** | could not be established; stated as a gap, never as a fact |

Much of the official documentation for these projects was unreachable from the
build environment. Where that happened, the research went to the projects'
public source repositories instead. For the question this benchmark actually
asks — *what can an administrator do from the web interface?* — a project's
module list and menu definitions are stronger evidence than its marketing
pages, because they are what ships.

**Pricing is deliberately almost absent.** Only one figure was read from a
primary source. Every other price found was second hand, and a wrong price in a
competitive document is worse than no price.

## The question this product is measured by

*Can an administrator run the system entirely from the web interface, or must
they open a terminal?*

Everything else in this comparison follows from that question, because it is
the one that determines who can own the system after it is installed.

## The comparison

Legend: yes · partial · no · unknown.

| Capability | This appliance | FreePBX (free) | Issabel | VitalPBX (free) | FusionPBX | Wazo | Asterisk alone |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Server static addressing from the interface | **yes** | **no** [source] | yes [source] | yes [source] | no [source] | no [docs] | no |
| Host name and time zone from the interface | **yes** | no [source] | partial [source] | yes [source] | no [source] | no | no |
| Interface card driver build from the interface | **yes** | **no** [source] | no [source] | no [index] | no | n/a | no |
| Span configuration from the interface | **yes** | **disabled by default** [source] | yes [source] | yes [source] | no | removed [docs] | no |
| Restart the telephony engine from the interface | **yes** | partial | yes | yes | **no** [source] | no | n/a |
| Restart or shut down the machine | **yes** | no [source] | yes [source] | no [source] | no [source] | no | no |
| Read logs in the interface | **yes** | yes | partial | yes | yes | no | no |
| Call history in the interface | **yes** | yes | yes | yes | yes | partial | no |
| Backup and restore in the interface | **yes** | yes | yes | yes | **no** [source] | no [docs] | no |
| Event driven live call view | **yes** | no | no [source] | unknown | yes [source] | capable, unused [docs] | n/a |
| Detects edits to generated files | **yes** | **no** [source] | no | unknown | n/a | n/a | n/a |
| Interactive menus, queues, conference rooms | **yes** | yes | yes | yes | yes | yes | n/a |
| Legacy Digium card support | **yes** | partial | yes | yes | **none** [source] | **removed** [docs] | yes |
| Runs without any paid component | **yes** | **no** [source] | yes | yes | yes | partial | yes |

## What was verified about the field, and what it means

### FreePBX — the free web interface cannot configure the operating system

Across all sixty-nine open source modules, none provides network
configuration **[source]**. The operating system level operations exist only as
hooks into the closed source System Admin module; the free modules call out to
verbs including `addnetwork`, `changenetwork`, `updateinterfaces`, and the
firewall management hooks **[source]**.

Three findings are worth stating precisely, because they are the sharpest
evidence for this product's thesis and each was read in the shipping source:

**The system update path is switched off entirely.** In the seventeenth
release, `canDoSystemUpdates()` opens with an unconditional `return false`, with
the comment that system updates are disabled for the Debian based system
**[source]**. The unreachable code beneath it requires the commercial module and
a licence with a machine identifier. Operating system updates are therefore a
terminal operation.

**The interface card configuration module ships read only.** The setting named
`DAHDIDISABLEWRITE` defaults to true, at an advanced settings level that hides
it **[source]**. Out of the box, span and channel configuration must be written
by hand until an administrator finds and flips a hidden setting. The warning
they must then accept names five files the module will overwrite and states
that it must never be used alongside the standard span configuration generator.

**Generated files are overwritten with no backup and no difference shown.** The
write is a plain `file_put_contents()` **[source]**. The header placed in every
generated file says not to edit it and points the reader at "the FreePBX.org
wiki page, or ask on IRC" — with no address given. Worse for this product's
target hardware: the interface card configuration file is copied to a backup
only the first time it is written, guarded by a check for whether the backup
already exists **[source]**. Every subsequent hand edit is destroyed with no
copy kept.

That last finding is Benchmark Defect One, in the field, in the shipping code.

**Correction to a claim this product should not make.** Research found no
evidence that the interface card configuration module has been deprecated or
removed: branches for the seventeenth and eighteenth releases both exist, the
latter updated in July of twenty twenty-six **[source]**. The accurate
characterisation is "minimally maintained and disabled by default", and that is
what this document says.

### Issabel — the real benchmark for system level coverage

Issabel, not FreePBX, is the free system that most completely covers system
administration from a browser: network parameters including address, mask,
gateway and resolvers; time configuration; shutdown; backup and restore; and a
genuine interface card configuration module that rewrites the channel
configuration, detects cards, and offers span timing, line build out, framing,
coding and four echo canceller choices **[source]**.

It is the standard this appliance had to beat, and it is beaten on three
points, all verified: package management through the interface was
**deliberately deleted** in twenty twenty-three, with the commit message
recording that it was not very usable and no longer worked **[source]**; the
distributed image is frozen at April of twenty twenty-four; and live call
monitoring is delegated to a separate commercial product, with the interface
using server sent events in one module and polling everywhere else — no
persistent socket anywhere **[source]**.

### VitalPBX — the deepest digital span interface in the field

An assumption worth correcting rather than repeating: VitalPBX's legacy
hardware support is not weak. Its own interface translation catalogue contains
four dedicated interface card modules including a hardware detection action,
line build out in the standard foot ranges, primary rate facility settings, and
multi frequency signalling with per layer logging **[source]**. Nobody ships
that signalling support without meaning it.

Its interface is closed source, so its transport could not be verified. The
widely repeated claim that the free tier is limited to a fixed number of
extensions is **[unverified]** and is contradicted by the absence of any such
limit in the licensing strings; this document does not repeat it.

### FusionPBX — correct a stale assumption, then find the real gap

FusionPBX is **not** polling based any more. It ships a complete socket server
as its own service, with four consumers including the operator panel and the
active call view **[source]**. Any comparison claiming otherwise is out of date.

The real and total gap is elsewhere: searching the whole tree for network
configuration, host name, time zone, or machine restart returns nothing
**[source]**. It cannot even restart its own telephony engine — only individual
profiles and modules **[source]**. And for this product's target hardware it
has nothing at all: no interface card support of any kind **[source]**.

### Wazo — capable transport, deliberately unavailable interface

Wazo has the best event transport in the field on paper: a socket service with
mandatory token authentication and per event authorisation evaluated at
delivery **[docs]**. No part of its own interface uses it for call monitoring.

Its administrative interface was not removed but demoted — commented out of the
default deployment, four mentions in the whole documentation tree, and a note in
its own read me that it is not always up to date with the interface behind it
**[docs]**. The vendor's ecosystem page states that the commercial products add
an additional web management console. Legacy interface hardware was dropped
outright, with explicit removal instructions **[docs]**.

### Grandstream — the appliance interface benchmark, and entirely unverified

The Grandstream unit is the standard for "everything from the interface" in an
appliance, and its analog port detection loop — a detect action that measures
line impedance and writes the result back into the trunk configuration — is the
specific behaviour this product's guided bring up is aiming at.

**Every claim about it is [index] only. Not one page was opened.** Two things
found only in summaries, if true, matter: the current unit series carries no
digital telephony at all, and no per service control was found. This document
therefore treats Grandstream as a design target, not as a measured competitor.

### Asterisk on its own

No web interface. The only markup in the tree belongs to an interface its own
documentation marks unsupported and advises against using **[source]**. The
configuration writing path is the manager interface, whose transports are
plain and secured sockets and the hypertext protocol — grepping its manager
implementation for socket upgrade support returns nothing **[source]**. The
newer interface writes no configuration files at all.

## Where this appliance is ahead

**Every operation has an interface path, including the operating system ones.**
Static addressing, host name, time zone, clock synchronisation, service
control, driver rebuild, span generation, restart and shutdown are all verbs on
a narrow privileged helper, reachable from the console. No competitor in the
free field covers driver compilation from the interface at all.

**Generated files are never silently overwritten.** Each rendered file's digest
is recorded when the appliance writes it, compared before the next write, and a
mismatch is refused and raised as an explicit choice — adopt or regenerate.
This is the direct answer to the strongest verified complaint about the market
leader.

**The interface is event driven end to end, and says when it is not.** A
persistent socket carries engine events as they happen, and a dual layer
heartbeat lets the page distinguish live from reconnecting from stale. Of the
systems examined, only FusionPBX also pushes, and none reports staleness.

**The interface and the appliance share one schema.** Forms are generated from
the same declaration the appliance validates against, so a field cannot exist
in one and not the other.

**Nothing is held back.** There is no paid tier, no licence check, no telemetry
callback, and no feature that requires registration.

## Where this appliance is behind — stated plainly

This product is a focused appliance, not a replacement for a mature
distribution. It does **not** have, and this document does not pretend
otherwise:

- paging and intercom, call parking, call recording, follow me, or call
  flow control;
- telephone provisioning for handset vendors, which the established systems do
  well and this appliance does not attempt;
- multiple tenants, or fine grained administrative roles — there is one
  administrator;
- fax handling, caller name lookup, or directory integration;
- a self service portal for end users;
- a packaged installation image; installation runs on an existing Linux system;
- a firewall or intrusion detection interface. The appliance binds one port
  and hardens its own service, but the machine's firewall is the site's to
  manage. Two competitors cover this from their interface and one requires a
  commercial component to.

What it *does* now have, and did not in its first release: extensions, trunks,
ring groups, inbound and outbound routes, time conditions, voicemail,
**interactive menus, call queues, and conference rooms** — each configured from
the interface and rendered into the engine's own configuration.

Two of the established systems still have a decade of accumulated telephony
features beyond that. The gap is narrower than it was and it is still real.
This appliance competes on the three defects it was built to overcome and on
legacy hardware bring up — not on feature count.

## An operational finding worth more than the comparison

The legacy interface card drivers were removed from the driver project in
twenty eighteen and restored after community pressure, the last of them landing
in the release numbered three point four point zero in April of twenty
twenty-four **[source]**. **No release has been tagged since.** The changes that
let the drivers compile against kernels from six point ten onward exist only in
the development tree **[source]**.

Any product that builds these drivers from the last released archive will fail
on a current kernel. This appliance therefore inspects the running kernel and
selects the development tree when it is newer than the last tagged release,
saying so in its log. That single behaviour may matter more to a real
deployment than every feature comparison above.
