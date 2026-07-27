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
        shedDashboards: null
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
                    return { ok: response.ok, status: response.status, blob: blob, payload: {} };
                });
            }
            return response.json().then(function (payload) {
                return { ok: response.ok, status: response.status, payload: payload };
            }).catch(function () {
                return { ok: response.ok, status: response.status, payload: {} };
            });
        });
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

    function toast(message, kind) {
        var holder = nodes.toastHolder;
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

    function renderAlarms(alarms) {
        clear(nodes.alarmList);
        if (!alarms.length) {
            nodes.alarmPanel.hidden = true;
            return;
        }
        nodes.alarmPanel.hidden = false;

        alarms.forEach(function (alarm) {
            var item = element('li', alarm.severity || 'warning');
            item.textContent = alarm.message + ' — raised ' + duration(alarm.age_seconds) + ' ago';
            if (alarm.detail) {
                item.appendChild(element('div', 'alarm-detail', numerals.sanitize(alarm.detail)));
            }
            nodes.alarmList.appendChild(item);
        });
    }

    function renderTrunks(snapshot) {
        if (!snapshot) {
            return;
        }
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

            var scroller = element('div', 'table-scroll');
            scroller.setAttribute('tabindex', '0');
            scroller.setAttribute('role', 'region');
            scroller.setAttribute('aria-label', 'the record of changes');
            scroller.appendChild(table);
            holder.appendChild(scroller);
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

    function renderEntityViewInto(kind, view) {
        var spec = specFor(kind);
        if (!spec || !view) {
            return Promise.resolve();
        }

        return loadReferences().then(function () {
            return request('/api/entities/' + kind);
        }).then(function (result) {
            clear(view);

            var panel = element('section', 'panel');
            panel.appendChild(element('h2', null, spec.plural));
            panel.appendChild(element('p', 'hint', spec.description));

            var records = (result.payload && result.payload.records) || [];
            panel.appendChild(forms.buildTable(spec, records, {
                edit: function (record) { openEntityForm(kind, record); },
                remove: function (record) { removeEntity(kind, record); }
            }));

            var actions = element('div', 'actions');
            var add = element('button', null, 'add ' + spec.singular);
            add.type = 'button';
            add.addEventListener('click', function () { openEntityForm(kind, null); });
            actions.appendChild(add);
            panel.appendChild(actions);

            view.appendChild(panel);

            var holder = element('section', 'panel form-holder');
            holder.id = 'form-holder-' + kind;
            holder.hidden = true;
            view.appendChild(holder);
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
        request('/api/backup').then(function (result) {
            if (!result.ok || !result.blob) {
                toast('the backup could not be produced', 'bad');
                return;
            }
            var url = URL.createObjectURL(result.blob);
            var anchor = element('a');
            anchor.href = url;
            anchor.download = 'myipbx-backup.tar.gz';
            document.body.appendChild(anchor);
            anchor.click();
            document.body.removeChild(anchor);
            URL.revokeObjectURL(url);
            toast('the backup was downloaded; store it as you would store a password');
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
        extensions: function () { renderEntityView('extensions'); },
        trunks: function () { renderEntityView('trunks'); },
        ring_groups: function () { renderEntityView('ring_groups'); },
        inbound_routes: function () { renderEntityView('inbound_routes'); },
        outbound_routes: function () { renderEntityView('outbound_routes'); },
        ivr_menus: function () { renderEntityView('ivr_menus'); },
        queues: function () { renderEntityView('queues'); },
        conferences: function () { renderEntityView('conferences'); },
        time_conditions: function () { renderEntityView('time_conditions'); },
        hardware: function () { loadHardware(); renderWizard(); },
        system: function () { loadSystem(); },
        firewall: function () { loadFirewall(); },
        security: function () { loadSecurity(); },
        configuration: function () { loadDrift(); },
        tasks: function () { loadTasks(); },
        logs: function () { loadLogCatalogue(); },
        journal: function () { loadJournal(); },
        backup: function () {},
        constraints: function () { loadConstraints(); }
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
            enterConsole();
        });
    }

    function signOut() {
        request('/api/session/end', { method: 'POST' }).then(function () {
            socket.close();
            nodes.console.hidden = true;
            nodes.signOutButton.hidden = true;
            nodes.signInPanel.hidden = false;
            renderLink({ state: 'reconnecting', reason: 'signed out' });
        });
    }

    function enterConsole() {
        nodes.signInPanel.hidden = true;
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
            toast(payload.message || 'an alarm was raised', 'bad');
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
            ['backupButton', 'backup-button'], ['restoreForm', 'restore-form'],
            ['restoreFile', 'restore-file'], ['restoreOutcome', 'restore-outcome'],
            ['restoreCredentials', 'restore-credentials'],
            ['journalTable', 'journal-table'], ['journalRefresh', 'journal-refresh'],
            ['constraintAllocation', 'constraint-allocation'],
            ['allocationStatement', 'allocation-statement'],
            ['allocationFindings', 'allocation-findings'],
            ['operationsExplanation', 'operations-explanation'],
            ['operationsBody', 'operations-body'],
            ['footerText', 'footer-text'], ['toastHolder', 'toast-holder'],
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
        nodes.restoreForm.addEventListener('submit', restoreBackup);
        nodes.journalRefresh.addEventListener('click', loadJournal);

        nodes.confirmYes.addEventListener('click', function () { settleConfirm(true); });
        nodes.confirmNo.addEventListener('click', function () { settleConfirm(false); });
    }

    function start() {
        bind();
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
            if (result.ok && result.payload && result.payload.authenticated) {
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
