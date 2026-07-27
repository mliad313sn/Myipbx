/*
 * User acceptance, driven at the level a person actually works at.
 *
 * The other browser script proves the console renders and does not throw. This
 * one asks the harder question: could somebody actually operate this appliance?
 * It walks the console with the keyboard alone, fills forms and gets them
 * wrong on purpose, opens the dialogue that destroys things, shrinks the window
 * to a telephone, turns the contrast up, and measures what it finds.
 *
 * Nothing here is a unit test. Every check corresponds to something a person
 * does, and a failure means a person would be stopped or misled.
 *
 * Invoked by tests/test_uat.py, which owns the appliance under test.
 * Reports one JSON document on the final line of standard output.
 */

'use strict';

const { chromium } = require('playwright');

const [, , baseUrl, username, password] = process.argv;

const report = {
    findings: [],
    keyboard: {},
    contrast: [],
    viewport: {},
    themes: {},
    pageErrors: [],
    consoleErrors: [],
    failedRequests: [],
    nonText: [],
    stage: 'starting',
};

/* A finding is something a person would hit, not something a test dislikes. */
const finding = function (severity, title, detail) {
    report.findings.push({ severity: severity, title: title, detail: detail });
};

/* Hand the report back on one line, then let the process end on its own.
 *
 * Calling process.exit here discarded the tail of the line: a write to a pipe
 * larger than the pipe's buffer completes asynchronously, and exit does not
 * wait for it. The result was a report that parsed as truncated JSON exactly
 * once the contrast sweep grew past sixty four kilobytes, which reads as the
 * run having crashed rather than as the harness having cut its own output off.
 * An exit code set here is honoured when the event loop drains. */
const emit = function () {
    console.log('RESULT ' + JSON.stringify(report));
    process.exitCode = 0;
};

const VIEWS = [
    'overview', 'calls', 'history',
    'extensions', 'trunks', 'ring_groups',
    'inbound_routes', 'outbound_routes', 'time_conditions',
    'ivr_menus', 'queues', 'conferences',
    'hardware', 'system', 'firewall', 'security',
    'configuration', 'tasks', 'logs', 'backup', 'constraints',
];

/* The relative luminance formula, so contrast is measured rather than judged. */
const CONTRAST_HELPERS = `
    function channel(value) {
        var c = value / 255;
        return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    function parse(colour) {
        var match = colour.match(/rgba?\\(([^)]+)\\)/);
        if (!match) { return null; }
        var parts = match[1].split(',').map(function (p) { return parseFloat(p.trim()); });
        return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
    }
    function luminance(c) {
        return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
    }
    function ratio(front, back) {
        var f = parse(front), b = parse(back);
        if (!f || !b) { return null; }
        var lf = luminance(f), lb = luminance(b);
        var hi = Math.max(lf, lb), lo = Math.min(lf, lb);
        return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
    }
    /* Composite the stack rather than stopping at the first tint.
     *
     * A translucent surface over a dark header is not the colour the browser
     * paints; taking it literally reported a button as one to one against
     * itself. Each layer is blended onto the one beneath until an opaque one
     * is reached. */
    function behind(element) {
        var layers = [];
        /* Start at the element, not its parent. Text painted on a button sits
         * on the button's own background; measuring it against the panel
         * underneath reported white on white for a filled button, which is the
         * measurement being wrong rather than the button. */
        var node = element;
        while (node) {
            var parsed = parse(getComputedStyle(node).backgroundColor);
            if (parsed && parsed.a > 0) {
                layers.push(parsed);
                if (parsed.a >= 0.999) { break; }
            }
            node = node.parentElement;
        }
        var base = { r: 255, g: 255, b: 255, a: 1 };
        for (var i = layers.length - 1; i >= 0; i -= 1) {
            var over = layers[i];
            base = {
                r: over.r * over.a + base.r * (1 - over.a),
                g: over.g * over.a + base.g * (1 - over.a),
                b: over.b * over.a + base.b * (1 - over.a),
                a: 1
            };
        }
        return 'rgb(' + Math.round(base.r) + ', ' + Math.round(base.g) + ', ' + Math.round(base.b) + ')';
    }

    /* Text this element paints itself, ignoring what its children paint.
     * An element with no text of its own has no text contrast to measure. */
    function ownText(element) {
        var text = '';
        for (var i = 0; i < element.childNodes.length; i += 1) {
            var child = element.childNodes[i];
            if (child.nodeType === 3) { text += child.nodeValue; }
        }
        return text.trim();
    }
`;

