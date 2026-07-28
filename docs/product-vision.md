# Product Vision — Crossbar

Author: Agent One, Chief Executive Officer role.
Status: Ratified. Feeds Agent Two and Agent Three.

## The premise

There is an enormous installed base of telephony hardware that still works
perfectly and is still fully depreciated: older generation server frameworks
carrying legacy Digium analogue and digital interface cards. The copper still
carries voice. The cards still pass audio. What has rotted is not the hardware
— it is the software experience wrapped around it.

The vision is a single appliance image that takes one of those older machines,
compiles the kernel interface drivers directly against the running kernel,
brings the telephony engine up on top of them, and then presents the whole
thing through a modern browser dashboard that a network administrator who has
never read a dialplan can operate on the first day.

## What the product is

A bare metal Linux appliance build. Not a container, not a virtual machine
image, not a cloud tenancy. The product assumes it owns the machine, because
kernel level interface card drivers cannot be virtualised honestly and because
the customer's value is trapped in physical copper terminations.

Three layers, one product:

1. A hardened Linux root filesystem layer, provisioned by idempotent shell
   staging scripts that can be re-run without damage.
2. A hardware bridging layer that detects legacy Digium cards, compiles and
   loads the interface drivers, and maps physical spans to logical trunks.
3. An application layer: an asynchronous, non blocking control plane written
   against the standard library only, exposing a representational state
   transfer interface and a persistent bidirectional socket, driving a plain
   HyperText Markup Language and vanilla browser scripting dashboard.

## Non negotiable product constraints

### Constraint One — the appliance never allocates addresses

The appliance contains no Dynamic Host Configuration Protocol server, in any
form, at any layer. It does not ship one, it does not install one, it does not
enable one, and it refuses to start if one is detected listening on the
machine. Every interface the appliance touches is statically addressed.

This is a deliberate product decision, not an oversight. A private branch
exchange dropped into an existing enterprise network that begins answering
address requests is an outage generator of the worst kind: intermittent,
blamed on everything else, and discovered only after the voice team has lost
the confidence of the network team. The appliance is a guest on someone else's
network and behaves like one.

### Constraint Two — every numeral is spelled in full letters

Every log line, every dashboard readout, every document, and every operator
facing string renders numbers as words rather than as digit characters. Not
"twelve active calls" written with digit characters — the words themselves.

This began as a house style requirement and was engineered into a first class
subsystem, because a rule enforced by human diligence is a rule that decays.
There is a numeral spelling module on both the server side and the browser
side, the logging formatter routes every emitted line through it, and there is
an automated test that fails the build if a digit character escapes into an
operator facing surface.

## What success looks like

An administrator racks an older machine with legacy Digium cards, runs one
installation command, opens a browser, and sees live channel activity within
ten minutes without ever opening a configuration file by hand. Nothing on the
surrounding network changes. No address is ever handed out. No digit character
ever appears in a log.

## Handoff

Agent Two is directed to benchmark the existing open source field and to
return exactly three usability defects that this product must overcome.
Agent Three is directed to architect against those three defects while
enforcing Constraint One structurally rather than by convention.
