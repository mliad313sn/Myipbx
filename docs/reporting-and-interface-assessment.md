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

## Part five — where this product remains behind, stated plainly

- **No call recording, and therefore no recording playback.** FreePBX plays and
  downloads recordings from the report row **[S1]**. This appliance records
  nothing, so there is nothing to play. That is a deliberate posture, not an
  oversight, but it is a real difference and an owner comparing the two will
  notice it.
- **No queue or agent reporting.** Calls offered, answered, abandoned, service
  level, agent talk and pause time. The queue entity exists; the report does
  not.
- **No scheduled or emailed reports.** Every report here is pulled by a person
  looking at a screen.
- **No voicemail, and so no voicemail column** where FusionPBX has one **[S3]**.
- **No cost or rating.** No tariff table, no per-call cost.
- **No per-user portal.** Every account that can see a report can see every
  report; there is no view scoped to one extension's owner.

## Part six — one place this diverges on purpose

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
