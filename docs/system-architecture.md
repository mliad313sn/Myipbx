# System Architecture Blueprint

Author: Agent Three, Chief Technology Officer role.
Input: Product Vision, Market Benchmark. Output: build contract for Agent Four.

## Governing decisions

**Standard library only on the server side.** The control plane imports
nothing that is not part of the Python standard library. The target machines
are older, frequently air gapped, and often have no working package index for
their vintage. A dependency tree is a liability on this hardware, so the
bidirectional socket protocol, the HyperText Transfer Protocol server, the
engine manager client, and the scheduler are all implemented directly.

**Single asynchronous event loop, no threads in the data path.** All network
input and output is cooperative and non blocking. A blocked event loop on a
telephony control plane means a missed engine event, so anything that can
block — subprocess execution, filesystem synchronisation, driver compilation —
is dispatched to a bounded worker pool and awaited, never called inline.

**One listening port, protocol dispatched.** The appliance listens once and
inspects the request line and headers. A request carrying a socket upgrade
header is promoted to the persistent bidirectional protocol; everything else
is served as HyperText Transfer Protocol. One port to firewall, one port to
document, no cross origin problem between the dashboard and its own socket.

**Every layer fails closed.** Absent hardware yields an empty inventory rather
than an exception. An unreachable engine yields a disconnected trunk state
rather than a crash. A failed authentication yields a constant time rejection.
A detected address allocation daemon yields a refusal to start.

## Layer One — Linux root filesystem layer

Provisioned by five ordered, individually re-runnable staging scripts driven
by one orchestrator, all sharing a common function library.

- Stage one prepares the base system: build toolchain, kernel headers matched
  to the running kernel, service account, directory tree, and file permissions.
- Stage two applies the static network configuration and performs the address
  allocation audit described below.
- Stage three compiles and installs the legacy interface card kernel drivers
  against the running kernel and persists the module load configuration.
- Stage four builds and installs the telephony engine, its manager interface,
  and the generated channel configuration.
- Stage five installs the control plane, its service unit, and its credentials.

Every stage is idempotent: it detects completed work and reports it as already
satisfied rather than repeating it. Every stage writes a receipt file into the
appliance state directory so that a re-run is auditable, and every stage can
be run in a rehearsal mode that performs no mutation.

## Layer Two — legacy Digium hardware bridging layer

Read only enumeration is separated from mutating provisioning.

Enumeration reads the peripheral bus inventory and matches the Digium vendor
identifier, then reads the driver's own exported device tree for span and
channel state, translating identifiers into plain language card family names.
On a machine with no such hardware, enumeration returns an empty inventory and
reports the absence as a normal condition — this is what allows the entire
control plane to be developed and tested off the target hardware.

Provisioning is the guided bring up: compile drivers, load modules, generate
span configuration, apply it, and map spans to logical trunks. Each step is an
addressable operation with its own state, its own log, and its own plain
language failure reason. This is the answer to Benchmark Defect Three.

## Layer Three — control plane and interface layer

### Engine manager client

An asynchronous client for the telephony engine's manager interface. It
maintains a single long lived connection, authenticates, and consumes the
event stream. Outgoing actions are correlated to responses by a generated
action identifier and awaited as futures with timeouts, so a slow engine
response never stalls the loop. Loss of connection triggers reconnection with
exponential backoff and jitter, and the trunk state is marked unknown rather
than being left stale.

### Asynchronous non blocking trunk registration layer

Trunk registration is modelled as an explicit state machine per trunk —
unconfigured, registering, registered, retrying, failed, disabled — advanced
only by engine events and timers. Registrations proceed concurrently, never
sequentially, so one dead carrier cannot delay the registration of a healthy
one. Retries use exponential backoff with jitter and a ceiling, and every
transition is timestamped and published to the socket.

### Configuration store and drift detection

One structured document on disk is the single source of truth. Engine
configuration files are rendered from it, each carrying a generation header
and a content digest recorded in the state directory. Before any render, the
store recomputes the digest of what is currently on disk; a mismatch means a
human edited a generated artefact, and the store refuses to overwrite,
raising the divergence to the dashboard as an explicit reconciliation choice —
adopt the on disk version or regenerate over it. Writes are atomic: render to
a temporary file in the same directory, synchronise, then rename. This is the
answer to Benchmark Defect One.

### Persistent bidirectional socket

A direct implementation of the standard socket upgrade handshake: the client
supplied key concatenated with the protocol's fixed identifier, hashed and
base sixty four encoded into the accept header. Framing supports text,
binary, close, ping, and pong, with mandatory client to server unmasking,
continuation frame reassembly, and a maximum message size that is enforced by
closing the connection rather than by buffering without limit.

The heartbeat is dual layer. The protocol level ping is emitted by the server
on a fixed interval and a peer that misses the configured number of
consecutive replies is closed. Above that sits an application level heartbeat
carrying the appliance's monotonic sequence number and the age of its last
engine event, which is what lets the dashboard distinguish live from
reconnecting from stale. This is the answer to Benchmark Defect Two.

