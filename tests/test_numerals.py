"""Constraint Two — the numeral spelling subsystem and its browser twin."""

from __future__ import annotations

import json
import re
import logging
import shutil
import subprocess
import unittest
from io import StringIO

from support import REPOSITORY_ROOT

from appliance import numerals
from appliance.logging_setup import configure_logging, get_logger


class SpellIntegerTests(unittest.TestCase):
    def test_the_small_values_are_named_exactly(self) -> None:
        expected = {
            0: "zero", 1: "one", 9: "nine", 10: "ten", 11: "eleven",
            13: "thirteen", 19: "nineteen", 20: "twenty", 21: "twenty-one",
            42: "forty-two", 99: "ninety-nine", 100: "one hundred",
            101: "one hundred one", 115: "one hundred fifteen",
            999: "nine hundred ninety-nine",
        }
        for value, words in expected.items():
            with self.subTest(value=value):
                self.assertEqual(numerals.spell_integer(value), words)

    def test_the_scale_groups_are_assembled_correctly(self) -> None:
        expected = {
            1000: "one thousand",
            1024: "one thousand twenty-four",
            5038: "five thousand thirty-eight",
            100000: "one hundred thousand",
            1000000: "one million",
            1000001: "one million one",
            123456789: "one hundred twenty-three million four hundred fifty-six "
                       "thousand seven hundred eighty-nine",
        }
        for value, words in expected.items():
            with self.subTest(value=value):
                self.assertEqual(numerals.spell_integer(value), words)

    def test_a_negative_value_is_named_as_negative(self) -> None:
        self.assertEqual(numerals.spell_integer(-7), "negative seven")
        self.assertEqual(numerals.spell_integer(-1000), "negative one thousand")

    def test_no_rendering_of_any_value_contains_a_digit(self) -> None:
        for value in list(range(0, 3000)) + [10**6, 10**9, 10**12, 2**31 - 1]:
            self.assertFalse(
                numerals.contains_digit(numerals.spell_integer(value)),
                f"the rendering of the value {value} leaked a digit character",
            )

    def test_a_value_beyond_the_scale_table_is_refused(self) -> None:
        with self.assertRaises(numerals.SpellingError):
            numerals.spell_integer(10**30)

    def test_a_non_integer_is_refused(self) -> None:
        for candidate in (1.5, "four", None, True):
            with self.subTest(candidate=candidate):
                with self.assertRaises(numerals.SpellingError):
                    numerals.spell_integer(candidate)  # type: ignore[arg-type]


class SpellDecimalTests(unittest.TestCase):
    def test_a_fraction_is_spoken_digit_by_digit(self) -> None:
        self.assertEqual(numerals.spell_decimal(3.14), "three point one four")
        self.assertEqual(numerals.spell_decimal(0.5, 1), "zero point five")

    def test_a_whole_value_omits_the_fraction(self) -> None:
        self.assertEqual(numerals.spell_decimal(12.0), "twelve")

    def test_a_non_finite_value_is_refused(self) -> None:
        for candidate in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(candidate=candidate):
                with self.assertRaises(numerals.SpellingError):
                    numerals.spell_decimal(candidate)


class SpellOrdinalTests(unittest.TestCase):
    def test_the_ordinals_are_named_exactly(self) -> None:
        expected = {
            1: "first", 2: "second", 3: "third", 5: "fifth", 12: "twelfth",
            13: "thirteenth", 20: "twentieth", 21: "twenty-first",
            42: "forty-second", 100: "one hundredth",
            101: "one hundred first", 1000: "one thousandth",
        }
        for value, words in expected.items():
            with self.subTest(value=value):
                self.assertEqual(numerals.spell_ordinal(value), words)


class SpellDurationTests(unittest.TestCase):
    def test_the_units_are_assembled_and_empty_units_omitted(self) -> None:
        expected = {
            0: "zero seconds",
            1: "one second",
            59: "fifty-nine seconds",
            60: "one minute",
            90: "one minute thirty seconds",
            3600: "one hour",
            86400: "one day",
            90061: "one day one hour one minute one second",
        }
        for seconds, words in expected.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(numerals.spell_duration(seconds), words)

    def test_a_negative_duration_is_refused(self) -> None:
        with self.assertRaises(numerals.SpellingError):
            numerals.spell_duration(-1)


