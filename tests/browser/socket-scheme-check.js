/*
 * The dashboard socket client's choice of transport scheme.
 *
 * The console is served over a secured transport, and a page loaded over one
 * may not open an unsecured socket: the browser refuses it outright, and the
 * dashboard would sit reconnecting forever while the appliance behind it was
 * perfectly healthy.  That is exactly the frozen screen this product exists to
 * remove, arriving by a different road.
 *
 * The client already derives its scheme from the scheme of the page it was
 * loaded by.  This harness exists to hold that property still, because it is
 * one line and nothing else in the tree would notice if it changed.
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

const stubWindow = function (protocol, host) {
    globalThis.window = {
        setTimeout: function () { return 0; },
        clearTimeout: function () {},
        setInterval: function () { return 0; },
        clearInterval: function () {},
        location: { protocol: protocol, host: host },
    };
};

stubWindow('http:', 'appliance.example');
const ApplianceSocket = require(path.resolve(__dirname, '../../web/js/socket.js'));

const addressFor = function (protocol, host) {
    stubWindow(protocol, host);
    return new ApplianceSocket.Client({}).address();
};

/* A secured page must produce a secured socket. */
const secured = addressFor('https:', 'appliance.example');
check(
    'a page served over a secured transport opens a secured socket',
    secured.indexOf('wss://') === 0,
    secured
);
check(
    'the secured socket keeps the host the page was loaded from',
    secured === 'wss://appliance.example/socket',
    secured
);

/* A secured page reached on an explicit port keeps that port. */
const securedWithPort = addressFor('https:', 'appliance.example:8088');
check(
    'the secured socket keeps the port the page was loaded from',
    securedWithPort === 'wss://appliance.example:8088/socket',
    securedWithPort
);

/* A plain page still produces a plain socket, so that an appliance with
 * transport security deliberately switched off is still usable. */
const plain = addressFor('http:', 'appliance.example');
check(
    'a page served over plain transport opens a plain socket',
    plain === 'ws://appliance.example/socket',
    plain
);

/* And the scheme is never hard coded either way. */
check(
    'the scheme follows the page rather than being fixed',
    secured.indexOf('wss://') === 0 && plain.indexOf('ws://') === 0 && secured !== plain,
    secured + ' against ' + plain
);

console.log('RESULT ' + JSON.stringify({ results: results, failures: failures }));

if (failures > 0) {
    process.exitCode = 1;
}