### Representational state transfer interface

A compact route table over the same port. Read routes cover system health,
hardware inventory, trunk state, channel state, configuration, tasks, and
logs. Write routes cover authentication, configuration mutation, trunk
control, task invocation, and reconciliation decisions. Every write route
requires an authenticated session and a matching origin.

### Automated task execution

A scheduler owning a registry of named tasks — configuration render, engine
reload, hardware rescan, backup, log rotation, health sweep. Tasks are
executed by the bounded worker pool, are single flight per name so a slow task
cannot be stacked on itself, carry a timeout, and publish start, progress, and
completion to the socket. Tasks may be invoked on a schedule or on demand from
the dashboard.

### Security posture

No default credentials: the installer generates the initial administrator
password and prints it once. Passwords are stored as salted key derivations
with a high iteration count and verified in constant time. Sessions are opaque
random tokens with an idle expiry, delivered as strict same site cookies
marked as unavailable to browser scripting and as unavailable over plain
transport. Authentication attempts are rate limited per source address with a
lockout. The socket upgrade validates both the session and the origin header
before completing the handshake.

### Transport security

The console carries the administrator's password on the way in and the session
cookie on every request afterwards, and authorises operations that reach as far
as rebooting the machine and recompiling kernel modules. None of that may cross
a site network in the clear, so the listener is secured and the appliance
refuses to serve without a certificate rather than falling back to plain
transport. A fallback would put the password on the wire on exactly the
machines where somebody had got the installation half right, and nobody would
find out until it mattered.

The listener is built from the standard library's own transport security
module, because these appliances are old and air gapped and cannot be asked to
acquire a package in order to be safe. The negotiated version floor is pinned
and the cipher selection is left to the library; naming ciphers here would
freeze this appliance's idea of which ones are sound at the moment it shipped,
and it is not updated often.

Two ports are bound. The secured one serves the console. The plain one serves
nothing at all: it answers every request with a redirect to the secured port,
issues no cookie and returns no body, so that an operator who types the
appliance's address without a scheme is sent to the right place rather than
left at a refused connection. The generated firewall ruleset opens both, ahead
of any declared rule, for the same reason it already opened the console: an
administrator must not be able to write a ruleset that removes their own way
back in.

**No certificate travels in the image.** An image is one file that many
machines boot, so a certificate inside it would give every appliance built from
it the same private key, and anybody holding the image could then read the
console traffic of every site running it — a worse position than the plain
transport it replaced, because it would look secured. Each appliance therefore
generates its own on first start: the installer runs the generator before the
service starts, and an image booted appliance runs the same generator from a
oneshot unit ordered before the control plane. The unit waits on nothing that
is not up early, because a unit that fails on every boot is worse than the
problem it solves, and this repository has shipped one of those before.

The certificate is self signed, so a browser will warn on a first connection.
The only way an operator can tell that warning apart from an interception is to
have seen the fingerprint somewhere the network was not involved, so the
fingerprint is printed on the appliance's own console beside the initial
administrator password. It is deliberately printed rather than logged, and
deliberately not rendered on the dashboard: Constraint Two spells every numeral
reaching an operator into words, and a spelled digest could not be compared
character by character against what a browser displays, which is the only thing
a fingerprint is for.

A site with its own certificate authority can install its own pair from the
console without reaching for a terminal. The pair is validated together — by
actually loading it, because that is the only check that answers whether the
listener will come up on it — before either file is stored. Installing it is a
second, separate step, because it restarts the console and ends every session
including the one that uploaded it, and the interface says so before it happens.
The control plane runs unprivileged and cannot write into the configuration
directory, so it stages the validated material in its own state directory and
the privileged helper installs it, which is the same shape the firewall already
uses.

### How the control plane reaches privilege

The product's central claim is that every operation can be performed from the
browser, and several of those operations need privilege the control plane must
not hold: restarting the telephony engine, applying a static network
configuration, recompiling the interface card drivers, restarting the machine.

The control plane runs as an unprivileged account under a unit that sets
`NoNewPrivileges`. It reaches privilege by asking a small daemon that already
holds it, over a Unix domain socket at `/run/crossbar/helper.sock`, mode `0660`,
owned `root:crossbar`. The exchange is one length-prefixed JSON request and one
reply. The daemon establishes who is calling by asking the kernel for the peer
credentials of the connection rather than by believing anything the request
says, and refuses any caller that is not the appliance's own service account
before it parses a single byte of what was sent.

It then accepts only a verb from the same fixed vocabulary of sixteen the
control plane knows, with every argument checked against the same patterns, and
passes the vector to the helper script as an argument list. There is no shell
anywhere on that path, so a value that came from a browser cannot become a
command. The validation happens three times — in the control plane, in the
daemon, and again in the helper script — and the repetition is deliberate: the
control plane's copy protects the operator from mistakes, the daemon's copy
protects the machine from the control plane, and the script's copy protects the
machine from anything that invokes the script directly.