class SanitizeTests(unittest.TestCase):
    def test_a_dotted_address_is_left_alone_because_it_is_an_identifier(self) -> None:
        """An address is something an operator types, not something they count.

        Spelling it was the old rule and it made the address unusable: nobody
        can put "one hundred ninety-two.one hundred sixty-eight.one.ten" into a
        browser. The unconditional form is still available and still spells it,
        so the machinery that guarantees no digit can escape is unchanged.
        """
        self.assertEqual(numerals.sanitize("192.168.1.10"), "192.168.1.10")
        self.assertEqual(
            numerals.sanitize("192.168.1.10", spell_identifiers=True),
            "one hundred ninety-two.one hundred sixty-eight.one.ten",
        )

    def test_a_replacement_does_not_fuse_with_adjacent_letters(self) -> None:
        self.assertEqual(numerals.sanitize("abc123def"), "abc one hundred twenty-three def")
        self.assertEqual(numerals.sanitize("x9y"), "x nine y")

    def test_leading_zeros_are_spoken(self) -> None:
        self.assertEqual(numerals.sanitize("007"), "zero zero seven")
        self.assertEqual(numerals.sanitize("000"), "zero zero zero")

    def test_text_without_digits_is_returned_unchanged(self) -> None:
        original = "the trunk moved from registering to registered"
        self.assertIs(numerals.sanitize(original), original)

    def test_no_quantity_survives_as_a_digit(self) -> None:
        """Everything that is a quantity is spelled, in ordinary operator text."""
        samples = [
            "call 1 of 100 answered after 3 seconds",
            "identifier abc-000123-xyz",
            "restarted 3 services and shed 2 dashboards",
            "the span reported 24 channels and 1 alarm",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertFalse(numerals.contains_digit(numerals.sanitize(sample)))

    def test_an_identifier_keeps_its_digits_and_its_neighbours_do_not(self) -> None:
        """The exemption is narrow: only the identifier itself survives.

        This is the test that would catch the exemption widening until it
        swallowed the constraint. Each sample carries an identifier that must
        come through untouched and a quantity beside it that must not.
        """
        samples = [
            ("the appliance bound port 8088 on address 10.0.0.5 for 3 trunks",
             ("port 8088", "10.0.0.5"), "three"),
            ("2026-07-27T11:07:00Z after 12 attempts",
             ("2026-07-27T11:07:00",), "twelve"),
            ("version 1.2.0 carries 32 modules", ("1.2.0",), "thirty-two"),
            ("eth0 dropped 7 frames", ("eth0",), "seven"),
            ("the trunk returned SIP 403 on 2 calls", ("SIP 403",), "two"),
        ]
        for sample, identifiers, quantity in samples:
            with self.subTest(sample=sample):
                rendered = numerals.sanitize(sample)
                for identifier in identifiers:
                    self.assertIn(identifier, rendered)
                self.assertIn(quantity, rendered)

    def test_the_unconditional_form_still_spells_absolutely_everything(self) -> None:
        """The original guarantee is intact and reachable.

        The exemption is a choice made by the caller. The machinery that leaves
        nothing as digits is unchanged underneath it, which is what makes the
        exemption reversible.
        """
        samples = [
            "the appliance bound port 8088 on address 10.0.0.5",
            "2026-07-27T11:07:00Z",
            "version 1.2.0 on eth0",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertFalse(
                    numerals.contains_digit(
                        numerals.sanitize(sample, spell_identifiers=True)
                    )
                )



def _message_of(line: str) -> str:
    """The part of a log line the appliance wrote, without its timestamp.

    A line begins with a date and a time, which are identifiers and keep their
    digits. Everything after them is what the appliance chose to say, and that
    is where a stray quantity would be a defect.
    """
    return re.sub(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s*", "", line)


class LoggingConstraintTests(unittest.TestCase):
    """The formatter must make Constraint Two unconditional."""

    def test_no_quantity_can_reach_a_log_line_as_a_digit(self) -> None:
        """A quantity logged anywhere, by any route, comes out as words.

        The routes are covered deliberately: a message argument, a value
        interpolated into a string, and an exception's own text. A stage author
        cannot leak a quantity by forgetting the rule, because they never apply
        it — the formatter does.
        """
        stream = StringIO()
        logger = configure_logging("DEBUG", log_file=None, stream=stream)
        component = get_logger("constraint-probe")

        component.warning("there are %d active calls of a permitted %d", 42, 256)
        component.error("the value %s failed at index %d", "abc123", 7)
        try:
            raise ValueError("a failure counting 500 things")
        except ValueError:
            component.exception("an exception carrying a quantity was logged")

        for handler in logger.handlers:
            handler.flush()

        output = stream.getvalue()
        self.assertTrue(output.strip(), "the probe produced no log output at all")

        # The timestamp each line begins with is an identifier and keeps its
        # digits on purpose, so the assertion is made against what the appliance
        # actually said rather than against when it said it.
        for line in output.splitlines():
            message = _message_of(line)
            self.assertFalse(
                numerals.contains_digit(message),
                f"a quantity escaped into a log line as digits: {message!r}",
            )

        self.assertIn("forty-two", output)
        self.assertIn("five hundred", output)

    def test_an_identifier_reaches_the_log_intact(self) -> None:
        """A log an engineer greps has to carry the address they are grepping for.

        This is the half of the rule that the old behaviour got wrong. An
        address, a port and a version spelled into words made the log unusable
        for the one purpose a log at three in the morning has.
        """
        stream = StringIO()
        logger = configure_logging("DEBUG", log_file=None, stream=stream)
        component = get_logger("constraint-probe")

        component.info("the appliance bound port %d on address %s", 8088, "10.0.0.5")
        component.info("the engine at %s reported %s", "192.0.2.15:5038", "SIP 403")

        for handler in logger.handlers:
            handler.flush()

        output = stream.getvalue()
        for identifier in ("port 8088", "10.0.0.5", "192.0.2.15:5038", "SIP 403"):
            self.assertIn(
                identifier,
                output,
                f"the identifier {identifier} did not survive into the log",
            )

    def test_reconfiguring_does_not_stack_handlers(self) -> None:
        first = configure_logging("INFO", log_file=None, stream=StringIO())
        count = len(first.handlers)
        second = configure_logging("INFO", log_file=None, stream=StringIO())
        self.assertEqual(len(second.handlers), count)

    def tearDown(self) -> None:
        logging.getLogger("myipbx").handlers.clear()


class CrossImplementationAgreementTests(unittest.TestCase):
    """The browser twin must agree with the server module exactly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest(
                "the browser scripting runtime is unavailable, so the agreement "
                "between the two implementations cannot be checked here"
            )

    def _run_browser_side(self, program: str) -> list[str]:
        module = (REPOSITORY_ROOT / "web/js/numerals.js").as_posix()
        script = f"const numerals = require({json.dumps(module)});\n{program}"
        completed = subprocess.run(
            [self.node, "-e", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            completed.returncode, 0, f"the browser side failed: {completed.stderr}"
        )
        return completed.stdout.splitlines()

    def test_the_integer_spellings_agree_across_the_exercised_range(self) -> None:
        values = list(range(0, 2500)) + [
            5038, 8088, 65535, 86400, 100000, 262144, 1000000, 123456789,
            -1, -42, -100000,
        ]
        expected = [numerals.spell_integer(value) for value in values]
        produced = self._run_browser_side(
            f"const values = {json.dumps(values)};\n"
            "console.log(values.map(function (v) { return numerals.spellInteger(v); }).join('\\n'));"
        )
        self.assertEqual(produced, expected)

    def test_the_ordinal_and_duration_spellings_agree(self) -> None:
        ordinals = list(range(0, 400))
        durations = [0, 1, 45, 60, 61, 3599, 3600, 7325, 86400, 90061, 172800]

        expected = [numerals.spell_ordinal(value) for value in ordinals]
        expected += [numerals.spell_duration(value) for value in durations]

        produced = self._run_browser_side(
            f"const ordinals = {json.dumps(ordinals)};\n"
            f"const durations = {json.dumps(durations)};\n"
            "const out = ordinals.map(function (v) { return numerals.spellOrdinal(v); })\n"
            "  .concat(durations.map(function (v) { return numerals.spellDuration(v); }));\n"
            "console.log(out.join('\\n'));"
        )
        self.assertEqual(produced, expected)

    def test_the_sanitiser_agrees_on_realistic_operator_text(self) -> None:
        samples = [
            "192.168.1.10", "port 5038", "abc123def", "007", "0", "x9y",
            "call 1 of 100", "2026-07-27 11:07:00", "span 1 channel 24",
            "the ceiling is 300 seconds with 25 percent jitter",
            # Every family of card model in share/digium-cards.tsv. The three
            # implementations disagreed here for as long as this shape existed:
            # the server side wrote its escapes twice over, so it spelled every
            # model while the other two kept the digits, and this list did not
            # sample one. A technician reads these against the silkscreen on a
            # card in their hand.
            "the Wildcard TDM2400P is fitted",
            "the Wildcard TE410P is fitted",
            "the Wildcard TE435 is fitted",
            "the Wildcard AEX2400 is fitted",
            "the Wildcard B410P is fitted",
            "the Wildcard A4A is fitted",
            "the Wildcard A8B is fitted",
            "the Wildcard TCE400P is fitted",
            "HA8-0000 is fitted",
            "HB8-0000 is fitted",
        ]
        expected = [numerals.sanitize(sample) for sample in samples]
        produced = self._run_browser_side(
            f"const samples = {json.dumps(samples)};\n"
            "console.log(samples.map(function (s) { return numerals.sanitize(s); }).join('\\n'));"
        )
        self.assertEqual(produced, expected)


class ShellImplementationAgreementTests(unittest.TestCase):
    """The staging scripts carry a third implementation, which must also agree.

    The installer runs before the interpreter package is in place, so the shell
    library spells its own numerals.  A third implementation is a third chance
    to drift, so it is held to the same agreement as the browser twin.
    """

    def _run_shell(self, program: str) -> list[str]:
        library = (REPOSITORY_ROOT / "scripts/lib/common.sh").as_posix()
        script = f'source "{library}"\n{program}'
        completed = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=300
        )
        self.assertEqual(
            completed.returncode, 0, f"the shell side failed: {completed.stderr}"
        )
        return completed.stdout.splitlines()

    def test_the_shell_integer_spelling_agrees_with_the_control_plane(self) -> None:
        values = list(range(0, 200)) + [
            256, 300, 512, 1000, 1024, 2048, 5038, 8088, 65535, 86400,
            100000, 1000000, 123456789,
        ]
        expected = [numerals.spell_integer(value) for value in values]
        produced = self._run_shell(
            "for value in " + " ".join(str(value) for value in values) + "; do\n"
            '    spell_integer "${value}"; printf "\\n"\n'
            "done"
        )
        self.assertEqual(produced, expected)

    def test_the_shell_sanitiser_agrees_with_the_control_plane(self) -> None:
        samples = [
            "the appliance bound port 8088",
            "192.168.1.10",
            "span 1 channel 24",
            "007",
            "stage 3 of 5 complete",
            # The pre-flight check names the fitted card, and it runs from this
            # library rather than from the control plane. A technician holding
            # the card reads what this prints against the silkscreen.
            "a legacy interface card is fitted: Wildcard A4A",
            "a legacy interface card is fitted: Wildcard TDM2400P",
            "a legacy interface card is fitted: Wildcard B410P",
            "a legacy interface card is fitted: HA8-0000",
            "a legacy interface card is fitted: Wildcard TE435",
        ]
        expected = [numerals.sanitize(sample) for sample in samples]

        quoted = " ".join(f"'{sample}'" for sample in samples)
        produced = self._run_shell(
            f"for sample in {quoted}; do\n"
            '    spell_all "${sample}"; printf "\\n"\n'
            "done"
        )
        self.assertEqual(produced, expected)

    def test_no_staging_script_emits_a_digit_when_rehearsed(self) -> None:
        """The installer's own output must satisfy Constraint Two."""
        completed = subprocess.run(
            ["bash", str(REPOSITORY_ROOT / "scripts/install-appliance.sh"), "--help"],
            capture_output=True, text=True, timeout=60,
        )
        combined = completed.stdout + completed.stderr
        self.assertTrue(combined.strip(), "the installer produced no output")
        self.assertFalse(
            numerals.contains_digit(combined),
            f"the installer emitted a digit character: {combined!r}",
        )


if __name__ == "__main__":
    unittest.main()
