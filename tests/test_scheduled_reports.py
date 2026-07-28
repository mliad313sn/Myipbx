"""Reports the appliance draws for itself, keeps, and sends.

Two properties matter more than the rest, and both are about what happens when
something goes wrong rather than when it goes right: a report is kept before any
attempt is made to send it, and a report that has already run today does not run
again because the appliance was restarted.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from appliance import entities, mailer, scheduled

MONDAY = date(2026, 7, 27)
TUESDAY = date(2026, 7, 28)
FIRST_OF_MONTH = date(2026, 8, 1)


class WhenAReportIsDueTests(unittest.TestCase):
    def test_a_daily_report_runs_every_day(self) -> None:
        self.assertTrue(scheduled.is_due("daily", "", TUESDAY))
        self.assertTrue(scheduled.is_due("daily", MONDAY.isoformat(), TUESDAY))

    def test_a_report_that_already_ran_today_does_not_run_again(self) -> None:
        """Restarting the appliance must not produce a second copy."""
        self.assertFalse(scheduled.is_due("daily", TUESDAY.isoformat(), TUESDAY))
        self.assertFalse(scheduled.is_due("weekly", MONDAY.isoformat(), MONDAY))

    def test_a_weekly_report_runs_on_its_day_rather_than_seven_days_on(self) -> None:
        """An interval counted from the last run drifts.

        A report asked for on a Monday that arrives on a Wednesday is a report
        nobody trusts.
        """
        self.assertTrue(scheduled.is_due("weekly", "", MONDAY))
        self.assertFalse(scheduled.is_due("weekly", "", TUESDAY))

    def test_a_monthly_report_runs_on_the_first(self) -> None:
        self.assertTrue(scheduled.is_due("monthly", "", FIRST_OF_MONTH))
        self.assertFalse(scheduled.is_due("monthly", "", TUESDAY))

    def test_an_unreadable_last_run_does_not_stop_a_report_running(self) -> None:
        """Better a duplicate than a report that silently never runs again."""
        self.assertTrue(scheduled.is_due("daily", "whenever", TUESDAY))

    def test_a_frequency_nobody_offers_never_runs(self) -> None:
        self.assertFalse(scheduled.is_due("hourly", "", TUESDAY))

    def test_only_enabled_schedules_with_names_are_due(self) -> None:
        due = scheduled.due_reports([
            {"name": "morning", "frequency": "daily", "enabled": True},
            {"name": "paused", "frequency": "daily", "enabled": False},
            {"name": "", "frequency": "daily", "enabled": True},
            {"name": "weekly-one", "frequency": "weekly", "enabled": True},
        ], {}, TUESDAY)
        self.assertEqual([entry["name"] for entry in due], ["morning"])


class WhereSnapshotsLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.store = scheduled.ScheduleStore(Path(self.workspace.name) / "reports")

    def test_a_snapshot_is_named_for_the_report_and_the_period(self) -> None:
        name = scheduled.snapshot_name(
            "the switchboard", {"from": "2026-07-01", "to": "2026-07-31"}
        )
        self.assertEqual(name, "the-switchboard_2026-07-01_to_2026-07-31.csv")

    def test_a_name_cannot_carry_a_separator_into_the_filename(self) -> None:
        name = scheduled.snapshot_name("../../etc/shadow", {"from": "a", "to": "b"})
        self.assertNotIn("/", name)
        self.assertFalse(name.startswith("."))
        self.assertEqual(name, "-..-etc-shadow_a_to_b.csv")

    def test_a_name_of_nothing_but_dots_still_produces_a_file(self) -> None:
        self.assertEqual(scheduled.snapshot_name("...", {"from": "a", "to": "b"}),
                         "report_a_to_b.csv")

    def test_a_snapshot_is_written_and_read_back(self) -> None:
        self.store.write("morning_2026-07-27_to_2026-07-27.csv", "key,calls\n201,4\n")
        listed = self.store.list()
        self.assertEqual(len(listed), 1)
        self.assertIn("201,4", self.store.read(listed[0]["name"]))

    def test_a_name_that_escapes_the_directory_reads_nothing(self) -> None:
        outside = Path(self.workspace.name) / "a-secret.csv"
        outside.write_text("this must never be served", encoding="utf-8")
        self.store.prepare()
        for name in ("../a-secret.csv", "/etc/passwd", "sub/report.csv",
                     ".hidden.csv", "report.txt"):
            with self.subTest(name=name):
                self.assertIsNone(self.store.read(name))

    def test_what_ran_when_survives_being_written(self) -> None:
        self.store.record_run("morning", datetime(2026, 7, 27, 6, 0, 0))
        self.assertEqual(self.store.last_runs(), {"morning": "2026-07-27"})

    def test_an_unreadable_ledger_reads_as_nothing_rather_than_raising(self) -> None:
        self.store.prepare()
        self.store.ledger.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.store.last_runs(), {})

    def test_only_the_newest_snapshots_are_kept(self) -> None:
        """A habit, not an archive: the appliance must not fill its own disk."""
        for index in range(8):
            self.store.write(f"morning_2026-07-{index + 10:02d}_to_x.csv", "x")
        removed = self.store.prune(keep=3)
        self.assertEqual(removed, 5)
        self.assertEqual(len(self.store.list()), 3)


class WhatIsSentTests(unittest.TestCase):
    def destination(self, **overrides) -> mailer.Destination:
        settings = {
            "name": "the office", "recipient": "owner@example.com",
            "sender": "appliance@example.com", "host": "mail.example.com",
            "port": 587, "security": "upgraded", "username": "appliance",
            "secret": "a-password",
        }
        settings.update(overrides)
        return mailer.destination_from(settings, settings.pop("secret", ""))

    def test_the_report_is_attached_as_a_file_rather_than_pasted_in(self) -> None:
        """A report somebody has to retype out of a message is not used."""
        message = mailer.build_message(
            self.destination(), "a subject", "a body",
            attachment=("report.csv", "key,calls\n201,4\n"),
        )
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "report.csv")
        self.assertIn(b"201,4", attachments[0].get_payload(decode=True))

    def test_a_destination_with_no_server_is_refused_before_anything_is_tried(self) -> None:
        with self.assertRaises(mailer.MailRefused) as caught:
            mailer.send(self.destination(host=""), EmailMessage())
        self.assertIn("no mail server", str(caught.exception))

    def test_a_destination_missing_an_address_is_refused(self) -> None:
        with self.assertRaises(mailer.MailRefused):
            mailer.send(self.destination(recipient=""), EmailMessage())

    def test_an_upgraded_connection_is_upgraded_before_anything_is_sent(self) -> None:
        recorder = _Recorder()
        mailer.send(self.destination(), EmailMessage(), transport=recorder)
        self.assertEqual(
            recorder.client.calls[:2],
            [("starttls",), ("login", "appliance", "a-password")],
        )
        self.assertIn(("send_message",), recorder.client.calls)

    def test_a_secured_connection_is_not_upgraded_again(self) -> None:
        recorder = _Recorder()
        mailer.send(self.destination(security="secured", port=465),
                    EmailMessage(), transport=recorder)
        self.assertNotIn(("starttls",), recorder.client.calls)

    def test_a_destination_with_no_account_does_not_try_to_sign_in(self) -> None:
        recorder = _Recorder()
        mailer.send(self.destination(username=""), EmailMessage(), transport=recorder)
        self.assertFalse(any(call[0] == "login" for call in recorder.client.calls))

    def test_a_server_that_refuses_gives_a_reason_naming_it(self) -> None:
        def failing(host, port, timeout=None):
            raise OSError("connection refused")

        with self.assertRaises(mailer.MailRefused) as caught:
            mailer.send(self.destination(), EmailMessage(), transport=failing)
        message = str(caught.exception)
        self.assertIn("mail.example.com", message)
        self.assertIn("owner@example.com", message)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, username, secret):
        self.calls.append(("login", username, secret))

    def send_message(self, message):
        self.calls.append(("send_message",))


class _Recorder:
    """Stands in for the standard library's client, called exactly as it is."""

    def __init__(self) -> None:
        self.client = _FakeClient()
        self.opened: tuple | None = None

    def __call__(self, host, port, timeout=None):
        self.opened = (host, port, timeout)
        return self.client


