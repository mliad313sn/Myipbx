"""What a call cost.

The one report anybody acts on financially, and the one whose defects are
invisible: a total that is out by a factor of sixty because a four second call
was divided rather than rounded up looks exactly like a total that is right.

Every figure below is arithmetic somebody can check by hand against a rate
table stated in the test.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from appliance import entities, rating, reports

NOW = datetime(2026, 7, 28, 12, 0, 0)

#: A rate table of the shape a real site has: a specific international rate, a
#: national rate that would also match those numbers, and no floor.
TARIFFS = [
    {"name": "international", "prefix": "00", "currency": "pounds",
     "connection_fee": "0.05", "per_minute": "0.18",
     "increment_seconds": 60, "minimum_seconds": 60, "enabled": True},
    {"name": "national", "prefix": "0", "currency": "pounds",
     "connection_fee": "0.02", "per_minute": "0.012",
     "increment_seconds": 60, "minimum_seconds": 0, "enabled": True},
]


def call(destination: str, talk: int, context: str = "outbound",
         disposition: str = "ANSWERED", source: str = "201") -> dict:
    at = NOW - timedelta(days=1)
    return {
        "account_code": "", "source": source, "destination": destination,
        "context": context, "caller_identity": "", "application": "Dial",
        "channel": "", "destination_channel": "",
        "started_at": at.strftime("%Y-%m-%d %H:%M:%S"),
        "answered_at": "", "ended_at": "",
        "duration": talk + 5, "billable_seconds": talk,
        "disposition": disposition, "unique_identifier": "u",
    }


def report_over(records, tariffs=TARIFFS):
    window = reports.resolve_window("last-thirty-days", now=NOW)
    return reports.build_report(
        records, window,
        extensions=[{"number": "201", "name": "reception"}],
        tariffs=tariffs,
        inbound_context="from-trunk", internal_context="internal",
    )


class WhichRateAppliesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rates = rating.load_tariffs(TARIFFS)

    def test_the_longest_matching_prefix_wins(self) -> None:
        """Both rates match an international number; the wrong one is cheap.

        Getting this backwards charges an international call at the national
        rate, which is the direction nobody notices until the bill arrives.
        """
        matched = rating.match_tariff(self.rates, "00441632960111")
        self.assertEqual(matched.name, "international")

    def test_a_shorter_prefix_still_matches_what_it_covers(self) -> None:
        self.assertEqual(rating.match_tariff(self.rates, "01632960111").name, "national")

    def test_a_number_no_rate_covers_is_unmatched_rather_than_free(self) -> None:
        self.assertIsNone(rating.match_tariff(self.rates, "911"))

    def test_an_empty_prefix_is_the_floor_of_the_table(self) -> None:
        rates = rating.load_tariffs(TARIFFS + [
            {"name": "everything else", "prefix": "", "currency": "pounds",
             "connection_fee": "0", "per_minute": "0.5",
             "increment_seconds": 1, "minimum_seconds": 0, "enabled": True},
        ])
        self.assertEqual(rating.match_tariff(rates, "911").name, "everything else")
        # And it does not steal from the specific rates above it.
        self.assertEqual(rating.match_tariff(rates, "00441632960111").name,
                         "international")

    def test_a_disabled_rate_is_not_in_the_table_at_all(self) -> None:
        rates = rating.load_tariffs([dict(TARIFFS[0], enabled=False), TARIFFS[1]])
        self.assertEqual(rating.match_tariff(rates, "00441632960111").name, "national")

    def test_a_rate_with_no_name_is_left_out(self) -> None:
        self.assertEqual(rating.load_tariffs([dict(TARIFFS[0], name="  ")]), [])


class WhatOneCallCostsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rates = rating.load_tariffs(TARIFFS)
        self.international = rating.match_tariff(self.rates, "00441632960111")
        self.national = rating.match_tariff(self.rates, "01632960111")

    def test_time_is_billed_in_whole_increments(self) -> None:
        """A carrier selling by the minute charges a minute for four seconds.

        Dividing four by sixty instead would be out by a factor of fifteen on
        exactly the calls a site makes most of.
        """
        cost = rating.charge(self.national, 4)
        self.assertEqual(rating.round_money(cost),
                         rating.round_money(Decimal("0.02") + Decimal("0.012")))

    def test_a_call_just_over_an_increment_is_charged_two(self) -> None:
        cost = rating.charge(self.national, 61)
        self.assertEqual(rating.round_money(cost),
                         rating.round_money(Decimal("0.02") + Decimal("0.024")))

    def test_a_minimum_lengthens_a_short_call_before_it_is_billed(self) -> None:
        # Sixty second minimum, sixty second increment: a four second
        # international call is one whole minute.
        cost = rating.charge(self.international, 4)
        self.assertEqual(rating.round_money(cost),
                         rating.round_money(Decimal("0.05") + Decimal("0.18")))

    def test_billing_by_the_second_charges_the_seconds(self) -> None:
        rate = rating.Tariff("per second", "", "pounds", Decimal("0"),
                             Decimal("0.60"), 1, 0)
        self.assertEqual(rating.round_money(rating.charge(rate, 30)),
                         Decimal("0.3000"))

    def test_a_call_with_no_conversation_costs_nothing(self) -> None:
        """It was never answered, so no connection charge is due either."""
        self.assertEqual(rating.charge(self.international, 0), Decimal("0"))

    def test_money_is_never_held_as_a_floating_point_number(self) -> None:
        """A tenth of a penny cannot be represented in binary.

        Summed over a quarter of a million calls, that shows.
        """
        rate = rating.Tariff("tenths", "", "pounds", Decimal("0"),
                             Decimal("0.006"), 60, 0)
        total = sum((rating.charge(rate, 60) for _ in range(1000)), Decimal("0"))
        self.assertEqual(rating.round_money(total), Decimal("6.0000"))
        self.assertIsInstance(total, Decimal)

    def test_an_amount_that_is_not_one_is_read_as_zero_rather_than_raising(self) -> None:
        """A report must draw even when one row of the rate table is wrong."""
        rates = rating.load_tariffs([dict(TARIFFS[1], per_minute="1,50")])
        self.assertEqual(rates[0].per_minute, Decimal("0"))


class HowMoneyIsWrittenTests(unittest.TestCase):
    def test_an_amount_is_spelled_like_every_other_quantity(self) -> None:
        self.assertEqual(rating.spell_money(Decimal("4.62"), "pounds"),
                         "four point six two pounds")

    def test_a_round_amount_does_not_read_as_a_string_of_zeroes(self) -> None:
        self.assertEqual(rating.spell_money(Decimal("4.0000"), "pounds"),
                         "four pounds")

    def test_a_rate_below_a_penny_survives_being_presented(self) -> None:
        """Rounded to two places every such rate would read as free."""
        self.assertIn("zero point zero one two five",
                      rating.spell_money(Decimal("0.0125"), "euro"))

    def test_a_currency_is_optional(self) -> None:
        self.assertEqual(rating.spell_money(Decimal("2.5")), "two point five")


class WhatTheReportTotalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = report_over([
            call("00441632960111", 65),   # international: two minutes
            call("01632960111", 4),       # national: one minute
            call("911", 30),              # covered by no rate
        ])

    def test_the_total_is_the_sum_of_the_calls_that_were_rated(self) -> None:
        expected = (
            Decimal("0.05") + Decimal("0.18") * 2   # international, two minutes
            + Decimal("0.02") + Decimal("0.012")    # national, one minute
        )
        self.assertEqual(Decimal(self.report["summary"]["cost"]["count"]),
                         rating.round_money(expected))

    def test_the_calls_no_rate_covered_are_counted_and_named(self) -> None:
        self.assertEqual(self.report["summary"]["rated"]["count"], 2)
        self.assertEqual(self.report["summary"]["unrated"]["count"], 1)

    def test_a_row_with_nothing_rated_says_so_rather_than_showing_zero(self) -> None:
        """Zero would read as "this call was free", which it was not."""
        rows = {row["key"]: row for row in self.report["breakdowns"]["by_destination"]}
        self.assertEqual(rows["911"]["figures"]["cost"]["text"], "not rated")
        self.assertNotIn("zero", rows["911"]["figures"]["cost"]["text"])

    def test_the_currency_is_named_in_every_amount(self) -> None:
        self.assertTrue(
            self.report["summary"]["cost"]["text"].endswith("pounds")
        )
        self.assertEqual(self.report["currency"], "pounds")

    def test_the_exported_amount_is_a_number_rather_than_words(self) -> None:
        payload, _ = reports.export_csv(self.report, "by_destination")
        self.assertIn("0.4100", payload)
        self.assertNotIn("point four one", payload)


class WhatIsNotChargedTests(unittest.TestCase):
    def test_an_inbound_call_is_never_charged(self) -> None:
        report = report_over([
            call("201", 300, context="from-trunk", source="+441632960111"),
        ])
        self.assertEqual(Decimal(report["summary"]["cost"]["count"]), Decimal("0"))
        self.assertEqual(report["summary"]["rated"]["count"], 0)
        self.assertEqual(report["summary"]["unrated"]["count"], 0)

    def test_a_call_between_two_extensions_is_never_charged(self) -> None:
        report = report_over([call("202", 300, context="internal")])
        self.assertEqual(report["summary"]["rated"]["count"], 0)

    def test_an_outbound_call_that_was_not_answered_is_never_charged(self) -> None:
        report = report_over([
            call("01632960111", 0, disposition="NO ANSWER"),
        ])
        self.assertEqual(Decimal(report["summary"]["cost"]["count"]), Decimal("0"))
        self.assertEqual(report["summary"]["rated"]["count"], 0)
        self.assertEqual(report["summary"]["unrated"]["count"], 0)


class WhenThereIsNoRateTableTests(unittest.TestCase):
    def test_no_cost_column_appears_at_all(self) -> None:
        """A column of zeroes would read as "these calls were free"."""
        report = report_over([call("01632960111", 60)], tariffs=[])
        self.assertEqual(report["currency"], "")
        self.assertNotIn("cost", report["summary"])
        headings = [column["key"] for column in report["columns"]["standard"]]
        self.assertNotIn("cost", headings)

    def test_two_currencies_produce_no_cost_and_say_why(self) -> None:
        """Adding two currencies gives a number that is not an amount."""
        report = report_over([call("01632960111", 60)], tariffs=[
            dict(TARIFFS[0], currency="pounds"),
            dict(TARIFFS[1], currency="euro"),
        ])
        self.assertEqual(report["currency"], "")
        self.assertEqual(sorted(report["currency_conflict"]), ["euro", "pounds"])
        self.assertIn("more than one currency", report["currency_note"])
        self.assertNotIn("cost", report["summary"])


class WhatTheTariffEntityOffersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = entities.ENTITY_SPECS["tariffs"]

    def test_the_rate_table_is_editable_from_the_console(self) -> None:
        names = [field.name for field in self.spec.fields]
        for expected in ("prefix", "currency", "connection_fee", "per_minute",
                         "increment_seconds", "minimum_seconds"):
            self.assertIn(expected, names)

    def test_a_prefix_is_an_identifier_and_keeps_its_digits(self) -> None:
        self.assertTrue(self.spec.field("prefix").identifier)

    def test_an_empty_prefix_is_allowed_because_it_is_the_floor(self) -> None:
        self.assertTrue(self.spec.field("prefix").pattern.match(""))
        self.assertTrue(self.spec.field("prefix").pattern.match("0044"))
        self.assertIsNone(self.spec.field("prefix").pattern.match("nought"))

    def test_an_amount_written_with_a_comma_is_refused(self) -> None:
        """A comma is a thousands separator in one country and a point in
        another, and accepting both would misread one by a factor of a
        thousand."""
        pattern = self.spec.field("per_minute").pattern
        self.assertTrue(pattern.match("0.012"))
        self.assertIsNone(pattern.match("1,50"))
        self.assertIsNone(pattern.match("-1"))

    def test_a_currency_is_a_word_rather_than_a_symbol(self) -> None:
        """Because every figure on these screens is read out in words."""
        pattern = self.spec.field("currency").pattern
        self.assertTrue(pattern.match("pounds"))
        self.assertTrue(pattern.match("swiss francs"))
        self.assertIsNone(pattern.match("£"))


if __name__ == "__main__":
    unittest.main()
