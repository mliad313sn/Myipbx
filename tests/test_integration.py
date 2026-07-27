"""End to end tests against a real appliance over real sockets.

Nothing here is mocked below the interface: a real listener is bound, real
requests are made, real socket upgrades are performed, and real frames are
exchanged.  The concurrency case carries no fewer than one hundred
simultaneous dashboard sessions and one hundred simultaneous active calls,
which is the scale the product specification names.
"""

from __future__ import annotations

import asyncio
import json
import re
import unittest

from support import (
    ApplianceHarness,
    SocketTestClient,
    TEST_PASSWORD,
    TEST_USERNAME,
    engine_event,
)

from appliance import numerals


DOCUMENT = {
    "revision": 1,
    "trunks": [
        {"name": "carrier-primary", "technology": "PJSIP", "host": "sip.example.net",
         "username": "primary-account", "enabled": True},
        {"name": "carrier-secondary", "technology": "PJSIP", "host": "sip2.example.net",
         "username": "secondary-account", "enabled": True},
    ],
    "extensions": [{"number": "201", "target": "PJSIP/201"}],
    "dialplan": {"internal_context": "internal", "inbound_context": "from-trunk"},
    "hardware": {"spans": []},
}


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    """The representational state transfer interface, over real transport."""

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)
        self.appliance = await self.harness.start()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    # -- authentication ---------------------------------------------------

    async def test_the_health_route_is_served_without_a_session(self) -> None:
        status, _, payload = await self.harness.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["product"], "Legacy-to-Modern IPBX Appliance")
        self.assertFalse(payload["assigns_addresses"])

    async def test_every_other_route_refuses_an_anonymous_request(self) -> None:
        for path in (
            "/api/state", "/api/trunks", "/api/hardware", "/api/tasks",
            "/api/sessions", "/api/constraints", "/api/configuration",
            "/api/configuration/drift",
        ):
            with self.subTest(path=path):
                status, _, payload = await self.harness.request("GET", path)
                self.assertEqual(status, 401)
                self.assertIn("session", payload["error"])

    async def test_a_write_route_refuses_an_anonymous_request(self) -> None:
        status, _, _ = await self.harness.request(
            "POST", "/api/tasks/run", body=json.dumps({"name": "health-sweep"})
        )
        self.assertEqual(status, 401)

    async def test_signing_in_issues_a_protected_session_cookie(self) -> None:
        status, headers, payload = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["signed_in"])

        cookie = headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)

    async def test_the_wrong_credentials_are_refused(self) -> None:
        status, _, payload = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": "the wrong one"}),
        )
        self.assertEqual(status, 401)
        self.assertIn("not accepted", payload["error"])

    async def test_repeated_failures_lock_the_source_out(self) -> None:
        for _ in range(self.harness.config.login_attempt_limit):
            await self.harness.request(
                "POST", "/api/session",
                body=json.dumps({"username": TEST_USERNAME, "password": "wrong"}),
            )
        status, _, payload = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        )
        self.assertEqual(status, 429)
        # Even the refusal spells its numerals.
        self.assertFalse(numerals.contains_digit(payload["error"]))

    async def test_signing_out_invalidates_the_session(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, _ = await self.harness.request("GET", "/api/state", cookie=cookie)
        self.assertEqual(status, 200)

        await self.harness.request("POST", "/api/session/end", cookie=cookie)
        status, _, _ = await self.harness.request("GET", "/api/state", cookie=cookie)
        self.assertEqual(status, 401)

    async def test_a_foreign_origin_is_refused_on_a_write_route(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, payload = await self.harness.request(
            "POST", "/api/tasks/run",
            body=json.dumps({"name": "health-sweep"}),
            cookie=cookie,
            extra_headers={"Origin": "https://an-attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("origin", payload["error"])

    async def test_a_foreign_origin_is_refused_on_the_sign_in_route(self) -> None:
        """Signing in is a write route too, and was the one that went unchecked.

        Every other write route demands a session before it looks at the
        origin, so signing in could not use the same guard and ended up with no
        origin check at all — leaving the one route an attacker would aim at
        first as the only one not covered by the rule.
        """
        status, _, payload = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps(
                {"username": TEST_USERNAME, "password": TEST_PASSWORD}
            ),
            extra_headers={"Origin": "https://an-attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("origin", payload["error"])
        self.assertFalse(numerals.contains_digit(payload["error"]))

    async def test_signing_in_from_the_appliance_own_origin_still_works(self) -> None:
        status, headers, _ = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps(
                {"username": TEST_USERNAME, "password": TEST_PASSWORD}
            ),
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 200)
        self.assertIn("set-cookie", {name.lower() for name in headers})

    async def test_signing_in_without_an_origin_header_still_works(self) -> None:
        """A request with no origin did not come from a browser page.

        Refusing those would break every script and every recovery procedure
        that signs in from the appliance's own command line, and they are not
        the threat the check exists to defeat.
        """
        status, _, _ = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps(
                {"username": TEST_USERNAME, "password": TEST_PASSWORD}
            ),
        )
        self.assertEqual(status, 200)

    async def test_the_appliance_own_origin_is_accepted(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, _ = await self.harness.request(
            "POST", "/api/tasks/run",
            body=json.dumps({"name": "hardware-rescan"}),
            cookie=cookie,
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 200)

    # -- reads --------------------------------------------------------------

    async def test_the_state_route_describes_the_appliance(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, payload = await self.harness.request("GET", "/api/state", cookie=cookie)

        self.assertEqual(status, 200)
        for key in ("sequence", "active_calls", "channels", "alarms", "socket", "spelled"):
            self.assertIn(key, payload)
        self.assertEqual(payload["spelled"]["active_calls"], "zero")

    async def test_the_trunk_route_reports_every_declared_trunk(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, payload = await self.harness.request("GET", "/api/trunks", cookie=cookie)

        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["spelled"]["total"], "two")
        names = {trunk["name"] for trunk in payload["trunks"]}
        self.assertEqual(names, {"carrier-primary", "carrier-secondary"})

    async def test_the_hardware_route_reports_the_inventory(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, payload = await self.harness.request("GET", "/api/hardware", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("summary", payload)
        self.assertIn("card_count", payload)

    async def test_the_constraint_route_reports_both_constraints(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, payload = await self.harness.request("GET", "/api/constraints", cookie=cookie)

        self.assertEqual(status, 200)
        allocation = payload["address_allocation"]
        self.assertIn("never assigns", allocation["statement"])
        self.assertEqual(len(allocation["enforced_at"]), 4)
        self.assertTrue(payload["spelled_numerals"]["satisfied"])

    async def test_the_dashboard_files_are_served(self) -> None:
        status, headers, body = await self.harness.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"Legacy-to-Modern IPBX Appliance", body)

        status, headers, _ = await self.harness.request("GET", "/js/numerals.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])

    async def test_a_traversal_attempt_against_the_served_root_is_refused(self) -> None:
        status, _, _ = await self.harness.request("GET", "/../appliance/server.py")
        self.assertIn(status, (403, 404))

    # -- writes -------------------------------------------------------------

    async def test_the_configuration_can_be_saved_and_rendered(self) -> None:
        cookie = await self.harness.sign_in()

        document = dict(DOCUMENT)
        document["extensions"] = [
            {"number": "201", "target": "PJSIP/201"},
            {"number": "202", "target": "PJSIP/202"},
        ]
        status, _, payload = await self.harness.request(
            "POST", "/api/configuration", body=json.dumps(document), cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["saved"])

        status, _, payload = await self.harness.request(
            "POST", "/api/configuration/render", body=json.dumps({}), cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["rendered"])
        self.assertIn("pjsip.conf", payload["written"])

        rendered = (self.harness.root / "asterisk/extensions.conf").read_text(encoding="utf-8")
        self.assertIn("exten => 202,1", rendered)

    async def test_a_local_edit_is_reported_rather_than_overwritten(self) -> None:
        """Benchmark Defect One, end to end through the interface."""
        cookie = await self.harness.sign_in()
        await self.harness.request(
            "POST", "/api/configuration/render", body=json.dumps({}), cookie=cookie
        )

        edited = self.harness.root / "asterisk/pjsip.conf"
        emergency = "; an emergency fix applied by hand\n"
        edited.write_text(emergency, encoding="utf-8")

        status, _, payload = await self.harness.request(
            "POST", "/api/configuration/render", body=json.dumps({}), cookie=cookie
        )
        self.assertEqual(status, 409)
        self.assertTrue(payload["reconciliation_required"])
        self.assertEqual(edited.read_text(encoding="utf-8"), emergency)

        # The drift route explains the decision the administrator must make.
        status, _, drift = await self.harness.request(
            "GET", "/api/configuration/drift", cookie=cookie
        )
        self.assertTrue(drift["reconciliation_required"])
        self.assertIn("adopt", drift["explanation"])

        # Adopting the edit resolves the divergence without destroying it.
        status, _, adopted = await self.harness.request(
            "POST", "/api/configuration/adopt",
            body=json.dumps({"name": "pjsip.conf"}), cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(edited.read_text(encoding="utf-8"), emergency)

        status, _, drift = await self.harness.request(
            "GET", "/api/configuration/drift", cookie=cookie
        )
        self.assertFalse(drift["reconciliation_required"])

    async def test_a_trunk_can_be_disabled_and_enabled(self) -> None:
        cookie = await self.harness.sign_in()

        status, _, payload = await self.harness.request(
            "POST", "/api/trunks/control",
            body=json.dumps({"name": "carrier-primary", "action": "disable"}),
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])

        trunk = self.appliance.trunks.get("carrier-primary")
        self.assertEqual(trunk.state.value, "disabled")

        status, _, payload = await self.harness.request(
            "POST", "/api/trunks/control",
            body=json.dumps({"name": "carrier-primary", "action": "enable"}),
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])

    async def test_controlling_an_unknown_trunk_is_refused(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, _ = await self.harness.request(
            "POST", "/api/trunks/control",
            body=json.dumps({"name": "a-trunk-that-does-not-exist", "action": "enable"}),
            cookie=cookie,
        )
        self.assertEqual(status, 404)

    async def test_a_task_can_be_invoked_through_the_interface(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, payload = await self.harness.request(
            "POST", "/api/tasks/run", body=json.dumps({"name": "hardware-rescan"}),
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["succeeded"])

    async def test_an_unknown_task_is_refused(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, _ = await self.harness.request(
            "POST", "/api/tasks/run", body=json.dumps({"name": "not-a-task"}), cookie=cookie
        )
        self.assertEqual(status, 404)

    async def test_a_malformed_body_is_refused_without_a_server_error(self) -> None:
        cookie = await self.harness.sign_in()
        status, _, _ = await self.harness.request(
            "POST", "/api/tasks/run", body="{ this is not valid", cookie=cookie
        )
        self.assertEqual(status, 400)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    """The persistent bidirectional socket, over real transport."""

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)
        self.appliance = await self.harness.start()
        self.cookie = await self.harness.sign_in()
        self.clients: list[SocketTestClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        await self.harness.stop()

    async def _client(self, cookie: str | None = None) -> SocketTestClient:
        client = SocketTestClient(
            "127.0.0.1", self.harness.port, cookie if cookie is not None else self.cookie
        )
        self.clients.append(client)
        return client

    async def test_the_upgrade_requires_a_valid_session(self) -> None:
        client = await self._client(cookie="myipbx_session=a-token-we-never-issued")
        self.assertFalse(await client.connect())
        self.assertEqual(self.appliance.hub.connection_count, 0)

    async def test_an_upgraded_connection_receives_a_welcome(self) -> None:
        client = await self._client()
        self.assertTrue(await client.connect())

        messages = await client.pump(timeout=5.0)
        self.assertTrue(messages, "no welcome message arrived")
        welcome = messages[0]
        self.assertEqual(welcome["topic"], "welcome")
        self.assertFalse(welcome["payload"]["assigns_addresses"])
        self.assertIn("state", welcome["payload"])
        self.assertIn("trunks", welcome["payload"])

    async def test_a_state_change_is_pushed_without_being_asked_for(self) -> None:
        """Benchmark Defect Two: events, not polling."""
        client = await self._client()
        self.assertTrue(await client.connect())
        await client.pump(timeout=5.0)

        self.appliance._on_engine_event(
            engine_event("Newchannel", uniqueid="one", channel="PJSIP/alpha",
                         channelstate="4", calleridnum="2015550123")
        )

        messages = await client.pump(timeout=5.0)
        topics = [message["topic"] for message in messages]
        self.assertIn("state.changed", topics)

        changed = next(m for m in messages if m["topic"] == "state.changed")
        self.assertEqual(changed["payload"]["active_calls"], 1)

    async def test_a_snapshot_can_be_requested_over_the_socket(self) -> None:
        client = await self._client()
        self.assertTrue(await client.connect())
        await client.pump(timeout=5.0)

        await client.send({"action": "snapshot"})
        messages = await client.pump(timeout=5.0)
        snapshot = next(m for m in messages if m["topic"] == "snapshot")
        for key in ("state", "trunks", "tasks", "hardware"):
            self.assertIn(key, snapshot["payload"])

    async def test_an_application_heartbeat_is_answered(self) -> None:
        client = await self._client()
        self.assertTrue(await client.connect())
        await client.pump(timeout=5.0)

        await client.send({"action": "heartbeat", "sequence": 7})
        messages = await client.pump(timeout=5.0)
        heartbeat = next(m for m in messages if m["topic"] == "heartbeat")
        self.assertEqual(heartbeat["payload"]["acknowledged"], 7)
        self.assertIn("state_sequence", heartbeat["payload"])

    async def test_an_unrecognised_action_is_refused_in_plain_language(self) -> None:
        client = await self._client()
        self.assertTrue(await client.connect())
        await client.pump(timeout=5.0)

        await client.send({"action": "something-we-do-not-support"})
        messages = await client.pump(timeout=5.0)
        error = next(m for m in messages if m["topic"] == "error")
        self.assertIn("not recognised", error["payload"]["reason"])

    async def test_a_task_can_be_invoked_over_the_socket(self) -> None:
        client = await self._client()
        self.assertTrue(await client.connect())
        await client.pump(timeout=5.0)

        await client.send({"action": "run-task", "name": "hardware-rescan"})
        deadline = asyncio.get_running_loop().time() + 5.0
        seen = None
        while asyncio.get_running_loop().time() < deadline and seen is None:
            for message in await client.pump(timeout=1.0):
                if message["topic"] == "task":
                    seen = message
                    break
        self.assertIsNotNone(seen, "the task result was never delivered")
        self.assertTrue(seen["payload"]["succeeded"])

    async def test_the_protocol_heartbeat_evicts_a_peer_that_stops_answering(self) -> None:
        client = await self._client()
        self.assertTrue(await client.connect())
        await client.pump(timeout=5.0)
        self.assertEqual(self.appliance.hub.connection_count, 1)

        # Drive the sweep directly rather than waiting on wall clock time.  The
        # client never answers, so it must be evicted at the missed limit.
        for _ in range(self.appliance.hub.missed_limit + 1):
            self.appliance.hub.sweep_once()
            await asyncio.sleep(0)

        await asyncio.sleep(0.2)
        self.assertEqual(self.appliance.hub.connection_count, 0)

    async def test_a_peer_that_answers_is_never_evicted(self) -> None:
        client = await self._client()
        self.assertTrue(await client.connect())
        await client.pump(timeout=5.0)

        for _ in range(6):
            self.appliance.hub.sweep_once()
            await asyncio.sleep(0)
            await client.pump(timeout=1.0)  # answers the protocol ping

        self.assertEqual(self.appliance.hub.connection_count, 1)
        self.assertGreater(client.pings_received, 0)

    async def test_the_hub_refuses_connections_beyond_its_ceiling(self) -> None:
        self.appliance.hub.maximum_connections = 2
        for index in range(2):
            client = await self._client()
            self.assertTrue(await client.connect(), f"connection {index} was refused early")
            await client.pump(timeout=5.0)

        surplus = await self._client()
        self.assertFalse(await surplus.connect())
        self.assertEqual(self.appliance.hub.connection_count, 2)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """The scale named in the specification: one hundred concurrent sessions."""

    SESSION_COUNT = 100
    CALL_COUNT = 100

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness(
            socket_maximum_connections=256, session_maximum=256
        )
        self.harness.write_document(DOCUMENT)
        self.appliance = await self.harness.start()
        self.cookie = await self.harness.sign_in()
        self.clients: list[SocketTestClient] = []

    async def asyncTearDown(self) -> None:
        await asyncio.gather(
            *(client.close() for client in self.clients), return_exceptions=True
        )
        await self.harness.stop()

    async def _open_sessions(self, count: int) -> None:
        async def open_one(index: int) -> SocketTestClient:
            client = SocketTestClient("127.0.0.1", self.harness.port, self.cookie)
            connected = await client.connect()
            self.assertTrue(connected, f"session number {index} failed to upgrade")
            await client.pump(timeout=10.0)
            return client

        self.clients = list(
            await asyncio.gather(*(open_one(index) for index in range(count)))
        )

    async def test_one_hundred_dashboards_connect_and_all_receive_updates(self) -> None:
        await self._open_sessions(self.SESSION_COUNT)
        self.assertEqual(self.appliance.hub.connection_count, self.SESSION_COUNT)

        # Every connection must have been welcomed.
        for index, client in enumerate(self.clients):
            self.assertTrue(client.received, f"session number {index} received nothing")
            self.assertEqual(client.received[0]["topic"], "welcome")

        # One state change must reach every one of them.
        self.appliance._on_engine_event(
            engine_event("Newchannel", uniqueid="broadcast-probe", channel="PJSIP/probe")
        )

        results = await asyncio.gather(
            *(client.pump(timeout=10.0) for client in self.clients)
        )
        delivered = sum(
            1
            for messages in results
            if any(message["topic"] == "state.changed" for message in messages)
        )
        self.assertEqual(
            delivered, self.SESSION_COUNT,
            f"only {delivered} of {self.SESSION_COUNT} dashboards received the update",
        )
        self.assertEqual(self.appliance.hub.slow_consumer_disconnections, 0)

    async def test_one_hundred_concurrent_calls_are_tracked_and_cleared(self) -> None:
        await self._open_sessions(self.SESSION_COUNT)

        # Raise one hundred simultaneous calls.
        for index in range(self.CALL_COUNT):
            self.appliance._on_engine_event(
                engine_event(
                    "Newchannel", uniqueid=f"call-{index}", channel=f"PJSIP/line-{index}",
                    channelstate="4", calleridnum=f"20155501{index:02d}", exten="201",
                )
            )
        for index in range(self.CALL_COUNT):
            self.appliance._on_engine_event(
                engine_event("Newstate", uniqueid=f"call-{index}", channelstatedesc="Up")
            )

        self.assertEqual(self.appliance.state.active_call_count, self.CALL_COUNT)
        self.assertEqual(self.appliance.state.answered_call_count, self.CALL_COUNT)
        self.assertEqual(self.appliance.state.peak_concurrent_calls, self.CALL_COUNT)

        # The interface must still answer promptly while carrying that load.
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, _, payload = await self.harness.request(
            "GET", "/api/state", cookie=self.cookie
        )
        elapsed = loop.time() - started

        self.assertEqual(status, 200)
        self.assertEqual(payload["active_calls"], self.CALL_COUNT)
        self.assertEqual(payload["spelled"]["active_calls"], "one hundred")
        self.assertLess(
            elapsed, 3.0,
            "the interface became unresponsive while carrying one hundred calls",
        )

        # Clear them all down.
        for index in range(self.CALL_COUNT):
            self.appliance._on_engine_event(
                engine_event("Hangup", uniqueid=f"call-{index}")
            )

        self.assertEqual(self.appliance.state.active_call_count, 0)
        self.assertEqual(self.appliance.state.calls_completed, self.CALL_COUNT)
        self.assertEqual(self.appliance.state.peak_concurrent_calls, self.CALL_COUNT)

    async def test_the_appliance_stays_responsive_through_a_heavy_event_burst(self) -> None:
        await self._open_sessions(self.SESSION_COUNT)

        # Two hundred events, each broadcast to one hundred dashboards.
        loop = asyncio.get_running_loop()
        started = loop.time()
        for index in range(self.CALL_COUNT):
            self.appliance._on_engine_event(
                engine_event("Newchannel", uniqueid=f"burst-{index}",
                             channel=f"PJSIP/burst-{index}")
            )
            self.appliance._on_engine_event(
                engine_event("Hangup", uniqueid=f"burst-{index}")
            )
        elapsed = loop.time() - started

        self.assertLess(
            elapsed, 10.0,
            "publishing the event burst to one hundred dashboards took too long",
        )
        self.assertEqual(self.appliance.state.active_call_count, 0)

        # Every connection must have survived the burst.
        self.assertEqual(self.appliance.hub.connection_count, self.SESSION_COUNT)

        status, _, _ = await self.harness.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(status, 200)

    async def test_a_dashboard_that_stops_reading_is_shed_not_tolerated(self) -> None:
        """Backpressure must never reach the appliance."""
        client = SocketTestClient("127.0.0.1", self.harness.port, self.cookie)
        self.clients.append(client)
        self.assertTrue(await client.connect())
        await asyncio.sleep(0.1)

        # The client never reads.  Publish far more than its queue can hold.
        limit = self.harness.config.socket_send_queue_limit
        for index in range(limit * 3):
            self.appliance.hub.broadcast("probe", {"index": index})

        await asyncio.sleep(0.2)
        self.assertGreaterEqual(
            self.appliance.hub.slow_consumer_disconnections, 1,
            "the appliance absorbed backpressure instead of shedding the peer",
        )
        self.assertEqual(self.appliance.hub.connection_count, 0)

    async def test_sustained_activity_leaves_nothing_accumulating(self) -> None:
        """Long term interface responsiveness: nothing may grow without bound."""
        await self._open_sessions(20)

        rounds = 40
        per_round = 25
        for round_index in range(rounds):
            for index in range(per_round):
                identifier = f"soak-{round_index}-{index}"
                self.appliance._on_engine_event(
                    engine_event("Newchannel", uniqueid=identifier,
                                 channel=f"PJSIP/soak-{index}")
                )
                self.appliance._on_engine_event(
                    engine_event("Newstate", uniqueid=identifier, channelstatedesc="Up")
                )
                self.appliance._on_engine_event(
                    engine_event("Hangup", uniqueid=identifier)
                )
            # Let the writers drain so the measurement is of steady state, not
            # of one instant inside a burst.
            await asyncio.sleep(0)
            await asyncio.gather(
                *(client.pump(timeout=0.05) for client in self.clients),
                return_exceptions=True,
            )

        total = rounds * per_round
        self.assertEqual(self.appliance.state.calls_started, total)
        self.assertEqual(self.appliance.state.calls_completed, total)

        # The channel table must be empty: every call was cleared down.
        self.assertEqual(self.appliance.state.active_call_count, 0)
        self.assertEqual(len(self.appliance.state.channels), 0)

        # The task history is bounded regardless of how much has happened.
        self.assertLessEqual(len(self.appliance.tasks.history(limit=1000)), 100)

        # Every connection survived, and none is left with a backlog.
        self.assertEqual(self.appliance.hub.connection_count, 20)
        for connection in self.appliance.hub.connections():
            self.assertLess(
                connection.as_dict()["queue_depth"],
                self.harness.config.socket_send_queue_limit,
                "a connection is accumulating an unbounded backlog",
            )

        # And the interface still answers.
        status, _, payload = await self.harness.request(
            "GET", "/api/state", cookie=self.cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["active_calls"], 0)

    async def test_a_heartbeat_sweep_over_one_hundred_sessions_is_prompt(self) -> None:
        await self._open_sessions(self.SESSION_COUNT)

        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = self.appliance.hub.sweep_once()
        elapsed = loop.time() - started

        self.assertEqual(outcome["delivered"], self.SESSION_COUNT)
        self.assertEqual(outcome["evicted"], 0)
        self.assertLess(elapsed, 1.0, "the heartbeat sweep was too slow at this scale")


class SpelledOutputTests(unittest.IsolatedAsyncioTestCase):
    """Constraint Two applied to what the appliance actually emits at run time."""

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)
        self.appliance = await self.harness.start()
        self.cookie = await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def test_no_line_in_the_appliance_log_contains_a_digit(self) -> None:
        # Exercise the appliance so the log is not empty.
        self.appliance._on_engine_event(
            engine_event("Newchannel", uniqueid="one", channel="PJSIP/alpha")
        )
        await self.appliance.tasks.run("hardware-rescan")
        await self.harness.request("GET", "/api/state", cookie=self.cookie)

        import logging

        for handler in logging.getLogger("myipbx").handlers:
            handler.flush()

        log_file = self.harness.root / "appliance.log"
        if not log_file.is_file():
            self.skipTest("the appliance log file was not created in this environment")

        content = log_file.read_text(encoding="utf-8")
        self.assertTrue(content.strip(), "the appliance produced no log output")
        offending = [line for line in content.splitlines() if re.search(r"\d", line)]
        self.assertEqual(
            offending, [],
            f"a digit character escaped into the appliance log: {offending[:3]}",
        )

    async def test_the_operator_facing_figures_are_spelled(self) -> None:
        for index in range(12):
            self.appliance._on_engine_event(
                engine_event("Newchannel", uniqueid=f"call-{index}",
                             channel=f"PJSIP/line-{index}")
            )

        _, _, payload = await self.harness.request("GET", "/api/state", cookie=self.cookie)
        spelled = payload["spelled"]
        self.assertEqual(spelled["active_calls"], "twelve")
        for value in spelled.values():
            self.assertFalse(numerals.contains_digit(value))

    async def test_the_health_summary_is_spelled(self) -> None:
        _, _, payload = await self.harness.request("GET", "/api/health")
        for key in ("uptime", "active_calls", "alarm_count"):
            self.assertFalse(
                numerals.contains_digit(str(payload[key])),
                f"the health field named {key} carried a digit: {payload[key]}",
            )


if __name__ == "__main__":
    unittest.main()
