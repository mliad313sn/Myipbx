"""Publication coalescing, the bypass list, and the cached state response.

These exercise the path between an engine event and a browser, which is the
path that decides whether the appliance stays responsive while it is busy.  A
hundred simultaneous calls raise several hundred events in a few milliseconds
and every one of them used to become a frame for every connected dashboard.
The tests here hold that path to its contract: superseded state is sent once,
urgent notices are never held, and the interface answers from a body it built
at most one window ago.

The measurements are asserted rather than reported because the budget is a
product requirement, not an observation.
"""

from __future__ import annotations

import asyncio
import unittest

from appliance import api, wsserver
from appliance.config import ApplianceConfig, ConfigError
from support import ApplianceHarness, SocketTestClient, engine_event

DOCUMENT: dict[str, object] = {"trunks": [], "firewall_rules": []}


async def drain(client: SocketTestClient, rounds: int = 6, timeout: float = 0.05) -> None:
    """Read until the connection has been quiet for several consecutive reads.

    The client's own pump returns as soon as anything at all has arrived, which
    is what a test waiting for one message wants and exactly not what a test
    counting messages wants.
    """
    quiet = 0
    for _ in range(rounds * 4):
        collected = await client.pump(timeout=timeout)
        if collected:
            quiet = 0
            continue
        quiet += 1
        if quiet >= 2:
            return


def topics_of(client: SocketTestClient, topic: str) -> list[dict]:
    return [message for message in client.received if message["topic"] == topic]


class CoalescingWindowTests(unittest.IsolatedAsyncioTestCase):
    """A window's worth of superseded state must cost one frame, not many."""

    EVENT_COUNT = 60

    async def _harness(self, window: int) -> tuple[ApplianceHarness, SocketTestClient]:
        harness = ApplianceHarness(broadcast_coalesce_milliseconds=window)
        harness.write_document(DOCUMENT)
        self.appliance = await harness.start()
        self.addAsyncCleanup(harness.stop)

        cookie = await harness.sign_in()
        client = SocketTestClient("127.0.0.1", harness.port, cookie)
        self.assertTrue(await client.connect())
        self.addAsyncCleanup(client.close)
        await client.pump(timeout=5.0)
        return harness, client

    async def test_many_events_inside_one_window_produce_a_single_frame(self) -> None:
        """The defect this whole change exists to remove."""
        _, client = await self._harness(window=150)

        before = self.appliance.hub.snapshot()["broadcast_sequence"]
        for index in range(self.EVENT_COUNT):
            self.appliance._on_engine_event(
                engine_event(
                    "Newchannel", uniqueid=f"burst-{index}", channel=f"PJSIP/burst-{index}"
                )
            )

        # Nothing may have reached the wire yet: the window is still open.
        self.assertEqual(self.appliance.hub.snapshot()["broadcast_sequence"], before)
        self.assertEqual(self.appliance.hub.pending_topics, ("state.changed",))

        await asyncio.sleep(0.35)
        await drain(client)

        broadcasts = self.appliance.hub.snapshot()["broadcast_sequence"] - before
        self.assertEqual(
            broadcasts, 1,
            f"{self.EVENT_COUNT} events inside one window produced {broadcasts} broadcasts",
        )

        changes = topics_of(client, "state.changed")
        self.assertEqual(
            len(changes), 1,
            f"the dashboard received {len(changes)} frames where one was owed",
        )

        # The single frame must describe the end of the burst, not its start.
        self.assertEqual(changes[0]["payload"]["active_calls"], self.EVENT_COUNT)

    async def test_a_window_of_zero_delivers_every_event(self) -> None:
        """Coalescing must be genuinely optional, not merely shortened."""
        _, client = await self._harness(window=0)

        before = self.appliance.hub.snapshot()["broadcast_sequence"]
        for index in range(self.EVENT_COUNT):
            self.appliance._on_engine_event(
                engine_event(
                    "Newchannel", uniqueid=f"single-{index}", channel=f"PJSIP/single-{index}"
                )
            )

        broadcasts = self.appliance.hub.snapshot()["broadcast_sequence"] - before
        self.assertEqual(
            broadcasts, self.EVENT_COUNT,
            "a window of zero must not hold anything back",
        )
        self.assertEqual(self.appliance.hub.pending_topics, ())

        await drain(client)
        self.assertEqual(len(topics_of(client, "state.changed")), self.EVENT_COUNT)

    async def test_a_topic_that_reports_an_occurrence_is_never_coalesced(self) -> None:
        """Discrete events must not be silently thinned out."""
        _, client = await self._harness(window=150)

        before = self.appliance.hub.snapshot()["broadcast_sequence"]
        for index in range(5):
            self.appliance._publish("task.finished", {"name": f"example-{index}"})

        after = self.appliance.hub.snapshot()["broadcast_sequence"] - before
        self.assertEqual(after, 5, "a discrete occurrence was coalesced away")

    async def test_the_pending_frame_survives_a_shutdown(self) -> None:
        """A held payload must be delivered, not discarded, when the hub stops."""
        harness, client = await self._harness(window=1000)

        self.appliance._on_engine_event(
            engine_event("Newchannel", uniqueid="held", channel="PJSIP/held")
        )
        self.assertEqual(self.appliance.hub.pending_topics, ("state.changed",))

        flushed = self.appliance.hub.flush_pending()
        self.assertEqual(flushed, 1)
        self.assertEqual(self.appliance.hub.pending_topics, ())

        await drain(client)
        self.assertEqual(len(topics_of(client, "state.changed")), 1)


