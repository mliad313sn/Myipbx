# Reporting and Interface Assessment

What an administrator can ask this console, measured against what the
established products let an administrator ask theirs, and what had to be built
to close the distance.

## How to read this

Every claim about another product names a file that was opened and read, with
the date. Claims that could not be established that way are marked
**UNVERIFIED — needs human confirmation** and are stated as gaps, never as
facts. No price, no benchmark figure and no market-share number appears
anywhere in this document, because none could be read from a primary source.

Sources are listed at the end. Read **28 July 2026**.

---

## Part one — the question

A private branch exchange is not finished when calls connect. The people who
own one have to answer questions about it, every month, to somebody who is not
a telephony engineer:

- How many calls did we take last month, and how many did we miss?
- Which extension answers the most, and which never picks up?
- What hour of the day are we busiest, and are we staffed for it?
- How long is the average conversation, and is it getting longer?
- Which trunk carries our traffic, and what happens if it fails?

None of these is a call. All of them are questions about *many* calls, and none
of them can be answered by looking at a list of the last hundred.

## Part two — what the field ships

### FreePBX, the reference implementation

The CDR module's single page **[S1]** builds a search form offering:

- a call-date span down to the minute — day, month, year, hour, minute, from
  and to
- duration, with a comparison
- disposition — answered, busy, failed, no answer, cancel — and a **not**
  checkbox that inverts it
- caller identity number and name, destination number, destination identity
  name, outbound caller identity number and name, direct inward dialling
  number, account code, user field, and channel technology
- a **group by** with sixteen values: minute, ten minutes, hour, hour of day,
  day of week, day, week, month, account code, user field, destination number,
  direct inward dialling number, caller identity number and name, outbound
  caller identity number and name, and destination caller identity name

and four output types, chosen by checkbox: the search itself, a **CSV file**, a
**call graph**, and **concurrent calls** **[S1]**. Results paginate at a hundred
rows per page, and each row that has a recording can be played in the browser or
downloaded **[S1]**.

### FusionPBX, and the metric vocabulary the field has settled on

Three separate menu entries, not one: *Call Detail Records*, *CDR Statistics*
and *Extension Summary* **[S2]**.

The extension summary is the interesting one, because its column list is the
industry's working definition of what a telephony report is **[S3]**:

> extension, description, number alias, domain, inbound calls, inbound
> duration, answered inbound, outbound calls, outbound duration, answered
> outbound, missed, no answer, busy, voicemail, and **ALOC**

over a date range with **presets**, and a toggle for whether internal calls
count. The statistics page adds **ASR** and a per-hour and per-day volume with
minutes and missed counts **[S4]**.

Two of those are terms of art worth naming plainly, because they are what a
telephony report is judged on:

- **ASR**, answer seizure ratio — of the calls attempted, the share that were
  answered. The single number that says whether a system is working.
- **ALOC**, average length of conversation — the mean talk time of the calls
  that *were* answered, which is not the same as the mean duration of all
  calls, because a thousand unanswered calls of six seconds each would drag
  that toward zero and tell you nothing.

### The others

Issabel's menu structure could not be read: the repository path tried returned
not found **[S5]**. **UNVERIFIED — needs human confirmation.**

3CX, Grandstream's unified communication manager, and VitalPBX's commercial
report modules are closed or partly closed, and nothing about their reporting
was read from a primary source. **UNVERIFIED — needs human confirmation.**

## Part three — what this console had

One view, *call history*. It issued a single request with a fixed limit of a
hundred and one free-text box, and drew six columns: started at, source,
destination, duration, talk time, disposition.

Measured against the section above, what was missing was not a feature. It was
the entire category:

| | FreePBX | FusionPBX | Crossbar, before |
| --- | :---: | :---: | :---: |
| filter by date range | yes **[S1]** | yes **[S3]** | **no** |
| filter by disposition | yes **[S1]** | — | **no** |
| filter by direction | — | yes **[S3]** | **no** |
| group by hour of day | yes **[S1]** | yes **[S4]** | **no** |
| group by day | yes **[S1]** | yes **[S4]** | **no** |
| per-extension summary | yes **[S1]** | yes **[S3]** | **no** |
| answer rate (ASR) | — | yes **[S4]** | **no** |
| average conversation (ALOC) | — | yes **[S3]** | **no** |
| a chart of anything | yes **[S1]** | — | **no** |
| export to a file | yes **[S1]** | — | **no** |
| more than a hundred rows | yes, paginated **[S1]** | — | **no** |

Ten of eleven. The console could show an engineer the last hundred calls and
could not answer a single question an owner would ask.

