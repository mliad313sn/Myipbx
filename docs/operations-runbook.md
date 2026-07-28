# Operations Runbook

For the engineer who has to make this work at three o'clock in the morning.
Procedures first, explanations second.

Throughout: this appliance assigns no addresses, and every numeral it prints is
spelled in full letters. If you see a digit character in a log line, that is
itself a fault worth reporting.

## Before you install

Confirm the machine does not allocate addresses. The installer will refuse to
proceed otherwise, so it is faster to check first.

```bash
sudo ./scripts/verify-no-dhcp.sh
```

An exit status of zero means the machine assigns no addresses. A status of one
means a blocking finding was made; the output names what was found and where.
Remove the allocation service, then run the audit again.

Confirm you have, on the machine:

- kernel headers matching the running kernel — without them the interface card
  drivers cannot be compiled, and this is the single most common installation
  failure;
- the interface driver and tools archives, and the telephony engine archive, in
  the source directory;
- the site's static address, prefix length, and gateway, written down. The
  appliance does not discover these and will not guess them.

Decide the management address before you install. The console binds one
address, not every address, and the installer takes it from
`APPLIANCE_LISTEN_ADDRESS`:

```bash
sudo APPLIANCE_LISTEN_ADDRESS=192.0.2.10 ./scripts/install-appliance.sh
```

If you give it none, the console binds the loopback address and is reachable
only from the machine itself. The installer says so in as many words when it
finishes. This is deliberate: a console that can reboot the machine, rewrite
the firewall and recompile kernel modules should appear on a network somebody
chose for it, and on no other. To change it afterwards, edit `listen_address`
in `/etc/crossbar/appliance.json` and restart `crossbar.service`.

## Installing

There are two ways to get an appliance, and which one you want depends on
whether the machine already runs Linux.

**From the appliance image, onto a bare machine.** Write the image to a flash
device or burn it, start the machine from it, and the appliance is running
immediately — but from the medium, so it forgets everything when the machine
stops. To keep it, install it onto the machine's own disk:

```bash
crossbar-install-to-disk --dry-run --disk /dev/sda   # report everything, write nothing
crossbar-install-to-disk --disk /dev/sda             # do it
```

Everything on that disk is destroyed. The command says exactly what it is about
to destroy and requires you to type a word to confirm, unless you pass
`--assume-yes` for an unattended installation. It refuses the disk the live
medium is on, a disk that is mounted, and a disk carrying swap in use.

The installed appliance keeps the static address the image arrived with, which
is printed on the boot screen. Change it from the console once you can reach it.
Remove the medium and start the machine from its disk.

**Onto a machine that already runs Linux**, using the staged installer below.

Rehearse first. Rehearsal changes nothing and reports what each stage would do.

```bash
sudo ./scripts/install-appliance.sh --rehearse
```

Then install:

```bash
sudo APPLIANCE_INTERFACE=eth0 \
     APPLIANCE_ADDRESS=192.0.2.20 \
     APPLIANCE_PREFIX_LENGTH=24 \
     APPLIANCE_GATEWAY=192.0.2.1 \
     APPLIANCE_RESOLVERS=192.0.2.2,192.0.2.3 \
     APPLIANCE_SOURCE_DIR=/usr/local/src/crossbar \
     ./scripts/install-appliance.sh
```

**Record the administrator password.** The control plane prints it once, on the
console, on its first start. It is never written to a log. If you miss it, see
the recovery procedure below.

**Record the certificate fingerprint printed beside it.** The console is served
over a secured connection, so open it with `https://`, not `http://`. This
appliance generated its own certificate the first time it started — no two
appliances share one, and none of them is signed by an authority any browser
knows — so your browser will warn on the first connection. Compare what it
shows against the fingerprint printed on the appliance's own screen before you
accept it. That comparison is the whole point: it is the one check that tells
an ordinary self signed certificate apart from somebody sitting in the middle
of the connection.

If you missed the fingerprint, read it back on the appliance itself:

```bash
sudo openssl x509 -in /etc/crossbar/tls/appliance.crt -noout -fingerprint -sha256
```

The appliance also listens on port number eight thousand and eighty. That port
serves nothing: it answers every request by redirecting to the secured port, so
that typing the address without a scheme takes you to the right place instead
of a refused connection.

### Installing your site's own certificate

If your site issues its own certificates, install one from the console rather
than from a terminal: **transport security** in the navigation, paste the
certificate and its private key, and press *check and store it*. The pair is
checked against each other before either is stored, so a mismatched pair is
refused there and then rather than at the moment it would have taken the
console down.

Storing it does not apply it. Press *apply the stored certificate* when you are
ready. That restarts the console, which signs out every session on the
appliance including your own, and every open dashboard has to sign in again. No
call in progress is affected. The certificate that was in place is kept beside
the new one as `appliance.crt.previous`, so a certificate that turns out to be
wrong for the site can be put back.