class BypassListTests(unittest.IsolatedAsyncioTestCase):
    """Some notices cannot wait for a window, and the list of them is explicit."""

    def test_the_bypass_list_is_a_named_constant_naming_what_it_covers(self) -> None:
        self.assertIsInstance(wsserver.IMMEDIATE_TOPICS, frozenset)
        self.assertEqual(
            wsserver.IMMEDIATE_TOPICS,
            frozenset(
                {
                    "alarm.raised",
                    "alarm.cleared",
                    "engine.disconnected",
                    "driver.stage-failed",
                }
            ),
        )
        # Nothing may appear on both lists: a topic is either superseded by the
        # next of its kind or it is urgent, never both.
        self.assertFalse(wsserver.IMMEDIATE_TOPICS & wsserver.COALESCED_TOPICS)

    async def test_an_alarm_is_delivered_without_waiting_for_the_window(self) -> None:
        # A window far longer than the test is willing to wait, so that an
        # alarm arriving at all proves it bypassed rather than got lucky.
        harness = ApplianceHarness(broadcast_coalesce_milliseconds=1000)
        harness.write_document(DOCUMENT)
        appliance = await harness.start()
        self.addAsyncCleanup(harness.stop)

        cookie = await harness.sign_in()
        client = SocketTestClient("127.0.0.1", harness.port, cookie)
        self.assertTrue(await client.connect())
        self.addAsyncCleanup(client.close)
        await client.pump(timeout=5.0)

        loop = asyncio.get_running_loop()
        started = loop.time()
        appliance.state.raise_alarm(
            "an-example-alarm",
            "critical",
            "an example condition the operator must see",
            "raised by the test suite",
        )

        deadline = started + 0.5
        while loop.time() < deadline and not topics_of(client, "alarm.raised"):
            await client.pump(timeout=0.05)
        elapsed = loop.time() - started

        raised = topics_of(client, "alarm.raised")
        self.assertTrue(raised, "the alarm was held behind the coalescing window")
        self.assertLess(
            elapsed, 1.0,
            "the alarm did not overtake a window of one thousand milliseconds",
        )
        self.assertEqual(
            raised[0]["payload"]["message"], "an example condition the operator must see"
        )

        # The urgent frame must not arrive ahead of the state that explains it.
        order = [message["topic"] for message in client.received]
        self.assertIn("state.changed", order)
        self.assertLess(order.index("state.changed"), order.index("alarm.raised"))

    async def test_losing_the_engine_is_announced_immediately(self) -> None:
        harness = ApplianceHarness(broadcast_coalesce_milliseconds=1000)
        harness.write_document(DOCUMENT)
        appliance = await harness.start()
        self.addAsyncCleanup(harness.stop)

        cookie = await harness.sign_in()
        client = SocketTestClient("127.0.0.1", harness.port, cookie)
        self.assertTrue(await client.connect())
        self.addAsyncCleanup(client.close)
        await client.pump(timeout=5.0)

        appliance._on_engine_event(engine_event("ApplianceManagerDisconnected"))

        for _ in range(10):
            if topics_of(client, "engine.disconnected"):
                break
            await client.pump(timeout=0.05)

        self.assertTrue(
            topics_of(client, "engine.disconnected"),
            "the loss of the engine was held behind the coalescing window",
        )
        self.assertFalse(appliance.state.engine_connected)


