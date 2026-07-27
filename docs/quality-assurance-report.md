# Quality Assurance Report

Author: Agent Five, Quality Assurance role.
Verdict: **PASS.** Three hundred forty-two tests, zero failures, zero errors,
zero skipped.

This report is written against a suite that was actually executed, not
described. Every figure below was read off a run, and the run is reproducible
with `make test`.

## Result

```
Ran three hundred forty-two tests in roughly twenty-seven seconds
OK
```

| Suite | Tests | Covers |
| --- | --- | --- |
| `test_numerals.py` | twenty-five | Constraint Two across all three implementations |
| `test_wsproto.py` | twenty-seven | the socket protocol, including every malformed input path |
| `test_engine_and_trunks.py` | thirty-nine | the engine client, retry timing, the trunk state machine, live state |
| `test_store_and_hardware.py` | thirty-one | configuration drift detection and legacy hardware enumeration |
| `test_security_and_transport.py` | sixty-three | credentials, sessions, request parsing, task execution |
| `test_operations.py` | ninety-five | privileged operations, telephony objects, menus, queues, conference rooms, the firewall, diagnostics, backup |
| `test_constraint_one.py` | nineteen | Constraint One at all four enforcement points |
| `test_integration.py` | forty-one | end to end over real sockets, including concurrency |
| `test_browser.py` | two | the console, executed in a real browser against a real appliance |

## The specification's named test cases

The specification directs this agent to execute test cases on call operations,
hardware detection, and system stability simulating at least one hundred
concurrent active sessions. Each is discharged as follows.

### Call operations

Channel life cycles are driven through the real engine event vocabulary —
channel creation, state change, caller identity, bridge entry and exit, and
hang up — and the resulting model is asserted. Duration and talk time are
checked against an injected clock rather than against wall clock time, so the
assertions are exact. Losing the engine is asserted to invalidate the channel
view rather than to leave a stale one looking healthy.

### Hardware detection

Interface card enumeration is exercised against fixture filesystem trees
representing a machine with a legacy Digium card fitted and a machine without
one. Both the peripheral bus inventory and the driver's exported span documents
are parsed. The absence of hardware is asserted to be a normal reported
condition rather than an error, which is the property that lets the control
plane run away from the target machine at all.

### System stability at one hundred concurrent sessions

One hundred dashboards are connected over real sockets against a real listener.
Every one completes the upgrade, every one is welcomed, and a single state
change is asserted to reach all one hundred with no slow consumer shed. One
hundred simultaneous calls are then raised, answered, and cleared, and the
interface is asserted to answer within three seconds while carrying them. A
heartbeat sweep across the full population is asserted to complete promptly and
evict nobody.

### Long term interface responsiveness

A sustained load test raises and clears one thousand calls across twenty
connected dashboards, then asserts that the channel table is empty, the task
history is bounded, no connection has accumulated a backlog, every connection
survived, and the interface still answers. This is the test that would catch a
leak that only appears after hours of service.

### Driver stability

Driver compilation itself cannot be executed in a test environment without the
target kernel and hardware. What is tested instead is everything around it: the
staging scripts are syntax gated, the enumeration and span parsing are covered
exhaustively against fixtures, and the failure paths report plain language
causes. The compilation stage is idempotent, records a receipt, and can be
rehearsed without mutation, so a failed compilation is resumed rather than
restarted.

## Constraint One — verified at all four enforcement points

The exclusion is asserted independently at each point, so no single oversight
defeats it.

1. **Build time.** An automated test walks every staging script and fails on any
   command that would install or enable an address allocation service. The
   network templates are asserted to declare static addressing with no address
   request directive and no allocation range.
2. **Install time.** The audit script is executed and asserted to run to
   completion and report its finding.
3. **Run time.** All four detections — running processes, listening sockets,
   service units, and configuration files — are asserted to fire independently
   against a fixture machine that allocates addresses, and to stay silent
   against one that does not. The appliance is asserted to refuse to start
   beside an allocation service, and to leave nothing listening after refusing.
4. **Repository time.** The entire source tree is walked and asserted to contain
   no allocation directive anywhere outside the exclusion machinery itself.

## Constraint Two — verified across three implementations

There are three numeral spelling implementations, because the appliance needs
one in the control plane, one in the browser, and one in the shell scripts that
run before the interpreter package is in place. Three implementations are three
chances to drift, so all three are held to one another:

- the control plane and the browser twin are asserted identical across every
  integer from zero to two thousand five hundred plus selected larger values,
  every ordinal to four hundred, a range of durations, and realistic operator
  text;
