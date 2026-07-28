# Enterprise Interface Assessment

The current state of the Crossbar console, measured rather than described, and
what has to change for it to become a communication operations platform.

Every score below is supported by something a reader can check in the
repository. Where a number appears it was counted, not estimated. Nothing here
is a plan for work that has already been done: this is the state of the console
*before* the transformation begins, recorded so that the change can be judged.

---

## A. Repository map

### Frontend

| File | Lines | What it is |
| --- | ---: | --- |
| `web/index.html` | four hundred eighty-seven | the entire console, one document, sixteen views |
| `web/js/dashboard.js` | one thousand five hundred thirty-nine | view rendering, state, every screen |
| `web/js/forms.js` | two hundred eighty-one | schema-driven form generation |
| `web/js/numerals.js` | two hundred sixty-one | the browser half of the numeral rule |
| `web/js/socket.js` | two hundred forty-nine | the live connection and its reconnection |
| `web/css/dashboard.css` | five hundred sixty-two | all styling |

Three thousand three hundred seventy-nine lines in total. No build step, no
framework, no dependency, and nothing fetched from a network. That is a genuine
asset and the transformation must not spend it.

### Backend surface the console consumes

Twenty-nine routes under `/api`, listed in `appliance/api.py`. Reads cover
state, hardware, trunks, tasks, sessions, constraints, configuration, drift,
logs, calls, schema, health and transport security. Writes cover the session,
configuration, rendering, adoption, trunk control, task invocation, the sixteen
privileged system verbs, entity creation and deletion, backup, restore and
certificate installation.

The live channel is a single socket carrying `state.changed`, heartbeat, and
the three bypass topics for alarms, engine loss and driver-stage failure.

### Existing components

There is no component library. `dashboard.js` builds elements inline with
helpers named `cell`, `row`, `card` and `field`. The same visual object is
constructed in several places with no shared definition, which is the root of
most of what follows.

### Existing tokens

Ten custom properties: `--ink`, `--ink-soft`, `--paper`, `--card`, `--rule`,
`--accent`, `--good`, `--warn`, `--bad`, `--shadow`. These are named for what
they look like rather than for what they mean, so there is no way to express
"the surface a dialog sits on" or "the border of a critical alert" without
picking a colour by eye.

### Existing screens

Sixteen views, flat, no grouping: overview, calls, history, extensions, trunks,
queues, conferences, hardware, firewall, security, system, configuration,
constraints, tasks, logs, backup.

---

## B. Current state, scored

Scores are out of ten. Each is followed by the evidence it rests on.

| Dimension | Score | Evidence |
| --- | ---: | --- |
| Visual consistency | 4 | Ten colour variables, none semantic. Status is expressed with `.good` / `.warn` / `.bad`, so a designer cannot distinguish "warning" from "maintenance" from "unknown" — the vocabulary does not exist. |
| Information architecture | 3 | Sixteen sibling views in one flat list, ordered by when they were built. A NOC operator wanting service health and an administrator wanting a dial plan land in the same undifferentiated menu. |
| Accessibility | 2 | Three `aria-` attributes, one `role`, one `aria-live`, no skip link, no `tabindex`, no `alt` — in four hundred eighty-seven lines of markup. Status is carried by colour alone. Nothing has been tested with a keyboard or a screen reader. |
| Responsive design | 3 | One media query, at eight hundred twenty pixels. Below it the desktop layout is narrowed rather than reorganised. There is no mobile operational path at all. |
| Error prevention | 5 | Genuinely good in one place: destructive verbs are marked `disruptive` and confirmed. But there is no preview, no impact analysis, no rollback, and a network change can strand the administrator. |
| Operational visibility | 5 | The live/reconnecting/stale indicator is better than most of the field and is a real strength. Against it: no service-level health at all. An operator reads process states and infers. |
| Workflow efficiency | 3 | No search, no command palette, no keyboard shortcuts, no bulk actions, no saved views, no clone. Every task is navigate-then-fill. |
| Enterprise readiness | 2 | One role, no permissions, no audit trail, no incident model, no multi-site concept. |
| Maintainability | 4 | One one-thousand-five-hundred-line file renders every view. Adding a screen means extending it. The absence of components is the constraint on everything else. |
| Product identity | 4 | Coherent and plain, but it reads as a competent admin panel rather than as a platform an enterprise would trust with its telephony. |
| **Overall** | **3.5** | |

### What is genuinely strong and must survive

These are not faint praise. They are the reasons this product is worth
transforming rather than replacing.

- **The live connection model.** Three distinguishable states — live,
  reconnecting, stale — where the field generally offers one. A console that
  can say "I cannot vouch for what I am showing you" is doing something most
  monitoring tools do not.
- **Schema-driven forms.** The console renders from the same schema the
  appliance validates against, so a field cannot exist in one and not the
  other. This is the correct foundation for the guided workflows to come.