class StateResponseCacheTests(unittest.IsolatedAsyncioTestCase):
    """The state route must not rebuild the whole appliance for every caller."""

    SESSION_COUNT = 100
    CALL_COUNT = 100
    BUDGET_SECONDS = 0.5

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
            self.assertTrue(await client.connect(), f"session number {index} failed")
            await client.pump(timeout=10.0)
            return client

        self.clients = list(
            await asyncio.gather(*(open_one(index) for index in range(count)))
        )

    def _raise_calls(self) -> None:
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

    async def test_the_state_route_answers_within_the_budget_under_full_load(self) -> None:
        """One hundred sessions, one hundred calls, and a five hundred millisecond
        budget on every single answer."""
        await self._open_sessions(self.SESSION_COUNT)
        self._raise_calls()

        loop = asyncio.get_running_loop()
        worst = 0.0
        for _ in range(self.CALL_COUNT):
            started = loop.time()
            status, _, payload = await self.harness.request(
                "GET", "/api/state", cookie=self.cookie
            )
            worst = max(worst, loop.time() - started)
            self.assertEqual(status, 200)

        self.assertEqual(payload["active_calls"], self.CALL_COUNT)
        self.assertEqual(payload["spelled"]["active_calls"], "one hundred")
        self.assertLess(
            worst, self.BUDGET_SECONDS,
            f"the slowest answer took {worst:.3f} seconds against a budget of "
            f"{self.BUDGET_SECONDS:.3f}",
        )

    async def test_a_change_is_never_hidden_behind_a_cached_body(self) -> None:
        """The cache may be stale by a window; it may never be stale by a change."""
        _, _, first = await self.harness.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(first["active_calls"], 0)

        self.appliance._on_engine_event(
            engine_event("Newchannel", uniqueid="fresh", channel="PJSIP/fresh")
        )

        _, _, second = await self.harness.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(second["active_calls"], 1)
        self.assertGreater(second["sequence"], first["sequence"])

        self.appliance._on_engine_event(engine_event("Hangup", uniqueid="fresh"))

        _, _, third = await self.harness.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(third["active_calls"], 0)
        self.assertGreater(third["sequence"], second["sequence"])

    async def test_the_cached_body_is_the_same_body_for_every_caller(self) -> None:
        """Nothing in this route varies by caller, which is why it may be shared.

        If that ever stops being true the cache becomes a disclosure, so the
        property is asserted here rather than assumed.
        """
        other = await self.harness.request(
            "POST", "/api/session",
            body='{"username": "administrator", '
                 '"password": "an-example-appliance-password"}',
            cookie="",
        )
        self.assertEqual(other[0], 200)
        second_cookie = other[1]["set-cookie"].split(";")[0]
        self.assertNotEqual(second_cookie, self.cookie)

        # Asked concurrently, so that the two answers fall inside one window
        # and any difference between them is a difference of caller rather
        # than a difference of moment.
        (_, _, mine), (_, _, theirs) = await asyncio.gather(
            self.harness.request("GET", "/api/state", cookie=self.cookie),
            self.harness.request("GET", "/api/state", cookie=second_cookie),
        )
        self.assertEqual(mine, theirs)

    async def test_a_repeated_ask_inside_one_window_is_not_rebuilt(self) -> None:
        """The saving is only real if the second caller does no work at all."""
        cache = api._StateResponseCache(self.appliance)
        first = cache.response()
        self.assertIs(cache.response(), first, "the body was rebuilt for no reason")

        self.appliance._on_engine_event(
            engine_event("Newchannel", uniqueid="rebuild", channel="PJSIP/rebuild")
        )
        self.assertIsNot(
            cache.response(), first, "a change did not force the body to be rebuilt"
        )

    async def test_the_route_still_refuses_a_caller_without_a_session(self) -> None:
        """A cached body must never escape the guard in front of it."""
        await self.harness.request("GET", "/api/state", cookie=self.cookie)
        status, _, payload = await self.harness.request("GET", "/api/state", cookie="")
        self.assertEqual(status, 401)
        self.assertNotIn("active_calls", payload)


