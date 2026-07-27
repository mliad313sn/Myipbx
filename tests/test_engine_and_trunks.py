"""The engine manager client, the retry policy, and the trunk state machine."""

from __future__ import annotations

import asyncio
import unittest

from support import engine_event

from appliance import retry
from appliance.ami import (
    ManagerClient,
    ManagerError,
    ManagerNotConnected,
    encode_action,
    parse_message,
)
from appliance.state import ApplianceState
from appliance.trunks import Trunk, TrunkRegistry, TrunkState


class MessageParsingTests(unittest.TestCase):
    def test_a_message_is_parsed_without_regard_to_field_case(self) -> None:
        message = parse_message("Event: Newchannel\r\nCHANNEL: PJSIP/one\r\nUniqueid: abc\r\n")
        self.assertEqual(message.event, "Newchannel")
        self.assertEqual(message.get("channel"), "PJSIP/one")
        self.assertEqual(message.get("UNIQUEID"), "abc")

    def test_a_repeated_field_retains_every_occurrence(self) -> None:
        message = parse_message(
            "Event: VarSet\r\nChanVariable: one=first\r\nChanVariable: two=second\r\n"
        )
        self.assertEqual(message.get_all("chanvariable"), ["one=first", "two=second"])

    def test_unstructured_output_is_preserved_rather_than_discarded(self) -> None:
        message = parse_message("Response: Follows\r\nsome unstructured line\r\n")
        self.assertEqual(message.get_all("output"), ["some unstructured line"])

    def test_a_missing_field_returns_the_supplied_default(self) -> None:
        message = parse_message("Event: Ping\r\n")
        self.assertEqual(message.get("absent", "fallback"), "fallback")
        self.assertFalse(message.has("absent"))

    def test_the_success_responses_are_recognised(self) -> None:
        self.assertTrue(parse_message("Response: Success\r\n").is_success)
        self.assertTrue(parse_message("Response: Goodbye\r\n").is_success)
        self.assertFalse(parse_message("Response: Error\r\n").is_success)


class ActionEncodingTests(unittest.TestCase):
    def test_an_action_is_terminated_by_a_blank_line(self) -> None:
        encoded = encode_action("Ping", {"ActionID": "one"})
        self.assertTrue(encoded.endswith(b"\r\n\r\n"))
        self.assertIn(b"Action: Ping\r\n", encoded)

    def test_a_line_ending_in_a_value_cannot_inject_a_field(self) -> None:
        encoded = encode_action(
            "Originate", {"Channel": "PJSIP/one\r\nCommand: rm -rf /"}
        )
        # The injected line ending is neutralised, so the attempted second
        # field stays inside the value as inert text.  The property that
        # matters is that no new field line was created.
        body = encoded.decode("utf-8")
        lines = [line for line in body.split("\r\n") if line]
        self.assertEqual(lines, ["Action: Originate", "Channel: PJSIP/one  Command: rm -rf /"])
        self.assertFalse(any(line.startswith("Command:") for line in lines))

    def test_a_field_whose_value_is_absent_is_omitted(self) -> None:
        encoded = encode_action("Ping", {"Present": "yes", "Absent": None})
        self.assertNotIn(b"Absent", encoded)

    def test_a_list_valued_field_is_repeated(self) -> None:
        encoded = encode_action("Test", {"Variable": ["one=first", "two=second"]})
        self.assertIn(b"Variable: one=first\r\n", encoded)
        self.assertIn(b"Variable: two=second\r\n", encoded)