**Why a socket rather than a setuid escalator.** The obvious design is to grant
the service account the right to run one helper script under `sudo`, and this
product shipped that design first. It could never have worked.
`NoNewPrivileges` sets the kernel's `no_new_privs` flag, which permanently
disables the setuid mechanism for the process and everything it spawns, and
under that flag `sudo` refuses to run at all — it reports that the no-new-
privileges flag is set and exits. Every privileged operation would have failed
on every real installation, and would have failed at the moment an operator
asked for one rather than at start up where somebody would have noticed. The
test suite did not catch it because the process runner was replaced in every
test of that path.

The service account now holds no privilege grant of any kind. There is no
`sudoers` file in this repository, the image build refuses to produce an image
containing one, and `tests/test_privileged_path.py` exercises the real daemon
over a real socket with nothing on the path replaced.

## Constraint One — structural enforcement of the address allocation exclusion

The exclusion is enforced at four independent points, so that no single
oversight can defeat it.

1. **Build time.** No staging script installs, enables, or configures any
   address allocation daemon. Network configuration templates are static
   address only and contain no allocation ranges, no lease directives, and no
   allocation pools.
2. **Install time.** The audit script scans installed packages, service units,
   and listening sockets for any address allocation server, and fails the
   installation if one is present and enabled.
3. **Run time.** The control plane repeats the audit during startup and on
   every health sweep. A detected allocation server listening on the machine
   is a fatal startup condition and, if it appears while running, raises a
   critical alarm on the dashboard.
4. **Repository time.** An automated test walks the entire source tree and
   fails the build if an address allocation server configuration directive
   appears anywhere outside of the exclusion machinery itself.

The appliance is a client of the network's addressing, never a source of it.

## Constraint Two — structural enforcement of spelled numerals

A numeral spelling module exists on both the server and the browser side,
producing identical output for identical input. The logging formatter routes
the entire formatted line through the sanitiser, so no code path can emit a
digit character into a log. The dashboard renders every numeric readout
through the browser side module. An automated test asserts the two
implementations agree across the full range exercised by the product and that
no operator facing surface emits a digit character.

### What is spelled, and what is not

A quantity is a number an operator reads; an identifier is a number an operator
uses. Quantities are spelled — twelve active calls, three minutes. Identifiers
keep their digits: addresses, ports, versions, device and interface names, card
models, telephone numbers, timestamps, protocol response codes and filesystem
paths.

The rule replaced an earlier one that spelled everything. That rule was
enforced perfectly and was wrong in a way only visible in use: the appliance
printed its own address in words, so nobody could type it; it told a technician
to configure an interface named "ethzero" when the interface is called eth0;
and it filled call history with "two hundred one" where the operator was
searching for 201. A constraint that makes the product unusable is not being
upheld by being obeyed.

The identifier shapes are listed in `appliance/numerals.py`, `web/js/numerals.js`
and `scripts/lib/common.sh`, and a test holds the three identical. The exemption
is narrow by construction: each identifier is lifted out, everything left is
spelled, and the identifier is put back, so only the identifier survives and
never the quantity beside it. The unconditional form remains available and is
still tested, which is what makes the exemption reversible.

A second-order defect appeared the moment identifiers kept their digits, and is
worth recording because it is the shape of the next one: call history search ran
across the whole record, so a search for a three digit extension beginning "202"
matched the year in every timestamp and returned every call ever made. The
search now looks only in the fields somebody would search.

### Where the spelling happens, and why it is not everywhere

The interface serves two audiences with one set of routes, and this is the rule
that keeps them from fighting:

- **A quantity the console does arithmetic on, compares, or thresholds is sent
  as a number**, and the browser spells it at the moment it is drawn. Call
  counts, span counts, queue depths, sequence numbers and uptimes are all of
  this kind. Sending these as words would mean the console could not tell
  whether a figure had risen.
- **A quantity that is only ever read is sent already spelled**, because
  nothing downstream needs it as a number and spelling it once at the source is
  one fewer place to forget.

Both kinds exist, deliberately, and the operator sees words either way — the
difference is only which side of the connection turned the number into them.

The hazard is that the two look identical in a payload, so a field quietly
changing kind would break its own readout and nothing else. A field sent as a
number and drawn without spelling puts a digit on screen; a field sent as words
and passed through the browser's spelling function renders as nonsense. Neither
would fail a test that only checked the route answered.

`tests/test_numeral_conventions.py` therefore pins the kind of every numeric
field the interface serves. A field that changes kind fails that test by name,
and the fix is either to change it back or to change the console with it.

## Build contract for Agent Four

Deliver: the five staging scripts and orchestrator with a shared library; the
control plane package implementing every component above; the browser
dashboard; the engine and service configuration templates; and a test suite
that exercises the socket protocol, the manager client, the trunk state
machine, the drift detection, both constraint enforcements, and a concurrency
scenario of no fewer than one hundred simultaneous active sessions.
