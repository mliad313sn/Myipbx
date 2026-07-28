/*
 * Capture the console being used to set up a telephone system.
 *
 * Every image this produces is a photograph of the running appliance: the
 * script signs in the way a person does, fills the forms the way a person
 * does, and screenshots what came back. Nothing is drawn, mocked or posed.
 * Where a screen depends on hardware this container does not have, the screen
 * is captured as it really appears on a machine without that hardware, and the
 * index says so rather than dressing it up.
 *
 * Invoked by tools/capture-console.py, which owns the appliance under test.
 */

'use strict';

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [, , baseUrl, username, password, outputDirectory] = process.argv;

const shots = [];
let sequence = 0;

async function shot(page, name, caption) {
    sequence += 1;
    const label = String(sequence).padStart(2, '0') + '-' + name + '.png';
    const file = path.join(outputDirectory, label);
    await page.screenshot({ path: file, fullPage: true });
    shots.push({ file: label, caption: caption });
    process.stdout.write('captured ' + label + '\n');
}

async function view(page, name) {
    const item = await page.$('.nav-item[data-view="' + name + '"]');
    if (!item) { return false; }
    await item.click();
    await page.waitForSelector('#view-' + name, { state: 'visible', timeout: 15000 });
    await page.waitForTimeout(500);
    return true;
}

async function fill(page, kind, values) {
    for (const [field, value] of Object.entries(values)) {
        const selector = '#field-' + kind + '-' + field;
        const node = await page.$(selector);
        if (!node) { continue; }
        const tag = await node.evaluate((n) => n.tagName.toLowerCase());
        const type = await node.evaluate((n) => n.getAttribute('type'));
        if (type === 'checkbox') {
            const checked = await node.isChecked();
            if (checked !== Boolean(value)) { await node.click(); }
        } else if (tag === 'select') {
            await node.selectOption(String(value)).catch(() => {});
        } else {
            await node.fill(String(value));
        }
    }
}

async function createEntity(page, kind, values) {
    const add = await page.$('#view-' + kind + ' button[data-role="add"]');
    if (!add) { return false; }
    await add.click();
    await page.waitForSelector('#form-holder-' + kind + ' form', { state: 'visible', timeout: 10000 });
    await fill(page, kind, values);
    return true;
}

async function submit(page, kind) {
    await page.click('#form-holder-' + kind + ' button[type="submit"]');
    await page.waitForTimeout(900);
}