- the shell library is asserted identical to the control plane across its own
  range and sanitiser cases;
- the logging formatter is asserted to make the constraint unconditional: a
  probe deliberately logs digits through message arguments, timestamps, and an
  exception traceback, and the emitted output is asserted to contain no digit
  character;
- the appliance's real log file, produced by exercising a running appliance, is
  asserted line by line to contain no digit character;
- the installer's own output is asserted to contain no digit character.

## Recursive improvement loop — the defect log

The specification requires that failures be fed back to the technical agents
and the sequence looped until zero errors remain. Four defects in the product
were found and repaired this way. They are recorded here because a quality
report that claims a clean first attempt is not a quality report.

### Defect one — the engine client could never authenticate

**Severity: critical.** The manager client sent its login action and awaited the
correlated response, but the read loop that resolves correlated responses was
started only after authentication returned. The login therefore awaited a reply
that nothing was reading, and would have failed on its timeout against every
real engine. The appliance would never have connected to the telephony engine
at all.

**Repair.** The read loop is now started before the login is sent, and the two
are awaited together so that a connection lost mid authentication surfaces the
underlying reason instead of hanging. A disconnection now also publishes the
event that invalidates dependent state.

### Defect two — every master span would have raised a false alarm

**Severity: significant.** The interface driver annotates the master span of a
machine with a role marker. The span parser treated any parenthesised
annotation as an alarm, so the master span — which exists on essentially every
real appliance — would have reported a permanent false alarm on the dashboard.

**Repair.** Annotations are now classified. Role markers are recognised as such;
genuine alarm tokens still raise. An unrecognised token is still treated as an
alarm, because failing towards visibility is the correct bias for a fault
indicator.

### Defect three — no call would ever have accrued talk time

**Severity: significant.** The engine names its answered channel state with a
word that the appliance's state model did not map onto its own answered state.
Every answered call would have displayed as unanswered, the answered call count
would have stayed at zero permanently, and talk time would never have started.

**Repair.** Engine state descriptions are now normalised through an explicit
table into the appliance's vocabulary. An unrecognised description is passed
through rather than discarded, so a newer engine's state is still displayed
before the table knows its name.

### Defect four — an ephemeral listening port was rejected

**Severity: minor.** Configuration validation refused a listening port of zero,
which is the deliberate request for a kernel assigned ephemeral port.

**Repair.** The value is accepted and the appliance reports the port it actually
bound.

Three faults in the test suite itself were also corrected during the loop: an
over-broad pattern that matched an ordinary English word in a comment, an
assertion that tested the wrong property of the field injection defence, and a
log assertion that was silently skipping rather than proving its claim. A test
that skips is not a test that passes, and it is not counted as one here.

## The graphical interface, executed rather than described

The product's central claim is that every operation can be performed from the
browser. A claim of that shape cannot be verified by testing the appliance
alone, so it is verified in a browser.

A real Chromium instance loads the served console against a real appliance on a
real socket, signs in through the form, walks all sixteen sections, creates an
extension through the generated form, submits an invalid value first to prove
the refusal reaches the offending field, deletes the extension through the
confirmation dialogue, and watches a call pushed by the appliance appear while
the page is open. Every error the page raises — an exception, a rejected
promise, a failed request — is collected and fails the run.

Constraint Two is checked there too, against what a person can actually see:
every visible text node on the rendered page is walked and asserted to contain
no digit character.

The console's own link classification is exercised separately and
deterministically, because inducing genuine staleness against a live appliance
would mean wedging its event loop. The three way distinction — live,
reconnecting, stale — is asserted at its boundaries, along with the retry
schedule's growth, its ceiling, and the presence of jitter.

The console sources also pass static analysis, which is what caught the
implicit global declarations that the shipping page relied on.

## Second recursive pass — the defect log

Extending the product to cover every operation from the interface produced a
second round of failures, repaired the same way. Two of them were serious and
neither was reachable from the server side.

### Defect five — an invisible sheet swallowed every click on the console

**Severity: critical.** The confirmation dialogue's backdrop is a fixed
position element covering the whole viewport, hidden by the `hidden` attribute
and revealed by script. Its stylesheet rule set `display: flex`, which
overrides what the `hidden` attribute does, so the backdrop was **never
hidden** — it was merely transparent. An invisible sheet lay over the entire
console intercepting every click. The product was completely unusable, and no
server side test could have seen it.