- **No dependencies, no build, no network.** Every enterprise comparator named
  in the mandate would fail an air-gapped installation. This one does not.
- **The privileged verb vocabulary.** Sixteen verbs, validated three times over,
  with no shell anywhere on the path. Safe operations have somewhere to stand.

---

## C. Prioritised backlog

### P0 — critical

**P0.1 Semantic token layer.** *Benefit:* every later change becomes possible;
without it there is no vocabulary for status, elevation or density.
*Risk:* low, additive. *Complexity:* medium. *Depends on:* nothing.
*Accepts when:* no component file contains a colour literal, and light, dark and
high-contrast all render from tokens alone.

**P0.2 Accessibility floor.** *Benefit:* the console becomes operable at all for
keyboard and screen-reader users; today it is not.
*Risk:* low. *Complexity:* medium. *Depends on:* P0.1 for focus tokens.
*Accepts when:* every workflow completes without a mouse, every status carries a
non-colour indicator, landmarks and a skip link exist, and focus is visible
everywhere.

**P0.3 Component primitives.** *Benefit:* ends the inline-construction pattern
that makes every screen a bespoke artefact. *Risk:* medium — `dashboard.js`
queries specific selectors and they must keep working. *Complexity:* high.
*Depends on:* P0.1.

### P1 — high

**P1.1 Application shell** — top bar with appliance and environment identity,
grouped side navigation, breadcrumbs, page headers, global status bar.
*Depends on:* P0.3.

**P1.2 Task-oriented information architecture** — the seven workspaces
(Overview, Monitor, Configure, Infrastructure, Operations, Security,
Administration) replacing the flat sixteen. *Risk:* high; this is where users
will notice change most, and every existing deep link must still resolve.

**P1.3 Service health model** — a computed view of voice, inbound, outbound,
internal, hardware, network, backup and security, so an operator reads service
state rather than inferring it from processes. *Note:* this needs backend work,
not only interface work.

**P1.4 Structured errors** — the framework in which every failure carries cause,
impact, evidence and next action.

**P1.5 Data table standard** — sorting, filtering, saved views, bulk actions,
sticky headers, and a real small-screen behaviour rather than a scroll bar.

### P2 — medium

**P2.1 Command palette and universal search.** **P2.2 Entity detail pages** with
the standard tab set. **P2.3 Guided workflows** for extension and trunk
creation. **P2.4 Safe destructive-action framework** — preview, impact,
rollback timer, verification. **P2.5 Responsive operational paths** for mobile.
**P2.6 Observability charts**, colour-blind safe, with accessible data tables
beside them.

### P3 — strategic

**P3.1 Roles and permissions.** **P3.2 Immutable audit trail.** **P3.3 Incident
model.** **P3.4 Dependency maps.** **P3.5 Multi-site readiness.** **P3.6
Optional assistive diagnostics**, never mandatory and never able to act
destructively without approval.

---

## D. Target architecture

```
web/
  css/
    tokens.css        every semantic token, and the three themes
    components.css    primitives, built only from tokens
    dashboard.css     screen composition, consuming both
  js/
    numerals.js       unchanged in role: the browser half of the numeral rule
    socket.js         unchanged in role: the live connection
    forms.js          extended with presentation metadata from the schema
    dashboard.js      progressively reduced as views move out of it
  design-system.html  the offline showcase, openable without a server
```

**Token structure.** Two layers. A primitive layer naming raw values, and a
semantic layer naming meanings that reference it. Components may only use the
semantic layer, which is what makes a theme a change in one file.

**Component structure.** Each primitive is a class prefixed `mx-`, styled only
from tokens, with variants and states as modifier classes. No component
contains a colour literal, and a test enforces that.

**State management.** No change. The socket remains the source of live state and
the existing store remains the shape it arrives in. The mandate does not ask for
a state library and adding one would cost the air-gap property for nothing.

**API adaptation.** No breaking change. New capability arrives as new routes and
as presentation metadata added to the existing schema, so an older console and a
newer appliance keep working together.

**Testing.** Three layers on top of what exists: token and component assertions
in Python against the files; browser assertions in the existing Playwright
harness for keyboard, focus and live regions; and visual checks of the showcase
page across themes.

**Migration.** Strictly incremental. Tokens land first and are consumed by the
existing stylesheet, so the console looks almost unchanged and is measurably
more consistent. Components land next and views adopt them one at a time. The
navigation change comes only once the components it needs exist. At no point is
there a rewrite, and at every point the suite passes.

---

## What this assessment does not claim

- **No user research was done.** The personas in the mandate are taken as
  given. Nothing here has been validated with a NOC operator or a field
  technician.
- **No accessibility audit tool was run.** The accessibility score counts
  markup features present and absent. That is enough to establish the floor is
  low; it is not a substitute for testing with a screen reader.
- **The scores are judgements.** The evidence under each is checkable; the
  number attached to it is an opinion, offered so that progress can be measured
  against something rather than nothing.
