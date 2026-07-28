/*
 * Drives the real console in a real browser against a real appliance.
 *
 * The server side is exercised elsewhere. This script is the other half: it
 * loads the served page in Chromium, signs in through the form, walks every
 * section of the console, creates and deletes a telephony object through the
 * generated forms, and watches the page update itself over the persistent
 * socket while the appliance pushes events at it.
 *
 * Any error the page raises -- an undefined reference, a rejected promise, a
 * failed request -- is captured and reported, because a console that throws in
 * the browser has stopped working while still looking alive.
 *
 * Invoked by tests/test_browser.py, which owns the appliance under test.
 * Reports one JSON document on the final line of standard output.
 */

'use strict';

const { chromium } = require('playwright');

const [, , baseUrl, username, password] = process.argv;

const report = {
    signedIn: false,
    linkState: null,
    tiles: {},
    initialChannelRows: [],
    liveChannelSeen: false,
    liveChannelRow: null,
    views: {},
    entityCreated: false,
    entityValidationRefused: false,
    entityDeleted: false,
    signedOut: false,
    pageErrors: [],
    consoleErrors: [],
    failedRequests: [],
    digitsOnPage: [],
    stage: 'starting',
};

const fail = function (message) {
    report.failure = message;
    console.log('RESULT ' + JSON.stringify(report));
    process.exit(0);
};

/* Every section the console offers. Visiting each one proves it renders
 * without throwing, which no amount of server side testing can show. */
const VIEWS = [
    'overview', 'calls', 'history',
    'extensions', 'trunks', 'ring_groups',
    'inbound_routes', 'outbound_routes', 'time_conditions',
    'ivr_menus', 'queues', 'conferences',
    'hardware', 'system', 'firewall', 'security', 'configuration', 'tasks', 'logs', 'backup',
    'constraints',
];