Three further gaps, outside reporting, found by reading this console's own
entity views against the same standard:

1. **No table could be searched, sorted or counted.** Every entity list —
   extensions, trunks, routes, groups, menus, queues — rendered every record in
   schema order with no filter box, no sortable column and no count. A site
   with two hundred extensions had a single unbroken table and no way to find
   one.
2. **No list could be exported.** Not the extensions, not the routes, not the
   firewall. An administrator asked for "the extension list" had to read it off
   the screen.
3. **The call history could not be narrowed by anything but a substring.** Not
   by date, not by disposition, not by direction.

## Part four — what was built

### The reporting engine

`appliance/reports.py`, standard library only, reading the same call detail
records the history view reads. It computes, over a chosen window:

- **the summary** — calls, answered, no answer, busy, failed and cancelled;
  answer seizure ratio; total and average conversation length; the longest
  call; total ring time and average time to answer
- **by extension** — inbound and outbound counts and durations, answered each
  way, missed, and per-extension answer rate and average conversation
- **by destination**, **by trunk or context**, and **by disposition**
- **by hour of day** and **by day**, each carrying volume, answered, missed and
  minutes

Windows are named rather than typed where a name will do — today, yesterday,
this week, this month, the last seven days, the last thirty days — with an
explicit from and to behind them.

### The reports view

A section in the console with the window control, the summary as tiles, an
hour-of-day distribution drawn as inline scalable vector graphics from the same
figures the table shows, and every breakdown as a sortable table. The chart is
described in text beside itself, so nothing in it is available only to somebody
who can see it.

### Export

Any report, and any entity list, downloads as a comma separated file. The
export carries the digits, not the spelled words: a file that is going to be
opened in a spreadsheet is not prose, and the same rule that keeps a port
typeable keeps a report's figures summable. The screen keeps the words.

### The tables

Every entity table now has a filter box, sortable columns and a count of what
is shown against what exists.

## Part five — the six gaps, and what was done about them

The first version of this document ended with six things this product did not
have. All six were built. What each one turned out to require is worth
recording, because in four of the six the interesting part was a refusal rather
than a feature.

### Queue and agent reporting — built

Offered, answered, abandoned, average and longest wait, service level, and a
per-member breakdown separating *answered* from *rang out*.

Drawn from the engine's **queue log**, which is a different file in a different
format from the call records, and had to be: a call that waited four minutes and
gave up appears in the call records as one unanswered call, and nothing in that
record says it waited. Because it is a different file it is a different request
and a different availability — a site with queues and no call records still has
a queue report.

The service level is measured against the queue's **own** agreed number of
seconds, which is a field on the queue and is written into the engine's
configuration, so the engine and the report cannot come to disagree about what
"in time" means. And the reasons callers stopped waiting are kept apart rather
than summed: a queue nobody is staffing and a queue people give up on are
different faults with different answers.

### Voicemail in reports — built

A call that rang out and a call that left a message are both recorded as
unanswered, and they are not the same thing: one reached somebody's attention.
The only place the difference survives is the last application the engine ran.

### Cost and rating — built

A rate table an operator edits: prefix, currency, connection charge, rate a
minute, billing increment, minimum. Longest prefix wins; an empty prefix is the
floor.

Three decisions worth naming:

- **Time is billed in whole increments.** A carrier selling by the minute
  charges a minute for a call of four seconds; dividing four by sixty would be
  out by a factor of fifteen on the calls a site makes most of.
- **Money is `Decimal`, never a floating point number.** A tenth of a penny
  cannot be represented in binary, and a quarter of a million calls a year is
  enough for that to show.
- **A call no rate covers is unrated, never free.** A row whose calls were all
  unrated reads "not rated" rather than showing a zero, and a rate table naming
  two currencies produces no cost at all with the reason on screen, because
  adding two currencies gives a number that is not an amount of anything.

### Call recording and playback — built

Per-extension, off until turned on, and the setting says — where the decision
is made rather than in a manual — that in most places a caller has to be told.
The appliance announces nothing itself.

Listing comes from the file names, with no database beside them, so a row in a
table and a file on disk cannot come to disagree. Serving one is the only route
in this appliance that hands a file off the disk to a browser, so two
independent rules both have to hold: the name must match the shape the
appliance itself writes, and the resolved path, after every symbolic link has
been followed, must still sit inside the recordings directory. Neither is
trusted alone, and each is tested against the other's blind spot.

Playing one is written to the audit journal. Retention runs as a task, because
recording fills a disk faster than anything else here and a telephone system
that stops taking calls because its disk is full is worse than a recording
nobody kept.

