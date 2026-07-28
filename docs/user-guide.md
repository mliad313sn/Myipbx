# User Guide

Everything in this guide is done from the web console. Where a terminal command
appears, it is offered as an alternative for someone who prefers one, never
because the console cannot do the job.

Two conventions run through the whole product. The appliance **assigns no
addresses** — it has no address allocation service and will refuse to start
beside one. And every number the appliance shows you is **spelled in words**;
the only numerals you will see are ones you type into a field, because those go
to a machine.

---

## Contents

**Getting started** — [signing in](#signing-in) · [the console at a glance](#the-console-at-a-glance) · [the link indicator](#the-link-indicator)

**Telephony** — [extensions](#extensions) · [trunks](#trunks) · [ring groups](#ring-groups) · [menus](#menus) · [queues](#queues) · [conference rooms](#conference-rooms) · [inbound routes](#inbound-routes) · [outbound routes](#outbound-routes) · [time conditions](#time-conditions) · [voicemail](#voicemail)

**Watching the system** — [live calls](#live-calls) · [call history](#call-history) · [reports](#reports) · [alarms](#alarms) · [logs](#logs)

**Legacy hardware** — [what is fitted](#what-is-fitted) · [guided bring up](#guided-bring-up) · [spans and channels](#spans-and-channels) · [when a card will not appear](#when-a-card-will-not-appear)

**The machine** — [readings](#machine-readings) · [network addressing](#network-addressing) · [identity and time](#identity-and-time) · [firewall](#firewall) · [services](#services) · [restart and shutdown](#restart-and-shutdown)

**Keeping it running** — [applying configuration](#applying-configuration) · [reconciliation](#reconciliation) · [automated tasks](#automated-tasks) · [backup](#backup) · [restore](#restore)

**Reference** — [what needs a restart](#what-needs-a-restart) · [privileged operations](#privileged-operations) · [the two constraints](#the-two-constraints) · [what this appliance will not do](#what-this-appliance-will-not-do)

---

## Getting started

### Starting from the appliance image

Write the image to a flash device or burn it to a disc, put it in the machine,
and start the machine from it. The appliance comes up by itself and prints, on
the machine's own screen, the address to open a browser at.

That address was written into the image. This appliance never asks a network
for an address and never hands one out, so it has to arrive already reachable
or you could not open its console the first time. Change it from the console
once you can reach it.

While the appliance is running from the medium it forgets everything when the
machine stops, which is the right shape for trying it and the wrong shape for
running a site on it. When you are ready to keep it, install it onto the
machine's own disk from the console, or from the machine's own keyboard:

```
crossbar-install-to-disk --dry-run --disk /dev/sda
crossbar-install-to-disk --disk /dev/sda
```

The first command reports every step and writes nothing. The second does it,
and destroys everything on that disk — it says exactly what it is about to
destroy and makes you type a word to agree. Then take the medium out and start
the machine again.

### Signing in

There are no default credentials. The installer generated a password and
printed it once, on the console, during installation. If you missed it, see
[recovering the password](#if-the-password-was-lost).

Open a browser at the address and port the installer reported and sign in. Five
failed attempts from one address lock that address out temporarily; the message
tells you how long is left.

Signing out ends the session on the appliance, not merely in the browser.

### The console at a glance

The sections run down the left. They are grouped: what is happening now at the
top, telephony configuration in the middle, and the machine itself at the
bottom.

| Section | What it is for |
| --- | --- |
| overview | headline figures, alarms, and trunk registration |
| live calls | every channel on the system, as it happens |
| call history | completed calls, newest first |
| reports | totals and breakdowns over a period, and files to download |
| extensions | the telephones |
| trunks | connections to carriers |
| ring groups | sets of telephones that ring together |
| menus | plays a greeting and sends the caller where they choose |
| queues | holds callers in order until somebody is free |
| conference rooms | rooms several callers can be in at once |
| inbound routes | where an arriving call is sent |
| outbound routes | which trunk carries a dialled number |
| time conditions | different destinations inside and outside business hours |
| hardware | interface cards, spans, and guided bring up |
| system | the machine: addressing, identity, services, power |
| firewall | which services are reachable, and from where |
| configuration | rendering the engine configuration, and reconciliation |
| tasks | automated jobs, and running one now |
| logs | the appliance's and the engine's logs |
| backup | download a backup, restore from one |
| constraints | proof that both product constraints hold |

**Every list works the same way.** Extensions, trunks, routes, groups, menus,
queues, conferences and time conditions all draw the same table, and all of
them have:

- a **search box** that looks in every column, so you can type what you
  remember rather than working out which field it is in;
- a **count** beside it saying what is shown against what exists, so a filtered
  table is never mistaken for the whole list;
- **sortable columns** — choose a heading to sort by it, choose it again to
  reverse. Numbers sort as numbers, so extension two hundred one comes after
  extension one hundred and not between twenty and twenty-one;
- **download the list**, which writes every record to a comma separated file.
  Passwords are never included.

### The link indicator

Top right, beside the sign out button. It reports one of three conditions, and
the distinction is the point.

| Indicator | Meaning | What to do |
| --- | --- | --- |
| **live** | the appliance spoke within the expected interval | nothing |
| **reconnecting** | the socket is down; a retry is scheduled | wait; if it persists, check the service |
| **stale** | the socket is open but the appliance has gone quiet | investigate now |

**Stale is the important one.** It means the page cannot vouch for what it is
showing. Treat the figures as history, not as current state. A console that
cannot tell you this will show you a frozen screen and let you read it as a
quiet night.

---

## Telephony

Every telephony section works the same way: a table of what exists, an **add**
button, and **edit** and **delete** on each row. The forms are generated from
the appliance's own schema, so every field you see is a field the appliance
validates — and a refusal is marked on the field that caused it, not as one
message about the whole form.

### Extensions

An extension is a telephone on this system.

| Field | Notes |
| --- | --- |
| extension number | what a caller dials. One to ten digits |
| display name | shown to other telephones here |
| password | set once; stored as a secret and never shown again |
| technology | how the telephone registers. Session protocol by default |
| voicemail | whether unanswered calls are offered a mailbox |
| voicemail password | the code the user enters to collect messages |
| electronic mail address | messages are announced here when set |
| ring time | how long it rings before the call moves on |
| enabled | an unticked extension is left out of the configuration entirely |

**On passwords.** A password field left blank on an edit means *leave it as it
is*. It is never blanked by accident. Passwords are held in a separate file
readable only by the appliance, never in the configuration document, and never
returned to the browser — the table shows only whether one is set.

After adding or changing an extension, go to
[applying configuration](#applying-configuration).

### Trunks

A trunk is a connection to a carrier or to another system.

| Field | Notes |
| --- | --- |
| trunk name | how the trunk is named throughout the appliance |
| technology | how the carrier is reached |
| carrier address | the carrier's host name or address |
| account name | the account the carrier issued |
| account password | stored as a secret |
| register with the carrier | untick when the carrier trusts your address instead |
| enabled | a disabled trunk is not driven and not rendered |

Registration state appears on the **overview** section and updates as it
changes. The states mean:

| State | Meaning |
| --- | --- |
| registering | an attempt is in flight |
| registered | the carrier accepted it |
| retrying | the attempt failed; another is scheduled, with a lengthening delay |
| failed | the attempt limit was reached, or the credentials were rejected |
| disabled | administratively off; no attempts are made |
| unknown | the driver stopped before the trunk settled |

A trunk stuck in **retrying** against a carrier that is down is behaving
correctly and will recover on its own. A trunk in **failed** with a rejection
reason needs its credentials fixed. Registrations run concurrently, so one dead
carrier never delays another — if *every* trunk is retrying, suspect the
network path or the engine rather than the trunks.

### Ring groups

A set of telephones that ring together.

Choose members from the extensions that exist — the field offers them, so a
group cannot name a telephone that is not there. **Ring all** rings every member
at once; **in order** tries them one at a time.

### Menus

A menu answers the call, plays a greeting, and sends the caller wherever they
choose.

| Field | Notes |
| --- | --- |
| menu number | callers reach the menu at this number |
| greeting recording | the recording played on arrival |
| options | what each key leads to |
| wait time | how long to wait for a choice |
| if nobody chooses, send to | left empty, the call is hung up |

**Writing the options.** One entry per key, a key, an equals sign, and where it
goes, separated by commas. Press one for reception and two for sales, where
reception is extension two hundred one and sales is ring group six hundred, is
written `1=201,2=600`. The star and hash keys may be used as well.

A key may appear only once — naming the same key twice is refused, because the
engine would silently take whichever entry was written first.

Every menu gets a deliberate ending. A caller who presses nothing, or presses
something the menu does not offer, goes to the destination you named; if you
named none, the call is hung up rather than left looping.

### Queues

A queue holds callers in order and offers them to whoever is free.

| Field | Notes |
| --- | --- |
| who answers | the extensions that take calls from this queue |
| how calls are offered | which member the next waiting call goes to |
| ring time | how long one member rings before the call moves on |
| most callers waiting | callers beyond this go to the overflow destination; zero means no limit |
| when full or timed out, send to | where a caller goes who could not be answered |
| music while waiting | the music class played to waiting callers |

The strategies are ring all, least recent, fewest calls, random, and round
robin with memory. **Ring all** rings every member at once; the rest offer the
call to one member at a time, differing in how they choose which.

### Conference rooms

A room several callers can be in at once.

Set an entry code to keep the room private — it is stored as a secret like any
other password and never shown again. A room with no entry code is open to
anyone who dials it, and the generated configuration says so explicitly rather
than inventing a code.

**Announce arrivals and departures** plays a tone as people come and go. Turn
it off for large rooms, where it becomes noise.

### Inbound routes

Where a call arriving from a carrier is sent.

The **number dialled** field takes the number the caller dialled. Use the
pattern marks to match more than one: a period matches anything at all, and the
usual digit class marks work as they do in the engine's dialplan. A route for
everything is a single period.

Then choose the destination: an extension, a ring group, a mailbox, or hang up.

### Outbound routes

Which trunk carries a call to a dialled number.

| Field | Notes |
| --- | --- |
| dialled pattern | what the dialled number must match |
| carried by | which trunk takes the call |
| digits to remove | leading digits stripped before dialling, such as a dialling out prefix |
| digits to add | digits placed in front, such as a country code |
| order | routes are tried lowest first |

The **carried by** field offers only trunks that exist, and a trunk that a
route still uses cannot be deleted — the refusal names the route.

### Time conditions

Sends calls to one place during business hours and another outside them.

Set the opening and closing times, tick the days, and give a destination for
each side. The condition is reachable by name from an inbound route.

### Voicemail

Voicemail is configured per extension rather than as a separate section: tick
**voicemail** on the extension, set a voicemail password, and optionally an
electronic mail address for announcements. The mailbox configuration is
generated with everything else.

---

## Watching the system

### Live calls

Every channel on the system, pushed from the engine's own event stream as it
happens. Nothing on this page is polled, so a call that arrives and clears in
under a second still appears.

Columns are the channel, its state, the caller, the extension dialled, how long
the call has existed, and how long it has been answered.

If the engine connection drops, this page deliberately empties rather than
leaving stale calls on screen. An empty list during an engine outage means *not
known*, not *no calls* — and the alarm on the overview says which.

### Call history

Completed calls, newest first, with a search box. Durations are spelled. For
anything covering more than the last hundred calls — a period, a total, a
per-extension figure — use [reports](#reports).

If the section reports that no history is available, the engine is not writing
call detail records to a comma separated file; enable that in the engine and
the view populates itself.

### Reports

Where you answer the questions somebody asks about the system rather than about
a call: how many calls were taken, how many were missed, which extension
answers and which does not, what hour is busiest, whether the average
conversation is getting longer.

**Choose the period first.** Most of them are named — today, yesterday, the
last seven days, this month, last month, the last thirty days, everything on
record — because a typed date is a place to make a mistake the report then
presents as a fact. *Between two dates* is there when you need it, and only
then does it show the two date boxes.

**The period at a glance** carries the figures the field judges a telephone
system on:

| Reading | What it means |
| --- | --- |
| answered | the share of calls attempted that were answered. The one number that says whether the system is working |
| missed | everything that was not answered, however it failed to be, so answered and missed always sum to the calls |
| average conversation | the mean talk time of the calls that *were* answered. Deliberately not the mean of every call: a thousand unanswered calls of six seconds each would drag that toward zero and tell you nothing |
| average time to answer | how long a call rings before somebody picks it up |
| calls in, out, inside | decided by the dialplan context the appliance itself wrote, not guessed from the numbers |

**When the calls came** draws every hour of the day. Each bar is the hour's
volume against the busiest hour, and the filled part inside it is what was
answered — so the gap between the two is what you are looking for. The busiest
hour is named in the sentence above the chart and every hour is in the table
below it, so nothing in the chart is only available by looking at it.

**The breakdowns** are by extension, by most called destination, by trunk, by
outcome, by hour of the day and by day. The per-extension table counts an
extension on whichever end of the call it was, so it is a table of what each
extension did rather than a division of the calls between them.

**Downloading.** Every breakdown, and the calls behind them, download as a
comma separated file. Those files carry figures as digits rather than words,
because a spreadsheet cannot add up a word. This is the one deliberate
exception to the spelling rule, and the console says so when you download one.

If the section reports that there is nothing to report on, the engine is not
writing call detail records; see [call history](#call-history).

### Alarms

Alarms appear at the top of the **overview** when there are any, with how long
each has been raised. They clear themselves when the condition clears. The ones
you will see:

| Alarm | Meaning |
| --- | --- |
| the engine is not connected | the control plane cannot reach the telephony engine |
| the engine did not answer a health probe | connected, but not responding |
| a span reports an alarm | the interface driver is reporting a fault on a span |
| a generated file was modified outside the appliance | see [reconciliation](#reconciliation) |
| an address allocation service appeared | something on this machine is handing out addresses |

### Logs

Choose a log, optionally a level and a search term, and read it. Available
logs are the appliance's own, the installation log, and the telephony engine's.

Only these named logs can be read; the viewer is not a file browser. Every line
is spelled before it reaches the page, so no digit appears here either.

---

## Legacy hardware

This is the part every other system leaves to the command line.

### What is fitted

The **hardware** section lists the interface cards found on the peripheral bus,
named in plain language, with the driver each needs and whether that driver is
currently bound to it. Below that are the spans the driver is exporting and
their channels.

A machine with no card fitted says so as a normal condition. That is not a
fault, and the appliance runs perfectly well without telephony hardware.

### Guided bring up

Four steps, each of which can be run on its own and repeated safely. Each
reports its own outcome underneath.

**First — detect the interface cards.** Reads the peripheral bus and names
anything Digium made. Safe at any time; changes nothing.

**Second — build and load the drivers.** Compiles the interface drivers against
the kernel that is actually running and loads them. This is the step that is
otherwise a command line ritual. It takes several minutes and interrupts
telephony, so it asks for confirmation.

The appliance chooses its own driver source, and the reason is worth knowing:
the legacy card drivers were removed from the driver project in twenty eighteen,
restored after community pressure by April of twenty twenty-four, and **no
release has been tagged since**. The changes needed to compile against kernels
from six point ten onward exist only in the development tree. So on a newer
kernel the appliance builds from the development tree, and on an older one from
the released archive. It says which it chose, and why, in the log.

**Third — generate the span configuration.** Asks the driver what it found and
writes the span configuration to match.

**Fourth — render and reload.** Writes the engine configuration from your
settings and reloads the engine.

### Spans and channels

Each span shows its number, description, channel count and alarm state. A span
in alarm is reported by the driver, not by the appliance.

Role markers such as the master span annotation are **not** alarms and are not
shown as such — a distinction worth mentioning because getting it wrong makes
every machine appear permanently faulty.

### When a card will not appear

In order of likelihood:

1. **The driver is built for a different kernel.** Usually after a kernel
   upgrade. Run *build and load the drivers* again.
2. **The kernel headers are missing.** The driver cannot be compiled without
   headers matching the running kernel; the step says so plainly.
3. **The card is not seated.** The peripheral bus enumeration in step one is
   read directly from the kernel; if the card is not listed there, no amount of
   software will find it.
4. **The card is not in the appliance's catalogue.** It is still detected and
   still reported as a Digium device — only its description is generic. This is
   not a fault.

---

## The machine

### Machine readings

The **system** section shows the host name, kernel, time zone, how long the
machine and the appliance have each been up, how many consoles are connected,
and memory, disk and load. All spelled.

### Network addressing

The interface table lists each interface, whether it is up, whether it has
carrier, and its hardware address.

To change addressing, choose the interface, type the address, choose the prefix
length, and optionally give a gateway. Applying it **interrupts the network**,
so it asks for confirmation — and if you are connected over the interface you
are changing, you will lose the console until you reconnect at the new address.

**This appliance requests no address and offers none.** There is no option to
obtain an address automatically, because the appliance has no address
allocation client behaviour to enable and no allocation service to run. Every
address here is one an administrator wrote down.

### Identity and time

Set the host name and the time zone, and apply. **Synchronise the clock** steps
the clock from the configured time source; telephony behaves badly when the
clock jumps, so prefer doing this during a quiet period.

### Firewall

A telephony system visible from an untrusted network is scanned continuously
and found within hours. The appliance therefore generates a complete ruleset
from what you declare rather than leaving the machine open.

**How it works.** You declare rules — a service and the networks it should be
reachable from. The appliance generates the ruleset; its helper loads it.
Nothing you type is turned into a rule by the privileged side, which is why
this is safe to operate from a browser.

**The console is always reachable.** That rule is emitted before any rule you
wrote, so a firewall cannot lock you out of the appliance you applied it from.

**Everything not named is dropped.** The loopback interface, traffic this
machine started, and the diagnostic messages a network genuinely needs are
allowed. Nothing else, unless a rule says so.

| Service | What it opens |
| --- | --- |
| session protocol | call signalling, in the clear |
| secure session protocol | call signalling, encrypted |
| media | the audio of calls in progress |
| secure shell | administrative access at the command line |
| name resolution | answering name lookups, which this appliance does not do |

There is deliberately no way to open an address allocation port, because there
is nothing on this appliance listening on one.

**Working order.** Declare your rules, press **generate the ruleset**, read the
preview at the bottom of the section, then press **apply it**. The preview is
the exact text that will be loaded — read it before applying, particularly the
first time.

**Reachable from** takes a network in prefix notation, or the word `any`. The
section counts how many of your rules are open to anywhere and says so, because
that is nearly always worth narrowing.

**Unload it** removes the appliance's rules and leaves the machine unfiltered
by them. Only the appliance's own table is touched, so a site with its own
rules keeps them.

### Services

Start, restart or stop the telephony engine, the appliance itself, or the
interface driver. Restarting and stopping interrupt service and ask for
confirmation.

Only these three services can be controlled. The appliance cannot reach any
other service on the machine, by design.

### Restart and shutdown

Both are on the **system** section and both confirm first. Every call in
progress is lost. The machine acknowledges before it goes, so the console will
report success and then stop responding — that is expected.

---

## Keeping it running

### Applying configuration

Changing an extension, a trunk or a route updates the appliance's own record
immediately. It does **not** reach the telephony engine until you render.

Go to **configuration**, press **render the engine configuration**, then
**reload the telephony engine**. The guided bring up's fourth step does both at
once.

### Reconciliation

The appliance holds one document as the source of truth and generates the
engine's configuration files from it. It records the digest of each file it
writes.

**If you edit a generated file by hand, the appliance notices.** It will refuse
to overwrite your edit and will show it on the **configuration** section as a
decision you must make:

- **adopt the file on disk** — your edit becomes authoritative and stops being
  reported. The edit survives. Use this when your change was right and should
  stay.
- **regenerate over it** — the appliance rewrites the file and your edit is
  lost, deliberately. Use this when the edit was a stopgap you have since put
  into the configuration properly.

There is no third option and the appliance will never choose for you. Losing an
administrator's emergency fix without telling them is the exact failure this
product exists to remove.

### Automated tasks

| Task | What it does | When |
| --- | --- | --- |
| health sweep | checks the engine, the address allocation exclusion, and expires idle sessions | on a schedule |
| hardware rescan | re-enumerates cards and spans | on a schedule |
| render configuration | writes the engine configuration from the source of truth | on demand |
| reload engine | asks the engine to reload | on demand |
| backup state | writes a timestamped copy of the source of truth | on demand |

Each can be run now from the **tasks** section. A task already running will not
be started a second time — the request is refused rather than stacked. Each
carries a timeout, and one that exceeds it is reported as abandoned.

### Backup

**Backup → download a backup** produces one archive containing the
configuration, the secrets, and the administrator credential.

**Treat that file as a password.** Anyone holding it holds your carrier
credentials and your administrator login. The archive says so in its own
manifest.

### Restore

**Backup → restore**, choose the archive, confirm.

The appliance validates the whole archive before writing anything: only its own
member names are accepted, each is checked for a path that would escape, and
anything that is not a plain file is refused outright. A bad archive is
rejected without a single byte written, so a failed restore cannot leave the
appliance half configured.

After restoring, **restart the appliance** so every component reads the
restored state, then render the engine configuration from it.

---

## Reference

### What needs a restart

| After changing | What is needed |
| --- | --- |
| an extension, trunk, or route | render, then reload the engine |
| the host name or time zone | nothing |
| network addressing | nothing, but the network drops briefly |
| the interface card drivers | the engine restarts as part of the step |
| a restored backup | restart the appliance, then render |
| the appliance's own configuration document | restart the appliance |

### Privileged operations

The **constraints** section lists every operation the appliance can perform
with privilege, what each does, and whether it interrupts service.

The design is worth knowing when you are judging the appliance's security: the
control plane runs unprivileged and performs no system operation itself. It
asks a helper that accepts only a fixed list of verbs and validates every
argument before acting. No value you type is ever assembled into a command for
a shell to interpret. The service account is granted the right to run that one
helper and nothing else.

### The two constraints

The **constraints** section proves both, live.

**The appliance assigns no addresses.** Enforced at four independent points:
the installer installs no allocation service; the audit fails the installation
if one is present; the appliance refuses to start beside one and raises an alarm
if one appears later; and an automated test fails the build if an allocation
directive appears anywhere in the source. The section shows the current audit
findings.

**Every number is spelled.** The same algorithm runs in the appliance, in this
console, and in the installer scripts, and an automated test holds all three
identical. The logging formatter spells every line it emits, so no code path can
opt out.

The one deliberate exception is a value you type into a field — an address, a
port, a time — which stays in machine format because it is going to a machine.

### If the password was lost

The one procedure in this guide that genuinely needs a terminal, because it
must: an interface that could reset its own administrator password without
authentication would not be a security boundary.

```bash
sudo rm /var/lib/crossbar/credentials.json
sudo systemctl restart crossbar.service
sudo journalctl -u crossbar.service -n 30    # the new password is printed once
```

### What this appliance will not do

Stated so that nobody spends an afternoon looking for a setting that does not
exist:

- it will never hand out an address, and has no setting that would let it;
- it will never overwrite a generated file you edited without asking;
- it will never print a digit in a log;
- it will never ship with a default password;
- it does not provision handsets, host multiple tenants, or record calls. See
  the competitive benchmark for the full and honest account of what it does
  not have.
