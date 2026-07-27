/*
 * Persistent bidirectional socket client.
 *
 * This is the answer to Benchmark Defect Two on the browser side.  State
 * arrives when the appliance pushes it, not when a timer decides to ask.  A
 * dual layer heartbeat runs above the protocol's own ping so that the page can
 * tell three conditions apart that a polling dashboard conflates:
 *
 *   live         -- the appliance spoke within the expected interval,
 *   reconnecting -- the socket is down and a retry is scheduled,
 *   stale        -- the socket claims to be open but the appliance has gone
 *                   quiet for longer than the permitted number of intervals.
 *
 * The third case is the one that matters.  A frozen screen that looks like a
 * quiet night is the failure this product exists to remove, so silence is
 * always rendered as silence.
 */

(function (root) {
    'use strict';

    var LINK_LIVE = 'live';
    var LINK_RECONNECTING = 'reconnecting';
    var LINK_STALE = 'stale';

    function Client(options) {
        options = options || {};
        this.heartbeatIntervalSeconds = options.heartbeatIntervalSeconds || 5;
        this.missedLimit = options.missedLimit || 3;
        this.retryBaseSeconds = options.retryBaseSeconds || 1;
        this.retryCeilingSeconds = options.retryCeilingSeconds || 30;

        this.socket = null;
        this.listeners = {};
        this.attempt = 0;
        this.sequence = 0;
        this.lastMessageAt = 0;
        this.lastStateSequence = null;
        this.deliberatelyClosed = false;
        this.heartbeatTimer = null;
        this.retryTimer = null;
    }

    Client.prototype.address = function () {
        var scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return scheme + '//' + window.location.host + '/socket';
    };

    Client.prototype.on = function (topic, handler) {
        if (!this.listeners[topic]) {
            this.listeners[topic] = [];
        }
        this.listeners[topic].push(handler);
        return this;
    };

    Client.prototype.emit = function (topic, payload, envelope) {
        var handlers = this.listeners[topic] || [];
        for (var index = 0; index < handlers.length; index += 1) {
            try {
                handlers[index](payload, envelope);
            } catch (error) {
                /* One faulty listener must not silence the rest of the page. */
                if (window.console && window.console.error) {
                    window.console.error('a socket listener failed', error);
                }
            }
        }
    };

    Client.prototype.connect = function () {
        var client = this;
        this.deliberatelyClosed = false;
        this.clearRetry();

        var socket;
        try {
            socket = new WebSocket(this.address());
        } catch (error) {
            this.scheduleRetry();
            return;
        }
        this.socket = socket;
        this.emit('link', { state: LINK_RECONNECTING, reason: 'the socket is opening' });

        socket.onopen = function () {
            client.attempt = 0;
            client.lastMessageAt = Date.now();
            client.startHeartbeat();
            client.emit('link', { state: LINK_LIVE, reason: 'the socket is open' });
        };

        socket.onmessage = function (event) {
            client.lastMessageAt = Date.now();
            var envelope;
            try {
                envelope = JSON.parse(event.data);
            } catch (error) {
                return;
            }
            if (envelope && typeof envelope.topic === 'string') {
                if (envelope.payload && envelope.payload.state_sequence !== undefined) {
                    client.lastStateSequence = envelope.payload.state_sequence;
                }
                client.emit(envelope.topic, envelope.payload, envelope);
                client.emit('any', envelope.payload, envelope);
            }
        };

        socket.onerror = function () {
            client.emit('link', {
                state: LINK_RECONNECTING,
                reason: 'the socket reported an error'
            });
        };

        socket.onclose = function () {
            client.stopHeartbeat();
            client.socket = null;
            if (client.deliberatelyClosed) {
                client.emit('link', {
                    state: LINK_RECONNECTING,
                    reason: 'the socket was closed by this page'
                });
                return;
            }
            client.scheduleRetry();
        };
    };

    Client.prototype.close = function () {
        this.deliberatelyClosed = true;
        this.clearRetry();
        this.stopHeartbeat();
        if (this.socket) {
            try {
                this.socket.close(1000, 'the page is signing out');
            } catch (error) {
                /* Closing an already closed socket is not an error worth showing. */
            }
            this.socket = null;
        }
    };

    Client.prototype.send = function (message) {
        if (!this.socket || this.socket.readyState !== 1) {
            return false;
        }
        try {
            this.socket.send(JSON.stringify(message));
            return true;
        } catch (error) {
            return false;
        }
    };

    /* -- heartbeat ------------------------------------------------------- */

    Client.prototype.startHeartbeat = function () {
        var client = this;
        this.stopHeartbeat();
        this.heartbeatTimer = window.setInterval(function () {
            client.sequence += 1;
            client.send({ action: 'heartbeat', sequence: client.sequence });
            client.emit('link', client.linkState());
        }, this.heartbeatIntervalSeconds * 1000);
    };

    Client.prototype.stopHeartbeat = function () {
        if (this.heartbeatTimer !== null) {
            window.clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = null;
        }
    };

    Client.prototype.silenceSeconds = function () {
        if (!this.lastMessageAt) {
            return 0;
        }
        return Math.floor((Date.now() - this.lastMessageAt) / 1000);
    };

    Client.prototype.linkState = function () {
        var silence = this.silenceSeconds();
        var tolerated = this.heartbeatIntervalSeconds * this.missedLimit;

        if (!this.socket || this.socket.readyState !== 1) {
            return {
                state: LINK_RECONNECTING,
                seconds: silence,
                reason: 'the socket is not open'
            };
        }
        if (silence > tolerated) {
            return {
                state: LINK_STALE,
                seconds: silence,
                reason: 'the appliance has not spoken within the permitted number of intervals'
            };
        }
        return { state: LINK_LIVE, seconds: silence, reason: 'the appliance is current' };
    };

    /* -- reconnection ---------------------------------------------------- */

    Client.prototype.scheduleRetry = function () {
        var client = this;
        this.attempt += 1;

        /* The same doubling with jitter the appliance uses, so a restarted
         * appliance is not met by a stampede of synchronised browsers. */
        var doublings = Math.min(this.attempt - 1, 16);
        var delay = Math.min(
            this.retryBaseSeconds * Math.pow(2, doublings),
            this.retryCeilingSeconds
        );
        delay = delay * (0.75 + Math.random() * 0.5);

        this.emit('link', {
            state: LINK_RECONNECTING,
            seconds: Math.round(delay),
            attempt: this.attempt,
            reason: 'a reconnection is scheduled'
        });

        this.retryTimer = window.setTimeout(function () {
            client.connect();
        }, delay * 1000);
    };

    Client.prototype.clearRetry = function () {
        if (this.retryTimer !== null) {
            window.clearTimeout(this.retryTimer);
            this.retryTimer = null;
        }
    };

    var ApplianceSocket = {
        Client: Client,
        LINK_LIVE: LINK_LIVE,
        LINK_RECONNECTING: LINK_RECONNECTING,
        LINK_STALE: LINK_STALE
    };

    root.ApplianceSocket = ApplianceSocket;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = ApplianceSocket;
    }
}(typeof globalThis !== 'undefined' ? globalThis : this));
