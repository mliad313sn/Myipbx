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
| **[Sn]** | a numbered entry in [Sources](#sources); every one names a file that was opened and read, and the date it was read |
| **[unverified]** | could not be established from a source that was actually read; stated as a gap, never as a fact |

Every citation in this document points at something a reader can open. Where a
claim is about a project's behaviour, the citation names the repository, the
branch or tag, and the file. Where no such citation could be produced, the
claim is marked **[unverified]** or has been removed. Nothing here is cited to
a page that was not read.

Where a project's official documentation was unreachable from the build
environment, the research went to its public source repository instead. For
the question this benchmark actually asks — *what can an administrator do from
the web interface?* — a project's module list and menu definitions are stronger
evidence than its marketing pages, because they are what ships.

**Pricing is deliberately absent.** No price was read from a primary source, and
a wrong price in a competitive document is worse than no price.

**This pass corrected the previous one.** Seven claims that the first draft
stated as fact turned out to be wrong when the source was actually opened. They
are corrected in place below and listed together in [Corrections](#corrections),
because a benchmark that quietly fixes its own errors is no more trustworthy
than one that never checked.

## The question this product is measured by

*Can an administrator run the system entirely from the web interface, or must
they open a terminal?*

Everything else in this comparison follows from that question, because it is
the one that determines who can own the system after it is installed.

## The comparison

Legend: yes · partial · no · unknown.

| Capability | This appliance | FreePBX (free) | Issabel | VitalPBX (free) | FusionPBX | Wazo | Asterisk alone |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Server static addressing from the interface | **yes** | **no** [S9] | yes [S10] | yes [S16] | no [S19] | no [S22] | no |
| Host name and time zone from the interface | **yes** | no [S9] | yes [S10] | yes [S16] | no [S19] | no [S22] | no |
| Interface card driver build from the interface | **yes** | **no** [S3] | no [S11] | no [unverified] | no [S19] | n/a | no |
| Span configuration from the interface | **yes** | **disabled by default** [S2] | yes [S11] | yes [S15] | no [S20] | removed [S24] | no |
| Restart the telephony engine from the interface | **yes** | partial | yes | yes | **no** [S19] | no | n/a |
| Restart or shut down the machine | **yes** | no [S9] | yes [S10] | yes [S16] | no [S19] | no [S22] | no |
| Read logs in the interface | **yes** | yes | partial | yes | yes | no | no |
| Call history in the interface | **yes** | yes | yes | yes | yes | partial | no |
| Backup and restore in the interface | **yes** | yes | yes [S10] | yes [S17] | **no** [S19] | no [S25] | no |
| Firewall rules from the interface | **yes** | **needs a closed component** [S6] [S7] | yes [S14] | yes [S17] | partial [unverified] | no [S22] | no |
| Event driven live call view | **yes** | no | partial [S13] | unknown | yes [S18] | capable, unused [S21] | n/a |
| Detects edits to generated files | **yes** | **no** [S4] | no [unverified] | unknown | n/a | n/a | n/a |
| Interactive menus, queues, conference rooms | **yes** | yes | yes | yes | yes | yes | n/a |
| Legacy Digium card support | **yes** | partial | yes | yes | **routing only** [S20] | **removed** [S24] | yes |
| Runs without any closed component | **yes** | **partial** [S7] | yes | yes | yes | partial | yes |

## What was verified about the field, and what it means

### FreePBX — the free web interface cannot configure the operating system

Searching the whole of the framework's web root for the tools a Linux host is
configured with — the interfaces file, netplan, the network manager client, the
host name and time and date controls — returns nothing, and so does a search for
a machine restart or power off [S9]. Those operations exist only as hooks into
the System Admin module, whose source repository is not publicly readable [S8].

Three findings are worth stating precisely, because they are the sharpest
evidence for this product's thesis and each was read in the shipping source:

**The system update path is switched off entirely.** In the seventeenth
release, `canDoSystemUpdates()` opens with an unconditional `return false`, with
the comment that system updates are disabled for the Debian based system [S1].
The unreachable code beneath it requires the System Admin module and a licence
carrying a machine identifier. Operating system updates are therefore a
terminal operation.

**The interface card configuration module ships read only.** The setting named
`DAHDIDISABLEWRITE` is defined with a default of true, at advanced settings
level one, and the module's own description states that by default it will not
write out any data [S2]. Out of the box, span and channel configuration must be
written by hand until an administrator finds and flips that setting. The warning
they must then accept names five files the module will overwrite and states
that it must never be used alongside `dahdi_genconfig`, the standard span
configuration generator [S3].

**Generated files are overwritten with no backup and no difference shown.** The
write is a plain `file_put_contents()`, in the framework's shared config writer
and again in four places in the interface card module [S4] [S5]. The framework's
header, which its own comment says is added to every generated file, tells the
reader not to edit it and points them at "the FreePBX.org wiki page, or ask on
IRC" — with no address given for either [S4].

That last finding is Benchmark Defect One, in the field, in the shipping code.

**Correction to a claim this product should not make.** Research found no
evidence that the interface card configuration module has been deprecated or
removed: branches for the seventeenth and eighteenth releases both exist, and
the eighteenth was opened with an initial alpha commit in July of twenty
twenty-six [S2]. The accurate characterisation is "minimally maintained and
disabled by default", and that is what this document says.

**A second correction, against this product's own interest.** FreePBX does run
without any closed component. The official Debian installer takes an
`--opensourceonly` flag which removes the commercial modules and purges the
System Admin helper package, and the system runs [S7]. What it loses is the
firewall and every operating system level operation, which is the point being
made here — but "cannot run without a paid component" would have been false, and
this document does not say it.

### Issabel — the real benchmark for system level coverage

Issabel, not FreePBX, is the free system that most completely covers system
administration from a browser. Its system module ships, as separate modules,
network parameters, time configuration, shutdown, backup and restore, a
hardware detector, and a DHCP server [S10]. The hardware detector is a genuine
interface card configuration module: it offers span timing source, line build
out, framing and coding, and five echo canceller choices — OSLEC, MG2, KB1,
SEC2 and SEC — alongside none [S11]. Firewall rules, port controls, a whitelist
and fail2ban administration are a second open source module set [S14].

It is the standard this appliance had to beat. Two of the three points on which
it is beaten were verified; the third could not be, and has been demoted
accordingly:

- Package management through the interface was **deliberately deleted** in
  September of twenty twenty-three. The commit message records that it was "not
  very usable and does not work with dnf caches any longer"; the repository
  management module was removed the following day [S12].
- Live call monitoring is delivered over server sent events, not a persistent
  socket: four call centre modules open an `EventSource` against a handler that
  sets `Content-Type: text/event-stream`, and the rest of the interface polls
  [S13]. No WebSocket was found in the two Issabel repositories that were
  actually searched, but the project has more than twenty and the rest were not,
  so "no persistent socket anywhere" is **[unverified]**.
- The claim that the distributed image is frozen at April of twenty twenty-four
  is **[unverified]**: the download host could not be reached from the build
  environment and no release index was read.

### VitalPBX — the deepest digital span interface in the field

An assumption worth correcting rather than repeating: VitalPBX's legacy
hardware support is not weak. Its published interface translation catalogue
declares four dedicated interface card modules — Interfaces, Profile
Assignments, Channel Groups and Clock Sources — including a Detect Hardware
action, line build out in the standard DSX-1 foot ranges from zero to six
hundred and fifty-five feet, ISDN primary rate settings, and MFC/R2 signalling
whose logging level is selected per protocol layer from error, warning, debug,
notice, CAS, MF and stack [S15]. Nobody ships that signalling support without
meaning it.

The same catalogue shows static addressing with netmask, gateway and both
resolvers, a host name, a time zone, a firewall, backup and restore, and both a
system restart and a system power off [S16] [S17]. On coverage of the operating
system from the browser, VitalPBX is at least Issabel's equal.

Its interface is closed source — only the translation catalogue is public — so
its event transport could not be verified, and neither could the presence or
absence of driver compilation. The widely repeated claim that the free tier is
limited to a fixed number of extensions is **[unverified]**; the licensing
strings in the published catalogue contain no such limit, but a translation
catalogue is not a licence file and this document draws no conclusion from it.

### FusionPBX — correct a stale assumption, then find the real gap

FusionPBX is **not** polling based any more. It ships a complete WebSocket
server as its own service, with four consumers: the operator panel, the active
call view, active conferences, and the system dashboard [S18]. Any comparison
claiming otherwise is out of date.

The real and total gap is elsewhere: searching the whole tree for the host
configuration tools, the time and date controls, or a machine restart returns
nothing, and there is no backup or restore application at all [S19]. It cannot
restart its own telephony engine either — the only engine commands it issues are
`reloadxml` and `reloadacl`, plus per module and per profile actions [S19].

For this product's target hardware it is close to empty, though not quite empty:
there is no interface card configuration anywhere in the interface, but the
outbound route builder does offer `freetdm` as a gateway type and generates the
matching dialplan entries, and the stock FreeSWITCH FreeTDM sample configuration
files ship unedited in the switch application [S20]. An administrator can route
calls out a span that somebody else configured; they cannot configure it here.

### Wazo — capable transport, deliberately unavailable interface

Wazo has the best event transport in the field on paper. Its WebSocket service
requires a wazo-auth token carrying the `websocketd` ACL, and authorisation is
evaluated per event at delivery rather than at subscription: the documentation
states plainly that no authorisation check is done at subscription time, and
that a client will not receive an event it lacks the matching ACL for [S21]. No
part of Wazo's own interface uses it for call monitoring.

That interface was not removed but demoted. It is commented out of the default
Ansible deployment, behind an instruction to uncomment two lines if you want it
[S22]; its own read me says the project "is not always up-to-date with the
entire API engine" [S22]; and its plugin set contains nothing for networking,
host name, time zone, machine restart or backup [S22]. The vendor's own
ecosystem page says the commercial Wazo Enterprise products add "a web
management console" on top of the platform [S23]. Legacy interface hardware was
dropped outright: DAHDI support was removed in the twenty three point zero two
release, having been off by default since twenty one point zero one, and the
upgrade notes give explicit manual removal instructions for the configuration,
the module and the package [S24]. Backup and restore is documented as shell
access to `/usr/sbin/wazo-backup` and a nightly logrotate task, with no
interface path [S25].

### Grandstream — the appliance interface benchmark, and entirely unverified

The Grandstream unit is widely treated as the standard for "everything from the
interface" in an appliance, and an analog port detection loop that measures line
impedance and writes the result back into the trunk configuration is the kind of
behaviour this product's guided bring up is aiming at.

**Every claim about Grandstream is [unverified]. Not one page was opened, and
nothing about it was checked in this pass.** The previous draft additionally
reported, from search result summaries alone, that the current unit series
carries no digital telephony and offers no per service control; neither could be
confirmed against a source that was actually read, and both have been removed
rather than restated. This document treats Grandstream as a design target, not
as a measured competitor, and it is deliberately absent from the comparison
table.

### Asterisk on its own

No web interface. The only markup in the tree is two files under `static-http`,
`ajamdemo.html` and `mantest.html`, which belong to AJAM — and Asterisk's own
documentation carries an "Unsupported" warning on the AJAM page saying it "is
not supported and may have issues and may be removed in the future. Do not use
it if at all possible" [S26] [S27]. The configuration writing path is the
manager interface, whose transports are plain and secured sockets and the
hypertext protocol; grepping its implementation for WebSocket support returns
nothing [S26]. The newer REST interface writes no configuration files at all.

## Where this appliance is ahead

**Every operation has an interface path, including the operating system ones.**
Static addressing, host name, time zone, clock synchronisation, service
control, driver rebuild, span generation, restart and shutdown are all verbs on
a narrow privileged helper, reachable from the console. No competitor examined
covers driver compilation from the interface at all — three were confirmed not
to [S3] [S11] [S19], and two could not be checked either way.

**Generated files are never silently overwritten.** Each rendered file's digest
is recorded when the appliance writes it, compared before the next write, and a
mismatch is refused and raised as an explicit choice — adopt or regenerate.
This is the direct answer to the strongest verified complaint about the market
leader [S4].

**The interface is event driven end to end, and says when it is not.** A
persistent socket carries engine events as they happen, and a dual layer
heartbeat lets the page distinguish live from reconnecting from stale. Of the
systems examined, only FusionPBX also pushes over a socket [S18], Issabel pushes
over server sent events in its call centre modules only [S13], and none of them
reports staleness.

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
- intrusion detection — repeated failed sign ins to the console are rate
  limited and locked out, but there is no equivalent for the telephony ports.
  Issabel ships fail2ban administration as an open source module [S14] and
  VitalPBX lists an intrusion detection module [S17]; FreePBX's is bound to the
  same closed component as its firewall [S6].

What it *does* now have, and did not in its first release: extensions, trunks,
ring groups, inbound and outbound routes, time conditions, voicemail,
**interactive menus, call queues, conference rooms, and a firewall** — each
configured from the interface and rendered into a generated artefact.

The firewall is worth calling out against the market leader specifically. The
free firewall module carries a hard file dependency on the System Admin
package: every privileged action it performs goes through a root hook helper
that refuses to run unless `/etc/incron.d/sysadmin` exists, and its console
command looks for its binaries under the System Admin module's own directory
[S6]. The official installer's open source mode removes the firewall module for
exactly that reason, and says so in a comment — "Remove firewall module also
because it depends on commercial sysadmin module" [S7]. Here the firewall is
generated from the same schema as everything else, denies by default, and always
keeps the console reachable so that applying a firewall cannot lock an
administrator out of the appliance that applied it.

Issabel and VitalPBX both cover the operating system from the browser about as
well as this appliance does, and both have a decade of accumulated telephony
features beyond it. The gap is narrower than it was and it is still real. This
appliance competes on the three defects it was built to overcome and on legacy
hardware bring up — not on feature count.

## An operational finding worth more than the comparison

Most of the legacy interface card drivers were removed from the DAHDI driver
project in October of twenty eighteen, weeks before the three point zero point
zero release [S28]. One family, the USB and mmap XPP drivers, was put back the
next day. The rest came back slowly and incompletely: the analog TDM400P driver
returned in twenty twenty-one and shipped in three point three point zero, and
the last four — `wcte12xp`, `wcte11xp`, `wct1xxp` and `wcfxo` — returned in a
single revert commit on the ninth of April twenty twenty-four whose stated
reason is that they are heavily used in the community. That commit first shipped
in the release numbered three point four point zero, tagged on the sixteenth of
April twenty twenty-four [S29]. Two drivers, `tor2` and `pciradio`, were never
restored and are still absent [S29].

**No release has been tagged since.** Three point four point zero remains the
newest tag, twenty seven months later [S30]. The development tree has moved on by
twenty commits, and among them are the changes that let the drivers build
against current kernels: a `const` correction to the sysfs binary attribute
signature needed from six point eleven, `hrtimer_setup` and the
`del_timer`/`timer_delete` rename needed from six point fifteen, and a wrapper
for the renamed `from_timer` needed from six point sixteen. The newest kernel
guard present in three point four point zero itself is six point five [S31].

Any product that builds these drivers from the last released archive will fail
on a current kernel. This appliance therefore inspects the running kernel and
selects the development tree when it is newer than the last tagged release,
saying so in its log. That single behaviour may matter more to a real
deployment than every feature comparison above.

## Corrections

Seven statements in the first pass of this document were checked against their
sources in this pass and found to be wrong. They are corrected above; they are
listed here so that anyone who read the earlier version knows what changed.

1. **VitalPBX cannot restart or shut down the machine.** It can. Its published
   catalogue carries a system restart and a system power off, each with a
   confirmation prompt [S16].
2. **Issabel's interface card module offers four echo cancellers.** It offers
   five: OSLEC, MG2, KB1, SEC2 and SEC [S11].
3. **Issabel uses server sent events in one module.** It uses them in four call
   centre modules [S13].
4. **FusionPBX has no interface card support of any kind.** It has no card
   configuration, but it does offer `freetdm` as an outbound gateway type and
   ships the stock FreeTDM sample configuration [S20].
5. **FreePBX cannot run without a paid component.** It can; the official
   installer has a supported open source only mode [S7].
6. **FreePBX backs up the interface card configuration file only the first
   time, guarded by a check for whether the backup already exists.** No such
   guard exists. The single backup in the module copies the channel groups file
   whenever new hardware is detected, guarded only by whether the source file
   exists, so it overwrites the previous backup each time; the DAHDI system and
   modprobe files are never backed up at all [S3] [S5].
7. **The kernel compatibility changes missing from the last DAHDI release start
   at six point ten.** They start at six point eleven [S31].

Three further statements were softened rather than corrected, because they were
plausible but could not be confirmed: the count of sixty-nine open source
FreePBX modules, the freezing of the Issabel image at April of twenty
twenty-four, and every claim about Grandstream.

## Sources

Every entry below was opened and read on **27 July 2026**. Repository sources
name the branch or tag read; where a claim rests on a search finding nothing,
that is stated as such.

- **[S1]** FreePBX `framework`, branch `release/17.0`,
  `amp_conf/htdocs/admin/libraries/Builtin/SystemUpdates.php` —
  `canDoSystemUpdates()` begins `return false; // Disabling System update for
  17/Debian based system as of now`. <https://github.com/FreePBX/framework>
- **[S2]** FreePBX `dahdiconfig`, branch `release/17.0`, `install.php` — the
  `DAHDIDISABLEWRITE` setting is defined with value and default `true`, level
  one, and the description "By default the DAHDi configuration module will NOT
  write out any data to protect any current configuration settings". The same
  repository carries both a `release/17.0` and a `release/18.0` branch; the
  latter's most recent commit, dated 17 July 2026, is "initial alpha commit".
  <https://github.com/FreePBX/dahdiconfig>
- **[S3]** FreePBX `dahdiconfig`, branch `release/17.0`, `page.dahdi.php` — the
  write disabled disclaimer lists `chan_dahdi_general.conf`,
  `chan_dahdi_groups.conf`, `chan_dahdi.conf`, `DAHDISYSTEMLOC` and
  `DAHDIMODPROBELOC`, and states the module "should never be used alongside
  \"dahdi_genconfig\"". The same file contains the module's only backup, a
  `copy()` of the channel groups file on hardware change. A search of the module
  for kernel module compilation returns nothing.
- **[S4]** FreePBX `framework`, branch `release/17.0`,
  `amp_conf/htdocs/admin/libraries/BMO/WriteConfig.class.php` — the `HEADER`
  constant, documented "Header gets added to every generated file", contains the
  line "FreePBX.org wiki page, or ask on IRC"; `writeFile()` ends in
  `file_put_contents($filename, $freepbxHeader.$header.$output);` with no
  backup and no comparison.
- **[S5]** FreePBX `dahdiconfig`, branch `release/17.0`,
  `includes/dahdi_cards.class.php` — four `file_put_contents()` calls write the
  generated files; the module's own header reads "Do NOT edit this file as it is
  auto-generated by FreePBX".
- **[S6]** FreePBX `firewall`, branch `release/17.0` — `Firewall.class.php`,
  `runHook()` throws unless `/etc/incron.d/sysadmin` exists ("Sysadmin RPM not up
  to date, or not a known OS. Can not start System Firewall."); twenty two
  distinct hooks including `addnetwork`, `changenetwork`, `updateinterfaces` and
  the fail2ban verbs are routed through it. `Console/Firewall.class.php` sets
  `$bindir = "/var/www/html/admin/modules/sysadmin/bin"`.
  <https://github.com/FreePBX/firewall>
- **[S7]** FreePBX `sng_freepbx_debian_install`, branch `master`,
  `sng_freepbx_debian_install.sh` — the `--opensourceonly` branch removes every
  module listed as Commercial, then `fwconsole ma -f remove firewall` under the
  comment "Remove firewall module also because it depends on commercial sysadmin
  module", and later purges the `sysadmin17` and `ioncube-loader-82` packages.
  <https://github.com/FreePBX/sng_freepbx_debian_install>
- **[S8]** `https://github.com/FreePBX/sysadmin.git` — `git ls-remote` against
  this repository is refused for want of credentials, so its source could not be
  read. This establishes that it is not publicly readable; it is *not* by itself
  proof of any particular licence.
- **[S9]** FreePBX `framework`, branch `release/17.0`, `amp_conf/` — searches for
  `hostnamectl`, `/etc/network/interfaces`, `netplan`, `nmcli`, `timedatectl`,
  `shutdown -r`, `shutdown -h`, `systemctl reboot` and `systemctl poweroff`
  return no matches. A negative result over one repository; other free modules
  were not exhaustively searched, and the claim that there are exactly sixty-nine
  of them is **[unverified]**.
- **[S10]** Issabel `system`, branch `master`, `modules/` — the shipped module
  directories are `applet_admin`, `backup_restore`, `betachannel`, `currency`,
  `dashboard`, `dhcp_by_mac`, `dhcp_clientlist`, `dhcp_server`,
  `hardware_detector`, `network_parameters`, `ping`, `shutdown` and
  `time_config`. <https://github.com/IssabelFoundation/system>
- **[S11]** Issabel `system`, branch `master`,
  `modules/hardware_detector/index.php` — span configuration labels for Timing
  source, Line build out, Framing and Coding; the echo canceller list is `none`,
  `OSLEC`, `MG2`, `KB1`, `SEC2`, `SEC`. No driver compilation appears in the
  module.
- **[S12]** Issabel `system`, commit `de9b04a`, 8 September 2023, "Deprecate
  packages modules as it is not very usable and does not work with dnf caches
  any longer"; commit `39f13f4`, 9 September 2023, "Remove repositories module."
- **[S13]** Issabel `callcenter`, branch `master` — `agent_console`,
  `rep_agents_monitoring`, `rep_incoming_calls_monitoring` and
  `campaign_monitoring` each open a browser `EventSource` against an
  `index.php` handler that sends `Content-Type: text/event-stream`. No WebSocket
  was found in the `system` or `callcenter` repositories; the organisation's
  other repositories were not searched, and no claim is made about them.
  <https://github.com/IssabelFoundation/callcenter>
- **[S14]** Issabel `security`, branch `master`, `modules/` — `sec_rules`
  (iptables rule editing, with an activate and deactivate firewall control),
  `sec_ports`, `sec_whitelist`, `sec_portknock_if`, `sec_portknock_users`,
  `sec_fb_admin` and `sec_fb_banned` (fail2ban), `sec_geoip_map`,
  `sec_letsencrypt`. <https://github.com/IssabelFoundation/security>
- **[S15]** VitalPBX `I18n`, branch `master`, `en_US/dahdi.txt` — declares
  `module.dahdi_hardware`, `module.dahdi_spans`, `module.dahdi_groups` and
  `module.dahdi_clocksource`; `profiles.dahdi_hardware.detect_hardware = Detect
  Hardware`; five DSX-1 line build out ranges from "0-133 feet" to "533-655
  feet"; an MFC/R2 section whose log level accepts "error", "warning", "debug",
  "notice", "cas", "mf", "stack". <https://github.com/VitalPBX/I18n>
- **[S16]** VitalPBX `I18n`, branch `master`, `en_US/network_settings.txt` and
  `en_US/system_misc.txt` — static IP address, netmask, gateway, primary and
  secondary DNS, domain and host name; `system_misc.timezone = Time Zone`
  ("Sets the server timezone"); `system_misc.system_restart` and
  `system_misc.system_power_off`, each with a confirmation string.
- **[S17]** VitalPBX `I18n`, branch `master`, `en_US/` — the catalogue contains
  `firewall.txt`, `geo_firewall.txt`, `intrusion_detection.txt`,
  `backup_and_restore.txt` and `logfiles_viewer.txt`. `licensing_usage.txt`
  contains a single key and no extension limit. This is a translation catalogue,
  not application source: it shows what the interface names, not how it behaves.
- **[S18]** FusionPBX, branch `master` —
  `core/websockets/resources/service/websockets.php` and
  `core/websockets/resources/classes/websocket_server.php`; the four classes
  implementing `resources/interfaces/websocket_service_interface.php` are
  `app/operator_panel/resources/classes/operator_panel_service.php`,
  `app/active_calls/resources/classes/active_calls_service.php`,
  `app/active_conferences/resources/classes/active_conferences_service.php` and
  `app/system/resources/classes/system_dashboard_service.php`.
  <https://github.com/fusionpbx/fusionpbx>
- **[S19]** FusionPBX, branch `master` — searches across `app/`, `core/` and
  `resources/` for `hostnamectl`, `/etc/hostname`, `/etc/resolv.conf`,
  `/etc/network`, `netplan`, `nmcli`, `timedatectl` and `/etc/timezone` return
  nothing; the only `reboot` matches are handset reboots in the provisioning and
  registration applications. There is no backup or restore application
  directory. The only engine level commands issued are `api('reloadxml')` and
  `api('reloadacl')`; no FreeSWITCH restart or shutdown call exists.
- **[S20]** FusionPBX, branch `master` —
  `app/switch/resources/conf/freetdm.conf` and
  `app/switch/resources/conf/autoload_configs/freetdm.conf.xml` are the stock
  FreeSWITCH samples, the former headed "!! THIS IS A SAMPLE CONFIGURATION ONLY
  !!"; `app/dialplan_outbound/dialplan_outbound_add.php` offers `freetdm` as a
  gateway type in three selects and names the generated dialplan entries
  `freetdm.<abbrv>`. No page edits either file.
- **[S21]** Wazo `wazo-platform.org`, branch `master`,
  `website/uc-doc/api_sdk/websocket.md` — connection requires a wazo-auth token
  with the `websocketd` ACL; the "Events Access Control" section states "No
  authorization check is done at subscription time. Checks are only done when an
  event is received." <https://github.com/wazo-platform/wazo-platform.org>
- **[S22]** Wazo `wazo-ansible`, branch `master`, `inventories/uc-engine` line
  26 — "# Uncomment the 2 following lines if you want to deploy wazo-ui too:".
  Wazo `wazo-ui`, branch `master`, `README.md` — "This project is not always
  up-to-date with the entire API engine, but you can still do a lot."; the
  `wazo_ui/plugins/` directory contains no networking, host name, time zone,
  machine restart, backup or firewall plugin.
  <https://github.com/wazo-platform/wazo-ansible>,
  <https://github.com/wazo-platform/wazo-ui>
- **[S23]** Wazo `wazo-platform.org`, branch `master`, `content/ecosystem.md` —
  under "Commercial Products", Wazo Enterprise Unified Communication is described
  as adding "web and mobile applications and a web management console".
- **[S24]** Wazo `wazo-platform.org`, branch `master`,
  `website/release-notes/sprint-review-2302.md` — "**DAHDI**: DAHDI support has
  been removed. DAHDI was removed by default since 21.01."; and
  `website/uc-doc/upgrade/upgrade_notes.md`, which instructs the reader to
  remove `/etc/wazo-confgend/conf.d/chan_dahdi.yml`, remove `chan_dahdi.so` from
  `modules.conf`, `apt purge wazo-asterisk-extra-modules`, and remove
  `/usr/share/wazo-upgrade/pre-start.d/upgrade-dahdi-linux-modules.sh`.
- **[S25]** Wazo `wazo-platform.org`, branch `master`,
  `website/uc-doc/system/backup_restore.md` — backups are a nightly logrotate
  task writing to `/var/backups/wazo`, retrieved "with shell access", via
  `/usr/sbin/wazo-backup`. No interface path is described.
- **[S26]** Asterisk, branch `master` — the only HTML tracked anywhere in the
  tree is `static-http/ajamdemo.html` and `static-http/mantest.html`. Searching
  `main/manager.c`, `main/manager_*.c` and `include/asterisk/manager.h` for
  WebSocket returns nothing; the file's own header comment describes its
  transports as the AMI socket and AMI over HTTP.
  <https://github.com/asterisk/asterisk>
- **[S27]** Asterisk `documentation`, branch `master`,
  `docs/Configuration/Interfaces/Asterisk-Manager-Interface-AMI/Asynchronous-Javascript-Asterisk-Manager-AJAM/Allow-Manager-Access-via-HTTP.md`
  — "/// warning | Unsupported — AJAM is not supported and may have issues and
  may be removed in the future. Do not use it if at all possible. Use standard
  TCP based AMI instead." <https://github.com/asterisk/documentation>
- **[S28]** `dahdi-linux`, commits of 3 and 4 October 2018 — `bfdfc47` removes
  `pciradio`, `60d058c` `tor2`, `14198ae` `wcfxo`, `dade6ac` `wct1xxp`,
  `04e759f` `wctdm`, `3748456` `wcte11xp`, `3697450` `wcte12xp`, `a36d266` the
  XPP drivers, `a66e88e` all but `wcb41xp`, `wcb43xp` and `wcb23xp`; `40f4f86`,
  the following day, reverts the XPP removal. Tag `v3.0.0` follows on 15
  November 2018. <https://github.com/asterisk/dahdi-linux>
- **[S29]** `dahdi-linux`, commit `8bda5af`, 9 April 2024, "drivers: Revert
  removals of widely used drivers. (#29)", restoring `wcte12xp`, `wcte11xp`,
  `wct1xxp` and `wcfxo`; `git tag --contains` reports its first tag as `v3.4.0`.
  The earlier commit `394726b`, 2 January 2021, restored `wctdm`, first tagged in
  `v3.3.0`. `drivers/dahdi/` on `master` contains no `tor2.c` and no
  `pciradio.c`. The associated pull request is
  <https://github.com/asterisk/dahdi-linux/pull/29>, whose stated rationale is
  that the drivers are heavily used in the community.
- **[S30]** `dahdi-linux` — `git ls-remote --tags` and
  `git for-each-ref refs/tags` both give `v3.4.0`, dated 16 April 2024, as the
  newest tag. `master` was last committed on 3 March 2026, twenty commits ahead
  of `v3.4.0`.
- **[S31]** `dahdi-linux` — the newest kernel version guard present at tag
  `v3.4.0` is `KERNEL_VERSION(6, 5)`. Commits after it add
  `KERNEL_VERSION(6, 11, 0)` in `drivers/dahdi/dahdi-sysfs.c`,
  `drivers/dahdi/dahdi-sysfs-chan.c` and `drivers/dahdi/xpp/xbus-sysfs.c`
  (commit `5358829`, const `bin_attribute`); `KERNEL_VERSION(6, 15, 0)` in
  `drivers/dahdi/dahdi_dummy.c` (`hrtimer_setup`) and `include/dahdi/kernel.h`
  (commit `67d909a`, `del_timer`/`del_timer_sync` renamed to `timer_delete`);
  and `KERNEL_VERSION(6, 16, 0)` in `include/dahdi/kernel.h` (commit `0d864e9`,
  the renamed `from_timer`).
