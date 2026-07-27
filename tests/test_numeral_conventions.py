"""Which numeric fields the interface sends as numbers, and which as words.

Constraint Two says the operator sees words. It does not say which side of the
connection turns the number into them, and the appliance deliberately does
both: a quantity the console compares or thresholds is sent as a number and
spelled when it is drawn, and a quantity that is only ever read is sent already
spelled.

That is a sound arrangement and an invisible one. The two kinds look identical
in a payload, so a field that quietly changes kind breaks its own readout and
nothing else — a number drawn without spelling puts a digit on screen, and a
spelled string passed through the browser's spelling function renders as
nonsense. Neither would fail a test that only checked the route answered.

So the kind of every numeric field is pinned here by name. A field that changes
kind fails by name, and whoever changed it has to decide whether to change it
back or to change the console with it. Adding a field fails too, which is the
point: the decision should be made once, on purpose, rather than discovered on
a dashboard.
"""

from __future__ import annotations

import json
import unittest

from support import ApplianceHarness, TEST_PASSWORD, TEST_USERNAME

from appliance import numerals


#: Routes whose whole payload is walked. Reads only — nothing here mutates.
SURVEYED_ROUTES = (
    "/api/state",
    "/api/hardware",
    "/api/trunks",
    "/api/tasks",
    "/api/sessions",
    "/api/constraints",
    "/api/firewall",
    "/api/health",
    "/api/configuration/drift",
    "/api/entities/extensions",
)

#: Fields the console spells itself, because it compares or thresholds them.
#: Paths are dotted; a list is written as an empty pair of brackets.
SENT_AS_NUMBERS = {
    "/api/entities/extensions.records[].ring_seconds",
    "/api/hardware.alarmed_span_count",
    "/api/hardware.card_count",
    "/api/hardware.channel_count",
    "/api/hardware.span_count",
    "/api/state.active_calls",
    # How long an alarm has stood. The console spells this itself through the
    # same duration helper the trunk tiles use, so that "three minutes" and
    # "one hour" read the same wherever they appear.
    "/api/state.alarms[].age_seconds",
    "/api/state.answered_calls",
    "/api/state.calls_completed",
    "/api/state.calls_started",
    "/api/state.engine.connection_count",
    "/api/state.engine.pending_actions",
    "/api/state.engine.port",
    "/api/state.engine.retry_attempt",
    "/api/state.events_applied",
    "/api/state.events_ignored",
    "/api/state.hardware.alarmed_span_count",
    "/api/state.hardware.card_count",
    "/api/state.hardware.channel_count",
    "/api/state.hardware.span_count",
    "/api/state.peak_concurrent_calls",
    "/api/state.sequence",
    "/api/state.socket.broadcast_sequence",
    "/api/state.socket.coalesce_milliseconds",
    "/api/state.socket.coalesced_publications",
    "/api/state.socket.connection_count",
    "/api/state.socket.flushed_publications",
    "/api/state.socket.maximum_connections",
    "/api/state.socket.rejected_connections",
    "/api/state.socket.slow_consumer_disconnections",
    "/api/state.uptime_seconds",
    "/api/tasks.tasks[].failure_count",
    "/api/tasks.tasks[].run_count",
    "/api/tasks.tasks[].seconds_since_last_run",
    "/api/trunks.registered",
    "/api/trunks.total",
    # The console spells these two itself, as a count and as a duration.
    "/api/trunks.trunks[].attempts",
    "/api/trunks.trunks[].seconds_in_state",
    # These two are data the console does not currently draw. They are numbers
    # so that whatever draws them next can compare them; if one is ever printed
    # as it arrives it will put a digit on screen, which is what this file is
    # for.
    "/api/trunks.trunks[].seconds_until_next_attempt",
    "/api/trunks.counts.retrying",
}

#: Fields already spelled when they leave the appliance, because nothing
#: downstream needs them as numbers.
SENT_AS_WORDS = {
    "/api/configuration/drift.diverged_count",
    "/api/constraints.address_allocation.critical_count",
    "/api/constraints.address_allocation.finding_count",
    "/api/entities/extensions.count",
    "/api/firewall.active_count",
    "/api/firewall.open_to_anywhere_count",
    "/api/firewall.rule_count",
    "/api/health.alarm_count",
    "/api/sessions.count",
}


