# Hardware Compatibility Matrix

This document records which interface cards this appliance has actually been
run against, on which kernel, with which driver, and by whom.

**Every row below is marked `UNVERIFIED`.** No real Digium interface card has
been driven by this code. The drivers are compiled into the image and the
bring-up path is written and tested against simulated fixtures, but nothing in
this repository is evidence that a physical card has ever worked. That is the
single largest gap between what this product is designed to do and what has
been shown, and putting the gap in a table is the first step to closing it.

A row here is worth more than any feature claim elsewhere in the documentation,
because it is the only kind of statement in this project that a field
technician can check against their own machine.

## How to add a row

You need the appliance running on the machine, the card fitted, and about
twenty minutes. Record what you actually observed, not what you expected.

1. **Identify the machine and the card.** From the appliance console, open
   *Legacy hardware*. It names the machine's peripheral bus devices and, for
   each Digium card it recognises, the model and the driver that claims it.
   If the card is listed as an unrecognised Digium device, record that — it
   still counts, and it tells the catalogue what to add.

   ```bash
   uname -r                      # the kernel the drivers were built against
   lspci -nn | grep -i digium    # the card as the bus reports it
   dahdi_hardware                # the card as the driver reports it
   ```

2. **Build and load the drivers** from the console, under *Legacy hardware →
   rebuild drivers*. Record whether it succeeded, and if it failed, the exact
   message the console showed. A failure is a valid row.

3. **Enumerate the spans.** Record how many spans appeared, their types, and
   whether any reported an alarm with no cable attached.

4. **Place a call in each direction** over the card, to a real number and from
   one. Record whether audio passed both ways, and whether the call cleared
   cleanly at both ends.

5. **Leave it running for at least twenty-four hours** with the spans up, then
   record whether any span dropped, any alarm appeared, or the driver module
   was unloaded or faulted.

6. **Add the row**, sign it with your name and the date, and say plainly what
   did not work. A row that reports a partial success is far more useful than
   no row, and considerably more useful than an optimistic one.

## The matrix

| Card | Driver | Driver version | Kernel | Distribution | Result | Date | Tested by | Notes |
|---|---|---|---|---|---|---|---|---|
| Digium B410P, quad BRI | `wcb4xxp` | three point one point zero | six point eight | Ubuntu noble | `UNVERIFIED — awaiting first real installation` | — | — | The card the product was designed around. Compiled into the image and never run. |
| Digium TDM410P, analogue FXO and FXS | `wctdm24xxp` | three point one point zero | six point eight | Ubuntu noble | `UNVERIFIED — awaiting first real installation` | — | — | Port type inversion is a known field failure on this card and is not yet detected. |
| Digium TE110P / TE120P, single span | `wcte11xp`, `wcte12xp` | three point one point zero | six point eight | Ubuntu noble | `UNVERIFIED` | — | — | Driver present in the image; no bring-up attempted. |
| Digium TE205P / TE405P, dual and quad span | `wct4xxp` | three point one point zero | six point eight | Ubuntu noble | `UNVERIFIED` | — | — | Driver present in the image; no bring-up attempted. |
| Digium TDM400P, analogue | `wctdm` | three point one point zero | six point eight | Ubuntu noble | `UNVERIFIED` | — | — | Driver present in the image; no bring-up attempted. |
| Xorcom Astribank | `xpp`, `xpd_fxo`, `xpd_fxs`, `xpd_pri`, `xpd_bri` | three point one point zero | six point eight | Ubuntu noble | `UNVERIFIED` | — | — | Driver present in the image; no bring-up attempted. |

## What the image actually carries

This part is verified, and it is a different claim from the one above: it says
what was compiled, not what was made to work.

The image build compiles the driver tree with the kernel module builder and
records what it produced. The last build produced thirty-two modules, including
every legacy Digium interface driver named in the matrix. That the module
exists and loads is necessary for a card to work and is nowhere near
sufficient.

## On the driver source

The driver project removed support for several of these older cards and later
restored it. The build selects its source by the kernel version it is building
against, because the fixes for recent kernels have not all reached a tagged
release. The specific dates and version numbers behind that decision are
recorded in `docs/competitive-benchmark.md`; where they could not be confirmed
against a primary source they are marked there as unverified, and they should
be read that way here too.

## Support matrix

Which combinations this product intends to support, as distinct from which have
been shown to work.

| | Intended | Shown |
|---|---|---|
| Distribution in the image | Ubuntu noble, long term support | Boots, reaches a login prompt, carries the engine and drivers |
| Kernel | six point eight generic, and older kernels on an existing installation | Only six point eight, and only in an emulator |
| Telephony engine | Twenty, as packaged by the distribution | Installed in the image; not driven against a real card |
| Machines | Older generation hardware with legacy Digium cards, and modern hardware | Emulated machine only. No physical machine of either kind. |

A combination absent from this table is not unsupported. It is unexamined,
which is a different and more honest thing to say.