class SlowConsumerTests(unittest.IsolatedAsyncioTestCase):
    """Backpressure is shed, counted, and reported to the operator."""

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness(socket_send_queue_limit=32)
        self.harness.write_document(DOCUMENT)
        self.appliance = await self.harness.start()
        self.cookie = await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def test_a_peer_that_never_reads_is_shed_and_counted_and_reported(self) -> None:
        client = SocketTestClient("127.0.0.1", self.harness.port, self.cookie)
        self.assertTrue(await client.connect())
        self.addAsyncCleanup(client.close)
        await asyncio.sleep(0.1)

        limit = self.harness.config.socket_send_queue_limit
        connection = next(iter(self.appliance.hub.connections()))

        # The client never reads.  Publish far more than its queue can hold.
        for index in range(limit * 10):
            self.appliance.hub.broadcast("probe", {"index": index})
            # Nothing may accumulate beyond the declared limit at any point.
            self.assertLessEqual(connection.as_dict()["queue_depth"], limit)

        await asyncio.sleep(0.2)
        self.assertEqual(self.appliance.hub.connection_count, 0)
        self.assertGreaterEqual(self.appliance.hub.slow_consumer_disconnections, 1)

        # And the operator can see it, which is the whole point of counting it.
        _, _, payload = await self.harness.request(
            "GET", "/api/state", cookie=self.cookie
        )
        self.assertGreaterEqual(payload["socket"]["slow_consumer_disconnections"], 1)


class CoalescingConfigurationTests(unittest.TestCase):
    """The window is configurable within stated bounds and nowhere outside them."""

    def test_the_default_window_is_one_hundred_and_fifty_milliseconds(self) -> None:
        self.assertEqual(ApplianceConfig().broadcast_coalesce_milliseconds, 150)

    def test_the_bounds_of_the_window_are_enforced(self) -> None:
        for accepted in (0, 1, 150, 1000):
            ApplianceConfig(broadcast_coalesce_milliseconds=accepted).validate()
        for refused in (-1, 1001, 10000):
            with self.assertRaises(ConfigError):
                ApplianceConfig(broadcast_coalesce_milliseconds=refused).validate()

    def test_the_window_can_be_set_from_the_environment(self) -> None:
        config = ApplianceConfig.load(
            path="/nonexistent/appliance.json",
            environment={"CROSSBAR_BROADCAST_COALESCE_MILLISECONDS": "0"},
        )
        self.assertEqual(config.broadcast_coalesce_milliseconds, 0)

    def test_a_hub_without_a_running_loop_publishes_immediately(self) -> None:
        """There is no timer to fire outside a loop, so holding would lose it."""
        hub = wsserver.SocketHub(coalesce_milliseconds=1000)
        hub.publish("state.changed", {"sequence": 1})
        self.assertEqual(hub.pending_topics, ())
        self.assertEqual(hub.snapshot()["broadcast_sequence"], 1)


if __name__ == "__main__":
    unittest.main()