class FakeEngine:
    """A scripted manager interface served over a real stream pair."""

    def __init__(self, accept_login: bool = True, answer_actions: bool = True) -> None:
        self.accept_login = accept_login
        self.answer_actions = answer_actions
        self.received: list[str] = []
        self.connections = 0
        self._client_reader: asyncio.StreamReader | None = None
        self._server_writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._to_client: asyncio.StreamReader | None = None

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Return a stream pair wired to this scripted engine."""
        self.connections += 1
        loop = asyncio.get_running_loop()

        to_client = asyncio.StreamReader()

        class _ClientSideWriter:
            """A writer that hands what the client sends to the engine."""

            def __init__(self, engine: "FakeEngine") -> None:
                self._engine = engine
                self._buffer = bytearray()

            def write(self, data: bytes) -> None:
                self._buffer.extend(data)
                while b"\r\n\r\n" in self._buffer:
                    block, _, remainder = bytes(self._buffer).partition(b"\r\n\r\n")
                    self._buffer = bytearray(remainder)
                    loop.create_task(self._engine._respond(block.decode("utf-8"), to_client))

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                to_client.feed_eof()

            async def wait_closed(self) -> None:
                return None

            def get_extra_info(self, name: str, default: object = None) -> object:
                return default

        to_client.feed_data(b"Asterisk Call Manager/five point zero point two\r\n")
        return to_client, _ClientSideWriter(self)  # type: ignore[return-value]

    async def _respond(self, block: str, to_client: asyncio.StreamReader) -> None:
        self.received.append(block)
        message = parse_message(block)
        action = (message.get("action") or "").lower()
        identifier = message.action_id or ""

        if action == "login":
            if self.accept_login:
                reply = f"Response: Success\r\nActionID: {identifier}\r\nMessage: Authentication accepted\r\n\r\n"
            else:
                reply = f"Response: Error\r\nActionID: {identifier}\r\nMessage: Authentication failed\r\n\r\n"
            to_client.feed_data(reply.encode("utf-8"))
            return

        if not self.answer_actions:
            return

        if action == "ping":
            reply = f"Response: Success\r\nActionID: {identifier}\r\nPing: Pong\r\n\r\n"
        else:
            reply = f"Response: Success\r\nActionID: {identifier}\r\n\r\n"
        to_client.feed_data(reply.encode("utf-8"))

    def emit_event(self, to_client: asyncio.StreamReader, block: str) -> None:
        to_client.feed_data(block.encode("utf-8"))


class ManagerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_client_authenticates_and_reports_itself_connected(self) -> None:
        engine = FakeEngine()
        client = ManagerClient(
            username="probe", secret="secret", connector=engine.connect
        )
        await client.start()
        await self._wait_until(lambda: client.connected)

        self.assertTrue(client.connected)
        self.assertEqual(client.connection_count, 1)
        self.assertIn("Action: Login", engine.received[0])
        self.assertIn("Username: probe", engine.received[0])
        await client.stop()

    async def test_an_action_is_correlated_to_its_own_response(self) -> None:
        engine = FakeEngine()
        client = ManagerClient(connector=engine.connect)
        await client.start()
        await self._wait_until(lambda: client.connected)

        responses = await asyncio.gather(
            client.send_action("Ping"),
            client.send_action("Reload"),
            client.send_action("Ping"),
        )
        for response in responses:
            self.assertTrue(response.is_success)
        self.assertEqual(client.pending_action_count, 0)
        await client.stop()

    async def test_an_unanswered_action_times_out_without_stalling_the_loop(self) -> None:
        engine = FakeEngine(answer_actions=False)
        client = ManagerClient(connector=engine.connect, action_timeout_seconds=0.2)
        await client.start()
        await self._wait_until(lambda: client.connected)

        with self.assertRaises(ManagerError):
            await client.send_action("Reload")

        # The loop is still responsive and the pending table did not leak.
        self.assertEqual(client.pending_action_count, 0)
        await asyncio.sleep(0)
        await client.stop()

    async def test_refused_credentials_are_reported_and_retried(self) -> None:
        engine = FakeEngine(accept_login=False)
        client = ManagerClient(
            connector=engine.connect,
            reconnect_base_seconds=0.05,
            reconnect_ceiling_seconds=0.1,
        )
        await client.start()
        await self._wait_until(lambda: engine.connections >= 2, timeout=5.0)

        self.assertFalse(client.connected)
        self.assertIsNotNone(client.last_error)
        await client.stop()

    async def test_an_action_before_connection_is_refused_cleanly(self) -> None:
        client = ManagerClient()
        with self.assertRaises(ManagerNotConnected):
            await client.send_action("Ping")

    async def test_a_failing_event_handler_does_not_break_the_stream(self) -> None:
        engine = FakeEngine()
        client = ManagerClient(connector=engine.connect)

        seen: list[str] = []
        client.add_event_handler(lambda message: (_ for _ in ()).throw(RuntimeError("bad")))
        client.add_event_handler(lambda message: seen.append(message.event or ""))

        await client.start()
        await self._wait_until(lambda: client.connected)
        await self._wait_until(lambda: "ApplianceManagerConnected" in seen)
        self.assertIn("ApplianceManagerConnected", seen)
        await client.stop()

    async def _wait_until(self, predicate, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("the awaited condition was not reached within its timeout")


class BackoffTests(unittest.TestCase):
    def test_the_delay_doubles_and_then_holds_at_the_ceiling(self) -> None:
        delays = [
            retry.compute_backoff(attempt, 1.0, 16.0, jitter_ratio=0.0)
            for attempt in range(1, 8)
        ]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 16.0, 16.0])

    def test_jitter_stays_inside_the_declared_band(self) -> None:
        for draw in (0.0, 0.25, 0.5, 0.75, 1.0):
            delay = retry.compute_backoff(3, 1.0, 100.0, 0.25, lambda: draw)
            self.assertGreaterEqual(delay, 3.0)
            self.assertLessEqual(delay, 5.0)

    def test_a_very_large_attempt_count_does_not_overflow(self) -> None:
        delay = retry.compute_backoff(10_000, 1.0, 300.0, jitter_ratio=0.0)
        self.assertEqual(delay, 300.0)

    def test_invalid_parameters_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            retry.compute_backoff(0, 1.0, 10.0)
        with self.assertRaises(ValueError):
            retry.compute_backoff(1, 0.0, 10.0)
        with self.assertRaises(ValueError):
            retry.compute_backoff(1, 10.0, 1.0)
        with self.assertRaises(ValueError):
            retry.compute_backoff(1, 1.0, 10.0, jitter_ratio=1.5)

    def test_the_policy_resets_its_attempt_count(self) -> None:
        policy = retry.BackoffPolicy(1.0, 10.0, jitter_ratio=0.0)
        policy.next_delay()
        policy.next_delay()
        self.assertEqual(policy.attempt, 2)
        policy.reset()
        self.assertEqual(policy.attempt, 0)
        self.assertEqual(policy.next_delay(), 1.0)


class TrunkStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self, **overrides) -> TrunkRegistry:
        settings = {
            "base_seconds": 0.01,
            "ceiling_seconds": 0.05,
            "jitter_ratio": 0.0,
            "attempt_timeout_seconds": 0.5,
        }
        settings.update(overrides)
        return TrunkRegistry(**settings)

    async def test_a_successful_attempt_reaches_the_registered_state(self) -> None:
        registry = self._registry(attempt_fn=lambda trunk: _immediately(True))
        registry.declare("carrier-one", host="sip.example.net")
        registry.start_all()
        self.assertTrue(await registry.wait_all(timeout=5.0))

        trunk = registry.get("carrier-one")
        assert trunk is not None
        self.assertIs(trunk.state, TrunkState.REGISTERED)
        self.assertEqual(trunk.attempts, 0)
        self.assertIsNotNone(trunk.registered_at)

    async def test_a_failing_attempt_retries_and_then_gives_up_at_the_limit(self) -> None:
        registry = self._registry(
            maximum_attempts=3, attempt_fn=lambda trunk: _immediately(False)
        )
        registry.declare("carrier-dead")
        registry.start_all()
        self.assertTrue(await registry.wait_all(timeout=5.0))

        trunk = registry.get("carrier-dead")
        assert trunk is not None
        self.assertIs(trunk.state, TrunkState.FAILED)
        self.assertEqual(trunk.attempts, 3)
        transitions = [record["to"] for record in trunk.history]
        self.assertIn("retrying", transitions)
        self.assertEqual(transitions[-1], "failed")

    async def test_an_attempt_that_hangs_is_abandoned_at_its_timeout(self) -> None:
        async def hang(trunk: Trunk) -> bool:
            await asyncio.sleep(30)
            return True

        registry = self._registry(
            maximum_attempts=1, attempt_timeout_seconds=0.05, attempt_fn=hang
        )
        registry.declare("carrier-silent")
        registry.start_all()
        self.assertTrue(await registry.wait_all(timeout=5.0))

        trunk = registry.get("carrier-silent")
        assert trunk is not None
        self.assertIs(trunk.state, TrunkState.FAILED)
        self.assertIn("did not answer", trunk.last_reason)

    async def test_a_raising_attempt_is_recorded_rather_than_escaping(self) -> None:
        async def explode(trunk: Trunk) -> bool:
            raise RuntimeError("the carrier refused the connection")

        registry = self._registry(maximum_attempts=1, attempt_fn=explode)
        registry.declare("carrier-broken")
        registry.start_all()
        self.assertTrue(await registry.wait_all(timeout=5.0))

        trunk = registry.get("carrier-broken")
        assert trunk is not None
        self.assertIs(trunk.state, TrunkState.FAILED)
        self.assertIn("refused the connection", trunk.last_reason)

    async def test_one_dead_carrier_does_not_delay_the_healthy_ones(self) -> None:
        """The defining property of the non blocking registration layer."""
        slow = "carrier-slow"

        async def attempt(trunk: Trunk) -> bool:
            if trunk.name == slow:
                await asyncio.sleep(1.5)
            return True

        registry = self._registry(attempt_timeout_seconds=5.0, attempt_fn=attempt)
        for index in range(1, 101):
            registry.declare(f"carrier-{index}")
        registry.declare(slow)

        loop = asyncio.get_running_loop()
        started = loop.time()
        registry.start_all()

        # Every healthy trunk must be registered long before the slow one is.
        deadline = started + 1.0
        while loop.time() < deadline:
            snapshot = registry.snapshot()
            if snapshot["registered"] >= 100:
                break
            await asyncio.sleep(0.01)

        elapsed = loop.time() - started
        snapshot = registry.snapshot()
        self.assertGreaterEqual(
            snapshot["registered"], 100,
            "the healthy trunks did not register while one carrier was stalled",
        )
        self.assertLess(
            elapsed, 1.2,
            "the healthy trunks were delayed by the stalled carrier",
        )
        self.assertIs(registry.get(slow).state, TrunkState.REGISTERING)

        await registry.stop_all()

    async def test_a_disabled_trunk_is_not_driven(self) -> None:
        registry = self._registry(attempt_fn=lambda trunk: _immediately(True))
        registry.declare("carrier-off", enabled=False)
        started = registry.start_all()
        self.assertEqual(started, 0)
        self.assertIs(registry.get("carrier-off").state, TrunkState.DISABLED)

    async def test_stopping_marks_an_unsettled_trunk_as_unknown(self) -> None:
        async def hang(trunk: Trunk) -> bool:
            await asyncio.sleep(30)
            return True

        registry = self._registry(attempt_timeout_seconds=30.0, attempt_fn=hang)
        registry.declare("carrier-pending")
        registry.start_all()
        await asyncio.sleep(0.05)
        await registry.stop_all()

        self.assertIs(registry.get("carrier-pending").state, TrunkState.UNKNOWN)

    async def test_an_invalid_transition_is_rejected(self) -> None:
        registry = self._registry()
        trunk = registry.declare("carrier-one")
        self.assertIs(trunk.state, TrunkState.UNCONFIGURED)
        # Unconfigured to registered is not a permitted edge; only the
        # registering state may reach it.
        self.assertFalse(registry._transition(trunk, TrunkState.REGISTERED, "invalid"))
        self.assertIs(trunk.state, TrunkState.UNCONFIGURED)

    async def test_an_engine_event_is_authoritative_over_the_driver(self) -> None:
        registry = self._registry(attempt_fn=lambda trunk: _immediately(True))
        registry.declare("carrier-one")
        registry.start_all()
        await registry.wait_all(timeout=5.0)
        self.assertIs(registry.get("carrier-one").state, TrunkState.REGISTERED)

        registry.apply_engine_event(
            engine_event("Registry", username="carrier-one", status="Unregistered")
        )
        self.assertIs(registry.get("carrier-one").state, TrunkState.RETRYING)

    async def test_an_engine_event_for_an_unknown_trunk_is_ignored(self) -> None:
        registry = self._registry()
        registry.declare("carrier-one")
        result = registry.apply_engine_event(
            engine_event("Registry", username="a-trunk-we-do-not-have", status="Registered")
        )
        self.assertIsNone(result)

    async def test_the_transition_history_stays_bounded(self) -> None:
        registry = self._registry()
        trunk = registry.declare("carrier-flapping")
        for _ in range(200):
            registry._transition(trunk, TrunkState.REGISTERING, "attempt")
            registry._transition(trunk, TrunkState.RETRYING, "failure")
        self.assertLessEqual(len(trunk.history), 64)

    async def test_a_publisher_failure_does_not_break_a_transition(self) -> None:
        def bad_publisher(topic: str, payload: dict) -> None:
            raise RuntimeError("the hub is gone")

        registry = TrunkRegistry(publisher=bad_publisher)
        trunk = registry.declare("carrier-one")
        self.assertTrue(registry._transition(trunk, TrunkState.REGISTERING, "attempt"))
        self.assertIs(trunk.state, TrunkState.REGISTERING)


class StateModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.moment = 1000.0
        self.state = ApplianceState(clock=lambda: self.moment)

    def test_a_channel_life_cycle_is_tracked_end_to_end(self) -> None:
        self.state.apply_engine_event(
            engine_event(
                "Newchannel", uniqueid="one", channel="PJSIP/alpha",
                channelstate="4", calleridnum="2015550123", exten="201",
            )
        )
        self.assertEqual(self.state.active_call_count, 1)
        self.assertEqual(self.state.calls_started, 1)

        self.moment += 5
        self.state.apply_engine_event(
            engine_event("Newstate", uniqueid="one", channelstatedesc="Up")
        )
        self.moment += 30
        self.state.apply_engine_event(
            engine_event("BridgeEnter", uniqueid="one", bridgeuniqueid="bridge-one")
        )

        snapshot = self.state.snapshot()
        channel = snapshot["channels"][0]
        self.assertEqual(channel["duration_seconds"], 35)
        self.assertEqual(channel["bridge_identifier"], "bridge-one")

        self.state.apply_engine_event(engine_event("Hangup", uniqueid="one"))
        self.assertEqual(self.state.active_call_count, 0)
        self.assertEqual(self.state.calls_completed, 1)

    def test_the_peak_concurrency_figure_is_retained(self) -> None:
        for index in range(10):
            self.state.apply_engine_event(
                engine_event("Newchannel", uniqueid=str(index), channel=f"PJSIP/{index}")
            )
        for index in range(10):
            self.state.apply_engine_event(engine_event("Hangup", uniqueid=str(index)))

        self.assertEqual(self.state.active_call_count, 0)
        self.assertEqual(self.state.peak_concurrent_calls, 10)

    def test_an_unknown_event_is_counted_and_ignored(self) -> None:
        applied = self.state.apply_engine_event(engine_event("SomethingUnrecognised"))
        self.assertFalse(applied)
        self.assertEqual(self.state.events_ignored, 1)
        self.assertEqual(self.state.events_applied, 0)

    def test_losing_the_engine_invalidates_the_channel_view(self) -> None:
        self.state.apply_engine_event(
            engine_event("Newchannel", uniqueid="one", channel="PJSIP/alpha")
        )
        self.state.apply_engine_event(engine_event("ApplianceManagerDisconnected"))

        self.assertEqual(self.state.active_call_count, 0)
        self.assertFalse(self.state.engine_connected)
        self.assertIn("engine-disconnected", self.state.alarms)

    def test_the_sequence_advances_on_every_change(self) -> None:
        before = self.state.sequence
        self.state.apply_engine_event(
            engine_event("Newchannel", uniqueid="one", channel="PJSIP/alpha")
        )
        self.assertGreater(self.state.sequence, before)

    def test_an_alarm_is_not_re_raised_when_unchanged(self) -> None:
        self.assertTrue(self.state.raise_alarm("key", "warning", "a message"))
        self.assertFalse(self.state.raise_alarm("key", "warning", "a message"))
        self.assertTrue(self.state.clear_alarm("key"))
        self.assertFalse(self.state.clear_alarm("key"))

    # -- what the panel looks like when several things are wrong ------------

    def test_the_worst_alarm_is_first_whatever_order_they_arose_in(self) -> None:
        """An operator reads down the panel and must meet the worst first.

        Conditions arise in the order the world produces them, which is never
        the order they matter in. A warning that a certificate expires in a
        month should not sit above a trunk that is carrying no calls because
        the certificate warning happened to be raised first.
        """
        self.state.raise_alarm("later", "information", "worth knowing")
        self.state.raise_alarm("middle", "warning", "worth watching")
        self.state.raise_alarm("first", "critical", "taking calls away")

        keys = [alarm["key"] for alarm in self.state.snapshot()["alarms"]]
        self.assertEqual(keys, ["first", "middle", "later"])

    def test_an_alarm_nobody_has_looked_at_outranks_one_somebody_is_on(self) -> None:
        self.state.raise_alarm("seen", "critical", "one somebody is working")
        self.state.raise_alarm("unseen", "critical", "one nobody has read")
        self.state.acknowledge_alarm("seen", "an operator")

        keys = [alarm["key"] for alarm in self.state.snapshot()["alarms"]]
        self.assertEqual(keys, ["unseen", "seen"])

    def test_acknowledging_does_not_clear_the_alarm(self) -> None:
        """The condition is still true, so the alarm is still raised.

        This is the whole distinction. An acknowledgement that removed the
        alarm would be a way of turning off the thing that tells the next
        operator a trunk has been down since Friday.
        """
        self.state.raise_alarm("trunk-down-carrier", "critical", "the trunk is down")
        self.assertTrue(self.state.acknowledge_alarm("trunk-down-carrier", "an operator"))

        self.assertIn("trunk-down-carrier", self.state.alarms)
        alarm = self.state.snapshot()["alarms"][0]
        self.assertTrue(alarm["acknowledged"])
        self.assertEqual(alarm["acknowledged_by"], "an operator")
        self.assertEqual(alarm["message"], "the trunk is down")

    def test_acknowledging_twice_changes_nothing(self) -> None:
        self.state.raise_alarm("key", "warning", "a message")
        self.assertTrue(self.state.acknowledge_alarm("key", "the first operator"))
        self.assertFalse(self.state.acknowledge_alarm("key", "the second operator"))
        self.assertEqual(
            self.state.alarms["key"].acknowledged_by, "the first operator"
        )

    def test_acknowledging_an_alarm_that_is_not_raised_is_refused(self) -> None:
        self.assertFalse(self.state.acknowledge_alarm("nothing", "an operator"))

    def test_a_worsened_condition_arrives_unacknowledged(self) -> None:
        """Somebody who acknowledged "retrying" has not acknowledged "failed"."""
        self.state.raise_alarm("trunk", "warning", "the trunk is retrying")
        self.state.acknowledge_alarm("trunk", "an operator")

        self.state.raise_alarm("trunk", "critical", "the trunk has failed")

        alarm = self.state.snapshot()["alarms"][0]
        self.assertFalse(
            alarm["acknowledged"],
            "a condition that got worse kept the acknowledgement of the "
            "milder one it replaced",
        )

    def test_an_alarm_that_clears_and_returns_is_unacknowledged(self) -> None:
        self.state.raise_alarm("key", "critical", "a message")
        self.state.acknowledge_alarm("key", "an operator")
        self.state.clear_alarm("key")
        self.state.raise_alarm("key", "critical", "a message")

        self.assertFalse(self.state.snapshot()["alarms"][0]["acknowledged"])

    def test_a_failing_publisher_does_not_break_a_state_change(self) -> None:
        def bad_publisher(topic: str, payload: dict) -> None:
            raise RuntimeError("the hub is gone")

        self.state.publisher = bad_publisher
        self.state.apply_engine_event(
            engine_event("Newchannel", uniqueid="one", channel="PJSIP/alpha")
        )
        self.assertEqual(self.state.active_call_count, 1)


async def _immediately(value: bool) -> bool:
    return value


if __name__ == "__main__":
    unittest.main()
