/*
 * The appliance console.
 *
 * Plain browser scripting, no framework, no build step. Every operation the
 * appliance supports has a path through this file, because the product's claim
 * is that an administrator never needs a terminal. Where an operation needs
 * privilege, the interface asks the appliance, which asks its helper; the
 * browser never sees a command.
 *
 * Every numeric readout is produced by ApplianceNumerals, the same algorithm
 * the appliance uses in its logs, so Constraint Two holds on both sides of the
 * socket.
 */

(function () {
    'use strict';

    var numerals = window.ApplianceNumerals;
    var forms = window.ApplianceForms;
    var element = forms.element;
    var clear = forms.clear;

    var socket = new window.ApplianceSocket.Client({
        heartbeatIntervalSeconds: 5,
        missedLimit: 3
    });

    var state = {
        schema: null,
        currentView: 'overview',
        system: null,
        references: {},
        pendingConfirm: null,
        /* Nothing until the first reading arrives, so that a console opened
         * after a dashboard was shed does not announce it as though it had
         * just happened. */
        shedDashboards: null,
        /* The control the operator is on, so a redraw can put them back on it.
         * Held from the moment they arrive rather than read off the document
         * once something has already taken it away. */
        focusKey: null
    };

    var nodes = {};

    /* ------------------------------------------------------------------ */
    /* small helpers                                                       */
    /* ------------------------------------------------------------------ */

    function byId(identifier) {
        return document.getElementById(identifier);
    }

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

    function request(path, options) {
        options = options || {};
        options.credentials = 'same-origin';
        options.headers = options.headers || {};
        if (options.body && !(options.body instanceof Blob) && !options.headers['Content-Type']) {
            options.headers['Content-Type'] = 'application/json';
        }
        return fetch(path, options).then(function (response) {
            var kind = response.headers.get('Content-Type') || '';
            if (kind.indexOf('application/json') === -1) {
                return response.blob().then(function (blob) {
                    return {
                        ok: response.ok,
                        status: response.status,
                        blob: blob,
                        /* The appliance already named the file it is sending.
                         * Reading that name here rather than restating it means
                         * the two cannot disagree, which they did: a name
                         * carrying the period a report covers cannot be written
                         * into the browser at all, because the browser is not
                         * the thing that decided the period. */
                        filename: filenameFrom(response.headers.get('Content-Disposition')),
                        payload: {}
                    };
                });
            }
            return response.json().then(function (payload) {
                return { ok: response.ok, status: response.status, payload: payload };
            }).catch(function () {
                return { ok: response.ok, status: response.status, payload: {} };
            });
        });
    }


    /* A table wide enough to overflow scrolls inside its own box rather than
     * taking the whole page sideways with it. The box is focusable and named,
     * because a region that scrolls but cannot be reached from a keyboard is a
     * region somebody cannot read the right hand end of. */
    function makeScrollable(table, label) {
        var scroller = element('div', 'table-scroll');
        scroller.setAttribute('tabindex', '0');
        scroller.setAttribute('role', 'region');
        scroller.setAttribute('aria-label', label || 'a table');
        scroller.appendChild(table);
        return scroller;
    }

    /* The name the appliance gave the file it is sending, if it gave one. */
    function filenameFrom(disposition) {
        var match = /filename="([^"]+)"/.exec(String(disposition || ''));
        return match ? match[1] : '';
    }

    /* Put a downloaded file in front of the operator.
     *
     * One place, because there are now five things this console downloads and
     * five copies of this were five chances for one of them to leak an object
     * address by never revoking it. */
    function saveBlob(result, fallbackName) {
        var url = URL.createObjectURL(result.blob);
        var anchor = element('a');
        anchor.href = url;
        anchor.download = result.filename || fallbackName;
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        URL.revokeObjectURL(url);
        return anchor.download;
    }

    /* The most useful line of a multi line explanation, for somewhere that can
     * only hold one. A toast is six seconds and one line; the full text goes to
     * the panel that stays on screen. */
    function firstLineOf(text) {
        var lines = String(text).split('\n').filter(function (line) {
            return line.trim().length > 0;
        });
        if (!lines.length) { return ''; }
        /* Prefer a line that reads as a cause rather than a heading. */
        for (var index = lines.length - 1; index >= 0; index -= 1) {
            if (/(missing|not match|failed|cannot|could not|absent|predates|no such)/i.test(lines[index])) {
                return lines[index].trim().slice(0, 200);
            }
        }
        return lines[lines.length - 1].trim().slice(0, 200);
    }

    function toast(message, kind, urgent) {
        /* A failure interrupts; everything else waits its turn. A screen
         * reader reading a table does not need to be cut into by "the
         * configuration was saved", and does need to be cut into by a trunk
         * that has stopped carrying calls. */
        var holder = urgent ? nodes.toastHolderUrgent : nodes.toastHolder;
        if (!holder) {
            holder = nodes.toastHolder;
        }
        var note = element('div', 'toast ' + (kind || 'good'), numerals.sanitize(String(message)));
        holder.appendChild(note);
        window.setTimeout(function () {
            if (note.parentNode) {
                note.parentNode.removeChild(note);
            }
        }, 6000);
    }

    /* The controls inside a container that a keyboard can actually reach. */
    function focusableWithin(container) {
        return Array.prototype.filter.call(
            container.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), ' +
                'select:not([disabled]), textarea:not([disabled]), [tabindex]'
            ),
            function (node) {
                return !node.hasAttribute('hidden') &&
                    node.getAttribute('tabindex') !== '-1' &&
                    node.getClientRects().length > 0;
            }
        );
    }

    /* A confirmation an operator must give before anything disruptive.
     *
     * A dialogue that claims to be modal has to behave like one. Without this,
     * the shade covered the page while focus stayed behind it: Tab walked the
     * navigation the operator could no longer see or click, Escape did
     * nothing, and a screen reader read the console rather than the question
     * being asked about deleting a trunk. Focus moves in, stays in, leaves by
     * Escape or by answering, and returns to whatever opened it. */
    function confirmAction(title, message) {
        return new Promise(function (resolve) {
            nodes.confirmTitle.textContent = numerals.sanitize(title);
            nodes.confirmMessage.textContent = numerals.sanitize(message);
            state.confirmOpener = document.activeElement;
            nodes.confirmShade.hidden = false;
            state.pendingConfirm = resolve;

            var box = nodes.confirmBox || nodes.confirmShade;
            /* The cancelling answer takes focus, not the destructive one. A
             * dialogue that lands on "go ahead" turns a stray Enter into a
             * deletion. */
            (nodes.confirmNo || box).focus();

            state.confirmKeydown = function (event) {
                if (event.key === 'Escape') {
                    event.preventDefault();
                    settleConfirm(false);
                    return;
                }
                if (event.key !== 'Tab') {
                    return;
                }
                var stops = focusableWithin(box);
                if (!stops.length) {
                    event.preventDefault();
                    return;
                }
                var first = stops[0];
                var last = stops[stops.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                } else if (stops.indexOf(document.activeElement) === -1) {
                    event.preventDefault();
                    first.focus();
                }
            };
            document.addEventListener('keydown', state.confirmKeydown, true);
        });
    }

    function settleConfirm(answer) {
        nodes.confirmShade.hidden = true;
        if (state.confirmKeydown) {
            document.removeEventListener('keydown', state.confirmKeydown, true);
            state.confirmKeydown = null;
        }
        var opener = state.confirmOpener;
        state.confirmOpener = null;
        if (state.pendingConfirm) {
            state.pendingConfirm(answer);
            state.pendingConfirm = null;
        }
        /* Focus goes back where it came from. Left on the body, the next Tab
         * starts at the top of the document and the operator loses their
         * place in a table they were halfway down. */
        if (opener && document.contains(opener) && typeof opener.focus === 'function') {
            opener.focus();
        }
    }

    function emptyRow(body, span, message) {
        var row = element('tr', 'empty');
        var cell = element('td', null, message);
        cell.setAttribute('colspan', String(span));
        row.appendChild(cell);
        body.appendChild(row);
    }

    function cell(row, text, className) {
        var node = element('td');
        if (className) {
            var pill = element('span', 'state-pill ' + className, text);
            node.appendChild(pill);
        } else {
            node.textContent = text;
        }
        row.appendChild(node);
        return node;
    }

    /* ------------------------------------------------------------------ */
    /* navigation                                                          */
    /* ------------------------------------------------------------------ */

    function showView(name) {
        state.currentView = name;

        Array.prototype.forEach.call(document.querySelectorAll('.view'), function (view) {
            view.hidden = view.id !== 'view-' + name;
        });
        Array.prototype.forEach.call(document.querySelectorAll('.nav-item'), function (item) {
            var here = item.dataset.view === name;
            item.classList.toggle('active', here);
            /* The fill says which section is open to somebody looking at it.
             * This says the same thing to somebody who is not: without it the
             * navigation announces twenty-two identical buttons and none of
             * them is the one you are in. */
            if (here) {
                item.setAttribute('aria-current', 'page');
            } else {
                item.removeAttribute('aria-current');
            }
        });

        var loader = VIEW_LOADERS[name];
        if (loader) {
            loader();
        }
    }

    /* ------------------------------------------------------------------ */
    /* overview and live calls                                             */
    /* ------------------------------------------------------------------ */

    function renderLink(report) {
        if (!report) {
            report = socket.linkState();
        }
        nodes.linkLamp.className = 'lamp ' + report.state;

        var text = report.state === 'live' ? 'live'
            : report.state === 'reconnecting' ? 'reconnecting'
            : report.state === 'stale' ? 'stale'
            : 'not connected';
        nodes.linkText.textContent = text;

        /* The reason is the difference between a dead appliance and a severed
         * uplink, and both used to read as the bare word "reconnecting".
         *
         * The socket client computes a specific reason on every path -- the
         * socket is not open, the appliance has not spoken within the permitted
         * number of intervals, a reconnection is scheduled, the socket reported
         * an error -- and this function read only the state and discarded it.
         * At three in the morning that is the difference between waking a site
         * contact and raising a network ticket. */
        if (report.state === 'live') {
            nodes.linkAge.textContent = '';
        } else {
            var parts = [];
            if (typeof report.seconds === 'number') {
                parts.push('last update ' + duration(report.seconds) + ' ago');
            }
            if (report.reason) {
                parts.push(report.reason);
            }
            nodes.linkAge.textContent = parts.length ? '— ' + parts.join('; ') : '';
        }
    }

    function renderState(appliance) {
        if (!appliance) {
            return;
        }
        preservingFocus(function () { renderStateTables(appliance); });
    }

    function renderStateTables(appliance) {

        /* Zero and unknown are not the same reading, and on this tile the
         * difference is the whole message.
         *
         * Call state comes from the telephony engine's event stream. When that
         * connection drops the appliance clears its channel table, because the
         * calls are not known to be gone -- they are no longer known to be
         * present. The counter then held zero, and the tile said "zero active
         * calls" in the same typeface it uses when the building really is
         * quiet. An operator reading it at three in the morning would conclude
         * nothing was happening on a system that might have been carrying
         * every call it could. The figure now says so, and the tile carries a
         * mark and a reason so that it reads as stale rather than as calm. */
        /* Read from the pushed snapshot's own field, not from the block the
         * read route adds on top of it. The live socket carries
         * engine_connected; the detailed engine block exists only on the
         * fetched route, so a console that looked only there believed the
         * engine was fine on every push and learned otherwise only when
         * somebody happened to reload. */
        var engineKnown = appliance.engine_connected !== undefined
            ? appliance.engine_connected !== false
            : (!appliance.engine || appliance.engine.connected !== false);
        markStale(nodes.figureActiveCalls, !engineKnown);

        if (engineKnown) {
            nodes.figureActiveCalls.textContent = count(appliance.active_calls);
            nodes.captionAnswered.textContent = count(appliance.answered_calls) + ' answered';
        } else {
            nodes.figureActiveCalls.textContent = 'unknown';
            nodes.captionAnswered.textContent =
                'the telephony engine is not connected, so call state cannot be read';
        }

        nodes.figureUptime.textContent = duration(appliance.uptime_seconds);
        nodes.captionPeak.textContent =
            'peak of ' + count(appliance.peak_concurrent_calls) + ' concurrent calls';

        renderChannels(appliance.channels || [], engineKnown);
        renderAlarms(appliance.alarms || []);
        if (appliance.hardware) {
            renderHardware(appliance.hardware);
        }
        if (appliance.socket) {
            renderTransport(appliance.socket);
        }
    }

    /* Redraw without taking the control out from under the person using it.
     *
     * The overview redraws from every pushed snapshot, which on a busy
     * appliance is about once a second. Each redraw cleared its list and built
     * a new one, so a keyboard user who had tabbed to a trunk's control, or to
     * an alarm's acknowledgement, lost it before they could press it: the
     * element they were on stopped existing and focus fell to the document
     * body. The control was reachable in principle and unusable in practice.
     *
     * Rather than reconcile every table by hand, the redraw is wrapped: the
     * focused element's key is noted, the redraw runs, and whatever now
     * carries that key takes focus back. A control that has genuinely gone --
     * a trunk that was deleted -- has no key to return to, and focus is left
     * where the browser put it rather than moved somewhere arbitrary. */
    function preservingFocus(redraw) {
        /* The key is remembered when the operator focuses the control, not
         * read off the document here.
         *
         * Reading it here was almost right and failed about one run in three.
         * A single redraw that is not wrapped -- the channel table, the
         * hardware panel, anything drawn before the alarms in the same tick --
         * drops focus to the document body first, and by the time a wrapped
         * redraw looks, there is no key left to remember. Holding the key from
         * the moment it was focused means any wrapped redraw can put it back,
         * whichever one broke it. */
        redraw();
        restoreFocusKey();
    }

    function restoreFocusKey() {
        var key = state.focusKey;
        if (!key) {
            return;
        }
        var active = document.activeElement;
        if (active && active.getAttribute &&
            active.getAttribute('data-focus-key') === key) {
            return;
        }
        /* Only repair a focus that fell to nowhere. If the operator has moved
         * to something else, that is their doing and must not be undone. */
        if (active && active !== document.body) {
            return;
        }
        var escaped = window.CSS && CSS.escape
            ? CSS.escape(key)
            : key.replace(/["\\]/g, '\\$&');
        var restored = document.querySelector('[data-focus-key="' + escaped + '"]');
        if (restored && typeof restored.focus === 'function') {
            restored.focus();
        }
    }

    /* What the operator is on, remembered as they arrive rather than looked up
     * after something has already taken it away. */
    function watchFocus() {
        document.addEventListener('focusin', function (event) {
            var target = event.target;
            var key = target && target.getAttribute
                ? target.getAttribute('data-focus-key')
                : null;
            /* A move to something unkeyed is a deliberate move away, and
             * clears the memory so nothing drags them back. A move to the body
             * is not deliberate -- it is what happens when the thing they were
             * on stopped existing -- so the memory survives it. */
            if (key) {
                state.focusKey = key;
            } else if (target && target !== document.body) {
                state.focusKey = null;
            }
        });
    }

    /* A figure the appliance cannot currently read.
     *
     * Marked three ways, because one is never enough: the class tints it, the
     * mark in front of it survives being printed or read by somebody who
     * cannot separate the colours, and aria-invalid tells a screen reader that
     * what it is about to read is not a current value. */
    function markStale(node, stale) {
        if (!node) {
            return;
        }
        node.classList.toggle('is-stale', Boolean(stale));
        if (stale) {
            node.setAttribute('aria-invalid', 'true');
        } else {
            node.removeAttribute('aria-invalid');
        }
    }

    /* The transport tile is built here rather than in the markup because the
     * numbers on it only exist once a dashboard has actually connected, and a
     * tile reading zero before the socket is up would be reporting a fault
     * that has not happened. */
    function transportTile() {
        if (nodes.transportTile) {
            return nodes.transportTile;
        }
        var tiles = document.querySelector('#view-overview .tiles');
        if (!tiles) {
            return null;
        }
        var tile = element('article', 'tile');
        tile.appendChild(element('h3', null, 'connected dashboards'));
        nodes.transportFigure = element('p', 'figure', 'zero');
        nodes.transportCaption = element('p', 'caption', 'none shed');
        tile.appendChild(nodes.transportFigure);
        tile.appendChild(nodes.transportCaption);
        tiles.appendChild(tile);
        nodes.transportTile = tile;
        return tile;
    }

    function renderTransport(transport) {
        if (!transportTile()) {
            return;
        }
        nodes.transportFigure.textContent = count(transport.connection_count);

        /* A shed dashboard is not an error the operator caused, but it is the
         * reason a screen somewhere in the building stopped updating, so it is
         * named plainly rather than buried in a log.  A rise in the count is
         * also announced, because a tile nobody happens to be looking at is
         * not how anyone finds out that a console has gone dark. */
        var shed = transport.slow_consumer_disconnections || 0;
        nodes.transportCaption.textContent = shed
            ? count(shed) + ' shed for falling behind'
            : 'none shed for falling behind';

        if (state.shedDashboards === null) {
            state.shedDashboards = shed;
        } else if (shed > state.shedDashboards) {
            toast(
                count(shed - state.shedDashboards) +
                    ' dashboard connection was shed for falling behind the update rate',
                'bad'
            );
            state.shedDashboards = shed;
        }
    }

    function renderChannels(channels, engineKnown) {
        var body = nodes.channelBody;
        clear(body);

        if (!channels.length) {
            /* An empty table means one of two very different things, and the
             * row has to say which. */
            emptyRow(body, 6, engineKnown === false
                ? 'the telephony engine is not connected, so no channel can be read; ' +
                  'this is not the same as no call being in progress'
                : 'no channel is active');
            return;
        }

        channels.forEach(function (channel) {
            var row = element('tr');
            cell(row, channel.name || 'an unnamed channel');
            cell(row, channel.state || 'unknown', (channel.state || '').replace(/\s+/g, '-'));
            /* A telephone number is dialled, not counted. Spelled, the number
             * two zero one five five five zero one zero zero came out as "two
             * billion fifteen million five hundred fifty thousand one hundred",
             * which nobody can read back to a handset or call back from. The
             * caller's number and the extension it reached are both
             * identifiers, and identifiers keep their digits. */
            cell(row, channel.caller_name
                ? channel.caller_name + ' — ' + (channel.caller_number || '')
                : (channel.caller_number || 'unknown'));
            cell(row, channel.extension || 'none');
            cell(row, duration(channel.duration_seconds));
            cell(row, channel.answered ? duration(channel.talk_seconds) : 'not answered');
            body.appendChild(row);
        });
    }

    /* The alarm panel, worst first and workable.
     *
     * The appliance already orders these: critical before warning, then
     * whatever nobody has looked at before whatever somebody is on, then
     * oldest first. The console draws them in the order it is given and adds
     * the one control that makes a panel usable on a machine where a condition
     * has been true for a week -- a way for an operator to say they have seen
     * it. That does not clear anything. The condition is still true and the
     * alarm is still raised; it moves down, and it says who moved it.
     *
     * There is deliberately no way to hide one. An alarm an operator can make
     * invisible is an alarm the next operator never sees, and on a telephone
     * system the next operator is usually the one who finds out that a trunk
     * has been down since Friday. */
    function renderAlarms(alarms) {
        preservingFocus(function () { renderAlarmList(alarms); });
    }

    function renderAlarmList(alarms) {  // eslint-disable-line no-unused-vars
        clear(nodes.alarmList);
        if (!alarms.length) {
            nodes.alarmPanel.hidden = true;
            return;
        }
        nodes.alarmPanel.hidden = false;

        alarms.forEach(function (alarm) {
            var classes = (alarm.severity || 'warning') +
                (alarm.acknowledged ? ' acknowledged' : '');
            var item = element('li', classes);

            var line = element('span', 'alarm-message',
                alarm.message + ' — raised ' + duration(alarm.age_seconds) + ' ago');
            item.appendChild(line);

            if (alarm.acknowledged) {
                item.appendChild(element('span', 'alarm-seen',
                    ' seen by ' + (alarm.acknowledged_by || 'somebody') +
                    ' ' + duration(alarm.acknowledged_age_seconds) + ' ago'));
            } else {
                var seen = element('button', 'secondary alarm-acknowledge', 'i have seen this');
                seen.type = 'button';
                seen.setAttribute('data-focus-key', 'alarm:' + alarm.key);
                seen.setAttribute('aria-label',
                    'acknowledge the alarm: ' + alarm.message);
                seen.addEventListener('click', function () {
                    acknowledgeAlarm(alarm.key, alarm.message);
                });
                item.appendChild(seen);
            }

            if (alarm.detail) {
                item.appendChild(element('div', 'alarm-detail', numerals.sanitize(alarm.detail)));
            }
            nodes.alarmList.appendChild(item);
        });
    }

    function acknowledgeAlarm(key, message) {
        return request('/api/alarms/' + encodeURIComponent(key) + '/acknowledge', {
            method: 'POST'
        }).then(function (result) {
            if (!result.ok) {
                toast(result.payload.error || 'the alarm could not be acknowledged', 'bad');
                return;
            }
            /* Said plainly, because the word "acknowledged" reads to some
             * operators as "dealt with" and this is neither. */
            toast('noted; the condition is still there and the alarm stays raised');
            loadState();
        });
    }

    function renderTrunks(snapshot) {
        if (!snapshot) {
            return;
        }
        preservingFocus(function () { renderTrunkTable(snapshot); });
    }

    function renderTrunkTable(snapshot) {
        nodes.figureTrunks.textContent = count(snapshot.registered);
        nodes.captionTrunks.textContent = 'of ' + count(snapshot.total) + ' declared';

        var body = nodes.trunkBody;
        clear(body);

        var trunks = snapshot.trunks || [];
        if (!trunks.length) {
            emptyRow(body, 7, 'no trunk is declared');
            return;
        }

        trunks.forEach(function (trunk) {
            var row = element('tr');
            cell(row, trunk.name);
            cell(row, trunk.technology);
            cell(row, trunk.state, trunk.state);
            cell(row, count(trunk.attempts));
            cell(row, duration(trunk.seconds_in_state));
            cell(row, numerals.sanitize(trunk.reason || ''));

            var control = element('td');
            var button = element('button', 'secondary', trunk.enabled ? 'disable' : 'enable');
            button.type = 'button';
            button.setAttribute('data-focus-key', 'trunk:' + trunk.name);
            button.addEventListener('click', function () {
                controlTrunk(trunk.name, trunk.enabled ? 'disable' : 'enable');
            });
            control.appendChild(button);
            row.appendChild(control);
            body.appendChild(row);
        });
    }

    function controlTrunk(name, action) {
        request('/api/trunks/control', {
            method: 'POST',
            body: JSON.stringify({ name: name, action: action })
        }).then(function () {
            toast('the trunk named ' + name + ' was asked to ' + action);
            return loadTrunks();
        });
    }

    function loadState() {
        return request('/api/state').then(function (result) {
            if (result.ok) {
                renderState(result.payload);
            }
        });
    }

    function loadTrunks() {
        return request('/api/trunks').then(function (result) {
            if (result.ok) {
                renderTrunks(result.payload);
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /* call history                                                        */
    /* ------------------------------------------------------------------ */

    function loadHistory() {
        var search = nodes.historySearch.value || '';
        return request('/api/calls?limit=100&search=' + encodeURIComponent(search))
            .then(function (result) {
                var payload = result.payload || {};
                nodes.historyExplanation.textContent = payload.available
                    ? 'showing ' + (payload.record_count || 'zero') + ' recent calls, newest first'
                    : numerals.sanitize(payload.explanation || 'no call history is available');

                var body = nodes.historyBody;
                clear(body);

                var records = payload.records || [];
                if (!records.length) {
                    emptyRow(body, 6, 'no call has been recorded yet');
                    return;
                }

                records.forEach(function (record) {
                    var row = element('tr');
                    cell(row, record.started_at || '');
                    cell(row, record.source || '');
                    cell(row, record.destination || '');
                    cell(row, record.duration || '');
                    cell(row, record.talk_time || '');
                    cell(row, (record.disposition || '').toLowerCase(),
                        record.answered ? 'registered' : 'failed');
                    body.appendChild(row);
                });
            });
    }

    /* ------------------------------------------------------------------ */
    /* recorded calls                                                      */
    /* ------------------------------------------------------------------ */

    function loadRecordings() {
        var query = [];
        if (nodes.recordingsSearch.value) {
            query.push('search=' + encodeURIComponent(nodes.recordingsSearch.value));
        }
        if (nodes.recordingsFrom.value) {
            query.push('from=' + encodeURIComponent(nodes.recordingsFrom.value));
        }
        if (nodes.recordingsTo.value) {
            query.push('to=' + encodeURIComponent(nodes.recordingsTo.value));
        }

        return request('/api/recordings?' + query.join('&')).then(function (result) {
            var payload = result.payload || {};
            var holder = nodes.recordingsTable;
            clear(holder);

            if (!payload.available) {
                nodes.recordingsExplanation.textContent = numerals.sanitize(
                    payload.explanation || 'no recording is available'
                );
                nodes.recordingsNote.hidden = true;
                return;
            }

            nodes.recordingsExplanation.textContent = numerals.sanitize(
                'showing ' + payload.record_count + ' recordings, newest first'
            );
            /* Files somebody put in that directory by hand are not listed and
             * not served. Said out loud, because an operator who copied them
             * there would otherwise conclude the appliance had lost them. */
            nodes.recordingsNote.hidden = !payload.explanation;
            if (payload.explanation) {
                nodes.recordingsNote.textContent = numerals.sanitize(payload.explanation);
            }

            var table = element('table', 'grid');
            var head = element('thead');
            var headRow = element('tr');
            ['when', 'from', 'to', 'size', 'listen'].forEach(function (heading) {
                headRow.appendChild(element('th', null, heading));
            });
            head.appendChild(headRow);
            table.appendChild(head);

            var body = element('tbody');
            var records = payload.records || [];
            if (!records.length) {
                emptyRow(body, 5, 'no recording matches what was asked for');
            }
            records.forEach(function (record) {
                var row = element('tr');
                cell(row, record.at);
                cell(row, record.source);
                cell(row, record.destination);
                cell(row, record.size);

                var actions = element('td', 'actions-cell');
                /* The browser's own player rather than one built here: it is
                 * reachable from a keyboard, it is what a screen reader
                 * already knows how to describe, and it costs nothing to
                 * carry on an appliance that loads nothing from anywhere. */
                var player = element('audio');
                player.controls = true;
                player.preload = 'none';
                player.src = '/api/recordings/' + encodeURIComponent(record.name);
                actions.appendChild(player);

                var save = element('a', 'download-link', 'download');
                save.href = player.src;
                save.download = record.name;
                actions.appendChild(save);

                row.appendChild(actions);
                body.appendChild(row);
            });
            table.appendChild(body);
            holder.appendChild(makeScrollable(table, 'recorded calls'));
        });
    }

    /* ------------------------------------------------------------------ */
    /* reports                                                             */
    /* ------------------------------------------------------------------ */

    /* The windows the appliance offers, and what to call them here. Held as a
     * list rather than read from the appliance because the order is a matter
     * of what an operator reaches for first, which is a question about people
     * and not about the data. The names themselves are checked against the
     * appliance by a test, so the two cannot drift apart silently. */
    var REPORT_WINDOWS = [
        { name: 'today', label: 'today' },
        { name: 'yesterday', label: 'yesterday' },
        { name: 'last-seven-days', label: 'the last seven days' },
        { name: 'this-month', label: 'this month' },
        { name: 'last-month', label: 'last month' },
        { name: 'last-thirty-days', label: 'the last thirty days' },
        { name: 'everything', label: 'everything on record' },
        { name: 'between', label: 'between two dates' }
    ];

    /* Which breakdowns to draw, in the order somebody reads them: the people
     * first, then where the calls went, then how they went, then time. */
    var REPORT_BREAKDOWNS = [
        {
            key: 'by_extension', heading: 'by extension', columns: 'extension',
            first: 'extension', second: 'name',
            hint: 'every extension that took part in a call, whichever end it was on'
        },
        {
            key: 'by_destination', heading: 'most called destinations', columns: 'standard',
            first: 'number', second: '',
            hint: 'the numbers dialled most often that are not extensions here'
        },
        {
            key: 'by_trunk', heading: 'by trunk', columns: 'standard',
            first: 'trunk', second: '',
            hint: 'the dialplan context each call arrived in or left by'
        },
        {
            key: 'by_disposition', heading: 'by outcome', columns: 'standard',
            first: 'outcome', second: '',
            hint: 'what the engine recorded as the end of each call'
        },
        {
            key: 'by_hour', heading: 'by hour of the day', columns: 'standard',
            first: 'hour', second: '', dropEmpty: true,
            hint: 'the hours that carried a call, summed across the whole ' +
                  'period. the chart above shows all twenty-four, including ' +
                  'the ones that carried nothing'
        },
        {
            key: 'by_day', heading: 'by day', columns: 'standard',
            first: 'day', second: '',
            hint: 'one row per day that carried a call'
        }
    ];

    /* The tiles, and the order they are read in. */
    var REPORT_TILES = [
        { key: 'calls', heading: 'calls', caption: 'answered' },
        { key: 'answer_ratio', heading: 'answered', caption: null },
        { key: 'missed', heading: 'missed', caption: null },
        { key: 'voicemail', heading: 'left a message', caption: null },
        { key: 'conversation', heading: 'total conversation', caption: null },
        { key: 'average_conversation', heading: 'average conversation', caption: null },
        { key: 'longest_call', heading: 'longest call', caption: null },
        { key: 'average_ring', heading: 'average time to answer', caption: null },
        { key: 'inbound', heading: 'calls in', caption: null },
        { key: 'outbound', heading: 'calls out', caption: null },
        { key: 'internal', heading: 'calls inside', caption: null },
        { key: 'cost', heading: 'cost', caption: null },
        { key: 'unrated', heading: 'unrated calls', caption: null }
    ];

    function buildReportWindows() {
        var select = nodes.reportWindow;
        if (!select || select.options.length) {
            return;
        }
        REPORT_WINDOWS.forEach(function (window_) {
            var option = element('option', null, window_.label);
            option.value = window_.name;
            select.appendChild(option);
        });
        select.value = 'last-seven-days';
        select.addEventListener('change', reportWindowChanged);
        reportWindowChanged();
    }

    /* The two date boxes exist only for the window that needs them. Left on
     * screen for every window they read as though they narrowed "last month",
     * which they do not. */
    function reportWindowChanged() {
        var chosen = nodes.reportWindow ? nodes.reportWindow.value : '';
        if (nodes.reportDates) {
            nodes.reportDates.hidden = chosen !== 'between';
        }
    }

    function reportQuery() {
        var chosen = nodes.reportWindow ? nodes.reportWindow.value : 'last-seven-days';
        var query = 'window=' + encodeURIComponent(chosen);
        if (chosen === 'between') {
            query += '&from=' + encodeURIComponent(nodes.reportFrom.value || '');
            query += '&to=' + encodeURIComponent(nodes.reportTo.value || '');
        }
        return query;
    }

    function loadReport() {
        buildReportWindows();
        return request('/api/reports?' + reportQuery()).then(function (result) {
            var payload = result.payload || {};

            /* The queues are drawn whatever happens to the calls above, which
             * is the whole reason they are a separate request: a site with
             * queues and no call record file still has a queue report, and
             * hiding it because the other file is missing would be the same
             * defect twice. */
            if (!result.ok) {
                nodes.reportsExplanation.textContent = numerals.sanitize(
                    payload.error || 'the report could not be produced'
                );
                hideReportPanels();
                loadSnapshots();
                return loadQueueReport();
            }
            if (!payload.available) {
                nodes.reportsExplanation.textContent = numerals.sanitize(
                    payload.explanation || 'there is nothing to report on yet'
                );
                hideReportPanels();
                loadSnapshots();
                return loadQueueReport();
            }

            var considered = (payload.considered || {}).text || 'zero';
            var explanation = 'drawn from ' + considered + ' calls, ' + payload.window.label;
            if (payload.currency_note) {
                /* Two currencies cannot be added into one figure, so no cost is
                 * reported at all rather than a total that is not an amount of
                 * anything. Said out loud, because a missing column otherwise
                 * reads as a system that does not cost calls. */
                explanation += '. ' + payload.currency_note;
            } else if (!payload.currency) {
                explanation += '. no cost is shown because no tariff is ' +
                    'configured; add one under tariffs and these figures gain ' +
                    'a cost column';
            }
            nodes.reportsExplanation.textContent = numerals.sanitize(explanation);

            nodes.reportTruncated.hidden = !payload.truncated;
            if (payload.truncated) {
                nodes.reportTruncated.textContent = numerals.sanitize(payload.truncation_note);
            }

            renderReportTiles(payload.summary || {});
            renderReportChart(payload.breakdowns.by_hour || []);
            renderReportBreakdowns(payload);
            loadQueueReport();
            loadSnapshots();
        });
    }

    function hideReportPanels() {
        [nodes.reportSummaryPanel, nodes.reportChartPanel, nodes.reportBreakdowns]
            .forEach(function (panel) {
                if (panel) { panel.hidden = true; }
            });
        if (nodes.reportTruncated) { nodes.reportTruncated.hidden = true; }
    }

    function renderReportTiles(summary) {
        var holder = nodes.reportTiles;
        clear(holder);
        REPORT_TILES.forEach(function (tile) {
            var figure = summary[tile.key];
            if (!figure) { return; }
            var article = element('article', 'tile');
            article.appendChild(element('h3', null, tile.heading));
            article.appendChild(element('p', 'figure', figure.text));
            if (tile.caption && summary[tile.caption]) {
                article.appendChild(element(
                    'p', 'caption', summary[tile.caption].text + ' answered'
                ));
            }
            holder.appendChild(article);
        });
        nodes.reportSummaryPanel.hidden = false;
    }

    /* The distribution of calls across the day, drawn from the same figures the
     * table below carries.
     *
     * Hand built rather than fetched: this appliance loads nothing from
     * anywhere, so a charting library is not an option, and a chart of
     * twenty-four bars does not need one. Every bar carries its own reading as
     * a title and the whole shape is described in the sentence above it, so an
     * operator who cannot see it is not being given less. */
    function renderReportChart(hours) {
        var holder = nodes.reportChart;
        clear(holder);

        var busiest = null;
        var total = 0;
        var tallest = 0;
        hours.forEach(function (hour) {
            var calls = hour.figures.calls.count;
            total += calls;
            if (calls > tallest) {
                tallest = calls;
                busiest = hour;
            }
        });

        if (!total) {
            nodes.reportChartSummary.textContent =
                'no call was recorded in this period, so there is nothing to draw';
            nodes.reportChartPanel.hidden = false;
            return;
        }

        nodes.reportChartSummary.textContent = numerals.sanitize(
            'the busiest hour was ' + busiest.label + ', carrying ' +
            busiest.figures.calls.text + ' calls of ' + count(total) +
            '. each bar below is one hour of the day, summed across the period.'
        );

        var chart = element('div', 'chart');
        chart.setAttribute('role', 'img');
        chart.setAttribute('aria-label', numerals.sanitize(
            'calls by hour of the day. the busiest hour was ' + busiest.label +
            ' with ' + busiest.figures.calls.text + ' calls.'
        ));

        hours.forEach(function (hour) {
            var calls = hour.figures.calls.count;
            var answered = hour.figures.answered.count;
            var column = element('div', 'chart-column');
            column.title = numerals.sanitize(
                hour.label + ': ' + hour.figures.calls.text + ' calls, ' +
                hour.figures.answered.text + ' answered'
            );

            var stack = element('div', 'chart-stack');
            /* Two bars, not one: the height is the calls that came and the
             * filled part is the calls that were answered, so the gap between
             * them is the thing an operator is looking for. */
            var bar = element('div', 'chart-bar');
            bar.style.height = (tallest ? Math.round((calls / tallest) * 100) : 0) + '%';
            var filled = element('div', 'chart-bar-answered');
            filled.style.height = (calls ? Math.round((answered / calls) * 100) : 0) + '%';
            bar.appendChild(filled);
            stack.appendChild(bar);

            column.appendChild(stack);
            column.appendChild(element('span', 'chart-label', hour.key));
            chart.appendChild(column);
        });

        holder.appendChild(chart);
        nodes.reportChartPanel.hidden = false;
    }

    function renderReportBreakdowns(payload) {
        var holder = nodes.reportBreakdowns;
        clear(holder);

        REPORT_BREAKDOWNS.forEach(function (breakdown) {
            var rows = payload.breakdowns[breakdown.key] || [];
            var columns = payload.columns[breakdown.columns] || [];

            /* Every hour of the day exists so the chart can draw a row of
             * hours rather than a row of gaps. In a table, seventeen lines
             * reading "zero" are noise between the lines that are not. */
            if (breakdown.dropEmpty) {
                rows = rows.filter(function (row) {
                    return row.figures.calls.count > 0;
                });
            }

            holder.appendChild(breakdownSection(
                breakdown, rows, columns, breakdown.key
            ));
        });

        holder.hidden = false;
    }

    /* The queue tiles, and the two breakdowns beneath them. */
    var QUEUE_TILES = [
        { key: 'offered', heading: 'offered' },
        { key: 'answered', heading: 'answered' },
        { key: 'abandoned', heading: 'gave up waiting' },
        { key: 'service_level', heading: 'answered in time' },
        { key: 'average_wait', heading: 'average wait' },
        { key: 'longest_wait', heading: 'longest wait' }
    ];

    var QUEUE_BREAKDOWNS = [
        {
            key: 'by_queue', heading: 'by queue', columns: 'queue',
            first: 'queue', second: 'description',
            hint: 'offered is everybody who joined; abandoned is everybody who ' +
                  'stopped waiting without being answered, however they stopped'
        },
        {
            key: 'by_member', heading: 'by member', columns: 'member',
            first: 'extension', second: '',
            hint: 'rang out counts the times a member was offered a call and ' +
                  'did not pick it up, which is not the same as a busy queue'
        }
    ];

    /* The snapshots the appliance drew for itself.
     *
     * Kept whether or not they could be sent, so a mail server that was down
     * on Monday costs the site a delivery rather than the report. */
    function loadSnapshots() {
        return request('/api/reports/scheduled').then(function (result) {
            var payload = result.payload || {};
            var holder = nodes.snapshotsTable;
            clear(holder);

            var snapshots = payload.snapshots || [];
            nodes.snapshotsExplanation.textContent = numerals.sanitize(
                snapshots.length
                    ? 'holding ' + payload.snapshot_count + ' reports, newest first'
                    : (payload.explanation || 'nothing has been drawn yet')
            );
            if (!snapshots.length) {
                return;
            }

            var table = element('table', 'grid');
            var head = element('thead');
            var headRow = element('tr');
            ['report', 'drawn', 'size', ''].forEach(function (heading) {
                headRow.appendChild(element('th', null, heading));
            });
            head.appendChild(headRow);
            table.appendChild(head);

            var body = element('tbody');
            snapshots.forEach(function (snapshot) {
                var row = element('tr');
                cell(row, snapshot.name);
                cell(row, snapshot.at);
                cell(row, snapshot.size);
                var actions = element('td', 'actions-cell');
                var link = element('a', 'download-link', 'download');
                link.href = '/api/reports/scheduled/' + encodeURIComponent(snapshot.name);
                link.download = snapshot.name;
                actions.appendChild(link);
                row.appendChild(actions);
                body.appendChild(row);
            });
            table.appendChild(body);
            holder.appendChild(makeScrollable(table, 'reports the appliance drew for itself'));
        });
    }

    function loadQueueReport() {
        return request('/api/reports/queues?' + reportQuery()).then(function (result) {
            var payload = result.payload || {};
            var panel = nodes.reportQueues;

            if (!result.ok || !payload.available) {
                nodes.reportQueuesExplanation.textContent = numerals.sanitize(
                    payload.explanation || payload.error ||
                    'the queues cannot be reported on'
                );
                nodes.reportQueueTiles.hidden = true;
                clear(nodes.reportQueueBreakdowns);
                panel.hidden = false;
                return;
            }

            nodes.reportQueuesExplanation.textContent = numerals.sanitize(
                'drawn from the queue log: ' +
                (payload.considered || {}).text + ' callers joined a queue, ' +
                payload.window.label
            );

            var tiles = nodes.reportQueueTiles;
            clear(tiles);
            QUEUE_TILES.forEach(function (tile) {
                var figure = payload.summary[tile.key];
                if (!figure) { return; }
                var article = element('article', 'tile');
                article.appendChild(element('h3', null, tile.heading));
                article.appendChild(element('p', 'figure', figure.text));
                tiles.appendChild(article);
            });
            tiles.hidden = false;

            var holder = nodes.reportQueueBreakdowns;
            clear(holder);
            QUEUE_BREAKDOWNS.forEach(function (breakdown) {
                holder.appendChild(breakdownSection(
                    breakdown,
                    payload.breakdowns[breakdown.key] || [],
                    payload.columns[breakdown.columns] || [],
                    'queue:' + breakdown.key
                ));
            });

            var reasons = payload.breakdowns.by_reason || [];
            if (reasons.length) {
                var section = element('section', 'breakdown');
                section.appendChild(element('h3', null, 'why callers stopped waiting'));
                section.appendChild(element('p', 'hint',
                    'kept apart rather than summed: a queue nobody is staffing ' +
                    'and a queue people give up on are different faults'));
                var table = element('table', 'grid');
                var head = element('thead');
                var headRow = element('tr');
                ['what happened', 'meaning', 'calls'].forEach(function (heading) {
                    headRow.appendChild(element('th', null, heading));
                });
                head.appendChild(headRow);
                table.appendChild(head);
                var body = element('tbody');
                reasons.forEach(function (row) {
                    var line = element('tr');
                    cell(line, row.key.toLowerCase());
                    cell(line, row.label);
                    cell(line, row.figures.calls.text);
                    body.appendChild(line);
                });
                table.appendChild(body);
                section.appendChild(makeScrollable(table, 'why callers stopped waiting'));
                holder.appendChild(section);
            }

            panel.hidden = false;
        });
    }

    /* One breakdown: heading, hint, table, and the button that downloads it.
     *
     * Shared by the call breakdowns and the queue breakdowns because they are
     * the same thing drawn from two files, and two copies of this would be two
     * places for a column to go missing. */
    function breakdownSection(breakdown, rows, columns, exportKey) {
        var section = element('section', 'breakdown');
        section.appendChild(element('h3', null, breakdown.heading));
        section.appendChild(element('p', 'hint', breakdown.hint));

        var table = element('table', 'grid');
        var head = element('thead');
        var headRow = element('tr');
        headRow.appendChild(element('th', null, breakdown.first));
        if (breakdown.second) {
            headRow.appendChild(element('th', null, breakdown.second));
        }
        columns.forEach(function (column) {
            headRow.appendChild(element('th', null, column.heading));
        });
        head.appendChild(headRow);
        table.appendChild(head);

        var body = element('tbody');
        if (!rows.length) {
            emptyRow(body, columns.length + (breakdown.second ? 2 : 1),
                'no call in this period falls under this heading');
        }
        rows.forEach(function (row) {
            var line = element('tr');
            cell(line, row.key);
            if (breakdown.second) {
                cell(line, row.label);
            }
            columns.forEach(function (column) {
                var figure = row.figures[column.key];
                cell(line, figure ? figure.text : '');
            });
            body.appendChild(line);
        });
        table.appendChild(body);
        section.appendChild(makeScrollable(table, breakdown.heading));

        var actions = element('div', 'actions');
        var download = element('button', null, 'download this as a file');
        download.type = 'button';
        download.addEventListener('click', function () {
            downloadReport(exportKey);
        });
        actions.appendChild(download);
        section.appendChild(actions);
        return section;
    }

    function downloadReport(breakdown) {
        var query = reportQuery();
        if (breakdown) {
            query += '&breakdown=' + encodeURIComponent(breakdown);
        }
        return request('/api/reports/export?' + query).then(function (result) {
            if (!result.ok || !result.blob) {
                toast(result.payload.error || 'the report could not be exported', 'bad');
                return;
            }
            var name = saveBlob(result, 'crossbar-report.csv');
            /* Said out loud, because it is a genuine departure from everything
             * else this console does and somebody opening the file will
             * otherwise wonder which of the two is wrong. */
            toast('the file ' + name + ' was downloaded; it carries figures as ' +
                  'digits rather than words, because a spreadsheet cannot add up a word');
        });
    }

    /* ------------------------------------------------------------------ */
    /* who changed what                                                    */
    /* ------------------------------------------------------------------ */

    function loadJournal() {
        return request('/api/journal?limit=200').then(function (result) {
            var payload = result.payload || {};
            var holder = nodes.journalTable;
            clear(holder);

            var entries = payload.entries || [];
            var table = element('table', 'grid');
            var head = element('thead');
            var headRow = element('tr');
            ['when', 'how long ago', 'who', 'from', 'what', 'where', 'outcome']
                .forEach(function (label) {
                    headRow.appendChild(element('th', null, label));
                });
            head.appendChild(headRow);
            table.appendChild(head);

            var body = element('tbody');
            if (!entries.length) {
                emptyRow(body, 7, 'nothing has been recorded on this appliance yet');
            }
            entries.forEach(function (entry) {
                var row = element('tr');
                /* The moment, the account and the address are identifiers, so
                 * they keep their digits. The age is a duration and arrives
                 * already spelled. */
                cell(row, entry.at || '');
                cell(row, entry.age || '');
                cell(row, entry.actor || '');
                cell(row, entry.source || '');
                cell(row, entry.action || '');
                cell(row, entry.target || '');
                cell(row, numerals.sanitize(entry.outcome || ''),
                    /^accepted|completed/.test(entry.outcome || '')
                        ? 'registered' : 'failed');
                body.appendChild(row);
            });
            table.appendChild(body);

            holder.appendChild(makeScrollable(table, 'the record of changes'));
        });
    }

    /* ------------------------------------------------------------------ */
    /* telephony objects, built from the schema                            */
    /* ------------------------------------------------------------------ */

    function specFor(kind) {
        if (!state.schema) {
            return null;
        }
        var matches = state.schema.kinds.filter(function (item) { return item.kind === kind; });
        return matches.length ? matches[0] : null;
    }

    function loadReferences() {
        if (!state.schema) {
            return Promise.resolve();
        }
        var needed = {};
        state.schema.kinds.forEach(function (spec) {
            spec.fields.forEach(function (field) {
                if (field.references) {
                    needed[field.references] = true;
                }
            });
        });

        return Promise.all(Object.keys(needed).map(function (kind) {
            return request('/api/entities/' + kind).then(function (result) {
                var spec = specFor(kind);
                var key = spec ? spec.key : 'name';
                state.references[kind] = (result.payload.records || []).map(function (record) {
                    return String(record[key]);
                });
            });
        }));
    }

    function renderEntityView(kind) {
        return renderEntityViewInto(kind, byId('view-' + kind));
    }

    /* How each list is currently being looked at: what has been typed into its
     * filter box and which column it is sorted by. Held per kind and outside
     * the render, so that saving an extension redraws the list the operator was
     * looking at rather than resetting them to the top of an unsorted table
     * they then have to find their place in again. */
    var listViews = {};

    function listView(kind) {
        if (!listViews[kind]) {
            listViews[kind] = { filter: '', sortField: null, sortDirection: 'ascending' };
        }
        return listViews[kind];
    }

    /* One record against one filter. Every column the table shows is searched,
     * because an operator typing "reception" does not know or care which field
     * the word is in. */
    function recordMatches(spec, record, needle) {
        if (!needle) {
            return true;
        }
        return spec.fields.some(function (field) {
            var value = record[field.name];
            if (value === undefined || value === null || field.kind === 'secret') {
                return false;
            }
            if (Array.isArray(value)) {
                value = value.join(' ');
            }
            return String(value).toLowerCase().indexOf(needle) !== -1;
        });
    }

    /* Sorting that puts a number where a person expects it.
     *
     * Compared as text, extension two hundred one sorts between twenty and
     * twenty-one, so a site numbered from one hundred upwards comes out in an
     * order nobody recognises. Where both values read as numbers they are
     * compared as numbers; anything else falls back to text, case folded. */
    function compareValues(left, right) {
        var leftNumber = Number(left);
        var rightNumber = Number(right);
        var bothNumeric = left !== '' && right !== ''
            && !isNaN(leftNumber) && !isNaN(rightNumber);
        if (bothNumeric) {
            return leftNumber - rightNumber;
        }
        return String(left).toLowerCase().localeCompare(String(right).toLowerCase());
    }

    function sortRecords(records, field, direction) {
        if (!field) {
            return records;
        }
        var sorted = records.slice();
        sorted.sort(function (left, right) {
            var value = compareValues(
                left[field] === undefined || left[field] === null ? '' : left[field],
                right[field] === undefined || right[field] === null ? '' : right[field]
            );
            return direction === 'descending' ? -value : value;
        });
        return sorted;
    }

    function renderEntityViewInto(kind, view) {
        var spec = specFor(kind);
        if (!spec || !view) {
            return Promise.resolve();
        }

        return loadReferences().then(function () {
            return request('/api/entities/' + kind);
        }).then(function (result) {
            var records = (result.payload && result.payload.records) || [];
            drawEntityView(kind, spec, view, records);
        });
    }

    function drawEntityView(kind, spec, view, records) {
        var settings = listView(kind);
        clear(view);

        var panel = element('section', 'panel');
        panel.appendChild(element('h2', null, spec.plural));
        panel.appendChild(element('p', 'hint', spec.description));

        var needle = settings.filter.trim().toLowerCase();
        var shown = sortRecords(
            records.filter(function (record) {
                return recordMatches(spec, record, needle);
            }),
            settings.sortField, settings.sortDirection
        );

        /* The filter, and what it is currently hiding. A table that quietly
         * shows six of two hundred rows is a table somebody reads as the whole
         * list and then reports a fault against. */
        var controls = element('div', 'actions list-controls');
        var filterLabel = element('label', 'mx-visually-hidden', 'search the ' + spec.plural);
        filterLabel.setAttribute('for', 'filter-' + kind);
        var filter = element('input');
        filter.type = 'search';
        filter.id = 'filter-' + kind;
        filter.placeholder = 'search the ' + spec.plural;
        filter.value = settings.filter;
        filter.addEventListener('input', function () {
            settings.filter = filter.value;
            drawEntityView(kind, spec, view, records);
            var redrawn = byId('filter-' + kind);
            if (redrawn) {
                redrawn.focus();
                /* Back where they were in what they were typing, not at the
                 * start of it. */
                redrawn.setSelectionRange(redrawn.value.length, redrawn.value.length);
            }
        });
        controls.appendChild(filterLabel);
        controls.appendChild(filter);

        var tally = element('p', 'hint list-count');
        tally.textContent = numerals.sanitize(
            shown.length === records.length
                ? count(records.length) + ' ' + (records.length === 1 ? spec.singular : spec.plural)
                : 'showing ' + count(shown.length) + ' of ' + count(records.length)
        );
        controls.appendChild(tally);
        panel.appendChild(controls);

        panel.appendChild(forms.buildTable(spec, shown, {
            edit: function (record) { openEntityForm(kind, record); },
            remove: function (record) { removeEntity(kind, record); }
        }, {
            sortField: settings.sortField,
            sortDirection: settings.sortDirection,
            emptyMessage: needle
                ? 'nothing here matches what was typed'
                : 'no ' + spec.plural + ' are configured yet',
            onSort: function (fieldName) {
                if (settings.sortField === fieldName) {
                    settings.sortDirection =
                        settings.sortDirection === 'ascending' ? 'descending' : 'ascending';
                } else {
                    settings.sortField = fieldName;
                    settings.sortDirection = 'ascending';
                }
                drawEntityView(kind, spec, view, records);
            }
        }));

        var actions = element('div', 'actions');
        /* Named for what they do rather than found by the words on them.
         * A test looking for the button whose label contains "add" found the
         * sortable heading "carrier address" instead, and clicked that. */
        var add = element('button', null, 'add ' + spec.singular);
        add.type = 'button';
        add.dataset.role = 'add';
        add.addEventListener('click', function () { openEntityForm(kind, null); });
        actions.appendChild(add);

        var download = element('button', 'secondary', 'download the list');
        download.type = 'button';
        download.dataset.role = 'export';
        download.addEventListener('click', function () { downloadEntities(kind, spec); });
        actions.appendChild(download);

        panel.appendChild(actions);
        view.appendChild(panel);

        var holder = element('section', 'panel form-holder');
        holder.id = 'form-holder-' + kind;
        holder.hidden = true;
        view.appendChild(holder);
    }

    function downloadEntities(kind, spec) {
        return request('/api/entities/' + kind + '/export').then(function (result) {
            if (!result.ok || !result.blob) {
                toast(result.payload.error || 'the list could not be exported', 'bad');
                return;
            }
            var name = saveBlob(result, 'crossbar-' + kind + '.csv');
            toast('the file ' + name + ' was downloaded; it carries every ' +
                  spec.singular + ' but no password');
        });
    }

    function openEntityForm(kind, record) {
        var spec = specFor(kind);
        var holder = byId('form-holder-' + kind);
        if (!spec || !holder) {
            return;
        }

        clear(holder);
        holder.hidden = false;

        var form = forms.buildForm(spec, record, state.references);
        holder.appendChild(form);

        form.querySelector('[data-role="cancel"]').addEventListener('click', function () {
            holder.hidden = true;
            clear(holder);
        });

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var values = forms.readForm(form, spec);
            var editing = Boolean(record);
            var path = '/api/entities/' + kind + (editing ? '/' + encodeURIComponent(record[spec.key]) : '');

            request(path, {
                method: editing ? 'PUT' : 'POST',
                body: JSON.stringify(values)
            }).then(function (result) {
                if (result.status === 422 || result.status === 409) {
                    forms.showErrors(form, result.payload.errors || {});
                    toast('the ' + spec.singular + ' was not accepted; see the marked fields', 'bad');
                    return;
                }
                if (!result.ok) {
                    toast(result.payload.error || 'the change could not be saved', 'bad');
                    return;
                }
                toast('the ' + spec.singular + ' was saved');
                holder.hidden = true;
                clear(holder);
                renderEntityView(kind);
                loadDrift();
            });
        });

        holder.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function removeEntity(kind, record) {
        var spec = specFor(kind);
        var key = String(record[spec.key]);

        confirmAction(
            'delete this ' + spec.singular,
            'the ' + spec.singular + ' identified as ' + numerals.sanitize(key) +
            ' will be removed from the configuration.'
        ).then(function (answer) {
            if (!answer) {
                return;
            }
            request('/api/entities/' + kind + '/' + encodeURIComponent(key), {
                method: 'DELETE'
            }).then(function (result) {
                if (result.status === 409) {
                    var errors = result.payload.errors || {};
                    var reason = Object.keys(errors).map(function (name) {
                        return errors[name];
                    }).join('; ');
                    toast(reason || 'it is still in use', 'bad');
                    return;
                }
                if (!result.ok) {
                    toast(result.payload.error || 'it could not be deleted', 'bad');
                    return;
                }
                toast('the ' + spec.singular + ' was deleted');
                renderEntityView(kind);
                loadDrift();
            });
        });
    }

    /* ------------------------------------------------------------------ */
    /* hardware                                                            */
    /* ------------------------------------------------------------------ */

    function renderHardware(inventory) {
        if (!inventory) {
            return;
        }
        nodes.figureCards.textContent = count(inventory.card_count);
        nodes.captionSpans.textContent = count(inventory.span_count) + ' spans detected';

        if (nodes.hardwareSummary) {
            nodes.hardwareSummary.textContent = numerals.sanitize(
                inventory.summary || 'the hardware inventory is not available'
            );
        }

        if (nodes.cardBody) {
            clear(nodes.cardBody);
            var cards = inventory.cards || [];
            if (!cards.length) {
                emptyRow(nodes.cardBody, 4, 'no legacy Digium interface card was detected');
            }
            cards.forEach(function (card) {
                var row = element('tr');
                cell(row, numerals.sanitize(card.slot));
                cell(row, numerals.sanitize(card.model));
                cell(row, card.driver_module);
                cell(row, card.driver_bound ? card.driver_bound : 'not bound',
                    card.driver_bound ? 'registered' : 'failed');
                nodes.cardBody.appendChild(row);
            });
        }

        if (nodes.spanBody) {
            clear(nodes.spanBody);
            var spans = inventory.spans || [];
            if (!spans.length) {
                emptyRow(nodes.spanBody, 4, 'no span is exported by the interface driver');
            }
            spans.forEach(function (span) {
                var row = element('tr');
                cell(row, count(span.number));
                cell(row, numerals.sanitize(span.description || 'an undescribed span'));
                cell(row, count(span.channel_count));
                cell(row, span.healthy ? 'no alarm' : numerals.sanitize(span.alarm),
                    span.healthy ? 'registered' : 'failed');
                nodes.spanBody.appendChild(row);
            });
        }
    }

    var WIZARD_STEPS = [
        {
            title: 'detect the interface cards',
            detail: 'reads the peripheral bus and names any Digium card fitted to this machine.',
            action: function () { return runTask('hardware-rescan'); },
            label: 'detect now'
        },
        {
            title: 'build and load the drivers',
            detail: 'compiles the interface drivers against the running kernel and loads them. ' +
                    'this is the step every other system leaves to the command line.',
            action: function () { return runOperation('driver-rebuild', {}, true); },
            label: 'build and load'
        },
        {
            title: 'generate the span configuration',
            detail: 'asks the driver what it found and writes the span configuration for it.',
            action: function () { return runOperation('span-generate', {}, true); },
            label: 'generate'
        },
        {
            title: 'render and reload',
            detail: 'writes the engine configuration from the source of truth, then reloads the engine.',
            action: function () {
                return renderConfiguration(false).then(function () {
                    return runOperation('engine-reload', {}, false);
                });
            },
            label: 'render and reload'
        }
    ];

    function renderWizard() {
        var list = nodes.hardwareWizard;
        clear(list);

        WIZARD_STEPS.forEach(function (step, index) {
            var item = element('li', 'wizard-step');
            item.appendChild(element('h4', null,
                numerals.spellOrdinal(index + 1) + ' — ' + step.title));
            item.appendChild(element('p', 'field-help', step.detail));

            var button = element('button', 'secondary', step.label);
            button.type = 'button';
            var outcome = element('p', 'wizard-outcome');

            button.addEventListener('click', function () {
                button.disabled = true;
                outcome.textContent = 'working';
                step.action().then(function (message) {
                    outcome.textContent = numerals.sanitize(message || 'done');
                    button.disabled = false;
                    loadHardware();
                }).catch(function (error) {
                    outcome.textContent = numerals.sanitize(String(error));
                    button.disabled = false;
                });
            });

            item.appendChild(button);
            item.appendChild(outcome);
            list.appendChild(item);
        });
    }

    function loadHardware() {
        return request('/api/hardware').then(function (result) {
            if (result.ok) {
                renderHardware(result.payload);
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /* the machine                                                         */
    /* ------------------------------------------------------------------ */

    function runOperation(verb, values, disruptive) {
        var send = function () {
            return request('/api/system/operations/' + encodeURIComponent(verb), {
                method: 'POST',
                body: JSON.stringify(values || {})
            }).then(function (result) {
                var payload = result.payload || {};
                if (!result.ok || payload.succeeded === false) {
                    /* What the helper printed is the diagnosis; detail is only
                     * the fact that something failed.
                     *
                     * The precedence used to be the other way round, and it
                     * threw away the one thing the technician needed. The
                     * driver stage writes a real explanation -- which kernel
                     * headers are missing, that the released driver archive
                     * predates this kernel, which compiler is absent -- the
                     * helper returns it, the appliance sends it, and the
                     * console replaced it with "the helper reported a
                     * failure". Somebody stood at the machine reading that. */
                    var explanation = (payload.output || '').trim();
                    var summary = payload.detail || payload.error || 'the operation failed';
                    toast(explanation ? firstLineOf(explanation) : summary, 'bad');
                    return explanation || summary;
                }
                toast('the operation named ' + verb + ' completed');
                return payload.output || 'the operation completed';
            });
        };

        if (!disruptive) {
            return send();
        }
        return confirmAction(
            'confirm the operation named ' + verb,
            'this interrupts service on this appliance. calls in progress may be lost.'
        ).then(function (answer) {
            return answer ? send() : 'the operation was cancelled';
        });
    }

    /* The prefix length is a choice rather than free text, so it can be
     * offered spelled while the value sent to the appliance stays numeric. */
    function populatePrefixLengths() {
        var select = nodes.networkPrefix;
        if (select.options.length) {
            return;
        }
        for (var length = 8; length <= 30; length += 1) {
            var option = element('option', null, numerals.spellInteger(length));
            option.value = String(length);
            option.selected = length === 24;
            select.appendChild(option);
        }
    }

    function renderSystem(payload) {
        state.system = payload;
        populatePrefixLengths();

        var readings = nodes.systemReadings;
        clear(readings);

        var entries = [
            ['host name', payload.hostname],
            ['kernel', payload.kernel_release],
            ['time zone', payload.timezone],
            ['machine uptime', payload.uptime],
            ['appliance uptime', payload.appliance && payload.appliance.uptime],
            ['appliance version', payload.appliance && payload.appliance.version],
            ['dashboards connected', payload.appliance && payload.appliance.dashboards_connected],
            ['memory', payload.memory && (payload.memory.available + ' available of ' + payload.memory.total)],
            ['memory used', payload.memory && payload.memory.used_portion],
            ['disk', payload.disk && (payload.disk.free + ' free of ' + payload.disk.total)],
            ['disk used', payload.disk && payload.disk.used_portion],
            ['load average', payload.load_average && payload.load_average.one_minute]
        ];

        entries.forEach(function (entry) {
            if (entry[1] === undefined || entry[1] === null) {
                return;
            }
            readings.appendChild(element('dt', null, entry[0]));
            readings.appendChild(element('dd', null, numerals.sanitize(String(entry[1]))));
        });

        var body = nodes.interfaceBody;
        clear(body);
        var interfaces = payload.interfaces || [];
        if (!interfaces.length) {
            emptyRow(body, 4, 'no network interface was found');
        }

        clear(nodes.networkInterface);
        interfaces.forEach(function (item) {
            var row = element('tr');
            cell(row, item.name);
            cell(row, item.state, item.state === 'up' ? 'registered' : 'failed');
            cell(row, item.carrier ? 'yes' : 'no');
            cell(row, numerals.sanitize(item.address || ''));
            body.appendChild(row);

            var option = element('option', null, item.name);
            option.value = item.name;
            nodes.networkInterface.appendChild(option);
        });

        nodes.systemHostname.value = payload.hostname || '';
        nodes.systemTimezone.value = payload.timezone || '';

        renderServices(payload.privileged_operations || {});
    }

    function renderServices(operations) {
        var body = nodes.serviceBody;
        clear(body);

        var services = operations.managed_services || [];
        if (!operations.available) {
            emptyRow(body, 2,
                'the privileged helper is not installed, so services cannot be controlled from here');
            return;
        }

        services.forEach(function (service) {
            var row = element('tr');
            cell(row, service);

            var controls = element('td');
            [['start', false], ['restart', true], ['stop', true]].forEach(function (pair) {
                var button = element('button', pair[1] ? 'caution' : 'secondary', pair[0]);
                button.type = 'button';
                button.addEventListener('click', function () {
                    runOperation('service-' + pair[0], { service: service }, pair[1]);
                });
                controls.appendChild(button);
            });
            row.appendChild(controls);
            body.appendChild(row);
        });
    }

    function loadSystem() {
        return request('/api/system').then(function (result) {
            if (result.ok) {
                renderSystem(result.payload);
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /* firewall                                                            */
    /* ------------------------------------------------------------------ */

    function loadFirewall() {
        /* The rules themselves are an ordinary object list, so the same
         * generated table and form serve them. */
        renderEntityViewInto('firewall_rules', nodes.firewallRulesPanel);

        return request('/api/firewall').then(function (result) {
            if (!result.ok) {
                return;
            }
            var payload = result.payload || {};
            nodes.firewallExplanation.textContent = numerals.sanitize(
                payload.explanation || ''
            );

            var readings = nodes.firewallReadings;
            clear(readings);
            [
                ['rules declared', payload.rule_count],
                ['rules active', payload.active_count],
                ['open to anywhere', payload.open_to_anywhere_count],
                ['default policy', payload.default_policy],
                ['ruleset generated', payload.generated ? 'yes' : 'not yet'],
                ['advice', payload.advice]
            ].forEach(function (entry) {
                if (entry[1] === undefined || entry[1] === null) {
                    return;
                }
                readings.appendChild(element('dt', null, entry[0]));
                readings.appendChild(element('dd', null, numerals.sanitize(String(entry[1]))));
            });

            nodes.firewallPreview.textContent = payload.preview
                ? numerals.sanitize(payload.preview)
                : numerals.sanitize(payload.error || 'no ruleset could be generated');
        });
    }

    /* ------------------------------------------------------------------ */
    /* transport security                                                   */
    /* ------------------------------------------------------------------ */

    function loadSecurity() {
        return request('/api/tls').then(function (result) {
            if (!result.ok) {
                return;
            }
            var payload = result.payload || {};
            nodes.securityExplanation.textContent = numerals.sanitize(
                payload.explanation || ''
            );

            var readings = nodes.securityReadings;
            clear(readings);

            /* The fingerprint the appliance reports is deliberately not among
             * these. It is hexadecimal, and every numeral rendered on this page
             * is written out in words; a spelled fingerprint could not be
             * compared against the one a browser displays, which is the only
             * thing a fingerprint is for. It is printed on the appliance's own
             * screen instead, where an operator is standing when they first
             * connect and where no spelling rule applies. */
            [
                ['connection', payload.secured
                    ? 'secured; the password and the session cookie are encrypted on the wire'
                    : 'NOT secured; the password and the session cookie cross this network in the clear'],
                ['certificate', payload.certificate],
                ['private key', payload.private_key],
                ['lowest version accepted', payload.minimum_version],
                ['plain port', payload.redirect_port
                    ? 'answers only by sending a browser to the secured port'
                    : 'not listening']
            ].forEach(function (entry) {
                if (entry[1] === undefined || entry[1] === null) {
                    return;
                }
                readings.appendChild(element('dt', null, entry[0]));
                readings.appendChild(element('dd', null, numerals.sanitize(String(entry[1]))));
            });
        });
    }

    function uploadCertificate(event) {
        event.preventDefault();

        var certificate = nodes.certificateBody.value || '';
        var privateKey = nodes.certificateKey.value || '';
        if (!certificate.trim() || !privateKey.trim()) {
            toast('both the certificate and its private key are needed', 'bad');
            return Promise.resolve();
        }

        nodes.certificateUpload.disabled = true;
        return request('/api/tls/certificate', {
            method: 'POST',
            body: JSON.stringify({ certificate: certificate, private_key: privateKey })
        }).then(function (result) {
            nodes.certificateUpload.disabled = false;
            var payload = result.payload || {};

            if (!result.ok) {
                nodes.certificateOutcome.hidden = false;
                nodes.certificateOutcome.textContent = numerals.sanitize(
                    payload.error || 'the certificate was not accepted'
                );
                toast(payload.error || 'the certificate was not accepted', 'bad');
                return;
            }

            /* The private key is cleared from the page as soon as it has been
             * accepted. There is no reason for it to sit in a form field on a
             * screen somebody may walk away from. */
            nodes.certificateKey.value = '';
            nodes.certificateBody.value = '';

            nodes.certificateOutcome.hidden = false;
            nodes.certificateOutcome.textContent = numerals.sanitize(
                (payload.warning || '') + ' ' + (payload.next_step || '')
            );
            toast('the certificate was checked and stored; apply it when you are ready');
        });
    }

    function applyCertificate() {
        return confirmAction(
            'apply the stored certificate',
            'this restarts the console. every session on this appliance ends, '
                + 'including this one, and every open dashboard has to sign in '
                + 'again. no call in progress is affected. if the new certificate '
                + 'is wrong for this site, the previous one is kept on the '
                + 'appliance and can be put back.'
        ).then(function (confirmed) {
            if (!confirmed) {
                return null;
            }
            return runOperation('certificate-apply', {}, false);
        });
    }

    /* ------------------------------------------------------------------ */
    /* configuration reconciliation                                        */
    /* ------------------------------------------------------------------ */

    function renderDrift(report) {
        var body = nodes.driftBody;
        clear(body);
        nodes.driftExplanation.textContent = numerals.sanitize(report.explanation || '');

        (report.reports || []).forEach(function (item) {
            var row = element('tr');
            cell(row, item.name);
            cell(row, item.status, item.diverged ? 'failed' : 'registered');

            var decision = element('td');
            if (item.diverged) {
                var button = element('button', 'secondary', 'adopt the file on disk');
                button.type = 'button';
                button.addEventListener('click', function () { adopt(item.name); });
                decision.appendChild(button);
            } else {
                decision.textContent = 'no decision required';
            }
            row.appendChild(decision);
            body.appendChild(row);
        });
    }

    function loadDrift() {
        return request('/api/configuration/drift').then(function (result) {
            if (result.ok) {
                renderDrift(result.payload);
            }
        });
    }

    function renderConfiguration(force) {
        return request('/api/configuration/render', {
            method: 'POST',
            body: JSON.stringify({ force: Boolean(force) })
        }).then(function (result) {
            if (result.status === 409) {
                toast('a generated file was edited by hand; choose what to do with it', 'warn');
            } else if (result.ok) {
                toast('the engine configuration was rendered');
            } else {
                toast(result.payload.error || 'the configuration could not be rendered', 'bad');
            }
            loadDrift();
            return result.payload;
        });
    }

    function adopt(name) {
        request('/api/configuration/adopt', {
            method: 'POST',
            body: JSON.stringify({ name: name })
        }).then(function () {
            toast('the file on disk was adopted as authoritative');
            loadDrift();
        });
    }

    /* ------------------------------------------------------------------ */
    /* tasks                                                               */
    /* ------------------------------------------------------------------ */

    function renderTasks(snapshot) {
        var body = nodes.taskBody;
        clear(body);

        var tasks = (snapshot && snapshot.tasks) || [];
        if (!tasks.length) {
            emptyRow(body, 5, 'no task is registered');
            return;
        }

        tasks.forEach(function (task) {
            var row = element('tr');
            cell(row, task.name);
            cell(row, task.description || '');
            cell(row, task.last_status
                ? task.last_status + (task.last_detail ? ' — ' + numerals.sanitize(task.last_detail) : '')
                : 'not yet run');
            cell(row, count(task.run_count));

            var control = element('td');
            var button = element('button', 'secondary', task.running ? 'running' : 'invoke');
            button.type = 'button';
            button.disabled = Boolean(task.running);
            button.addEventListener('click', function () { runTask(task.name); });
            control.appendChild(button);
            row.appendChild(control);
            body.appendChild(row);
        });
    }

    function runTask(name) {
        return request('/api/tasks/run', {
            method: 'POST',
            body: JSON.stringify({ name: name })
        }).then(function (result) {
            var payload = result.payload || {};
            if (payload.succeeded) {
                toast('the task named ' + name + ' completed');
            } else {
                toast(payload.detail || ('the task named ' + name + ' did not succeed'), 'bad');
            }
            loadTasks();
            return payload.detail || 'the task completed';
        });
    }

    function loadTasks() {
        return request('/api/tasks').then(function (result) {
            if (result.ok) {
                renderTasks(result.payload);
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /* logs                                                                */
    /* ------------------------------------------------------------------ */

    function loadLogCatalogue() {
        return request('/api/logs').then(function (result) {
            var select = nodes.logSource;
            var chosen = select.value;
            clear(select);

            (result.payload.logs || []).forEach(function (log) {
                var option = element('option', null,
                    log.label + (log.available ? '' : ' — not present'));
                option.value = log.key;
                option.disabled = !log.available;
                select.appendChild(option);
            });
            if (chosen) {
                select.value = chosen;
            }
        });
    }

    function readLog() {
        var key = nodes.logSource.value;
        if (!key) {
            return Promise.resolve();
        }
        var query = '?lines=300' +
            '&level=' + encodeURIComponent(nodes.logLevel.value || '') +
            '&search=' + encodeURIComponent(nodes.logSearch.value || '');

        return request('/api/logs/' + encodeURIComponent(key) + query).then(function (result) {
            var payload = result.payload || {};
            if (!payload.available) {
                nodes.logView.textContent = numerals.sanitize(
                    payload.explanation || 'this log could not be read'
                );
                return;
            }
            var lines = (payload.lines || []).map(function (line) { return line.text; });
            nodes.logView.textContent = lines.length
                ? lines.join('\n')
                : 'no line matched';
        });
    }

    /* ------------------------------------------------------------------ */
    /* backup and restore                                                  */
    /* ------------------------------------------------------------------ */

    function downloadBackup() {
        var includeSecrets = Boolean(
            nodes.backupSecrets && nodes.backupSecrets.checked
        );
        request('/api/backup?include_secrets=' + (includeSecrets ? 'yes' : 'no'))
            .then(function (result) {
                if (!result.ok || !result.blob) {
                    toast('the backup could not be produced', 'bad');
                    return;
                }
                /* The file names itself after what is in it, because the
                 * difference matters months later when somebody finds it on a
                 * share and has to decide what it is. The appliance chose that
                 * name; this only offers what arrived. */
                saveBlob(result, includeSecrets
                    ? 'crossbar-backup-with-secrets.tar.gz'
                    : 'crossbar-backup.tar.gz');
                toast(includeSecrets
                    ? 'the backup was downloaded, and it carries every password in the clear; keep it as you keep a password'
                    : 'the backup was downloaded; it carries no password, so it can be stored wherever is convenient');
            });
    }

    function downloadSupportBundle() {
        var button = nodes.supportBundleButton;
        if (button) {
            button.disabled = true;
        }
        return request('/api/support-bundle').then(function (result) {
            if (button) {
                button.disabled = false;
            }
            if (!result.ok || !result.blob) {
                toast('the support bundle could not be produced', 'bad');
                return;
            }
            saveBlob(result, 'crossbar-support.tar.gz');
            toast('the support bundle was downloaded; it carries no password, but it does describe this site');
        });
    }

    function restoreBackup(event) {
        event.preventDefault();
        var file = nodes.restoreFile.files && nodes.restoreFile.files[0];
        if (!file) {
            return;
        }

        var replaceCredentials = Boolean(
            nodes.restoreCredentials && nodes.restoreCredentials.checked
        );

        confirmAction(
            'restore from this archive',
            'the configuration and secrets on this appliance will be replaced by those in the archive. ' +
            (replaceCredentials
                ? 'the administrator password will also be put back to what it was when the archive was taken, and the password you signed in with will stop working.'
                : 'the password you signed in with is kept, as is the way this appliance secures itself.')
        ).then(function (answer) {
            if (!answer) {
                return;
            }
            return request('/api/restore?replace_credentials=' +
                (replaceCredentials ? 'yes' : 'no'), {
                method: 'POST',
                body: file,
                headers: { 'Content-Type': 'application/gzip' }
            }).then(function (result) {
                nodes.restoreOutcome.hidden = false;
                if (!result.ok) {
                    nodes.restoreOutcome.className = 'notice bad';
                    nodes.restoreOutcome.textContent = numerals.sanitize(
                        result.payload.error || 'the archive was refused'
                    );
                    return;
                }
                nodes.restoreOutcome.className = 'notice';
                /* What was held back is said out loud. A restore that quietly
                 * ignores part of what it was handed is as surprising as one
                 * that quietly accepts all of it. */
                var held = result.payload.held_back || [];
                nodes.restoreOutcome.textContent = numerals.sanitize(
                    'the backup was restored. ' +
                    (held.length
                        ? 'kept as it was: ' + held.join(', ') + '. '
                        : '') +
                    (result.payload.next_step || '')
                );
                toast('the backup was restored');
            });
        });
    }

    /* ------------------------------------------------------------------ */
    /* constraints                                                         */
    /* ------------------------------------------------------------------ */

    function loadConstraints() {
        return request('/api/constraints').then(function (result) {
            if (!result.ok) {
                return;
            }
            var allocation = result.payload.address_allocation || {};
            nodes.constraintAllocation.className =
                'constraint ' + (allocation.satisfied ? 'satisfied' : 'breached');
            nodes.allocationStatement.textContent = numerals.sanitize(allocation.statement || '');

            clear(nodes.allocationFindings);
            var findings = allocation.findings || [];
            if (!findings.length) {
                nodes.allocationFindings.appendChild(element('li', null,
                    'the audit found nothing; no address allocation service exists on this machine'));
            }
            findings.forEach(function (finding) {
                nodes.allocationFindings.appendChild(element('li', null,
                    finding.severity + ': ' + numerals.sanitize(finding.subject) +
                    ' — ' + numerals.sanitize(finding.detail)));
            });
        }).then(function () {
            return request('/api/system/operations');
        }).then(function (result) {
            if (!result.ok) {
                return;
            }
            nodes.operationsExplanation.textContent = numerals.sanitize(
                result.payload.explanation || ''
            );
            var body = nodes.operationsBody;
            clear(body);
            (result.payload.operations || []).forEach(function (operation) {
                var row = element('tr');
                cell(row, operation.verb);
                cell(row, operation.description);
                cell(row, operation.disruptive ? 'yes' : 'no',
                    operation.disruptive ? 'failed' : 'registered');
                body.appendChild(row);
            });
        });
    }

    /* ------------------------------------------------------------------ */
    /* view loaders                                                        */
    /* ------------------------------------------------------------------ */

    var VIEW_LOADERS = {
        overview: function () { loadState(); loadTrunks(); },
        calls: function () { loadState(); },
        history: function () { loadHistory(); },
        reports: function () { loadReport(); },
        recordings: function () { loadRecordings(); },
        extensions: function () { renderEntityView('extensions'); },
        trunks: function () { renderEntityView('trunks'); },
        ring_groups: function () { renderEntityView('ring_groups'); },
        inbound_routes: function () { renderEntityView('inbound_routes'); },
        outbound_routes: function () { renderEntityView('outbound_routes'); },
        ivr_menus: function () { renderEntityView('ivr_menus'); },
        queues: function () { renderEntityView('queues'); },
        conferences: function () { renderEntityView('conferences'); },
        time_conditions: function () { renderEntityView('time_conditions'); },
        tariffs: function () { renderEntityView('tariffs'); },
        scheduled_reports: function () { renderEntityView('scheduled_reports'); },
        mail_destinations: function () { renderEntityView('mail_destinations'); },
        hardware: function () { loadHardware(); renderWizard(); },
        system: function () { loadSystem(); },
        firewall: function () { loadFirewall(); },
        security: function () { loadSecurity(); },
        configuration: function () { loadDrift(); },
        tasks: function () { loadTasks(); },
        logs: function () { loadLogCatalogue(); },
        journal: function () { loadJournal(); },
        backup: function () {},
        constraints: function () { loadConstraints(); },
        accounts: function () { loadAccounts(); }
    };

    /* ------------------------------------------------------------------ */
    /* sessions                                                            */
    /* ------------------------------------------------------------------ */

    function signIn(event) {
        event.preventDefault();
        nodes.signInFailure.hidden = true;
        nodes.signInButton.disabled = true;

        request('/api/session', {
            method: 'POST',
            body: JSON.stringify({
                username: nodes.username.value,
                password: nodes.password.value
            })
        }).then(function (result) {
            nodes.signInButton.disabled = false;
            if (!result.ok) {
                nodes.signInFailure.hidden = false;
                nodes.signInFailure.textContent = numerals.sanitize(
                    result.payload.error || 'the credentials were not accepted'
                );
                return;
            }
            nodes.password.value = '';
            /* Where somebody lands is decided by what the appliance said their
             * account is, not by anything the browser chose. */
            if ((result.payload || {}).role === 'extension') {
                enterPortal();
            } else {
                enterConsole();
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /* the portal, and the accounts that reach it                          */
    /* ------------------------------------------------------------------ */

    function enterPortal() {
        nodes.signInPanel.hidden = true;
        nodes.console.hidden = true;
        nodes.portal.hidden = false;
        nodes.signOutButton.hidden = false;
        /* No socket. The live channel carries the state of the whole
         * appliance, and an account scoped to one extension has no business
         * holding one open.
         *
         * And so no link indicator either: a lamp reporting a connection this
         * page deliberately never opens reads as a fault, and the first thing
         * it would produce is somebody ringing to report one. */
        nodes.linkState.hidden = true;
        loadPortal();
    }

    function loadPortal() {
        request('/api/portal').then(function (result) {
            var payload = result.payload || {};
            nodes.portalHeading.textContent = numerals.sanitize(
                payload.extension
                    ? 'extension ' + payload.extension +
                      (payload.name ? ', ' + payload.name : '')
                    : 'your extension'
            );
            nodes.portalExplanation.textContent = numerals.sanitize(
                payload.explanation || ''
            );
        });

        request('/api/portal/calls').then(function (result) {
            var payload = result.payload || {};
            var holder = nodes.portalCalls;
            clear(holder);
            nodes.portalCallsExplanation.textContent = numerals.sanitize(
                payload.available
                    ? 'showing ' + payload.record_count + ' calls, newest first'
                    : (payload.explanation || 'there is nothing to show yet')
            );
            if (!payload.available) { return; }

            var table = element('table', 'grid');
            var head = element('thead');
            var headRow = element('tr');
            ['when', 'from', 'to', 'duration', 'talk time', 'outcome']
                .forEach(function (heading) {
                    headRow.appendChild(element('th', null, heading));
                });
            head.appendChild(headRow);
            table.appendChild(head);

            var body = element('tbody');
            var records = payload.records || [];
            if (!records.length) {
                emptyRow(body, 6, 'no call has been recorded against this extension');
            }
            records.forEach(function (record) {
                var row = element('tr');
                cell(row, record.started_at);
                cell(row, record.source);
                cell(row, record.destination);
                cell(row, record.duration);
                cell(row, record.talk_time);
                cell(row, (record.disposition || '').toLowerCase(),
                    record.answered ? 'registered' : 'failed');
                body.appendChild(row);
            });
            table.appendChild(body);
            holder.appendChild(makeScrollable(table, 'your calls'));
        });

        request('/api/portal/recordings').then(function (result) {
            var payload = result.payload || {};
            var holder = nodes.portalRecordings;
            clear(holder);
            nodes.portalRecordingsExplanation.textContent = numerals.sanitize(
                payload.available
                    ? 'showing ' + payload.record_count + ' recordings, newest first'
                    : (payload.explanation || 'there is no recording to show')
            );
            if (!payload.available || !(payload.records || []).length) { return; }

            var list = element('ul', 'recording-list');
            payload.records.forEach(function (record) {
                var item = element('li');
                item.appendChild(element('span', null,
                    numerals.sanitize(record.at + ', ') + record.source +
                    ' to ' + record.destination + ' '));
                var player = element('audio');
                player.controls = true;
                player.preload = 'none';
                player.src = '/api/portal/recordings/' + encodeURIComponent(record.name);
                item.appendChild(player);
                list.appendChild(item);
            });
            holder.appendChild(list);
        });
    }

    function loadAccounts() {
        return request('/api/accounts').then(function (result) {
            var payload = result.payload || {};
            nodes.accountsExplanation.textContent = numerals.sanitize(
                payload.explanation || ''
            );

            var holder = nodes.accountsTable;
            clear(holder);
            var table = element('table', 'grid');
            var head = element('thead');
            var headRow = element('tr');
            ['account', 'may read', 'actions'].forEach(function (heading) {
                headRow.appendChild(element('th', null, heading));
            });
            head.appendChild(headRow);
            table.appendChild(head);

            var body = element('tbody');
            (payload.accounts || []).forEach(function (account) {
                var row = element('tr');
                cell(row, account.username);
                cell(row, account.role === 'administrator'
                    ? 'the whole appliance'
                    : 'the extension numbered ' + account.scope);
                var actions = element('td', 'actions-cell');
                if (account.role !== 'administrator') {
                    var remove = element('button', 'caution', 'delete');
                    remove.type = 'button';
                    remove.addEventListener('click', function () {
                        removeAccount(account.username);
                    });
                    actions.appendChild(remove);
                }
                row.appendChild(actions);
                body.appendChild(row);
            });
            table.appendChild(body);
            holder.appendChild(makeScrollable(table, 'accounts'));
        });
    }

    function saveAccount(event) {
        event.preventDefault();
        request('/api/accounts', {
            method: 'POST',
            body: JSON.stringify({
                username: nodes.accountUsername.value,
                scope: nodes.accountScope.value,
                password: nodes.accountPassword.value
            })
        }).then(function (result) {
            if (!result.ok) {
                toast(result.payload.error || 'the account could not be saved', 'bad');
                return;
            }
            nodes.accountPassword.value = '';
            toast('the account was saved');
            loadAccounts();
        });
    }

    function removeAccount(username) {
        confirmAction('delete the account named ' + username,
            'that person will no longer be able to sign in.')
            .then(function (answer) {
                if (!answer) { return; }
                request('/api/accounts/' + encodeURIComponent(username), {
                    method: 'DELETE'
                }).then(function (result) {
                    if (!result.ok) {
                        toast(result.payload.error || 'the account could not be deleted', 'bad');
                        return;
                    }
                    toast('the account was deleted');
                    loadAccounts();
                });
            });
    }

    function signOut() {
        request('/api/session/end', { method: 'POST' }).then(function () {
            socket.close();
            nodes.console.hidden = true;
            nodes.portal.hidden = true;
            nodes.linkState.hidden = false;
            nodes.signOutButton.hidden = true;
            nodes.signInPanel.hidden = false;
            renderLink({ state: 'reconnecting', reason: 'signed out' });
        });
    }

    function enterConsole() {
        nodes.signInPanel.hidden = true;
        nodes.linkState.hidden = false;
        nodes.console.hidden = false;
        nodes.signOutButton.hidden = false;

        request('/api/schema').then(function (result) {
            if (result.ok) {
                state.schema = result.payload;
            }
            showView(state.currentView);
        });

        loadState();
        loadTrunks();
        socket.connect();
    }

    /* ------------------------------------------------------------------ */
    /* socket wiring                                                       */
    /* ------------------------------------------------------------------ */

    function wireSocket() {
        socket.on('link', renderLink);

        socket.on('welcome', function (payload) {
            if (payload.state) {
                renderState(payload.state);
            }
            if (payload.trunks) {
                renderTrunks(payload.trunks);
            }
            nodes.footerText.textContent = payload.assigns_addresses
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

        socket.on('trunk.transition', function () { loadTrunks(); });

        /* The topics the appliance never holds back.  Each one is already
         * described by the state snapshot that accompanies it, so the handler
         * only has to say it out loud; the panels redraw from the snapshot. */
        socket.on('alarm.raised', function (payload) {
            /* A critical alarm interrupts. It is the one thing on this console
             * that means somebody has to stop what they are doing. */
            toast(payload.message || 'an alarm was raised', 'bad',
                (payload.severity || '') === 'critical');
        });
        socket.on('engine.disconnected', function () {
            toast('the telephony engine connection was lost', 'bad');
        });
        socket.on('driver.stage-failed', function (payload) {
            toast('the driver stage named ' + (payload.verb || 'unknown') + ' failed', 'bad');
        });

        socket.on('task.finished', function () {
            if (state.currentView === 'tasks') {
                loadTasks();
            }
        });
        socket.on('heartbeat', function () { renderLink(socket.linkState()); });
    }

    /* ------------------------------------------------------------------ */
    /* startup                                                             */
    /* ------------------------------------------------------------------ */

    function bind() {
        [
            ['firewallExplanation', 'firewall-explanation'],
            ['firewallReadings', 'firewall-readings'],
            ['firewallPreview', 'firewall-preview'],
            ['firewallRulesPanel', 'firewall-rules-panel'],
            ['firewallRender', 'firewall-render'],
            ['firewallApply', 'firewall-apply'],
            ['firewallStatus', 'firewall-status'],
            ['firewallClear', 'firewall-clear'],
            ['securityExplanation', 'security-explanation'],
            ['securityReadings', 'security-readings'],
            ['certificateForm', 'certificate-form'],
            ['certificateBody', 'certificate-body'],
            ['certificateKey', 'certificate-key'],
            ['certificateUpload', 'certificate-upload'],
            ['certificateApply', 'certificate-apply'],
            ['certificateOutcome', 'certificate-outcome'],
            ['signInPanel', 'sign-in-panel'], ['signInForm', 'sign-in-form'],
            ['signInFailure', 'sign-in-failure'], ['signInButton', 'sign-in-button'],
            ['username', 'username'], ['password', 'password'],
            ['console', 'console'], ['signOutButton', 'sign-out-button'],
            ['linkLamp', 'link-lamp'], ['linkText', 'link-text'], ['linkAge', 'link-age'],
            ['figureActiveCalls', 'figure-active-calls'], ['captionAnswered', 'caption-answered'],
            ['figureTrunks', 'figure-trunks'], ['captionTrunks', 'caption-trunks'],
            ['figureCards', 'figure-cards'], ['captionSpans', 'caption-spans'],
            ['figureUptime', 'figure-uptime'], ['captionPeak', 'caption-peak'],
            ['alarmPanel', 'alarm-panel'], ['alarmList', 'alarm-list'],
            ['channelBody', 'channel-body'], ['trunkBody', 'trunk-body'],
            ['historyBody', 'history-body'], ['historySearch', 'history-search'],
            ['historyExplanation', 'history-explanation'], ['historyRefresh', 'history-refresh'],
            ['reportsExplanation', 'reports-explanation'], ['reportWindow', 'report-window'],
            ['reportDates', 'report-dates'], ['reportFrom', 'report-from'],
            ['reportTo', 'report-to'], ['reportRefresh', 'report-refresh'],
            ['reportDownloadCalls', 'report-download-calls'], ['reportTiles', 'report-tiles'],
            ['reportSummaryPanel', 'report-summary-panel'], ['reportChartPanel', 'report-chart-panel'],
            ['reportChart', 'report-chart'], ['reportChartSummary', 'report-chart-summary'],
            ['reportBreakdowns', 'report-breakdowns'], ['reportTruncated', 'report-truncated'],
            ['reportQueues', 'report-queues'], ['reportQueueTiles', 'report-queue-tiles'],
            ['reportQueuesExplanation', 'report-queues-explanation'],
            ['reportQueueBreakdowns', 'report-queue-breakdowns'],
            ['recordingsExplanation', 'recordings-explanation'],
            ['recordingsSearch', 'recordings-search'], ['recordingsFrom', 'recordings-from'],
            ['recordingsTo', 'recordings-to'], ['recordingsRefresh', 'recordings-refresh'],
            ['recordingsTable', 'recordings-table'], ['recordingsNote', 'recordings-note'],
            ['snapshotsExplanation', 'snapshots-explanation'],
            ['snapshotsTable', 'snapshots-table'],
            ['linkState', 'link-state'], ['portal', 'portal'], ['portalHeading', 'portal-heading'],
            ['portalExplanation', 'portal-explanation'],
            ['portalCalls', 'portal-calls'],
            ['portalCallsExplanation', 'portal-calls-explanation'],
            ['portalRecordings', 'portal-recordings'],
            ['portalRecordingsExplanation', 'portal-recordings-explanation'],
            ['accountsExplanation', 'accounts-explanation'],
            ['accountsTable', 'accounts-table'], ['accountForm', 'account-form'],
            ['accountUsername', 'account-username'], ['accountScope', 'account-scope'],
            ['accountPassword', 'account-password'],
            ['hardwareSummary', 'hardware-summary'], ['cardBody', 'card-body'],
            ['spanBody', 'span-body'], ['hardwareWizard', 'hardware-wizard'],
            ['systemReadings', 'system-readings'], ['interfaceBody', 'interface-body'],
            ['networkInterface', 'network-interface'], ['networkForm', 'network-form'],
            ['networkAddress', 'network-address'], ['networkPrefix', 'network-prefix'],
            ['networkGateway', 'network-gateway'], ['identityForm', 'identity-form'],
            ['systemHostname', 'system-hostname'], ['systemTimezone', 'system-timezone'],
            ['timeSyncButton', 'time-sync-button'], ['serviceBody', 'service-body'],
            ['rebootButton', 'reboot-button'], ['powerOffButton', 'power-off-button'],
            ['driftBody', 'drift-body'], ['driftExplanation', 'drift-explanation'],
            ['renderButton', 'render-button'], ['renderForceButton', 'render-force-button'],
            ['engineReloadButton', 'engine-reload-button'],
            ['taskBody', 'task-body'],
            ['logSource', 'log-source'], ['logLevel', 'log-level'], ['logSearch', 'log-search'],
            ['logForm', 'log-form'], ['logView', 'log-view'],
            ['backupButton', 'backup-button'], ['backupSecrets', 'backup-secrets'],
            ['supportBundleButton', 'support-bundle-button'],
            ['restoreForm', 'restore-form'],
            ['restoreFile', 'restore-file'], ['restoreOutcome', 'restore-outcome'],
            ['restoreCredentials', 'restore-credentials'],
            ['journalTable', 'journal-table'], ['journalRefresh', 'journal-refresh'],
            ['constraintAllocation', 'constraint-allocation'],
            ['allocationStatement', 'allocation-statement'],
            ['allocationFindings', 'allocation-findings'],
            ['operationsExplanation', 'operations-explanation'],
            ['operationsBody', 'operations-body'],
            ['footerText', 'footer-text'], ['toastHolder', 'toast-holder'],
            ['toastHolderUrgent', 'toast-holder-urgent'],
            ['confirmShade', 'confirm-shade'], ['confirmBox', 'confirm-box'],
            ['confirmTitle', 'confirm-title'],
            ['confirmMessage', 'confirm-message'],
            ['confirmYes', 'confirm-yes'], ['confirmNo', 'confirm-no']
        ].forEach(function (pair) {
            nodes[pair[0]] = byId(pair[1]);
        });
    }

    function wireControls() {
        nodes.signInForm.addEventListener('submit', signIn);
        nodes.signOutButton.addEventListener('click', signOut);

        Array.prototype.forEach.call(document.querySelectorAll('.nav-item'), function (item) {
            item.addEventListener('click', function () { showView(item.dataset.view); });
        });

        nodes.historyRefresh.addEventListener('click', loadHistory);
        nodes.reportRefresh.addEventListener('click', loadReport);
        nodes.recordingsRefresh.addEventListener('click', loadRecordings);
        nodes.accountForm.addEventListener('submit', saveAccount);
        nodes.reportDownloadCalls.addEventListener('click', function () {
            downloadReport('');
        });
        nodes.renderButton.addEventListener('click', function () { renderConfiguration(false); });
        nodes.renderForceButton.addEventListener('click', function () {
            confirmAction('regenerate over local edits',
                'edits made by hand to the generated files will be lost.')
                .then(function (answer) {
                    if (answer) {
                        renderConfiguration(true);
                    }
                });
        });
        nodes.engineReloadButton.addEventListener('click', function () {
            runOperation('engine-reload', {}, false);
        });

        nodes.networkForm.addEventListener('submit', function (event) {
            event.preventDefault();
            runOperation('network-apply', {
                interface: nodes.networkInterface.value,
                address: nodes.networkAddress.value,
                prefix: nodes.networkPrefix.value,
                gateway: nodes.networkGateway.value || ''
            }, true).then(loadSystem);
        });

        nodes.identityForm.addEventListener('submit', function (event) {
            event.preventDefault();
            runOperation('hostname-set', { hostname: nodes.systemHostname.value }, false)
                .then(function () {
                    return runOperation('timezone-set', { timezone: nodes.systemTimezone.value }, false);
                })
                .then(loadSystem);
        });

        nodes.timeSyncButton.addEventListener('click', function () {
            runOperation('time-synchronise', {}, false);
        });

        nodes.rebootButton.addEventListener('click', function () {
            runOperation('reboot', {}, true);
        });
        nodes.powerOffButton.addEventListener('click', function () {
            runOperation('power-off', {}, true);
        });

        nodes.logForm.addEventListener('submit', function (event) {
            event.preventDefault();
            readLog();
        });

        nodes.firewallRender.addEventListener('click', function () {
            runTask('render-firewall').then(loadFirewall);
        });
        nodes.firewallApply.addEventListener('click', function () {
            runOperation('firewall-apply', {}, false).then(loadFirewall);
        });
        nodes.firewallStatus.addEventListener('click', function () {
            runOperation('firewall-status', {}, false);
        });
        nodes.certificateForm.addEventListener('submit', uploadCertificate);
        nodes.certificateApply.addEventListener('click', applyCertificate);
        nodes.firewallClear.addEventListener('click', function () {
            runOperation('firewall-clear', {}, true).then(loadFirewall);
        });

        nodes.backupButton.addEventListener('click', downloadBackup);
        nodes.supportBundleButton.addEventListener('click', downloadSupportBundle);
        nodes.restoreForm.addEventListener('submit', restoreBackup);
        nodes.journalRefresh.addEventListener('click', loadJournal);

        nodes.confirmYes.addEventListener('click', function () { settleConfirm(true); });
        nodes.confirmNo.addEventListener('click', function () { settleConfirm(false); });
    }

    function start() {
        bind();
        watchFocus();
        wireSocket();
        wireControls();

        /* The staleness readout must advance even when nothing arrives — that
         * is the entire point of distinguishing stale from quiet. */
        window.setInterval(function () {
            if (!nodes.console.hidden) {
                renderLink(socket.linkState());
            }
        }, 1000);

        request('/api/session').then(function (result) {
            var payload = (result.ok && result.payload) || {};
            if (!payload.authenticated) {
                return;
            }
            /* A reload lands where the account belongs, for the same reason a
             * sign in does: the appliance says which, not the browser. */
            if (payload.role === 'extension') {
                enterPortal();
            } else {
                enterConsole();
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
}());
