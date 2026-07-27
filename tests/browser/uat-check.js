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
    stage: 'starting',
};

/* A finding is something a person would hit, not something a test dislikes. */
const finding = function (severity, title, detail) {
    report.findings.push({ severity: severity, title: title, detail: detail });
};

const fail = function (message) {
    report.failure = message;
    console.log('RESULT ' + JSON.stringify(report));
    process.exit(0);
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
        var node = element.parentElement;
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
                hasSkipLink: !!document.querySelector('a[href^="#"].skip-link, a.skip-link, [data-skip-link]'),
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
            await page.fill('#field-number', 'not-a-number');
            await page.fill('#field-name', 'A Test Telephone');
            await page.click('#form-holder-extensions button[type="submit"]');
            await page.waitForTimeout(700);

            const validation = await page.evaluate(function () {
                var field = document.querySelector('#field-number');
                var holder = document.querySelector('#form-holder-extensions');
                var message = holder ? holder.querySelector('.field.has-error .field-error, .field-error') : null;
                var summary = holder ? holder.querySelector('[role="alert"], .form-error-summary') : null;
                return {
                    fieldFound: !!field,
                    ariaInvalid: field ? field.getAttribute('aria-invalid') : null,
                    ariaDescribedBy: field ? field.getAttribute('aria-describedby') : null,
                    messageShown: !!(message && (message.textContent || '').trim()),
                    messageText: message ? (message.textContent || '').trim().slice(0, 140) : null,
                    messageId: message ? (message.id || null) : null,
                    summaryPresent: !!summary,
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

            /* Now get it right, and prove the happy path still works. */
            await page.fill('#field-number', '241');
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
        report.stage = 'contrast';
        for (const theme of ['light', 'dark', 'high-contrast']) {
            await page.evaluate(function (name) {
                document.documentElement.setAttribute('data-theme', name);
            }, theme);
            await page.waitForTimeout(150);
            const measured = await page.evaluate(new Function(CONTRAST_HELPERS + `
                var results = [];
                var samples = document.querySelectorAll(
                    'body, .nav-item, .tile, button, input, .lamp, .state-pill, a');
                var seen = {};
                Array.prototype.slice.call(samples).forEach(function (element) {
                    if (element.getClientRects().length === 0) { return; }
                    if (!ownText(element)) { return; }
                    var style = getComputedStyle(element);
                    if (parseFloat(style.opacity) === 0) { return; }
                    var key = element.tagName + '|' + String(element.className).split(' ')[0];
                    if (seen[key]) { return; }
                    seen[key] = true;
                    var r = ratio(style.color, behind(element));
                    if (r !== null) {
                        results.push({
                            element: key,
                            colour: style.color,
                            background: behind(element),
                            ratio: r,
                            fontSize: style.fontSize,
                        });
                    }
                });
                return results;
            `));
            report.contrast.push({ theme: theme, samples: measured });
            measured.forEach(function (sample) {
                const large = parseFloat(sample.fontSize) >= 24;
                const needed = large ? 3.0 : 4.5;
                if (sample.ratio < needed) {
                    finding('high', 'Text contrast below the required ratio',
                        theme + ': ' + sample.element + ' measures ' + sample.ratio +
                        ':1 (' + sample.colour + ' on ' + sample.background + '), needs ' + needed + ':1.');
                }
            });
        }
        await page.evaluate(function () { document.documentElement.removeAttribute('data-theme'); });

        // -- a narrow screen, and a magnified one -------------------------
        report.stage = 'viewport';
        await page.setViewportSize({ width: 320, height: 640 });
        await page.waitForTimeout(300);
        const narrow = await page.evaluate(function () {
            var overflow = document.documentElement.scrollWidth > document.documentElement.clientWidth + 2;
            var nav = document.querySelector('nav, [role="navigation"]');
            var navReachable = nav ? (nav.getClientRects().length > 0) : false;
            var offscreen = Array.prototype.slice.call(
                document.querySelectorAll('button, a[href], input')
            ).filter(function (node) {
                var box = node.getBoundingClientRect();
                return box.width > 0 && (box.left < -4 || box.right > window.innerWidth + 4);
            }).length;
            return {
                horizontalOverflow: overflow,
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                navigationReachable: navReachable,
                controlsOffScreen: offscreen,
            };
        });
        report.viewport.narrow = narrow;
        if (narrow.horizontalOverflow) {
            finding('high', 'The console scrolls sideways on a telephone',
                'At three hundred twenty pixels the document is ' + narrow.scrollWidth +
                ' wide against a viewport of ' + narrow.clientWidth + '.');
        }
        if (narrow.controlsOffScreen > 0) {
            finding('high', 'Controls sit outside the screen on a telephone',
                narrow.controlsOffScreen + ' interactive controls are wholly or partly off screen.');
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
        if (assertive.length > 1) {
            finding('medium', 'Several assertive live regions',
                assertive.length + ' regions interrupt the screen reader. Routine updates should be polite.');
        }
        if (!live.length) {
            finding('high', 'Nothing is announced when the appliance pushes an update',
                'No aria-live region exists, so a screen reader user never learns that state changed.');
        }

        report.stage = 'complete';
    } catch (error) {
        report.stage = 'threw at ' + report.stage;
        fail(String(error && error.stack ? error.stack : error));
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
    console.log('RESULT ' + JSON.stringify(report));
    process.exit(0);
})();