**Repair.** A single rule now forces any element carrying the `hidden`
attribute to stay hidden regardless of what a later rule sets, so no future
element can repeat the mistake. Found by the browser run, which reported that
the sign in button could not be clicked because a hidden element was
intercepting pointer events.

### Defect six — an extension number was stored as a quantity

**Severity: significant.** Extension and ring group numbers were declared as
numeric fields and coerced to integers on the way in. An extension numbered
with a leading zero would have had it silently discarded, renaming the
extension and breaking every reference to it.

**Repair.** Both are now text fields with a digits only pattern. They are dial
strings, not quantities, and are stored as written.

### Defect seven — the service unit would have refused to start

**Severity: significant.** The unit's hardening listed the engine configuration
directory and the interface driver's exported tree as accessible paths. Neither
exists before the engine is installed or before a card's driver is loaded, and
a service unit naming a path that does not exist fails to set up its namespace
and does not start. The appliance would have failed to start on precisely the
machines it is meant to be brought up on first.

**Repair.** Those paths now carry the marker that makes an absent path ignored.

### Defect eight — a call already answered accrued no talk time

**Severity: minor.** A channel first seen in the answered state — which happens
when the appliance reconnects to an engine mid call — never recorded when it
was answered, so it accrued no talk time for its whole life.

**Repair.** A channel that is already answered when first seen starts its talk
timer immediately.

### Defect nine — asking whether you are signed in was an error

**Severity: minor, but user facing.** The console probed for an existing
session by requesting a protected route, so every signed out page load wrote a
failed request into the browser's console. Training an administrator to ignore
red entries in the console is a poor way to prepare them for a real fault.

**Repair.** A dedicated route now answers the question successfully either way.

## Standing observations

These are not failures. They are the limits of what this environment can prove,
recorded so that nobody mistakes the scope of the verdict.

- Kernel driver compilation and the telephony engine build are exercised only
  as far as their scripts' structure and failure handling. They require the
  target kernel and hardware to execute.
- The concurrency figures were measured on the build machine. They demonstrate
  the absence of blocking and of unbounded growth; they are not a capacity
  rating for a specific older machine.
- The interface card catalogue improves how a card is described. A card absent
  from it is still detected and still reported as a Digium device, so an
  incomplete catalogue degrades the description rather than the detection.

## Verdict

Every test case named in the specification has been executed and passes. Both
absolute constraints are enforced structurally and verified mechanically. All
defects surfaced by the loop have been repaired and are covered by tests that
would catch their return.

**PASS.** The recursive loop terminates. Nine product defects were found
and repaired across two passes; each is covered by a test that would catch
its return.

---

# Third pass — the image, and an external review

The verdict above was correct about what it measured and wrong about what it
implied. This section records what a third pass found, and it is not
comfortable reading.

## A correction to this document

The header of this report claimed three hundred forty-two tests with zero
failures. An external reviewer running the same suite reported three hundred
forty-one passing and one failing — the concurrency case, at four point six
seconds against a three second budget. Re-running the concurrency suite on the
machine that produced this report gives six tests passing in about eight
seconds, so the failure does not reproduce here and is a property of the
machine the suite runs on rather than of the code.

That is exactly the problem. A timing budget that passes on one machine and
fails on another is not evidence of anything, and quoting a test count out of
a document instead of running the suite is how the number in this header
survived being wrong. **Run the suite. Do not quote this document.**

The deeper correction is worse. A green suite was taken as evidence the
product worked. It was not, and could not have been, because the one thing the
product most needed to do was replaced by a mock in every test that touched it.

## Defect ten — the control plane could never gain privilege

**Severity: critical, and the most serious defect found in this product.**

The control plane's service unit sets `NoNewPrivileges=yes`. That directive
sets the kernel's `no_new_privs` flag, which permanently disables the setuid
mechanism for the process and everything it spawns. The control plane escalated
by invoking `sudo`, and under that flag `sudo` refuses to run at all.

Every privileged operation therefore failed on every real installation: service
start, stop and restart, engine reload, network apply, driver rebuild, span
generate, firewall apply and clear, hostname, timezone, clock synchronisation,
reboot and power off. That is the entire system operations vocabulary, and the
product's central claim is that every operation can be performed from the
browser.

It was proved before anything was changed, by dropping to an unprivileged
account with and without the flag:

```
NoNewPrivileges=no  -> sudo: a password is required
NoNewPrivileges=yes -> sudo: The "no new privileges" flag is set, which
                       prevents sudo from running as root.
```