(async () => {
    const browser = await chromium.launch();
    const context = await browser.newContext();
    const page = await context.newPage();

    page.on('pageerror', (error) => {
        report.pageErrors.push(String(error && error.message ? error.message : error));
    });
    page.on('console', (message) => {
        if (message.type() === 'error') {
            report.consoleErrors.push(message.text());
        }
    });
    page.on('requestfailed', (request) => {
        report.failedRequests.push(request.url() + ' -- ' + (request.failure() || {}).errorText);
    });

    const visit = async function (name) {
        await page.click('.nav-item[data-view="' + name + '"]');
        await page.waitForSelector('#view-' + name, { state: 'visible', timeout: 15000 });
        // Give the section's loader time to fetch and render.
        await page.waitForTimeout(350);
        const text = await page.innerText('#view-' + name);
        report.views[name] = text.replace(/\s+/g, ' ').trim().slice(0, 2000);
    };

    try {
        report.stage = 'navigating';
        await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });

        report.stage = 'awaiting the sign in form';
        await page.waitForSelector('#sign-in-form', { state: 'visible', timeout: 15000 });
        report.consoleHiddenBeforeSignIn = await page.isHidden('#console');

        report.stage = 'signing in';
        await page.fill('#username', username);
        await page.fill('#password', password);
        await page.click('#sign-in-button');

        report.stage = 'awaiting the console';
        await page.waitForSelector('#console', { state: 'visible', timeout: 20000 });
        report.signedIn = true;

        report.stage = 'awaiting the live link condition';
        await page.waitForFunction(
            () => document.getElementById('link-text').textContent.trim() === 'live',
            null,
            { timeout: 20000 }
        );
        report.linkState = (await page.textContent('#link-text')).trim();

        report.stage = 'reading the overview tiles';
        report.tiles = {
            activeCalls: (await page.textContent('#figure-active-calls')).trim(),
            answered: (await page.textContent('#caption-answered')).trim(),
            trunks: (await page.textContent('#figure-trunks')).trim(),
            trunksCaption: (await page.textContent('#caption-trunks')).trim(),
            cards: (await page.textContent('#figure-cards')).trim(),
            uptime: (await page.textContent('#figure-uptime')).trim(),
        };

        report.stage = 'walking every section of the console';
        for (const name of VIEWS) {
            report.stage = 'walking the section named ' + name;
            await visit(name);
        }

        /* ---- create a telephony object through the generated form ---- */

        report.stage = 'opening the extension form';
        await page.click('.nav-item[data-view="extensions"]');
        await page.waitForSelector('#view-extensions', { state: 'visible' });
        await page.waitForTimeout(400);
        await page.click('#view-extensions button[data-role="add"]');
        await page.waitForSelector('#form-holder-extensions form', { state: 'visible', timeout: 10000 });

        // First submit a value the appliance must refuse, to prove the
        // validation reaches the operator on the field that caused it.
        report.stage = 'submitting an invalid extension';
        await page.fill('#field-extensions-number', 'not-a-number');
        await page.fill('#field-extensions-name', 'A Test Telephone');
        await page.click('#form-holder-extensions button[type="submit"]');
        await page.waitForTimeout(600);
        const errorText = await page.innerText('#form-holder-extensions .field.has-error .field-error')
            .catch(() => '');
        report.entityValidationRefused = Boolean(errorText && errorText.trim());
        report.validationMessage = (errorText || '').trim();

        report.stage = 'submitting a valid extension';
        await page.fill('#field-extensions-number', '241');
        await page.fill('#field-extensions-name', 'A Test Telephone');
        await page.click('#form-holder-extensions button[type="submit"]');
        await page.waitForTimeout(900);

        const listedAfterCreate = await page.innerText('#view-extensions');
        report.entityCreated = listedAfterCreate.indexOf('A Test Telephone') !== -1;
        report.extensionsAfterCreate = listedAfterCreate.replace(/\s+/g, ' ').trim().slice(0, 400);

        /* ---- delete it again, confirming through the dialog ---- */

        report.stage = 'deleting the extension';
        // Target the row that was just created, not merely the first row that
        // happens to carry a delete button.
        await page.click(
            '#view-extensions tr:has-text("A Test Telephone") button:has-text("delete")'
        );
        await page.waitForSelector('#confirm-shade', { state: 'visible', timeout: 10000 });
        await page.click('#confirm-yes');
        await page.waitForTimeout(900);

        const listedAfterDelete = await page.innerText('#view-extensions');
        report.entityDeleted = listedAfterDelete.indexOf('A Test Telephone') === -1;

        /* ---- a pushed update must reach the live call section ---- */

        report.stage = 'awaiting a pushed update';
        await page.click('.nav-item[data-view="calls"]');
        await page.waitForSelector('#view-calls', { state: 'visible' });

        report.initialChannelRows = await page.$$eval('#channel-body tr', (rows) =>
            rows.map((row) => row.innerText.replace(/\s+/g, ' ').trim())
        );

        try {
            await page.waitForFunction(
                () => document.getElementById('channel-body').innerText.indexOf('live-probe') !== -1,
                null,
                { timeout: 30000 }
            );
            report.liveChannelSeen = true;
            report.liveChannelRow = await page.$$eval('#channel-body tr', (rows) => {
                const match = rows.filter((row) => row.innerText.indexOf('live-probe') !== -1);
                return match.length ? match[0].innerText.replace(/\s+/g, ' ').trim() : null;
            });

            // The headline tile must move too, not merely the table.
            await page.click('.nav-item[data-view="overview"]');
            await page.waitForFunction(
                () => document.getElementById('figure-active-calls').textContent.trim() === 'two',
                null,
                { timeout: 15000 }
            );
            report.tilesAfterPush = {
                activeCalls: (await page.textContent('#figure-active-calls')).trim(),
                answered: (await page.textContent('#caption-answered')).trim(),
            };
        } catch (error) {
            report.liveChannelSeen = false;
            report.pushFailure = String(error && error.message ? error.message : error);
        }

        /* ---- Constraint Two, as a person actually sees it ---- */

        report.stage = 'checking for digit characters on the rendered page';
        report.digitsOnPage = await page.evaluate(() => {
            const offending = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node = walker.nextNode();
            while (node) {
                const text = node.nodeValue;
                // Only text a person can actually see counts; a hidden section
                // is not on screen, and an input's value is machine format by
                // documented policy.
                const visible = node.parentElement && node.parentElement.offsetParent !== null;
                if (visible && text && /[0-9]/.test(text) && text.trim()) {
                    offending.push(text.trim().slice(0, 120));
                }
                node = walker.nextNode();
            }
            return offending;
        });

        report.stage = 'signing out';
        await page.click('#sign-out-button');
        await page.waitForSelector('#sign-in-form', { state: 'visible', timeout: 15000 });
        report.signedOut = await page.isHidden('#console');

        report.stage = 'complete';
    } catch (error) {
        report.failure = 'the browser run failed while ' + report.stage + ': ' +
            String(error && error.message ? error.message : error);
    } finally {
        await browser.close();
    }

    console.log('RESULT ' + JSON.stringify(report));
})().catch((error) => {
    fail('the browser harness itself failed: ' + String(error && error.message ? error.message : error));
});
