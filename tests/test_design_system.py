"""The design system's own rules, checked rather than remembered.

A design system is a set of promises about how a console behaves, and every one
of them is the kind of promise that decays quietly. A stylesheet gains one
literal colour and stops following the dark theme in one place nobody opens. A
status variant gains a colour and forgets its mark, and works perfectly for
everybody who can see the difference. A showcase gains a font from a public
network and works on every machine except the air gapped ones the product is
sold for.

None of those fail loudly, and none of them are visible in a review of the line
that caused them. So they are checked here instead.

Four rules are enforced.

Nothing is fetched. The appliance is frequently installed on a network that can
reach nothing at all, so a stylesheet or a page that names a remote address is a
console that renders differently — or not at all — on the machines that matter.

No component writes a colour. Every colour in the component layer is a reference
to a token, because a literal cannot follow a theme, and a component carrying
one has silently opted out of the dark and high contrast themes.

No state is carried by colour alone. Every status variant carries a mark, every
health lamp carries a shape, and both are checked by name.

Nothing asks for a token that does not exist. A misspelled custom property does
not raise; it resolves to nothing, and the element it was meant to colour draws
whatever it inherited.
"""

from __future__ import annotations

import re
import unittest

from support import REPOSITORY_ROOT

WEB_ROOT = REPOSITORY_ROOT / "web"
TOKENS = WEB_ROOT / "css/tokens.css"
COMPONENTS = WEB_ROOT / "css/components.css"
DASHBOARD = WEB_ROOT / "css/dashboard.css"
SHOWCASE = WEB_ROOT / "design-system.html"
CONSOLE = WEB_ROOT / "index.html"

#: The colour keywords a stylesheet is most likely to reach for by accident.
#: They are searched for in declaration values only, because several of them are
#: also parts of property names — ``white-space`` being the obvious one.
NAMED_COLOURS = (
    "white", "black", "red", "green", "blue", "gray", "grey", "silver",
    "navy", "teal", "orange", "yellow", "purple", "pink", "lime", "aqua",
    "fuchsia", "maroon", "olive", "cyan", "magenta", "gold", "beige",
    "currentcolor", "transparent",
)

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_DECLARATION = re.compile(r"([-a-zA-Z]+)\s*:\s*([^;{}]+)")
_DEFINITION = re.compile(r"(--[a-z0-9-]+)\s*:")
_USAGE = re.compile(r"var\(\s*(--[a-z0-9-]+)")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_REFERENCE = re.compile(r"var\(\s*--[a-z0-9-]+\s*\)")


def _without_comments(text: str) -> str:
    return _COMMENT.sub(" ", text)


def _rules(text: str) -> list[tuple[str, str]]:
    """Every selector and its body, comments already removed."""
    return [
        (selector.strip(), body)
        for selector, body in _RULE.findall(_without_comments(text))
    ]


def _definitions(text: str) -> set[str]:
    """Every custom property a stylesheet declares.

    Read out of rule bodies rather than the whole file, because a variant class
    such as ``.mx-button--primary:hover`` looks exactly like a declaration of a
    property named ``--primary`` to anything that only searches for a name
    followed by a colon.
    """
    found: set[str] = set()
    for _, body in _rules(text):
        found |= set(_DEFINITION.findall(body))
    return found


def _rules_matching(text: str, fragment: str) -> list[tuple[str, str]]:
    return [
        (selector, body)
        for selector, body in _rules(text)
        if fragment in selector
    ]


