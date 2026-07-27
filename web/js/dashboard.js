/*
 * Dashboard behaviour.
 *
 * Plain browser scripting with no framework and no build step.  Every numeric
 * readout on the page is produced by ApplianceNumerals, which is the same
 * algorithm the appliance uses in its logs, so Constraint Two holds on both
 * sides of the socket.
 */

(function () {
    'use strict';

    var numerals = window.ApplianceNumerals;
    var socket = new window.ApplianceSocket.Client({
        heartbeatIntervalSeconds: 5,
        missedLimit: 3
    });

    var elements = {};
    var latest = {
        state: null,
        trunks: null,
        tasks: null,
        hardware: null
    };

    /* -- element lookup --------------------------------------------------- */

    function bind() {
        [
            'sign-in-panel', 'sign-in-form', 'sign-in-failure', 'sign-in-button',
            'username', 'password', 'dashboard', 'sign-out-button',
            'link-lamp', 'link-text', 'link-age',
            'figure-active-calls', 'caption-answered', 'figure-trunks',
            'caption-trunks', 'figure-cards', 'caption-spans', 'figure-uptime',
            'caption-peak', 'alarm-panel', 'alarm-list', 'channel-body',
            'trunk-body', 'hardware-summary', 'span-body', 'drift-body',
            'drift-explanation', 'render-button', 'render-force-button',
            'task-body', 'allocation-statement', 'allocation-findings',
            'constraint-allocation', 'footer-text'
        ].forEach(function (identifier) {
            elements[identifier] = document.getElementById(identifier);
        });
    }

    /* -- small helpers ---------------------------------------------------- */

    function count(value) {
        var number = typeof value === 'number' ? value : parseInt(value, 10);
        if (isNaN(number)) {
            return 'an unknown number of';
        }
        return numerals.spellInteger(number);
    }

    function duration(seconds) {
        var number = typeof seconds === 'number' ? seconds : parseInt(seconds, 10);
        if (isNaN(number) || number < 0) {
            return 'an unknown duration';
        }
        return numerals.spellDuration(number);
    }

    function clear(node) {
        while (node && node.firstChild) {
            node.removeChild(node.firstChild);
        }
    }

    function cell(row, text, className) {
        var node = document.createElement('td');
        if (className) {
            var pill = document.createElement('span');
            pill.className = 'state-pill ' + className;
            pill.textContent = text;
            node.appendChild(pill);
        } else {
            node.textContent = text;
        }
        row.appendChild(node);
        return node;
    }

    function emptyRow(body, span, message) {
        var row = document.createElement('tr');
        row.className = 'empty';
        var node = document.createElement('td');
        node.setAttribute('colspan', String(span));
        node.textContent = message;
        row.appendChild(node);
        body.appendChild(row);
    }

    function request(path, options) {
        options = options || {};
        options.credentials = 'same-origin';
        options.headers = options.headers || {};
        if (options.body) {
            options.headers['Content-Type'] = 'application/json';
        }
        return fetch(path, options).then(function (response) {
            return response.json().then(function (payload) {
                return { ok: response.ok, status: response.status, payload: payload };
            }).catch(function () {
                return { ok: response.ok, status: response.status, payload: {} };
            });
        });
    }

    /* -- link indicator ---------------------------------------------------- */

    function renderLink(report) {
        if (!report) {
            report = socket.linkState();
        }
        elements['link-lamp'].className = 'lamp ' + report.state;

        var text = 'not connected';
        if (report.state === 'live') {
            text = 'live';
        } else if (report.state === 'reconnecting') {
            text = 'reconnecting';
        } else if (report.state === 'stale') {
            text = 'stale';
        }
        elements['link-text'].textContent = text;

        if (report.state === 'live') {
            elements['link-age'].textContent = '';
        } else if (typeof report.seconds === 'number') {
            elements['link-age'].textContent = '— last update ' + duration(report.seconds) + ' ago';
        } else {
            elements['link-age'].textContent = '';
        }
    }

    /* -- rendering --------------------------------------------------------- */

    function renderState(state) {
        if (!state) {
            return;
        }
        latest.state = state;

        elements['figure-active-calls'].textContent = count(state.active_calls);
        elements['caption-answered'].textContent = count(state.answered_calls) + ' answered';
        elements['figure-uptime'].textContent = duration(state.uptime_seconds);
        elements['caption-peak'].textContent =
            'peak of ' + count(state.peak_concurrent_calls) + ' concurrent calls';

        renderChannels(state.channels || []);
        renderAlarms(state.alarms || []);

        if (state.hardware) {
            renderHardware(state.hardware);
        }
    }

    function renderChannels(channels) {
        var body = elements['channel-body'];
        clear(body);

        if (!channels.length) {
            emptyRow(body, 6, 'no channel is active');
            return;
        }

        channels.forEach(function (channel) {
            var row = document.createElement('tr');
            cell(row, channel.name || 'an unnamed channel');
            cell(row, channel.state || 'unknown', (channel.state || '').replace(/\s+/g, '-'));
            cell(row, channel.caller_name
                ? channel.caller_name + ' — ' + numerals.sanitize(channel.caller_number || '')
                : numerals.sanitize(channel.caller_number || 'unknown'));
            cell(row, numerals.sanitize(channel.extension || 'none'));
            cell(row, duration(channel.duration_seconds));
            cell(row, channel.answered ? duration(channel.talk_seconds) : 'not answered');
            body.appendChild(row);
        });
    }

    function renderAlarms(alarms) {
        var panel = elements['alarm-panel'];
        var list = elements['alarm-list'];
        clear(list);

        if (!alarms.length) {
            panel.hidden = true;
            return;
        }
        panel.hidden = false;

        alarms.forEach(function (alarm) {
            var item = document.createElement('li');
            item.className = alarm.severity || 'warning';
            item.textContent = alarm.message + ' — raised ' + duration(alarm.age_seconds) + ' ago';
            if (alarm.detail) {
                var detail = document.createElement('div');
                detail.className = 'alarm-detail';
                detail.textContent = numerals.sanitize(alarm.detail);
                item.appendChild(detail);
            }
            list.appendChild(item);
        });
    }

    function renderTrunks(snapshot) {
        if (!snapshot) {
            return;
        }
        latest.trunks = snapshot;

        elements['figure-trunks'].textContent = count(snapshot.registered);
        elements['caption-trunks'].textContent = 'of ' + count(snapshot.total) + ' declared';

        var body = elements['trunk-body'];
        clear(body);

        var trunks = snapshot.trunks || [];
        if (!trunks.length) {
            emptyRow(body, 7, 'no trunk is declared');
            return;
        }

        trunks.forEach(function (trunk) {
            var row = document.createElement('tr');
            cell(row, trunk.name);
            cell(row, trunk.technology);
            cell(row, trunk.state, trunk.state);
            cell(row, count(trunk.attempts));
            cell(row, duration(trunk.seconds_in_state));
            cell(row, numerals.sanitize(trunk.reason || ''));

            var control = document.createElement('td');
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'secondary';
            button.textContent = trunk.enabled ? 'disable' : 'enable';
            button.addEventListener('click', function () {
                controlTrunk(trunk.name, trunk.enabled ? 'disable' : 'enable');
            });
            control.appendChild(button);
            row.appendChild(control);

            body.appendChild(row);
        });
    }

    function renderHardware(inventory) {
        if (!inventory) {
            return;
        }
        latest.hardware = inventory;

        elements['figure-cards'].textContent = count(inventory.card_count);
        elements['caption-spans'].textContent =
            count(inventory.span_count) + ' spans detected';
        elements['hardware-summary'].textContent = numerals.sanitize(
            inventory.summary || 'the hardware inventory is not available'
        );

        var body = elements['span-body'];
        clear(body);

        var spans = inventory.spans || [];
        if (!spans.length) {
            emptyRow(body, 4, 'no span is exported by the interface driver');
            return;
        }

        spans.forEach(function (span) {
            var row = document.createElement('tr');
            cell(row, count(span.number));
            cell(row, numerals.sanitize(span.description || 'an undescribed span'));
            cell(row, count(span.channel_count));
            cell(row, span.healthy ? 'no alarm' : numerals.sanitize(span.alarm),
                span.healthy ? 'registered' : 'failed');
            body.appendChild(row);
        });
    }

    function renderTasks(snapshot) {
        if (!snapshot) {
            return;
        }
        latest.tasks = snapshot;

        var body = elements['task-body'];
        clear(body);

        var tasks = snapshot.tasks || [];
        if (!tasks.length) {
            emptyRow(body, 5, 'no task is registered');
            return;
        }

        tasks.forEach(function (task) {
            var row = document.createElement('tr');
            cell(row, task.name);
            cell(row, task.description || '');
            cell(row, task.last_status
                ? task.last_status + (task.last_detail ? ' — ' + numerals.sanitize(task.last_detail) : '')
                : 'not yet run');
            cell(row, count(task.run_count));

            var control = document.createElement('td');
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'secondary';
            button.textContent = task.running ? 'running' : 'invoke';
            button.disabled = Boolean(task.running);
            button.addEventListener('click', function () {
                runTask(task.name);
            });
            control.appendChild(button);
            row.appendChild(control);

            body.appendChild(row);
        });
    }

    function renderDrift(report) {
        var body = elements['drift-body'];
        clear(body);

        elements['drift-explanation'].textContent = numerals.sanitize(
            report.explanation || ''
        );

        (report.reports || []).forEach(function (item) {
            var row = document.createElement('tr');
            cell(row, item.name);
            cell(row, item.status, item.diverged ? 'failed' : 'registered');

            var decision = document.createElement('td');
            if (item.diverged) {
                var button = document.createElement('button');
                button.type = 'button';
                button.className = 'secondary';
                button.textContent = 'adopt the file on disk';
                button.addEventListener('click', function () {
                    adopt(item.name);
                });
                decision.appendChild(button);
            } else {
                decision.textContent = 'no decision required';
            }
            row.appendChild(decision);
            body.appendChild(row);
        });
    }

    function renderConstraints(report) {
        var allocation = report.address_allocation || {};
        var panel = elements['constraint-allocation'];

        panel.className = 'constraint ' + (allocation.satisfied ? 'satisfied' : 'breached');
        elements['allocation-statement'].textContent = numerals.sanitize(
            allocation.statement || ''
        );

        var list = elements['allocation-findings'];
        clear(list);

        var findings = allocation.findings || [];
        if (!findings.length) {
            var item = document.createElement('li');
            item.textContent =
                'the audit found nothing; no address allocation service exists on this machine';
            list.appendChild(item);
            return;
        }

        findings.forEach(function (finding) {
            var entry = document.createElement('li');
            entry.textContent =
                finding.severity + ': ' + numerals.sanitize(finding.subject) +
                ' — ' + numerals.sanitize(finding.detail);
            list.appendChild(entry);
        });
    }

    /* -- actions ----------------------------------------------------------- */

    function refreshAll() {
        request('/api/state').then(function (result) {
            if (result.ok) {
                renderState(result.payload);
            }
        });
        request('/api/trunks').then(function (result) {
            if (result.ok) {
                renderTrunks(result.payload);
            }
        });
        request('/api/hardware').then(function (result) {
            if (result.ok) {
                renderHardware(result.payload);
            }
        });
        request('/api/tasks').then(function (result) {
            if (result.ok) {
                renderTasks(result.payload);
            }
        });
        request('/api/configuration/drift').then(function (result) {
            if (result.ok) {
                renderDrift(result.payload);
            }
        });
        request('/api/constraints').then(function (result) {
            if (result.ok) {
                renderConstraints(result.payload);
            }
        });
    }

    function controlTrunk(name, action) {
        request('/api/trunks/control', {
            method: 'POST',
            body: JSON.stringify({ name: name, action: action })
        }).then(function () {
            return request('/api/trunks');
        }).then(function (result) {
            if (result.ok) {
                renderTrunks(result.payload);
            }
        });
    }

    function runTask(name) {
        request('/api/tasks/run', {
            method: 'POST',
            body: JSON.stringify({ name: name })
        }).then(function () {
            return request('/api/tasks');
        }).then(function (result) {
            if (result.ok) {
                renderTasks(result.payload);
            }
            refreshDrift();
        });
    }

    function refreshDrift() {
        request('/api/configuration/drift').then(function (result) {
            if (result.ok) {
                renderDrift(result.payload);
            }
        });
    }

    function renderConfiguration(force) {
        request('/api/configuration/render', {
            method: 'POST',
            body: JSON.stringify({ force: Boolean(force) })
        }).then(function () {
            refreshDrift();
        });
    }

    function adopt(name) {
        request('/api/configuration/adopt', {
            method: 'POST',
            body: JSON.stringify({ name: name })
        }).then(function () {
            refreshDrift();
        });
    }

    function signIn(event) {
        event.preventDefault();
        elements['sign-in-failure'].hidden = true;
        elements['sign-in-button'].disabled = true;

        request('/api/session', {
            method: 'POST',
            body: JSON.stringify({
                username: elements.username.value,
                password: elements.password.value
            })
        }).then(function (result) {
            elements['sign-in-button'].disabled = false;
            if (!result.ok) {
                elements['sign-in-failure'].hidden = false;
                elements['sign-in-failure'].textContent = numerals.sanitize(
                    result.payload.error || 'the credentials were not accepted'
                );
                return;
            }
            elements.password.value = '';
            enterDashboard();
        });
    }

    function signOut() {
        request('/api/session/end', { method: 'POST' }).then(function () {
            socket.close();
            elements.dashboard.hidden = true;
            elements['sign-in-panel'].hidden = false;
            renderLink({ state: 'reconnecting', reason: 'signed out' });
        });
    }

    function enterDashboard() {
        elements['sign-in-panel'].hidden = true;
        elements.dashboard.hidden = false;
        refreshAll();
        socket.connect();
    }

    /* -- socket wiring ------------------------------------------------------ */

    function wireSocket() {
        socket.on('link', renderLink);

        socket.on('welcome', function (payload) {
            if (payload.state) {
                renderState(payload.state);
            }
            if (payload.trunks) {
                renderTrunks(payload.trunks);
            }
            elements['footer-text'].textContent = payload.assigns_addresses
                ? 'warning: this appliance reports that it assigns addresses'
                : 'this appliance assigns no addresses';
        });

        socket.on('state.changed', renderState);

        socket.on('snapshot', function (payload) {
            renderState(payload.state);
            renderTrunks(payload.trunks);
            renderTasks(payload.tasks);
            renderHardware(payload.hardware);
        });

        socket.on('trunk.transition', function () {
            request('/api/trunks').then(function (result) {
                if (result.ok) {
                    renderTrunks(result.payload);
                }
            });
        });

        socket.on('task.finished', function () {
            request('/api/tasks').then(function (result) {
                if (result.ok) {
                    renderTasks(result.payload);
                }
            });
        });

        socket.on('heartbeat', function () {
            renderLink(socket.linkState());
        });
    }

    /* -- startup ------------------------------------------------------------ */

    function start() {
        bind();
        wireSocket();

        elements['sign-in-form'].addEventListener('submit', signIn);
        elements['sign-out-button'].addEventListener('click', signOut);
        elements['render-button'].addEventListener('click', function () {
            renderConfiguration(false);
        });
        elements['render-force-button'].addEventListener('click', function () {
            renderConfiguration(true);
        });

        /* The staleness readout must advance even when nothing arrives — that
         * is the entire point of distinguishing stale from quiet. */
        window.setInterval(function () {
            if (!elements.dashboard.hidden) {
                renderLink(socket.linkState());
            }
        }, 1000);

        /* An existing session means the browser can go straight in. */
        request('/api/state').then(function (result) {
            if (result.ok) {
                enterDashboard();
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
}());