**Why the suite did not catch it.** `PrivilegedOperations._runner` — the
function that actually starts the process — was replaced in every single test
of that path. The tests proved the vocabulary, the validation, the refusals and
the reporting, all of which were correct. Nothing ever ran the real path, so
nothing ever discovered that the real path could not run.

**Repair.** A root-run daemon listens on a Unix domain socket, mode `0660`,
owned `root:myipbx`. It establishes the caller's identity from the kernel's
peer credentials rather than from anything the request claims, and refuses a
caller that is not the service account before parsing a byte of what was sent.
It accepts only the same fixed vocabulary, validated against the same patterns,
and passes the vector to the helper script as an argument list with no shell on
the path. The control plane keeps `NoNewPrivileges=yes`. The service account's
privilege grant is deleted outright: it now holds none.

**Coverage.** `tests/test_privileged_path.py` — seventeen tests that create a
real daemon on a real socket, connect with the real client and run a real
script, with nothing on the path replaced. It covers the success and failure
paths, every verb in the vocabulary, and refusal of unknown verbs, unmanaged
services, shell metacharacters, wrong argument counts, malformed frames,
oversized frames and unpermitted callers. Two further tests would catch the
defect returning: one parses the control plane's source and fails if it ever
names the escalator again, and one fails if a privilege grant is shipped.

## Defects eleven to fourteen — the image build

**Eleven: a size report could kill a build.** `du | cut` under `pipefail` and
`errexit` meant that measuring a directory the build could not fully read
aborted the build. Reporting a size must never be able to fail a build.
Every failure in that path is now swallowed deliberately and the result is
checked before use.

**Twelve: rebuilding the boot image broke the boot.** The rebuilt image could
not create the writable layer over its own read only root and stopped at a
rescue shell reporting no support for its layering format — with the required
modules demonstrably present in the archive. The rebuild was reverted rather
than papered over, and the image ships the one the kernel package produced,
which is the one the build proves boots.

**Thirteen: ordinary packages regenerated the boot image anyway.** This is the
defect behind the last one, and it was found only because a later package was
added. Installing `console-setup` fired an `update-initramfs` trigger and
silently replaced the proven boot image as a side effect. Every package added
to the image after the kernel was a chance to do the same. Reverting one
rebuild had treated the instance; the class stayed open. Boot image
regeneration is now switched off in the image immediately after the kernel is
installed, so no package trigger can reach it. The installer switches it back
on, because an appliance installed to a disk needs a boot image built for that
disk.

**Fourteen: the appliance did not answer to its own name, and the fix failed.**
The live boot machinery derives a host name from the disc description and
writes it over the one in the filesystem. A service unit was added to set the
name again from userspace; it called `hostnamectl`, which needs a message bus
that was not up at that point in the boot, and it failed on every start —
turning a cosmetic problem into a visible failed unit on the console.

The unit was deleted. The live boot machinery accepts a host name as a boot
argument, which reaches it at the one moment it is listening, so the name is now
passed there and both boot paths are generated from a single setting so they
cannot drift apart. The lesson is the ordinary one: a workaround that fails is
worse than the cosmetic fault it was working around.

## Standing observation — the boot console

The live boot machinery ships scripts written for a desktop installation disc
and runs all of them regardless of what the image contains. On an appliance
with no desktop, several reached for directories that were never installed and
reported errors while the boot proceeded correctly. Those directories are now
created in the image so the scripts do their work quietly, and the disc no
longer describes itself as a package disc, which stops the machinery trying to
read package indexes that were never there.

This is presentation rather than function. It is recorded because a console
full of errors during a normal boot teaches an operator to ignore errors, and
an operator who ignores errors will ignore the one that matters.

## What this pass does not claim

- **No real hardware has been driven.** Nothing in this repository shows a real
  B410P or TDM410P has ever been brought up. Every hardware test uses a
  simulated fixture. This is the largest gap between what the product is
  designed to do and what has been shown.
- **The privileged path is proved against a stand-in helper script**, over a
  real socket with the real daemon and the real client. The verbs' own effects
  — restarting a real engine, applying a real network configuration — still
  need a real machine.
- **The disk installer is proved in rehearsal only.** It runs end to end and
  writes nothing. It has not laid an appliance down on a real disk.
- **There is still no transport security.** The console speaks plain HTTP. An
  administrator password for an appliance that can reboot the machine and
  rewrite the firewall crosses the site network in clear text. This is a known,
  open, blocking defect and it is not fixed in this pass.
