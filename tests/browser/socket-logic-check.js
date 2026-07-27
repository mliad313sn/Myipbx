/*
 * Deterministic tests for the dashboard socket client's own logic.
 *
 * The browser test drives the whole page against a live appliance, which is
 * the right way to prove the page works but the wrong way to prove what it
 * does at the edges: inducing genuine staleness would mean wedging a real
 * appliance's event loop.  So the classification itself is exercised here,
 * directly and without a browser, by setting the client's observable state and
 * reading back what it concludes.
 *
 * This is the browser half of Benchmark Defect Two.  A dashboard that cannot
 * tell stale from quiet is the failure the product exists to remove, so the
 * distinction is tested rather than assumed.
 *
 * Reports one JSON document on the final line of standard output.
 */

'use strict';

const path = require('path');

const results = [];
let failures = 0;

const check = function (name, condition, detail) {
    results.push({ name: name, passed: Boolean(condition), detail: detail || null });
    if (!condition) {
        failures += 1;
    }
};

/* The client reaches for timer functions through the window object.  A minimal
 * stand in lets the retry schedule be observed without a browser. */
const scheduledDelays = [];
globalThis.window = {
    setTimeout: function (callback, delay) {
        scheduledDelays.push(delay);
        return scheduledDelays.length;
    },
    clearTimeout: function () {},
    setInterval: function () { return 0; },
    clearInterval: function () {},
    location: { protocol: 'http:', host: 'appliance.example' },
};

const ApplianceSocket = require(path.resolve(__dirname, '../../web/js/socket.js'));

const OPEN = 1;
const CLOSED = 3;

/* -- the three way link classification ---------------------------------- */

(function classification() {
    const client = new ApplianceSocket.Client({
        heartbeatIntervalSeconds: 5,
        missedLimit: 3,
    });

    // No socket at all.
    client.socket = null;
    client.lastMessageAt = Date.now();
    check(
        'a client with no socket reports itself reconnecting',
        client.linkState().state === ApplianceSocket.LINK_RECONNECTING
    );

    // A socket that is not open.
    client.socket = { readyState: CLOSED };
    check(
        'a client whose socket is closed reports itself reconnecting',
        client.linkState().state === ApplianceSocket.LINK_RECONNECTING
    );

    // Open and current.
    client.socket = { readyState: OPEN };
    client.lastMessageAt = Date.now();
    check(
        'a client that heard from the appliance just now reports itself live',
        client.linkState().state === ApplianceSocket.LINK_LIVE
    );

    // Open, silent, but still inside the tolerated window.
    client.lastMessageAt = Date.now() - 10000;
    check(
        'a client inside the tolerated silence still reports itself live',
        client.linkState().state === ApplianceSocket.LINK_LIVE,
        'silence of ten seconds against a tolerated fifteen'
    );

    // Open, but silent for longer than the permitted number of intervals.
    // This is the case a polling dashboard cannot see at all.
    client.lastMessageAt = Date.now() - 20000;
    const stale = client.linkState();
    check(
        'a socket that is open but silent past the limit reports itself stale',
        stale.state === ApplianceSocket.LINK_STALE,
        'silence of twenty seconds against a tolerated fifteen'
    );
    check(
        'the stale report carries the age of the silence',
        stale.seconds >= 19 && stale.seconds <= 21,
        'reported ' + stale.seconds
    );
    check(
        'the stale report explains itself in plain language',
        typeof stale.reason === 'string' && stale.reason.length > 0 &&
            !/[0-9]/.test(stale.reason),
        stale.reason
    );

    // The boundary itself: exactly at the limit is not yet stale.
    client.lastMessageAt = Date.now() - 15000;
    check(
        'silence exactly at the tolerated limit is not yet stale',
        client.linkState().state === ApplianceSocket.LINK_LIVE
    );
}());

/* -- the reconnection schedule ------------------------------------------- */

(function reconnection() {
    scheduledDelays.length = 0;

    const client = new ApplianceSocket.Client({
        retryBaseSeconds: 1,
        retryCeilingSeconds: 30,
    });

    const observed = [];
    for (let attempt = 0; attempt < 10; attempt += 1) {
        client.scheduleRetry();
        observed.push(scheduledDelays[scheduledDelays.length - 1] / 1000);
    }

    check(
        'the retry delay grows with each failed attempt',
        observed[3] > observed[0],
        'first ' + observed[0] + ', fourth ' + observed[3]
    );
    check(
        'the retry delay never exceeds the ceiling plus its jitter',
        observed.every(function (delay) { return delay <= 30 * 1.25 + 0.001; }),
        JSON.stringify(observed)
    );
    check(
        'every retry delay is positive',
        observed.every(function (delay) { return delay > 0; })
    );
    check(
        'the delay settles at the ceiling rather than growing without bound',
        observed[9] >= 30 * 0.75 && observed[9] <= 30 * 1.25,
        'tenth delay ' + observed[9]
    );

    // Jitter must actually vary, or a population of dashboards would return in
    // lockstep and stampede a restarted appliance.
    const late = observed.slice(6);
    const allIdentical = late.every(function (delay) { return delay === late[0]; });
    check('the retry delay is jittered rather than identical', !allIdentical);
}());

/* -- the socket address -------------------------------------------------- */

(function address() {
    const client = new ApplianceSocket.Client({});
    check(
        'the socket address is derived from the page it was served from',
        client.address() === 'ws://appliance.example/socket',
        client.address()
    );

    globalThis.window.location = { protocol: 'https:', host: 'appliance.example' };
    check(
        'a page served over a secured transport uses the secured socket scheme',
        client.address() === 'wss://appliance.example/socket',
        client.address()
    );
}());

/* -- listener isolation --------------------------------------------------- */

(function listeners() {
    const client = new ApplianceSocket.Client({});
    const seen = [];

    client.on('probe', function () { throw new Error('a faulty listener'); });
    client.on('probe', function (payload) { seen.push(payload); });

    let threw = false;
    try {
        client.emit('probe', { value: 'delivered' });
    } catch (error) {
        threw = true;
    }

    check('one faulty listener does not stop the others', seen.length === 1);
    check('a faulty listener does not propagate its error', !threw);
}());

console.log('RESULT ' + JSON.stringify({ results: results, failures: failures }));