(async () => {
    const browser = await chromium.launch({ args: ['--no-sandbox'] });
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
    const page = await context.newPage();

    page.on('pageerror', function (error) { report.pageErrors.push(String(error)); });
    page.on('console', function (message) {
        if (message.type() === 'error') { report.consoleErrors.push(message.text()); }
    });
    page.on('requestfailed', function (request) {
        report.failedRequests.push(request.url() + ' ' + String(request.failure() && request.failure().errorText));
    });

    try {
        // -- signing in, with the keyboard only ---------------------------
        report.stage = 'sign-in';
        await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });

        /* A person arriving at a console reaches for Tab. Where does it land,
         * and can they see where they are? */
        await page.keyboard.press('Tab');
        const firstStop = await page.evaluate(function () {
            var active = document.activeElement;
            if (!active) { return null; }
            var style = getComputedStyle(active);
            return {
                tag: active.tagName.toLowerCase(),
                text: (active.textContent || '').trim().slice(0, 40),
                id: active.id || null,
                outlineWidth: style.outlineWidth,
                outlineStyle: style.outlineStyle,
            };
        });
        report.keyboard.firstStop = firstStop;
        if (!firstStop) {
            finding('high', 'Nothing receives focus on the first Tab',
                'A keyboard user pressing Tab on the sign in page focuses nothing.');
        } else if (firstStop.outlineStyle === 'none' || firstStop.outlineWidth === '0px') {
            finding('high', 'The first focusable element shows no focus ring',
                'Focused ' + firstStop.tag + ' with outline ' + firstStop.outlineStyle +
                ' ' + firstStop.outlineWidth + '. A keyboard user cannot see where they are.');
        }

        /* Sign in without touching the mouse at all. */
        await page.fill('#username', username);
        await page.fill('#password', password);
        await page.press('#password', 'Enter');
        await page.waitForSelector('#console', { state: 'visible', timeout: 20000 });
        report.keyboard.signedInWithKeyboard = true;

        // -- every view renders, and says something ----------------------
        report.stage = 'views';
        report.views = {};
        for (const view of VIEWS) {
            const link = await page.$('[data-view="' + view + '"]');
            if (!link) {
                finding('medium', 'A section is not reachable', 'No navigation control for ' + view + '.');
                continue;
            }
            await link.click();
            await page.waitForTimeout(220);
            const state = await page.evaluate(function (name) {
                var section = document.querySelector('#view-' + name);
                if (!section) { return { present: false }; }
                var text = (section.innerText || '').trim();
                return {
                    present: true,
                    visible: section.offsetParent !== null || section.getClientRects().length > 0,
                    characters: text.length,
                    empty: text.length < 12,
                    firstLine: text.split('\\n')[0].slice(0, 90),
                };
            }, view);
            report.views[view] = state;
            if (state.present && state.empty) {
                finding('medium', 'A section renders almost nothing',
                    'The ' + view + ' section shows ' + state.characters +
                    ' characters. An empty screen with no explanation tells an operator nothing.');
            }
        }

        // -- keyboard-only navigation of the whole console ---------------
        report.stage = 'keyboard-walk';
        const walk = await page.evaluate(function () {
            /* Collect the focusable order the way a browser would give it. */
            var selector = 'a[href], button:not([disabled]), input:not([disabled]), ' +
                           'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
            var nodes = Array.prototype.slice.call(document.querySelectorAll(selector));
            var reachable = nodes.filter(function (node) {
                return node.offsetParent !== null || node.getClientRects().length > 0;
            });
            var unlabelled = reachable.filter(function (node) {
                var name = (node.getAttribute('aria-label') || '').trim() ||
                           (node.textContent || '').trim() ||
                           (node.getAttribute('title') || '').trim() ||
                           (node.getAttribute('alt') || '').trim();
                if (name) { return false; }
                if (node.id) {
                    var label = document.querySelector('label[for="' + node.id + '"]');
                    if (label && (label.textContent || '').trim()) { return false; }
                }
                return true;
            });
            var positiveTabindex = reachable.filter(function (node) {
                var value = parseInt(node.getAttribute('tabindex') || '0', 10);
                return value > 0;
            });
            return {
                focusableCount: reachable.length,
                unlabelled: unlabelled.map(function (n) {
                    return n.tagName.toLowerCase() + (n.className ? '.' + String(n.className).split(' ')[0] : '');
                }).slice(0, 12),
                positiveTabindexCount: positiveTabindex.length,
                /* Look for what a skip link does rather than what it is
                 * called. A previous run reported this missing because the
                 * class carries the design system's prefix; the console had
                 * one all along, and a check that reads a class name tests the
                 * naming convention instead of the behaviour. What matters is
                 * that some link near the top of the document points at a
                 * target that exists. */
                hasSkipLink: (function () {
                    var links = document.querySelectorAll('a[href^="#"]');
                    for (var i = 0; i < links.length && i < 5; i += 1) {
                        var target = links[i].getAttribute('href').slice(1);
                        if (target && document.getElementById(target)) { return true; }
                    }
                    return false;
                }()),
                landmarks: {
                    banner: !!document.querySelector('[role="banner"], header'),
                    nav: !!document.querySelector('nav, [role="navigation"]'),
                    main: !!document.querySelector('main, [role="main"]'),
                },
                h1Count: document.querySelectorAll('h1').length,
                liveRegions: document.querySelectorAll('[aria-live]').length,
            };
        });
        report.keyboard.walk = walk;
        if (walk.unlabelled.length) {
            finding('high', 'Focusable controls carry no accessible name',
                walk.unlabelled.length + ' reachable controls have no name: ' +
                walk.unlabelled.join(', ') + '. A screen reader announces these as "button".');
        }
        if (walk.positiveTabindexCount) {
            finding('medium', 'A positive tabindex overrides the natural order',
                walk.positiveTabindexCount + ' elements carry a positive tabindex.');
        }
        if (!walk.hasSkipLink) {
            finding('medium', 'No skip link', 'A keyboard user must tab through the whole navigation on every view.');
        }
        if (walk.h1Count !== 1) {
            finding('low', 'The page does not have exactly one first level heading',
                'Found ' + walk.h1Count + '.');
        }

        // -- a form, got wrong on purpose --------------------------------
        // -- a control that redraws under the person using it -------------
        /* The overview redraws from every pushed snapshot, about once a second
         * on a busy appliance. A redraw that rebuilds its list takes the
         * control out from under a keyboard user before they can press it:
         * reachable in principle, unusable in practice. */
        report.stage = 'focus-across-redraw';
        const overviewForFocus = await page.$('.nav-item[data-view="overview"]');
        if (overviewForFocus) {
            await overviewForFocus.click();
            await page.waitForTimeout(500);
        }
        /* Driven by a real change rather than by reaching into the page.
         * Saving anything advances the appliance's sequence, which publishes a
         * snapshot on the socket, which is what redraws the overview. That is
         * the same path a busy appliance takes every second, so this measures
         * the thing an operator actually meets. */
        const focused = await page.evaluate(function () {
            var target = document.querySelector('[data-focus-key]');
            if (!target) { return null; }
            target.focus();
            return {
                key: target.getAttribute('data-focus-key'),
                took: document.activeElement === target,
            };
        });

        let focusAcrossRedraw = { tested: false, reason: 'no keyed control is on screen' };
        if (focused && focused.took) {
            await page.evaluate(async function () {
                await fetch('/api/entities/extensions', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        number: '299', name: 'A Redraw Probe', technology: 'PJSIP',
                        voicemail: false, ring_seconds: 20, enabled: true,
                    }),
                });
            });
            await page.waitForTimeout(1200);
            focusAcrossRedraw = await page.evaluate(function (key) {
                var active = document.activeElement;
                return {
                    tested: true,
                    key: key,
                    focusedBefore: true,
                    keptFocus: !!(active && active.getAttribute &&
                        active.getAttribute('data-focus-key') === key),
                    landedOn: active === document.body ? 'the document body'
                        : String((active && active.className) || (active && active.tagName)),
                };
            }, focused.key);
        }
        report.focusAcrossRedraw = focusAcrossRedraw;
        if (focusAcrossRedraw.tested && focusAcrossRedraw.focusedBefore &&
            !focusAcrossRedraw.keptFocus) {
            finding('high', 'A redraw takes the control out from under a keyboard user',
                'Focus was on ' + focusAcrossRedraw.key + ' and a state update moved it to ' +
                focusAcrossRedraw.landedOn + '. The console redraws about once a second, so ' +
                'this control cannot be operated from the keyboard at all.');
        }

        // -- what the tiles say when the engine is not there --------------
        /* The appliance under test is deliberately pointed at an engine that
         * is not listening, which is the condition an operator meets at three
         * in the morning. What the overview says then is the whole point of
         * this check: "zero active calls" and "call state cannot be read" are
         * different sentences, and only one of them is true. */
        report.stage = 'stale-state';
        const overview = await page.$('.nav-item[data-view="overview"]');
        if (overview) {
            await overview.click();
            await page.waitForTimeout(600);
        }
        const staleness = await page.evaluate(async function () {
            var response = await fetch('/api/state', { credentials: 'same-origin' });
            var state = await response.json();
            var figure = document.querySelector('#figure-active-calls');
            var caption = document.querySelector('#caption-answered');
            return {
                engineConnected: state.engine ? state.engine.connected : null,
                figureText: figure ? (figure.textContent || '').trim() : null,
                figureMarkedInvalid: figure ? figure.getAttribute('aria-invalid') : null,
                figureClass: figure ? String(figure.className) : null,
                captionText: caption ? (caption.textContent || '').trim() : null,
            };
        });
        report.staleness = staleness;
        if (staleness.engineConnected === false) {
            if (/^zero\b/.test(staleness.figureText || '')) {
                finding('critical', 'A count is reported as zero when it is unknown',
                    'The telephony engine is not connected, so no channel can be read, and the ' +
                    'overview reads "' + staleness.figureText + '". An operator is told the ' +
                    'building is quiet on a system that may be carrying every call it can.');
            }
            if (staleness.figureMarkedInvalid !== 'true') {
                finding('high', 'A figure that cannot be read is not marked as such',
                    'The active call figure carries aria-invalid=' +
                    String(staleness.figureMarkedInvalid) + ', so a screen reader reads it ' +
                    'as a current value.');
            }
            if (!/engine/.test(staleness.captionText || '')) {
                finding('medium', 'Nothing says why the figure cannot be read',
                    'The caption reads "' + staleness.captionText + '".');
            }
        }

        report.stage = 'form-validation';
        await page.click('.nav-item[data-view="extensions"]');
        await page.waitForSelector('#view-extensions', { state: 'visible', timeout: 15000 });
        await page.waitForTimeout(400);

        const addButton = await page.$('#view-extensions button:has-text("add extension")');
        if (!addButton) {
            finding('high', 'No way to create an extension from the console',
                'The extensions section offers no create control, so the claim that every ' +
                'operation is possible from the browser fails for the commonest one.');
        } else {
            await addButton.click();
            await page.waitForSelector('#form-holder-extensions form', { state: 'visible', timeout: 10000 });

            /* Get it wrong on purpose, the way a person does. */
            await page.fill('#field-extensions-number', 'not-a-number');
            await page.fill('#field-extensions-name', 'A Test Telephone');
            await page.click('#form-holder-extensions button[type="submit"]');
            await page.waitForTimeout(700);

            const validation = await page.evaluate(function () {
                var field = document.querySelector('#field-extensions-number');
                var holder = document.querySelector('#form-holder-extensions');
                /* The marked field's message, and only failing that any
                 * message at all. A comma separated selector returns whichever
                 * matches first in the document, so asking for both at once
                 * returned the empty slot above the offending field. */
                var message = holder
                    ? (holder.querySelector('.field.has-error .field-error') ||
                       holder.querySelector('.field-error'))
                    : null;
                var summary = holder ? holder.querySelector('[role="alert"], .form-error-summary') : null;
                var summaryVisible = !!(summary && !summary.hidden &&
                    summary.getClientRects().length && (summary.textContent || '').trim());
                return {
                    fieldFound: !!field,
                    ariaInvalid: field ? field.getAttribute('aria-invalid') : null,
                    ariaDescribedBy: field ? field.getAttribute('aria-describedby') : null,
                    messageShown: !!(message && (message.textContent || '').trim()),
                    messageText: message ? (message.textContent || '').trim().slice(0, 140) : null,
                    messageId: message ? (message.id || null) : null,
                    summaryPresent: summaryVisible,
                    focusIsOnTheField: document.activeElement === field,
                    focusedId: document.activeElement ? (document.activeElement.id || null) : null,
                };
            });
            report.validation = validation;

            if (!validation.messageShown) {
                finding('critical', 'A rejected form says nothing',
                    'Submitting an invalid extension number produced no visible message.');
            }
            if (validation.ariaInvalid !== 'true') {
                finding('high', 'A rejected field is not marked invalid',
                    'The field carries aria-invalid=' + String(validation.ariaInvalid) +
                    '. A screen reader user is never told which field is the problem.');
            }
            if (!validation.ariaDescribedBy) {
                finding('high', 'A validation message is not associated with its field',
                    'No aria-describedby links the message to the input, so it is never read out ' +
                    'when the field receives focus. The message shown was: ' +
                    String(validation.messageText));
            }
            if (!validation.focusIsOnTheField) {
                finding('medium', 'Focus is not moved to the first invalid field',
                    'Focus stayed on ' + String(validation.focusedId) +
                    ', so a keyboard user must hunt for the error.');
            }
            if (!validation.summaryPresent) {
                finding('medium', 'A refusal is not announced',
                    'The form shows no live region summarising what was rejected, so a ' +
                    'screen reader user is told nothing when the submission comes back.');
            }

            /* Two elements with one id is not a style problem. The label
             * association silently binds to whichever came first, so a person
             * editing a queue can be read the label of an extension. */
            const duplicateIds = await page.evaluate(function () {
                var seen = {};
                var repeated = [];
                Array.prototype.forEach.call(document.querySelectorAll('[id]'), function (node) {
                    if (seen[node.id]) {
                        if (repeated.indexOf(node.id) === -1) { repeated.push(node.id); }
                    }
                    seen[node.id] = true;
                });
                return repeated;
            });
            report.duplicateIds = duplicateIds;
            if (duplicateIds.length) {
                finding('high', 'The same identifier is used more than once',
                    'Repeated: ' + duplicateIds.join(', ') +
                    '. A label points at whichever came first, so the wrong field is named.');
            }

            /* Now get it right, and prove the happy path still works. */
            await page.fill('#field-extensions-number', '241');
            await page.click('#form-holder-extensions button[type="submit"]');
            await page.waitForTimeout(800);
            report.entityCreated = await page.evaluate(function () {
                return (document.querySelector('#view-extensions').innerText || '').indexOf('241') !== -1;
            });
            if (!report.entityCreated) {
                finding('critical', 'A valid extension could not be created',
                    'The form accepted a corrected value but the extension does not appear.');
            }
        }

        // -- the dialogue that destroys things ---------------------------
        report.stage = 'confirmation';
        const dialogState = await page.evaluate(function () {
            var shade = document.querySelector('.confirm-shade, [data-confirm-shade]');
            var dialog = document.querySelector('.confirm, [role="dialog"], [role="alertdialog"]');
            return {
                shadePresent: !!shade,
                shadeHidden: shade ? (shade.hasAttribute('hidden') ||
                    getComputedStyle(shade).display === 'none') : null,
                dialogRole: dialog ? dialog.getAttribute('role') : null,
                dialogModal: dialog ? dialog.getAttribute('aria-modal') : null,
                dialogLabelled: dialog ? !!(dialog.getAttribute('aria-labelledby') ||
                    dialog.getAttribute('aria-label')) : null,
            };
        });
        report.confirmation = dialogState;
        if (dialogState.dialogRole && dialogState.dialogModal !== 'true') {
            finding('medium', 'The confirmation dialogue is not marked modal',
                'role=' + dialogState.dialogRole + ' without aria-modal=true.');
        }
        if (dialogState.dialogRole && !dialogState.dialogLabelled) {
            finding('medium', 'The confirmation dialogue has no accessible name',
                'Neither aria-labelledby nor aria-label is present.');
        }

        // -- contrast, measured -------------------------------------------
        /* Four themes an operator can be handed by choice, and two more they
         * can be handed by their operating system without ever visiting the
         * switch. The second pair is the one that went wrong: "more contrast"
         * means move the ink away from the ground, and which way that is
         * depends on which ground is underneath, so the two have to be asked
         * about separately. */
        report.stage = 'contrast';
        const reportedNonText = {};
        const CONTRAST_VIEWS = ['overview', 'extensions', 'trunks', 'hardware', 'firewall'];
        const SCHEMES = [
            { name: 'light', theme: 'light' },
            { name: 'dark', theme: 'dark' },
            { name: 'high-contrast', theme: 'high-contrast' },
            { name: 'system light, more contrast', media: { colorScheme: 'light', contrast: 'more' } },
            { name: 'system dark, more contrast', media: { colorScheme: 'dark', contrast: 'more' } },
        ];
        for (const scheme of SCHEMES) {
            const theme = scheme.name;
            if (scheme.media) {
                /* The system preference blocks are scoped to a page that has
                 * not chosen a theme, so the choice has to be taken away
                 * before the preference can be seen at all. */
                await page.evaluate(function () {
                    document.documentElement.removeAttribute('data-theme');
                });
                await page.emulateMedia(scheme.media);
            } else {
                await page.emulateMedia({ colorScheme: 'light', contrast: 'no-preference' });
                await page.evaluate(function (name) {
                    document.documentElement.setAttribute('data-theme', name);
                }, scheme.theme);
            }
            await page.waitForTimeout(200);

            /* Measured across several sections rather than whichever happened
             * to be open. A stylesheet is only as good as its worst view, and
             * the tiles, the tables and the generated ruleset each paint
             * something the others do not. */
            const measured = [];
            for (const view of CONTRAST_VIEWS) {
                const item = await page.$('.nav-item[data-view="' + view + '"]');
                if (!item) { continue; }
                await item.click();
                await page.waitForTimeout(250);
                const batch = await page.evaluate(new Function(CONTRAST_HELPERS + `
                    var results = [];
                    /* Wide, because the narrow list was the reason a defect
                     * survived: the big figure on a tile is drawn in the brand
                     * colour and was never sampled, so brand-as-ink measured
                     * nothing at all until it was asked for by name. */
                    var samples = document.querySelectorAll(
                        'body, .nav-item, .tile, button, input, .lamp, .state-pill, a, ' +
                        '.figure, .caption, .hint, .summary, .subtitle, .link-text, .link-age, ' +
                        'label, th, td, li, h1, h2, h3, p, .field-help, .field-error');
                    var seen = {};
                    Array.prototype.slice.call(samples).forEach(function (element) {
                        if (element.getClientRects().length === 0) { return; }
                        if (!ownText(element)) { return; }
                        var style = getComputedStyle(element);
                        if (parseFloat(style.opacity) === 0) { return; }
                        var ground = behind(element);
                        /* Keyed on what is actually painted, not on the tag
                         * alone. Collapsing every paragraph into one sample
                         * meant the first paragraph on the page decided
                         * whether any paragraph anywhere was measured. */
                        var key = element.tagName + '|' +
                            String(element.className || '').split(' ').slice(0, 2).join('.') +
                            '|' + style.color + '|' + ground;
                        if (seen[key]) { return; }
                        seen[key] = true;
                        var r = ratio(style.color, ground);
                        if (r !== null) {
                            results.push({
                                element: element.tagName + '|' +
                                    String(element.className || '').split(' ')[0],
                                colour: style.color,
                                background: ground,
                                ratio: r,
                                fontSize: style.fontSize,
                                fontWeight: style.fontWeight,
                            });
                        }
                    });
                    return results;
                `));
                batch.forEach(function (sample) {
                    sample.view = view;
                    measured.push(sample);
                });
            }

            /* Only what is worth reading back: everything that fails, and
             * the ten tightest of the rest so a ratio drifting towards the
             * line is visible before it crosses it. The whole sweep is
             * thousands of samples and the report travels as one line. */
            const ranked = measured.slice().sort(function (a, b) { return a.ratio - b.ratio; });
            report.contrast.push({
                theme: theme,
                sampleCount: measured.length,
                samples: ranked.slice(0, 10),
            });

            /* Non-text contrast, which the text sweep cannot see.
             *
             * The link lamp carries no text of its own, so it was skipped by
             * every measurement above while being one of the two things on the
             * page an operator reads from across a room. A graphical indicator
             * needs three to one against what it sits on, and the lamp sits on
             * the brand ground rather than on a panel, which is exactly where a
             * status colour chosen against a white panel stops working. */
            const lamps = await page.evaluate(new Function(CONTRAST_HELPERS + `
                var results = [];
                var seen = {};
                function measure(element, label) {
                    if (element.getClientRects().length === 0) { return; }
                    var style = getComputedStyle(element);
                    var own = style.backgroundColor;
                    var parent = element.parentElement;
                    var ground = parent ? behind(parent) : 'rgb(255, 255, 255)';
                    var key = label + '|' + own + '|' + ground;
                    if (seen[key]) { return; }
                    seen[key] = true;
                    var r = ratio(own, ground);
                    if (r !== null) {
                        results.push({
                            element: label,
                            colour: own,
                            background: ground,
                            ratio: r,
                        });
                    }
                }

                /* Every state the lamp can be in, not only the one it happens
                 * to be in during the run. The link is live throughout an
                 * acceptance run by design, so measuring what is on screen
                 * measures one third of the indicator and leaves the two
                 * states an operator only sees when something is wrong — the
                 * two that matter most — never looked at. */
                Array.prototype.slice.call(document.querySelectorAll('.lamp, .mx-lamp'))
                    .forEach(function (element) {
                        var original = element.className;
                        ['live', 'reconnecting', 'stale', ''].forEach(function (state) {
                            element.className = 'lamp' + (state ? ' ' + state : '');
                            measure(element, 'lamp ' + (state || 'not connected'));
                        });
                        element.className = original;
                    });

                Array.prototype.slice.call(document.querySelectorAll('.state-pill'))
                    .forEach(function (element) {
                        measure(element, String(element.className || '').trim());
                    });
                return results;
            `));
            report.nonText.push({ theme: theme, samples: lamps });
            lamps.forEach(function (sample) {
                if (sample.ratio >= 3.0) { return; }
                const key = 'lamp' + theme + sample.element + sample.colour + sample.background;
                if (reportedNonText[key]) { return; }
                reportedNonText[key] = true;
                finding('high', 'A status indicator is not distinct enough from its ground',
                    theme + ': ' + sample.element + ' measures ' + sample.ratio + ':1 (' +
                    sample.colour + ' on ' + sample.background + '), needs 3:1.');
            });
            const reported = {};
            measured.forEach(function (sample) {
                /* Large text is eighteen point, or fourteen point bold. Both
                 * halves matter: a bold heading at nineteen pixels is large
                 * text and a light one at the same size is not. */
                const size = parseFloat(sample.fontSize);
                const weight = parseInt(sample.fontWeight, 10) || 400;
                const large = size >= 24 || (size >= 18.66 && weight >= 700);
                const needed = large ? 3.0 : 4.5;
                if (sample.ratio >= needed) { return; }
                const key = theme + sample.element + sample.colour + sample.background;
                if (reported[key]) { return; }
                reported[key] = true;
                finding('high', 'Text contrast below the required ratio',
                    theme + ', ' + sample.view + ': ' + sample.element + ' measures ' +
                    sample.ratio + ':1 (' + sample.colour + ' on ' + sample.background +
                    '), needs ' + needed + ':1.');
            });
        }
        await page.emulateMedia({ colorScheme: 'light', contrast: 'no-preference' });
        await page.evaluate(function () { document.documentElement.removeAttribute('data-theme'); });

        // -- a narrow screen, and a magnified one -------------------------
        report.stage = 'viewport';
        await page.setViewportSize({ width: 320, height: 640 });
        await page.waitForTimeout(300);
        const narrow = await page.evaluate(function () {
            var overflow = document.documentElement.scrollWidth > document.documentElement.clientWidth + 2;
            var nav = document.querySelector('nav, [role="navigation"]');
            var navReachable = nav ? (nav.getClientRects().length > 0) : false;
            var outside = Array.prototype.slice.call(
                document.querySelectorAll('button, a[href], input')
            ).filter(function (node) {
                var box = node.getBoundingClientRect();
                return box.width > 0 && (box.left < -4 || box.right > window.innerWidth + 4);
            });
            /* Being outside the viewport is only a fault when there is no way
             * back to it. A control inside a box that scrolls sideways is in
             * the tab order and is scrolled into view when it takes focus, so
             * it is reachable by keyboard and by finger alike. A control
             * outside the viewport with nothing scrollable above it is not
             * reachable at all, and that is the finding worth raising. Both
             * counts are reported so the distinction is visible rather than
             * assumed. */
            function scrollableAncestor(node) {
                var walk = node.parentElement;
                while (walk) {
                    var style = getComputedStyle(walk);
                    if (/(auto|scroll)/.test(style.overflowX) &&
                        walk.scrollWidth > walk.clientWidth + 2) {
                        return walk;
                    }
                    walk = walk.parentElement;
                }
                return null;
            }
            var stranded = outside.filter(function (node) {
                return !scrollableAncestor(node);
            });
            return {
                horizontalOverflow: overflow,
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                navigationReachable: navReachable,
                controlsOffScreen: outside.length,
                controlsStranded: stranded.length,
                strandedDescription: stranded.map(function (node) {
                    return node.tagName.toLowerCase() +
                        (node.className ? '.' + String(node.className).split(' ')[0] : '') +
                        ' "' + (node.textContent || '').trim().slice(0, 24) + '"';
                }).slice(0, 8),
            };
        });
        report.viewport.narrow = narrow;
        if (narrow.horizontalOverflow) {
            finding('high', 'The console scrolls sideways on a telephone',
                'At three hundred twenty pixels the document is ' + narrow.scrollWidth +
                ' wide against a viewport of ' + narrow.clientWidth + '.');
        }
        if (narrow.controlsStranded > 0) {
            finding('high', 'Controls sit outside the screen with no way to reach them',
                narrow.controlsStranded + ' interactive controls are outside the viewport and ' +
                'have no scrollable box above them: ' + narrow.strandedDescription.join(', ') + '.');
        }

        await page.setViewportSize({ width: 1400, height: 900 });
        await page.evaluate(function () { document.body.style.zoom = '2'; });
        await page.waitForTimeout(300);
        const zoomed = await page.evaluate(function () {
            return {
                horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
            };
        });
        report.viewport.zoomed = zoomed;
        if (zoomed.horizontalOverflow) {
            finding('medium', 'The console scrolls sideways at double magnification',
                'Document ' + zoomed.scrollWidth + ' against viewport ' + zoomed.clientWidth + '.');
        }
        await page.evaluate(function () { document.body.style.zoom = ''; });

        // -- what a screen reader would be told about live changes --------
        report.stage = 'live-regions';
        const live = await page.evaluate(function () {
            return Array.prototype.slice.call(document.querySelectorAll('[aria-live]')).map(function (node) {
                return {
                    politeness: node.getAttribute('aria-live'),
                    atomic: node.getAttribute('aria-atomic'),
                    characters: (node.textContent || '').trim().length,
                    className: String(node.className).split(' ')[0] || null,
                };
            });
        });
        report.liveRegions = live;
        const assertive = live.filter(function (r) { return r.politeness === 'assertive'; });
        const polite = live.filter(function (r) { return r.politeness === 'polite'; });
        if (assertive.length > 1) {
            finding('medium', 'Several assertive live regions',
                assertive.length + ' regions interrupt the screen reader. Routine updates should be polite.');
        }
        /* One that interrupts and one that waits. Every region being polite
         * puts "the trunk carrying every outside call has failed" in the same
         * queue as "the configuration was saved"; every region being assertive
         * cuts into a screen reader for things that could have waited. */
        if (!assertive.length) {
            finding('medium', 'Nothing on the console can interrupt',
                'Every live region is polite, so a critical alarm waits behind whatever a ' +
                'screen reader is already saying. A condition taking calls away is the one ' +
                'thing that warrants interrupting.');
        }
        if (!polite.length) {
            finding('medium', 'Everything on the console interrupts',
                'There is no polite region, so an ordinary confirmation cuts into whatever ' +
                'is being read.');
        }
        if (!live.length) {
            finding('high', 'Nothing is announced when the appliance pushes an update',
                'No aria-live region exists, so a screen reader user never learns that state changed.');
        }

        report.stage = 'complete';
    } catch (error) {
        report.stage = 'threw at ' + report.stage;
        report.failure = String(error && error.stack ? error.stack : error);
        await browser.close();
        emit();
        return;
    }

    if (report.pageErrors.length) {
        finding('critical', 'The page threw in the browser',
            report.pageErrors.slice(0, 3).join(' | '));
    }
    if (report.failedRequests.length) {
        finding('high', 'Requests failed while the console was in use',
            report.failedRequests.slice(0, 3).join(' | '));
    }

    await browser.close();
    emit();
})();