def _numeric_fields(payload: object, prefix: str) -> tuple[set[str], set[str]]:
    """Split a payload's numeric fields into the two kinds, by dotted path."""
    numbers: set[str] = set()
    words: set[str] = set()

    def walk(node: object, path: str) -> None:
        if isinstance(node, bool):
            # A flag is not a quantity, and reads as an integer if not caught.
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for value in node[:1]:
                walk(value, f"{path}[]")
        elif isinstance(node, int):
            numbers.add(path)
        elif isinstance(node, str) and _looks_like_a_spelled_quantity(path, node):
            words.add(path)

    walk(payload, prefix)
    return numbers, words


def _looks_like_a_spelled_quantity(path: str, value: str) -> bool:
    """A string is a spelled quantity when its name and its content agree.

    Both halves matter. A name alone would catch a host name ending in
    ``count``; content alone would catch the word ``one`` inside a description.
    """
    name = path.rsplit(".", 1)[-1]
    if not name.endswith(("_count", "_seconds", "_port", "_milliseconds")):
        return False
    return bool(value) and not numerals.contains_digit(value)


class NumeralConventionTests(unittest.IsolatedAsyncioTestCase):
    """Every numeric field keeps the kind it was designed with."""

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(
            {
                "revision": 1,
                "trunks": [
                    {
                        "name": "carrier",
                        "technology": "PJSIP",
                        "host": "sip.example.net",
                        "username": "account",
                        "enabled": True,
                    }
                ],
                "extensions": [{"number": "201", "target": "PJSIP/201"}],
                "dialplan": {
                    "internal_context": "internal",
                    "inbound_context": "from-trunk",
                },
                "hardware": {"spans": []},
            }
        )
        self.appliance = await self.harness.start()
        self.cookie = await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def _survey(self) -> tuple[set[str], set[str]]:
        numbers: set[str] = set()
        words: set[str] = set()
        for route in SURVEYED_ROUTES:
            status, _, payload = await self.harness.request(
                "GET", route, cookie=self.cookie
            )
            self.assertEqual(status, 200, f"the route {route} did not answer")
            found_numbers, found_words = _numeric_fields(payload, route)
            numbers |= found_numbers
            words |= found_words
        return numbers, words

    async def test_no_field_has_quietly_changed_kind(self) -> None:
        numbers, words = await self._survey()

        changed_to_words = SENT_AS_NUMBERS & words
        self.assertEqual(
            changed_to_words,
            set(),
            "these were sent as numbers and are now words; the console spells "
            "them itself and will render them as nonsense",
        )

        changed_to_numbers = SENT_AS_WORDS & numbers
        self.assertEqual(
            changed_to_numbers,
            set(),
            "these were sent as words and are now numbers; the console prints "
            "them as they arrive and will put a digit on screen",
        )

    async def test_no_numeric_field_is_unaccounted_for(self) -> None:
        """A new field is a decision, and it should be made here rather than found.

        This is the test that fails when somebody adds a count without choosing
        which side spells it. The fix is one line in the appropriate set above,
        and a glance at whether the console draws it correctly.
        """
        numbers, words = await self._survey()
        unaccounted = (numbers - SENT_AS_NUMBERS) | (words - SENT_AS_WORDS)

        self.assertEqual(
            unaccounted,
            set(),
            "these numeric fields are not recorded in either set; decide which "
            "side spells each one and add it: " + ", ".join(sorted(unaccounted)),
        )

    async def test_every_field_sent_as_words_really_is_free_of_digits(self) -> None:
        _, words = await self._survey()
        self.assertTrue(words, "the survey found no spelled fields at all")

        for route in SURVEYED_ROUTES:
            status, _, payload = await self.harness.request(
                "GET", route, cookie=self.cookie
            )
            self.assertEqual(status, 200)
            rendered = json.dumps(payload)
            for path in sorted(SENT_AS_WORDS):
                if not path.startswith(route + "."):
                    continue
                value = payload
                for step in path[len(route) + 1 :].split("."):
                    value = value.get(step) if isinstance(value, dict) else None
                if isinstance(value, str):
                    self.assertFalse(
                        numerals.contains_digit(value),
                        f"{path} is meant to arrive spelled and carries a digit",
                    )
            self.assertIsInstance(rendered, str)


if __name__ == "__main__":
    unittest.main()
