# Market Benchmark — Open Source Private Branch Exchange Field

Author: Agent Two, Product Management role.
Input: Product Vision. Output: three ratified usability defects for Agent Three.

## Field surveyed

The comparison set is the established open source telephony management field:
the large web managed distributions built on top of the Asterisk engine, the
graphical configuration front ends that generate engine configuration files,
the alternative engine ecosystems aimed at carrier scale, and the bare engine
itself operated directly from its own command line console with hand written
configuration files.

Each of these is credible software with real deployments. The defects below
are not claims of incompetence; they are structural consequences of how each
category chose to solve the configuration problem, and they are the openings
this product exists to occupy.

## Defect One — the configuration source of truth is ambiguous

The dominant pattern is a web front end that owns a relational database, and
regenerates engine configuration files from that database on demand. The
engine, meanwhile, reads plain text files and can be reconfigured directly by
an administrator with an editor and a console.

The result is two sources of truth that silently diverge. An engineer fixes a
trunk at three o'clock in the morning by editing the text file, restores
service, and goes to bed. Someone presses the apply configuration button in
the browser the following week and the fix evaporates with no warning, no
difference display, and no audit record. The failure surfaces as a mysterious
regression days after the change that caused it.

Every administrator who has run one of these systems for more than a year has
this story. It is the single most cited operational complaint in the field.

**Overcome by:** a single source of truth held in one structured document on
disk. The engine configuration files are rendered artefacts, generated from
that document and marked as generated. The control plane detects when a
rendered artefact has been modified outside of itself, refuses to silently
overwrite the modification, and surfaces the divergence on the dashboard as an
explicit reconciliation decision for a human. Drift is reported, never
resolved by ambush.

## Defect Two — operational visibility is polling shaped, not event shaped

The graphical layers present call activity by asking the engine for a snapshot
on a timer, typically every several seconds, through a request and response
cycle that carries the entire state each time. Anything shorter than the
timer interval is invisible. A call that arrives and clears between two polls
never existed as far as the browser is concerned. The session silently expires
in a background tab and the operator reads a frozen screen as a quiet network.

Real time telephony diagnosis is precisely the case where sub second causality
matters. An administrator watching a trunk flap needs to see the flap, not an
average of the flap.

**Overcome by:** a persistent bidirectional socket from the browser to the
appliance, fed directly from the engine's own asynchronous event stream, so
that state transitions are pushed at the moment they occur. The socket carries
an application layer heartbeat in both directions, so a dead connection is
detected in seconds and visibly reported rather than being mistaken for
silence. The dashboard distinguishes three states without ambiguity: live and
current, reconnecting, and stale with the age of the last confirmed update
displayed in words.

## Defect Three — legacy interface hardware provisioning is an unguided manual ritual

Bringing up a legacy Digium interface card is, today, a sequence of console
incantations performed from memory or from a decade old forum thread: probe
the bus, load the correct driver module for the specific card family, generate
a span configuration, apply the span configuration, generate an engine channel
configuration, reconcile the automatically chosen channel numbering against
what the site actually has terminated on the punch down block, then restart
everything and find out.

None of the graphical layers meaningfully assist with this. They begin their
usefulness after the hardware is already working, which is exactly the phase
where the administrator does not need help. When the ritual fails, the failure
is a kernel module refusing to load with a message that assumes the reader
already knows why.

**Overcome by:** a guided hardware bring up path that is a first class feature
rather than a prerequisite. The appliance enumerates the peripheral bus,
identifies interface cards by vendor identifier, names them in plain language,
compiles and loads the interface drivers as an observable staged operation
with the failure reason rendered in plain language, presents detected spans
and channels in the browser, and lets the administrator map physical spans to
logical trunks by choosing rather than by typing. Every stage is idempotent
and every stage is reversible.

## Ratification

Exactly three defects identified, as directed. Each is structural rather than
cosmetic, each is independently verifiable against the shipped product, and
each maps to a named architectural component in the blueprint that follows.