class AirGapTests(unittest.TestCase):
    """Nothing the console draws itself with comes from anywhere else."""

    def test_no_stylesheet_names_a_remote_address(self) -> None:
        for path in (TOKENS, COMPONENTS, DASHBOARD):
            with self.subTest(stylesheet=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("http://", text)
                self.assertNotIn("https://", text)
                self.assertNotIn("@import", text)

    def test_no_page_names_a_remote_address(self) -> None:
        for path in (SHOWCASE, CONSOLE):
            with self.subTest(page=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("http://", text)
                self.assertNotIn("https://", text)

    def test_the_showcase_references_only_files_beside_it(self) -> None:
        """A reference with a scheme, or without a host of its own, is remote.

        The showcase is the page most likely to acquire one, because it is the
        page a designer opens, and every design tool in existence offers to
        paste a font in.
        """
        text = SHOWCASE.read_text(encoding="utf-8")
        references = re.findall(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", text)

        self.assertTrue(references, "the showcase referenced nothing at all")
        for reference in references:
            with self.subTest(reference=reference):
                self.assertFalse(
                    reference.startswith(("http:", "https:", "//", "data:")),
                    "the showcase reached outside the appliance",
                )
                if reference.startswith("#"):
                    continue
                self.assertTrue(
                    (SHOWCASE.parent / reference).is_file(),
                    "the showcase referenced a file that is not there",
                )

    def test_the_showcase_opens_without_a_server(self) -> None:
        """Its own references are relative, so a file on a laptop works.

        The people who review this work are frequently nowhere near an
        appliance, and an absolute path would need one.
        """
        text = SHOWCASE.read_text(encoding="utf-8")
        for reference in re.findall(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", text):
            with self.subTest(reference=reference):
                self.assertFalse(
                    reference.startswith("/"),
                    "an absolute path needs a server to resolve it",
                )


class TokenReferenceTests(unittest.TestCase):
    """The component layer names tokens and never values."""

    def _literal_colours(self, text: str) -> list[str]:
        offences: list[str] = []
        for name, value in _DECLARATION.findall(_without_comments(text)):
            # A reference to a token is not a value, and the token named
            # ``--mx-color-transparent`` would otherwise be read as the keyword
            # it stands for.
            value = _REFERENCE.sub(" ", value)
            if re.search(r"#[0-9a-fA-F]{3,8}\b", value):
                offences.append(f"{name}: {value.strip()}")
                continue
            if re.search(r"\b(?:rgba?|hsla?)\(", value):
                offences.append(f"{name}: {value.strip()}")
                continue
            for colour in NAMED_COLOURS:
                if re.search(r"\b" + colour + r"\b", value, re.IGNORECASE):
                    offences.append(f"{name}: {value.strip()}")
                    break
        return offences

    def test_the_component_layer_writes_no_colour_of_its_own(self) -> None:
        offences = self._literal_colours(COMPONENTS.read_text(encoding="utf-8"))
        self.assertEqual(
            offences, [],
            "a component wrote a colour instead of naming a token, and has "
            "quietly opted out of the dark and high contrast themes: "
            + ", ".join(offences),
        )

    def test_the_console_stylesheet_writes_no_colour_of_its_own(self) -> None:
        """The console was migrated onto the tokens and must stay there.

        This is the file a hurried change lands in, because it is the file that
        already existed.
        """
        offences = self._literal_colours(DASHBOARD.read_text(encoding="utf-8"))
        self.assertEqual(
            offences, [],
            "the console stylesheet wrote a colour instead of naming a token: "
            + ", ".join(offences),
        )

    def test_nothing_asks_for_a_token_that_does_not_exist(self) -> None:
        """A misspelled custom property is silent, which is why it is checked.

        The browser does not complain. The declaration is simply dropped and
        the element keeps whatever it inherited, which on a status badge means
        a badge that looks almost right.
        """
        sources = {
            "components.css": COMPONENTS.read_text(encoding="utf-8"),
            "dashboard.css": DASHBOARD.read_text(encoding="utf-8"),
            "design-system.html": SHOWCASE.read_text(encoding="utf-8"),
        }
        defined = _definitions(TOKENS.read_text(encoding="utf-8"))

        for name, text in sources.items():
            stripped = _without_comments(text)
            # A component may declare a value of its own — the mark a status
            # variant sets, say — so those count as defined for this file.
            local = _definitions(text)
            dangling = sorted(set(_USAGE.findall(stripped)) - defined - local)
            with self.subTest(source=name):
                self.assertEqual(
                    dangling, [],
                    f"{name} asked for tokens nothing defines: "
                    + ", ".join(dangling),
                )

    def test_the_component_layer_defines_only_the_values_it_varies(self) -> None:
        """Anything a component declares for itself is a local, not a token.

        A component that quietly defines a global token would be a second
        source of truth for it, and the two would drift.
        """
        local = _definitions(COMPONENTS.read_text(encoding="utf-8"))
        permitted = {
            "--mx-button-ink", "--mx-button-surface", "--mx-button-border",
            "--mx-button-height", "--mx-status-mark", "--mx-validation-mark",
        }
        self.assertEqual(
            local - permitted, set(),
            "the component layer defined a value that belongs in the token file",
        )


class NoColourAloneTests(unittest.TestCase):
    """Every state says itself twice, and only one of the two is a colour."""

    def test_every_status_variant_carries_a_mark(self) -> None:
        text = COMPONENTS.read_text(encoding="utf-8")
        variants = _rules_matching(text, ".mx-status--")

        self.assertGreaterEqual(
            len(variants), 20,
            "the status variants are fewer than the conditions the appliance "
            "reports, so something is being drawn without one",
        )
        for selector, body in variants:
            with self.subTest(variant=selector):
                self.assertIn(
                    "--mx-status-mark", body,
                    "this status variant is a colour and nothing else",
                )

    def test_the_badge_actually_draws_the_mark(self) -> None:
        """A mark every variant sets and nothing reads would be worse than none."""
        text = _without_comments(COMPONENTS.read_text(encoding="utf-8"))
        self.assertRegex(
            text,
            r"\.mx-status::before\s*\{[^}]*content:\s*var\(--mx-status-mark\)",
            "the status badge does not draw the mark its variants set",
        )

    def test_every_validation_message_carries_a_mark(self) -> None:
        text = COMPONENTS.read_text(encoding="utf-8")
        variants = _rules_matching(text, ".mx-validation--")

        self.assertTrue(variants, "there are no validation variants at all")
        for selector, body in variants:
            with self.subTest(variant=selector):
                self.assertIn("--mx-validation-mark", body)

    def test_every_health_lamp_carries_a_shape(self) -> None:
        """The lamp has no room for a word inside it, so it changes shape.

        A circle, a diamond, a square and a dashed ring are told apart across a
        room, which is where this indicator is actually read.
        """
        text = COMPONENTS.read_text(encoding="utf-8")
        variants = _rules_matching(text, ".mx-health--")

        self.assertGreaterEqual(len(variants), 5)
        for selector, body in variants:
            with self.subTest(variant=selector):
                self.assertTrue(
                    "border-radius" in body or "border-style" in body
                    or "transform" in body,
                    "this health lamp differs from the others only in colour",
                )

    def test_every_console_state_pill_carries_a_mark(self) -> None:
        text = DASHBOARD.read_text(encoding="utf-8")
        variants = [
            (selector, body)
            for selector, body in _rules(text)
            if ".state-pill." in selector
        ]

        self.assertTrue(variants, "the console draws no state pills at all")
        for selector, body in variants:
            with self.subTest(variant=selector):
                self.assertIn(
                    "--state-pill-mark", body,
                    "a trunk or channel state is being drawn in colour alone",
                )

    def test_the_console_link_lamp_changes_shape(self) -> None:
        """Live, reconnecting and stale are the three the operator watches."""
        text = DASHBOARD.read_text(encoding="utf-8")
        for state in ("live", "reconnecting", "stale"):
            matches = _rules_matching(text, f".lamp.{state}")
            with self.subTest(state=state):
                self.assertTrue(matches, f"the {state} lamp is not drawn")
                body = matches[0][1]
                self.assertTrue(
                    "border-radius" in body or "transform" in body,
                    f"the {state} lamp differs from the others only in colour",
                )

    def test_every_alarm_severity_is_prefixed_as_well_as_coloured(self) -> None:
        text = _without_comments(DASHBOARD.read_text(encoding="utf-8"))
        for severity in ("critical", "warning"):
            with self.subTest(severity=severity):
                self.assertRegex(
                    text,
                    r"\.alarm-list li\." + severity + r"::before\s*\{[^}]*content:",
                    "an alarm severity is being carried by its colour alone",
                )


class ThemeTests(unittest.TestCase):
    """Three themes, two signals, and one rule about what may not be lost."""

    def test_the_operating_system_preference_is_honoured(self) -> None:
        text = TOKENS.read_text(encoding="utf-8")
        self.assertIn("@media (prefers-color-scheme: dark)", text)
        self.assertIn("@media (prefers-contrast: more)", text)

    def test_an_explicit_choice_wins_in_both_directions(self) -> None:
        """The dark preference is scoped so that an explicit light theme beats it.

        A switch that can only turn the console dark is not a switch.
        """
        text = _without_comments(TOKENS.read_text(encoding="utf-8"))
        self.assertIn(':root:not([data-theme])', text)
        for theme in ("light", "dark", "high-contrast"):
            with self.subTest(theme=theme):
                self.assertIn(f'[data-theme="{theme}"]', text)

        light = [
            body for selector, body in _rules(text)
            if selector.strip() == '[data-theme="light"]'
        ]
        self.assertTrue(light)
        self.assertIn(
            "--mx-background-canvas", light[0],
            "the explicit light theme restates nothing, so it cannot re-light "
            "a page whose surroundings are dark",
        )

    def test_the_high_contrast_theme_keeps_its_borders_and_its_focus_ring(self) -> None:
        text = _without_comments(TOKENS.read_text(encoding="utf-8"))
        blocks = [
            body for selector, body in _rules(text)
            if selector.strip() == '[data-theme="high-contrast"]'
        ]
        self.assertTrue(blocks, "there is no high contrast theme")

        body = blocks[0]
        self.assertIn("--mx-border-default", body)
        self.assertIn("--mx-border-strong", body)
        self.assertIn("--mx-border-focus", body)
        self.assertIn("--mx-focus-ring-width", body)
        self.assertIn(
            "--mx-elevation-raised: none", body,
            "the high contrast theme kept its shadows, which are the soft "
            "edges it exists to remove",
        )

    def test_reduced_motion_is_honoured_in_both_layers(self) -> None:
        self.assertIn(
            "@media (prefers-reduced-motion: reduce)",
            TOKENS.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "@media (prefers-reduced-motion: reduce)",
            COMPONENTS.read_text(encoding="utf-8"),
        )

    def test_the_dark_theme_is_not_an_inversion(self) -> None:
        """Its canvas is a dark slate rather than black, and it is stated.

        A theme built by inverting the light one smears every edge on the panel
        of a rack mounted monitor, which is where this console is read at two
        in the morning.
        """
        text = _without_comments(TOKENS.read_text(encoding="utf-8"))
        blocks = [
            body for selector, body in _rules(text)
            if selector.strip() == '[data-theme="dark"]' and "--mx-background-canvas" in body
        ]
        self.assertTrue(blocks, "there is no dark theme")

        canvas = re.search(r"--mx-background-canvas:\s*(#[0-9a-fA-F]{6})", blocks[0])
        self.assertIsNotNone(canvas)
        self.assertNotIn(
            canvas.group(1).lower(), ("#000000", "#ffffff"),
            "the dark canvas is pure black, which is an inversion rather than "
            "a theme",
        )


class TokenCompletenessTests(unittest.TestCase):
    """The whole vocabulary exists, so that no rule has to invent a value."""

    def setUp(self) -> None:
        self.defined = _definitions(TOKENS.read_text(encoding="utf-8"))

    def _require(self, names: list[str]) -> None:
        missing = sorted(name for name in names if name not in self.defined)
        self.assertEqual(missing, [], "these tokens are not defined: " + ", ".join(missing))

    def test_the_background_text_and_border_ramps_are_complete(self) -> None:
        self._require(
            ["--mx-background-" + name for name in (
                "canvas", "surface", "surface-elevated", "surface-subtle",
                "hover", "pressed", "selected", "disabled", "inverse",
            )]
            + ["--mx-text-" + name for name in (
                "primary", "secondary", "tertiary", "disabled", "inverse",
                "link", "link-hover",
            )]
            + ["--mx-border-" + name for name in (
                "default", "subtle", "strong", "focus", "selected", "disabled",
            )]
        )

    def test_every_status_condition_has_its_four_values_and_its_mark(self) -> None:
        conditions = (
            "success", "information", "warning", "critical",
            "offline", "unknown", "maintenance", "in-progress",
        )
        self._require([
            f"--mx-status-{condition}-{role}"
            for condition in conditions
            for role in ("text", "background", "border", "icon", "mark")
        ])

    def test_the_telephony_vocabulary_is_complete(self) -> None:
        conditions = (
            "inbound", "outbound", "internal", "external", "secure",
            "encrypted", "recorded", "emergency", "queued", "answered",
            "missed", "failed",
        )
        self._require([
            f"--mx-telephony-{condition}-{role}"
            for condition in conditions
            for role in ("text", "background", "border", "mark")
        ])

    def test_the_type_scale_is_complete(self) -> None:
        self._require(
            ["--mx-font-sans", "--mx-font-mono"]
            + [f"--mx-type-{step}-size" for step in (
                "display-large", "display", "heading-1", "heading-2",
                "heading-3", "heading-4", "body-large", "body", "body-small",
                "label", "caption", "monospace", "numeric-kpi",
            )]
        )

    def test_the_spacing_scale_is_on_a_four_pixel_baseline(self) -> None:
        steps = (2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80)
        self._require([f"--mx-space-{step}" for step in steps])

        text = _without_comments(TOKENS.read_text(encoding="utf-8"))
        for step in steps:
            with self.subTest(step=step):
                self.assertRegex(text, rf"--mx-space-{step}:\s*{step}px")

    def test_radius_elevation_motion_depth_and_density_are_complete(self) -> None:
        self._require(
            [f"--mx-radius-{name}" for name in
             ("none", "small", "medium", "large", "full")]
            + [f"--mx-elevation-{name}" for name in
               ("flat", "raised", "floating", "overlay", "modal")]
            + [f"--mx-motion-{name}" for name in
               ("instant", "fast", "standard", "slow")]
            + [f"--mx-z-{name}" for name in
               ("base", "sticky", "dropdown", "drawer", "overlay", "modal",
                "toast", "critical-alert")]
            + ["--mx-density-touch-target", "--mx-density-control-height",
               "--mx-focus-ring-width", "--mx-focus-ring-offset"]
        )

    def test_the_comfortable_density_can_be_hit_with_a_finger(self) -> None:
        """Forty-four pixels, because this console is used standing at a rack."""
        text = _without_comments(TOKENS.read_text(encoding="utf-8"))
        self.assertRegex(text, r"--mx-density-touch-target:\s*44px")

        compact = [
            body for selector, body in _rules(text)
            if selector.strip() == '[data-density="compact"]'
        ]
        self.assertTrue(compact, "there is no compact density")
        self.assertIn("--mx-density-touch-target", compact[0])


class ConsoleIntegrationTests(unittest.TestCase):
    """The console loads the layer, and the keyboard can get past the column."""

    def test_the_console_loads_the_tokens_before_anything_that_uses_them(self) -> None:
        text = CONSOLE.read_text(encoding="utf-8")
        order = [
            text.index("/css/tokens.css"),
            text.index("/css/components.css"),
            text.index("/css/dashboard.css"),
        ]
        self.assertEqual(order, sorted(order), "the stylesheets are loaded out of order")

    def test_the_console_offers_a_way_past_the_navigation(self) -> None:
        """Twenty-one sections sit between the keyboard and the page."""
        text = CONSOLE.read_text(encoding="utf-8")
        self.assertIn('class="mx-skip-link"', text)
        self.assertIn('href="#main-content"', text)
        self.assertIn('id="main-content"', text)

    def test_the_console_still_carries_the_selectors_the_script_relies_on(self) -> None:
        """The design work was additive, and this is what that claim means.

        The console is driven by identifiers and class names in
        ``web/js/dashboard.js``. A design change that renames one of them
        removes a section of the interface without removing a line of script.
        """
        text = CONSOLE.read_text(encoding="utf-8")
        for identifier in (
            "sign-in-panel", "sign-in-form", "console", "link-lamp",
            "link-text", "figure-active-calls", "trunk-body", "channel-body",
            "toast-holder", "confirm-shade", "confirm-yes", "confirm-no",
            "hardware-wizard", "log-view", "footer-text",
        ):
            with self.subTest(identifier=identifier):
                self.assertIn(f'id="{identifier}"', text)

        for class_name in ("nav-item", "view", "panel", "tile", "figure", "caption"):
            with self.subTest(class_name=class_name):
                self.assertIn(class_name, text)


if __name__ == "__main__":
    unittest.main()