(async () => {
    fs.mkdirSync(outputDirectory, { recursive: true });
    const browser = await chromium.launch({ args: ['--no-sandbox'] });
    const context = await browser.newContext({
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: 2,
    });
    const page = await context.newPage();
    page.on('pageerror', (error) => process.stderr.write('PAGEERROR ' + error + '\n'));

    // -- arriving -------------------------------------------------------
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(700);
    await shot(page, 'sign-in',
        'The console as it is first met. The appliance ships with no default password; the installer printed one once.');

    await page.fill('#username', username);
    await page.fill('#password', password);
    await shot(page, 'sign-in-filled', 'Signing in. The whole console is reachable from the keyboard alone.');

    await page.keyboard.press('Enter');
    await page.waitForSelector('#console', { state: 'visible', timeout: 20000 });
    await page.waitForTimeout(1500);
    await shot(page, 'overview',
        'The overview. The link lamp beside the heading is the persistent socket, and the figures move without the page being asked.');

    // -- the machine underneath -----------------------------------------
    if (await view(page, 'hardware')) {
        await shot(page, 'hardware',
            'Interface cards and spans, read from the driver. This machine has no card fitted, and the section says so rather than showing an empty table.');
    }
    if (await view(page, 'system')) {
        await shot(page, 'system',
            'The machine itself: host name, time, services, and the operations that need privilege.');
    }

    // -- extensions -----------------------------------------------------
    if (await view(page, 'extensions')) {
        await shot(page, 'extensions-before', 'Extensions, before anything has been added.');

        if (await createEntity(page, 'extensions', {
            number: '201', name: 'Reception', secret: 'a-long-telephone-password',
            technology: 'PJSIP', voicemail: true, ring_seconds: '20',
            electronic_mail: 'reception@example.net', enabled: true,
        })) {
            await shot(page, 'extension-form',
                'Adding a telephone. Every field, its help text and its validation come from one schema the appliance itself validates against.');
            await submit(page, 'extensions');
            await shot(page, 'extension-created',
                'The extension is created. Its number keeps its digits because it is dialled; the ring time is spelled because it is a quantity.');
        }

        // A refusal, on purpose, because that is half of what a form is for.
        if (await createEntity(page, 'extensions', { number: 'not-a-number', name: 'A Mistake' })) {
            await submit(page, 'extensions');
            await shot(page, 'extension-refused',
                'A value the appliance will not accept. The field is marked, the reason sits beneath it, the refusal is announced, and focus moves to the field that caused it.');
            const cancel = await page.$('#form-holder-extensions [data-role="cancel"]');
            if (cancel) { await cancel.click(); }
            await page.waitForTimeout(400);
        }

        for (const extension of [
            { number: '202', name: 'Sales Desk', secret: 'another-long-password', ring_seconds: '25' },
            { number: '203', name: 'Workshop', secret: 'a-third-long-password', ring_seconds: '30' },
        ]) {
            if (await createEntity(page, 'extensions', extension)) {
                await submit(page, 'extensions');
            }
        }
        await shot(page, 'extensions-list', 'Three telephones on the system.');
    }

    // -- trunks ---------------------------------------------------------
    if (await view(page, 'trunks')) {
        if (await createEntity(page, 'trunks', {
            name: 'carrier-primary', technology: 'PJSIP', host: 'sip.carrier.example',
            username: 'site-account', secret: 'a-carrier-password',
            register: true, enabled: true,
        })) {
            await shot(page, 'trunk-form', 'Adding the connection to a carrier.');
            await submit(page, 'trunks');
        }
        await shot(page, 'trunks-list',
            'The trunk is declared. Registration is driven by the appliance and reported as it happens; this one cannot reach its carrier from here, and the state says so.');
    }

    // -- routing --------------------------------------------------------
    if (await view(page, 'inbound_routes')) {
        if (await createEntity(page, 'inbound_routes', {
            did: '2015550100', description: 'Main number',
            destination_kind: 'extension', destination_value: '201', enabled: true,
        })) {
            await shot(page, 'inbound-route-form',
                'Where a call arriving from the carrier is sent. The dialled number keeps its digits: it is a number somebody calls.');
            await submit(page, 'inbound_routes');
        }
        await shot(page, 'inbound-routes-list', 'Inbound routing.');
    }

    if (await view(page, 'outbound_routes')) {
        if (await createEntity(page, 'outbound_routes', {
            name: 'national', pattern: '0NXXXXXXXXX', trunk: 'carrier-primary',
            strip_digits: '1', prepend_digits: '', priority: '10', enabled: true,
        })) {
            await shot(page, 'outbound-route-form', 'Which trunk carries a dialled number out.');
            await submit(page, 'outbound_routes');
        }
        await shot(page, 'outbound-routes-list', 'Outbound routing.');
    }

    if (await view(page, 'ring_groups')) {
        if (await createEntity(page, 'ring_groups', {
            number: '600', name: 'Front Office', strategy: 'ring all', ring_seconds: '25',
            enabled: true,
        })) {
            const members = await page.$('#field-ring_groups-members');
            if (members) { await members.selectOption(['201', '202']).catch(() => {}); }
            await shot(page, 'ring-group-form',
                'A group of telephones that ring together. The members are the extensions that exist, offered by the schema rather than typed.');
            await submit(page, 'ring_groups');
        }
        await shot(page, 'ring-groups-list', 'The ring group.');
    }

    if (await view(page, 'ivr_menus')) {
        if (await createEntity(page, 'ivr_menus', {
            number: '700', name: 'Main Menu', greeting: 'vm-enter-num-to-call',
            options: '1=201,2=600', wait_seconds: '10', timeout_destination: '201',
            enabled: true,
        })) {
            await shot(page, 'menu-form', 'A menu: what each key the caller presses leads to.');
            await submit(page, 'ivr_menus');
        }
        await shot(page, 'menus-list', 'The menu.');
    }

    if (await view(page, 'queues')) {
        if (await createEntity(page, 'queues', {
            number: '900', name: 'Support Queue', strategy: 'least recent',
            ring_seconds: '20', maximum_waiting: '10', overflow_destination: '201',
            music_class: 'default', enabled: true,
        })) {
            const members = await page.$('#field-queues-members');
            if (members) { await members.selectOption(['202', '203']).catch(() => {}); }
            await shot(page, 'queue-form', 'A queue that holds callers in order until somebody is free.');
            await submit(page, 'queues');
        }
        await shot(page, 'queues-list', 'The queue.');
    }

    if (await view(page, 'conferences')) {
        if (await createEntity(page, 'conferences', {
            number: '800', name: 'Board Room', pin: 'a-long-entry-code',
            announce_arrivals: true, music_class: 'default', enabled: true,
        })) {
            await submit(page, 'conferences');
        }
        await shot(page, 'conferences-list',
            'A conference room. The entry code is held as a secret and is never shown again, here or in a backup.');
    }

    if (await view(page, 'time_conditions')) {
        if (await createEntity(page, 'time_conditions', {
            name: 'business-hours', starts_at: '09:00', ends_at: '17:30',
            open_destination: '700', closed_destination: '201', enabled: true,
        })) {
            await shot(page, 'time-condition-form',
                'Business hours: where calls go inside them and outside them.');
            await submit(page, 'time_conditions');
        }
    }

    // -- the machine's own defences --------------------------------------
    if (await view(page, 'firewall')) {
        await shot(page, 'firewall',
            'The firewall the appliance generates and root loads. Sources are read once, by one reading shared with everything else that reads an address.');
    }
    if (await view(page, 'security')) {
        await shot(page, 'transport-security',
            'How the console is protected on the wire, including the certificate it is serving and its fingerprint.');
    }
    if (await view(page, 'configuration')) {
        await shot(page, 'configuration',
            'The source of truth, what has been rendered from it, and whether anything on disk has diverged from what the appliance wrote.');
    }
    if (await view(page, 'tasks')) {
        await shot(page, 'tasks', 'What the appliance does for itself, on what interval, and when each last ran.');
    }
    if (await view(page, 'journal')) {
        await shot(page, 'journal',
            'Who changed what, and from where. Every change above is in here, with the account and the address behind it. No request body is ever recorded.');
    }
    if (await view(page, 'backup')) {
        await shot(page, 'backup',
            'Backup, restore and the support bundle. Passwords travel only when asked for, and the console says plainly that such an archive is not encrypted.');
    }
    if (await view(page, 'constraints')) {
        await shot(page, 'constraints',
            'The exclusion this product is built around, audited on the running machine rather than asserted.');
    }
    if (await view(page, 'extensions')) {
        const filter = await page.$('#filter-extensions');
        if (filter) {
            await filter.fill('2');
            await page.waitForTimeout(300);
            await shot(page, 'extensions-filtered',
                'Every list can be searched, sorted by any column, and downloaded. The count says what is being shown against what exists, so a filtered table is never read as the whole list.');
            await page.locator('#filter-extensions').fill('');
            await page.waitForTimeout(300);
        }
    }
    if (await view(page, 'calls')) {
        await shot(page, 'live-calls',
            'Live call activity. The engine is not connected here, and the section distinguishes that from a quiet system rather than reporting zero.');
    }
    if (await view(page, 'logs')) {
        await shot(page, 'logs', 'The logs, read from the appliance itself, spelled before they reach the page.');
    }

    // -- reports ---------------------------------------------------------
    //
    // The appliance under test has been given a real call record file, so
    // these are figures aggregated from real records rather than a drawing of
    // what a report would look like.
    if (await view(page, 'reports')) {
        await page.selectOption('#report-window', 'everything');
        await page.click('#report-refresh');
        await page.waitForSelector('#report-summary-panel', { state: 'visible', timeout: 15000 });
        await page.waitForTimeout(600);
        await shot(page, 'reports-summary',
            'A period at a glance. Answer rate and average conversation are the two figures the field judges a telephone system on, and the average counts only the calls that were answered.');

        await page.locator('#report-chart').scrollIntoViewIfNeeded();
        await page.waitForTimeout(300);
        await shot(page, 'reports-distribution',
            'When the calls came. The outer bar is the hour\'s volume against the busiest hour and the filled part is what was answered, so the gap is the thing being looked for. The same reading is in the sentence above it and in the table below.');

        await page.locator('#report-breakdowns').scrollIntoViewIfNeeded();
        await page.waitForTimeout(300);
        await shot(page, 'reports-by-extension',
            'Every extension that took part in a call, whichever end it was on: calls in and out, answered each way, and its own answer rate. Each breakdown downloads as a file.');

        const between = await page.$('#report-window');
        if (between) {
            await page.selectOption('#report-window', 'between');
            await page.waitForTimeout(300);
            await shot(page, 'reports-between-dates',
                'The two date boxes appear only for the period that needs them; every other period is named rather than typed, because a typed date is a place to make a mistake a report then presents as a fact.');
        }
    }

    // -- the same console, other ways -----------------------------------
    await view(page, 'overview');
    for (const theme of ['dark', 'high-contrast']) {
        await page.evaluate((name) => document.documentElement.setAttribute('data-theme', name), theme);
        await page.waitForTimeout(400);
        await shot(page, 'overview-' + theme,
            'The same overview in the ' + theme.replace('-', ' ') + ' theme. Every colour comes from one token layer, and every reading was measured against the ratio it has to meet.');
    }
    await page.evaluate(() => document.documentElement.removeAttribute('data-theme'));

    await page.emulateMedia({ colorScheme: 'dark', contrast: 'more' });
    await page.waitForTimeout(400);
    await shot(page, 'overview-system-dark-high-contrast',
        'What an operator gets when their system asks for dark mode and more contrast together, which is the pairing a low vision operator is most likely to be running.');
    await page.emulateMedia({ colorScheme: 'light', contrast: 'no-preference' });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(500);
    await view(page, 'extensions');
    await shot(page, 'telephone-width',
        'The console on a telephone. The page does not scroll sideways; only the table does, inside a box that can be reached from the keyboard.');
    await page.setViewportSize({ width: 1440, height: 900 });

    // -- the dialogue that destroys things -------------------------------
    await view(page, 'extensions');
    await page.waitForTimeout(400);
    const remove = await page.$('#view-extensions button:has-text("delete")');
    if (remove) {
        await remove.click();
        await page.waitForTimeout(500);
        await shot(page, 'confirmation',
            'Nothing disruptive happens without being asked. The dialogue takes focus, keeps it, closes on Escape, and gives it back to whatever opened it.');
        const no = await page.$('#confirm-no');
        if (no) { await no.click(); }
    }

    fs.writeFileSync(
        path.join(outputDirectory, 'captures.json'),
        JSON.stringify(shots, null, 2),
        'utf-8'
    );
    process.stdout.write('RESULT ' + shots.length + '\n');
    await browser.close();
})();