class WhatTheEntitiesOfferTests(unittest.TestCase):
    def test_a_scheduled_report_can_choose_any_report_the_console_draws(self) -> None:
        field = entities.ENTITY_SPECS["scheduled_reports"].field("report")
        for expected in ("by_extension", "by_day", "queue:by_queue",
                         "the calls themselves"):
            self.assertIn(expected, field.choices)

    def test_every_period_a_schedule_offers_is_one_the_appliance_resolves(self) -> None:
        from appliance import reports
        field = entities.ENTITY_SPECS["scheduled_reports"].field("period")
        known = {name for name, _ in reports.WINDOWS}
        for choice in field.choices:
            self.assertIn(choice, known)

    def test_a_schedule_may_keep_a_report_without_sending_it(self) -> None:
        field = entities.ENTITY_SPECS["scheduled_reports"].field("destination")
        self.assertFalse(field.required)

    def test_a_destination_names_the_server_this_appliance_reaches_out_to(self) -> None:
        """It accepts no mail and runs no mail server."""
        spec = entities.ENTITY_SPECS["mail_destinations"]
        self.assertIn("accepts no mail", spec.field("host").help)

    def test_an_unprotected_connection_has_to_be_chosen_explicitly(self) -> None:
        field = entities.ENTITY_SPECS["mail_destinations"].field("security")
        self.assertEqual(field.default, "upgraded")
        self.assertIn("none", field.choices)
        self.assertIn("in the clear", field.help)

    def test_a_destination_cannot_be_deleted_while_a_report_uses_it(self) -> None:
        spec = entities.ENTITY_SPECS["mail_destinations"]
        self.assertIn(("scheduled_reports", "destination"), spec.referenced_by)


if __name__ == "__main__":
    unittest.main()
