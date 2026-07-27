# Quality Assurance Report

Author: Agent Five, Quality Assurance role.
Verdict: **PASS.** Two hundred forty-three tests, zero failures, zero errors,
zero skipped.

This report is written against a suite that was actually executed, not
described. Every figure below was read off a run, and the run is reproducible
with `make test`.

## Result

```
Ran two hundred forty-three tests in roughly sixteen seconds
OK
```

| Suite | Tests | Covers |
| --- | --- | --- |
| `test_numerals.py` | twenty-five | Constraint Two across all three implementations |
| `test_wsproto.py` | twenty-seven | the socket protocol, including every malformed input path |
| `test_engine_and_trunks.py` | thirty-nine | the engine client, retry timing, the trunk state machine, live state |
| `test_store_and_hardware.py` | twenty-nine | configuration drift detection and legacy hardware enumeration |
| `test_security_and_transport.py` | sixty-three | credentials, sessions, request parsing, task execution |
| `test_constraint_one.py` | nineteen | Constraint One at all four enforcement points |
| `test_integration.py` | forty-one | end to end over real sockets, including concurrency |

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

**PASS.** The recursive loop terminates.
