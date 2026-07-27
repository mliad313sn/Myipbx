/*
 * Static analysis for the browser sources.
 *
 * The dashboard is deliberately written as plain scripts with no module
 * system and no build step, so the configuration below describes that
 * environment explicitly rather than assuming a bundler's defaults.
 *
 * No rule set is inherited from a package, because the appliance carries no
 * dependency tree and this configuration must keep working on a machine that
 * cannot reach a package index.
 */

'use strict';

const browserGlobals = {
    globalThis: 'readonly',
    window: 'readonly',
    document: 'readonly',
    console: 'readonly',
    fetch: 'readonly',
    WebSocket: 'readonly',
    setTimeout: 'readonly',
    clearTimeout: 'readonly',
    setInterval: 'readonly',
    clearInterval: 'readonly',
    Blob: 'readonly',
    URL: 'readonly',
    Promise: 'readonly',
    // Present only when the numeral module is loaded by the agreement test.
    module: 'writable',
};

const nodeGlobals = {
    globalThis: 'readonly',
    require: 'readonly',
    __dirname: 'readonly',
    module: 'writable',
    process: 'readonly',
    console: 'readonly',
    // Supplied by the browser context inside page evaluation callbacks.
    document: 'readonly',
    NodeFilter: 'readonly',
};

const sharedRules = {
    // Correctness.
    'no-undef': 'error',
    // A caught binding that is deliberately ignored still documents what
    // is being swallowed, so it is not treated as dead code.
    'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }],
    'no-redeclare': 'error',
    'no-dupe-keys': 'error',
    'no-dupe-args': 'error',
    'no-duplicate-case': 'error',
    'no-unreachable': 'error',
    'no-fallthrough': 'error',
    'no-cond-assign': 'error',
    'no-constant-condition': 'error',
    'no-self-compare': 'error',
    'use-isnan': 'error',
    'valid-typeof': 'error',

    // Practices that bite in a long lived dashboard.
    eqeqeq: ['error', 'smart'],
    'no-implicit-globals': 'error',
    'no-eval': 'error',
    'no-implied-eval': 'error',
    'no-new-func': 'error',
    'no-throw-literal': 'error',
    'no-return-assign': 'error',
    'no-shadow-restricted-names': 'error',
    curly: 'error',
    'no-console': 'off',
};

module.exports = [
    {
        // The dashboard: plain browser scripts, no module system.
        files: ['web/js/**/*.js'],
        languageOptions: {
            ecmaVersion: 2018,
            sourceType: 'script',
            globals: browserGlobals,
        },
        rules: sharedRules,
    },
    {
        // The browser test harness, which runs under the scripting runtime.
        files: ['tests/browser/**/*.js'],
        languageOptions: {
            ecmaVersion: 2020,
            sourceType: 'script',
            globals: nodeGlobals,
        },
        rules: sharedRules,
    },
];