### If a stage fails

Fix the reported cause and run the same command again. Stages that already
completed report themselves as satisfied and are not repeated. To run one stage
alone:

```bash
sudo ./scripts/install-appliance.sh --from-stage 3 --to-stage 3
```

To force a completed stage to repeat:

```bash
sudo FORCE_RERUN=yes ./scripts/install-appliance.sh --from-stage 3 --to-stage 3
```

## Daily operation

The dashboard is served on the address and port recorded in
`/etc/crossbar/appliance.json`. Everything below can also be done from it; the
commands are given for when the dashboard is what you are trying to diagnose.

```bash
systemctl status crossbar.service      # is the control plane running
journalctl -u crossbar.service -f      # follow the control plane log
tail -f /var/log/crossbar/appliance.log
```

### Reading the link indicator

The indicator in the dashboard header reports one of three conditions. The
distinction matters more than it looks.

| Condition | Meaning | What to do |
| --- | --- | --- |
| live | the appliance spoke within the expected interval | nothing |
| reconnecting | the socket is down and a retry is scheduled | wait; if it persists, check the service |
| stale | the socket is open but the appliance has gone quiet | investigate now — this is not a quiet night |

A dashboard that shows **stale** is telling you it cannot vouch for what it is
displaying. Treat the figures on screen as history, not as current state.

## Diagnosing

### The dashboard will not load

```bash
systemctl status crossbar.service
journalctl -u crossbar.service -n 50
```

If the service refuses to start with a message about an address allocation
service, the machine has acquired one since installation. This is a deliberate
refusal, not a fault. Find it and remove it:

```bash
sudo ./scripts/verify-no-dhcp.sh
```

If the service refuses to start with a message naming a certificate or a
private key, the appliance has no usable transport security material and will
not fall back to serving in the clear. The message names the file it could not
read. Generate one:

```bash
sudo /opt/crossbar/bin/crossbar-generate-certificate.sh
sudo systemctl restart crossbar.service
```

That script does nothing at all if the appliance already holds a usable
certificate and its matching key, so it is safe to run at any time. To replace
a certificate that is present but wrong, add `--force`.

If the browser reports that it cannot connect at all, check that you used
`https://` and not `http://`, and that you used the port the console is bound
to. If the browser connects but refuses to proceed past a certificate warning
you cannot dismiss, the certificate probably names an address other than the
one you typed — it is issued for the appliance's configured management address
and its host name. Reach it by that name, or regenerate the certificate after
correcting `listen_address`:

```bash
sudo /opt/crossbar/bin/crossbar-generate-certificate.sh --force
sudo systemctl restart crossbar.service
```

If the console is unreachable from anywhere except the machine itself, the
installation was given no management address and bound the loopback address.
Set `listen_address` in `/etc/crossbar/appliance.json`, regenerate the
certificate with `--force` so that it names the new address, and restart.

### A button in the interface reports that the helper is not running

Every operation that needs privilege — restarting the engine, applying a
network configuration, rebuilding the drivers, restarting the machine — is
performed by a small daemon that runs as the administrator and listens on a
local socket. The control plane itself holds no privilege and never will. If
that daemon is not running, the interface reports it plainly rather than
failing silently.

```bash
systemctl status crossbar-helperd.service
journalctl -u crossbar-helperd.service -n 50
ls -l /run/crossbar/helper.sock
```

The socket should exist, be owned `root:crossbar`, and be readable and writable
by its owner and group and by nobody else. If it is missing, start the daemon:

```bash
sudo systemctl restart crossbar-helperd.service
```

If you find a file at `/etc/sudoers.d/crossbar`, an installation older than this
one left it behind. It grants the service account privilege it no longer needs
and no longer uses. Remove it:

```bash
sudo rm /etc/sudoers.d/crossbar
```

### The dashboard loads but shows the engine as disconnected

The control plane reaches the telephony engine over the manager interface on
the loopback address. Check the engine first, then the credential.

```bash
systemctl status asterisk
sudo asterisk -rx "manager show connected"
```

The credential the control plane uses is in `/etc/crossbar/appliance.json`; the
engine's copy is in its own manager configuration. If they disagree, the engine
will refuse the login and the control plane will retry with a lengthening
backoff, reporting the refusal in its log each time.

While the engine is disconnected, the appliance deliberately clears its channel
view rather than leaving stale calls on screen. An empty call list during an
engine outage means "not known", not "no calls".

### A trunk will not register

Look at the trunk's state and reason on the dashboard; both are pushed as they
change. The states mean:

| State | Meaning |
| --- | --- |
| registering | an attempt is in flight |
| registered | the carrier accepted the registration |
| retrying | the attempt failed; another is scheduled with backoff |
| failed | the attempt limit was reached, or the carrier rejected the credentials |
| disabled | administratively disabled; no attempts are made |
| unknown | the driver was stopped before the trunk settled |