### Scheduled reports — built

Drawn on a daily, weekly or monthly schedule, kept on the appliance, and sent by
electronic mail where a destination is named. Two things kept deliberately
apart:

- **Drawing and keeping come first, sending second**, so a mail server that is
  down costs a delivery rather than the report.
- **Due-ness is decided against the calendar, not an elapsed interval.** A
  weekly report set for a Monday runs on the Monday, once, however many times
  the appliance restarted over the weekend.

Sending uses the standard library alone, will not send a password over an
unprotected connection unless that was chosen explicitly, and will not fail the
task because a server was unreachable.

### Per-user portal — built

An account scoped to one extension: its own calls, its own recordings, and
nothing else.

This is an authorisation boundary in the guard every route already passes
through, not a hidden menu — a portal that merely does not draw a link to the
rest of the appliance is a suggestion. Every administrator route refuses a
scoped account with a reason. The scope is on the session, put there when the
account signed in, and is never taken from the request: a portal that asked
which extension to show would be a portal that showed any of them.

A recording belonging to somebody else is refused exactly as one that does not
exist is, so the portal cannot be used to learn who spoke to whom. Credentials
are not configuration: they are hashed, they are not in the document, and they
are not in a backup or a support bundle.

## Part six — what is still not here

Stated as plainly as the six above were.

- **No pause or wrap-up reporting for members.** Answered and rang out are
  counted; time spent paused is not, because the appliance does not configure
  pausing.
- **No live wallboard.** The queue figures are drawn when the page is asked
  for; there is no display that updates itself on a wall.
- **No voicemail retrieval from the console.** Mailboxes are configured and
  calls that reached one are counted, but the messages themselves are collected
  from a telephone.
- **No stereo recording, and no on-demand start and stop.** Recording is a
  property of an extension, decided before the call.
- **No currency conversion.** One currency per appliance, enforced by refusing
  to total two.
- **The portal is read-only.** Its owner cannot change their own password from
  it; an administrator sets it.

## Part seven — one place this diverges on purpose

Constraint Two spells quantities in words. A report is almost entirely
quantities, so a Crossbar report reads "one thousand two hundred forty-three
calls, of which nine hundred eighty-one were answered" where every product in
the comparison would print two numerals.

This is slower to scan and it is the constraint working as specified, not a
defect. Two things keep it usable:

- identifiers keep their digits, so an extension is `201` and a dialled number
  is `+441632960123` in a report exactly as everywhere else; and
- the exported file carries digits throughout, because a spreadsheet cannot sum
  a word.

Anybody weighing this product against the field should weigh that consciously.
It is the single largest legibility difference between this console and every
other console in the comparison.

---

## Sources

Every entry was fetched and read on **28 July 2026**.

- **[S1]** FreePBX `cdr`, branch `release/17.0`, `page.cdr.php` (one thousand
  three hundred two lines) — the search form's date span, duration, disposition
  with its `disposition_neg` inversion, and the identity and account fields; the
  `group` select's sixteen `<option>` values; the four report-type checkboxes
  `need_html`, `need_csv`, `need_chart` and `need_chart_cc`; `$db_result_limit =
  100`; and the `download_audio` and `cdr_play` actions.
  <https://github.com/FreePBX/cdr>
- **[S2]** FusionPBX `fusionpbx`, branch `master`, `app/xml_cdr/app_menu.php` —
  three menu entries, `Call Detail Records`, `CDR Statistics` and
  `Extension Summary`, pointing at `xml_cdr.php`,
  `xml_cdr_statistics.php` and `xml_cdr_extension_summary.php`.
  <https://github.com/fusionpbx/fusionpbx>
- **[S3]** FusionPBX `fusionpbx`, branch `master`,
  `app/xml_cdr/xml_cdr_extension_summary.php` — the label set quoted above,
  including `label-aloc`, `label-missed`, `label-answered_inbound`,
  `label-answered_outbound`, `label-inbound_duration`,
  `label-outbound_duration`, `label-voicemail`, `label-preset` and
  `label-include_internal`.
- **[S4]** FusionPBX `fusionpbx`, branch `master`,
  `app/xml_cdr/xml_cdr_statistics.php` (three hundred fifty-six lines) — the
  column headers `label-hours`, `label-days`, `label-time`, `label-volume`,
  `label-minutes`, `label-missed`, `label-asr` and `label-aloc`.
- **[S5]** Issabel — `https://github.com/asternic/issabel4/tree/master/menus`
  returned not found. No Issabel source was read.
  **UNVERIFIED — needs human confirmation.**