A trunk stuck in **retrying** with a lengthening interval is normal behaviour
against a carrier that is down; it will recover on its own when the carrier
does. A trunk in **failed** with a rejection reason needs a credential fix.

Registrations run concurrently, so one dead carrier never delays another. If
every trunk is retrying, suspect the network path or the engine, not the
trunks.

### An interface card is not detected

```bash
lspci -nn | grep -i d161        # the Digium vendor identifier
lsmod | grep dahdi              # is the interface driver loaded
cat /proc/dahdi/*               # what spans does the driver export
dmesg | grep -i dahdi           # why did the driver refuse to load
```

If the driver is not loaded, the usual cause is that it was compiled against a
different kernel than the one running — typically after a kernel upgrade.
Recompile:

```bash
sudo FORCE_RERUN=yes ./scripts/install-appliance.sh --from-stage 3 --to-stage 3
```

A card that appears on the peripheral bus but is not in the appliance's
catalogue is still detected and still reported as a Digium device; only its
description is generic. That is not a fault.

### A span reports an alarm

Span alarms come from the driver, not from the appliance. A red alarm generally
means loss of signal — check the physical connection and the far end before
anything else. Role markers such as the master span annotation are not alarms
and are not displayed as such.

## Configuration reconciliation

The appliance holds one structured document as the single source of truth and
renders the engine configuration files from it. Each rendered file's digest is
recorded when the appliance writes it.

**If you edit a generated file by hand, the appliance will notice.** It will
refuse to overwrite your edit and will raise the divergence on the dashboard as
a decision:

- **adopt the file on disk** — your edit becomes authoritative and the
  divergence stops being reported. The edit survives. Use this when your change
  was correct and should stay.
- **regenerate over it** — the appliance rewrites the file from the source of
  truth and your edit is lost, deliberately. Use this when the edit was a
  stopgap you have since folded into the source of truth.

There is no third option, and the appliance will never choose for you. This is
the entire point: an emergency edit must not evaporate without somebody
deciding that it should.

To reconcile from the command line, use the interface:

```bash
curl -s --cookie "$COOKIE" http://127.0.0.1:8088/api/configuration/drift
```

## Automated tasks

| Task | What it does | Runs |
| --- | --- | --- |
| `health-sweep` | verifies the engine, the exclusion, and expires idle sessions | on a schedule |
| `hardware-rescan` | re-enumerates interface cards and spans | on a schedule |
| `render-configuration` | renders the engine configuration from the source of truth | on demand |
| `reload-engine` | asks the engine to reload its configuration | on demand |
| `backup-state` | writes a timestamped copy of the source of truth | on demand |

Tasks are single flight: invoking one that is already running is refused rather
than stacked. Each carries a timeout, and a task that exceeds it is reported as
abandoned rather than left to leak.

## Recovery

### The administrator password was lost

Remove the credential file and restart. The control plane generates a new
credential on its next start and prints it once.

```bash
sudo rm /var/lib/crossbar/credentials.json
sudo systemctl restart crossbar.service
sudo journalctl -u crossbar.service -n 30    # the new password is printed here
```

### The configuration is wrong and you want the last good one

```bash
ls -l /var/lib/crossbar/backups/
sudo cp /var/lib/crossbar/backups/appliance-<stamp>.json /etc/crossbar/appliance.json
sudo systemctl restart crossbar.service
```

Then render the engine configuration from the restored source of truth, from
the dashboard or by invoking the render task.

### Backing out entirely

```bash
sudo ./scripts/uninstall-appliance.sh --rehearse    # see what would be removed
sudo ./scripts/uninstall-appliance.sh
```

The telephony engine and the interface card drivers are deliberately **not**
removed. They are the site's telephony service, and removing them would take
the telephones down. Configuration and state are preserved unless you pass
`--remove-state`.

## Upgrading the kernel

The interface card drivers are compiled against the running kernel. A kernel
upgrade will break them until they are recompiled. Plan the recompilation into
the same maintenance window as the upgrade:

```bash
sudo reboot                                         # into the new kernel
sudo FORCE_RERUN=yes ./scripts/install-appliance.sh --from-stage 1 --to-stage 3
sudo systemctl restart asterisk crossbar.service
```

Stage one is included because it verifies that headers matching the *new*
kernel are present before stage three attempts to compile against them.

## What this appliance will never do

Recorded so that nobody spends an afternoon looking for a setting that does not
exist:

- it will never hand out an Internet Protocol address, and it has no setting
  that would let it;
- it will never overwrite a generated configuration file you edited without an
  explicit decision;
- it will never print a digit character in a log line;
- it will never ship with a default password;
- it will never ship with a certificate, so no two appliances share a private
  key; each generates its own the first time it starts;
- it will never serve its console in the clear because a certificate was
  missing. It refuses to serve at all instead, and names the file.
