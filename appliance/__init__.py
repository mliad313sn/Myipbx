"""Crossbar — control plane.

An appliance that brings older generation hardware carrying legacy Digium
interface cards up as a modern, browser managed telephony system.

Two constraints govern every module in this package.

Constraint One: the appliance contains no address allocation service of any
kind and never assigns Internet Protocol addresses.  The exclusion is enforced
at build time, install time, run time, and repository time.

Constraint Two: every operator facing surface renders numbers as words rather
than as digit characters.  The logging formatter sanitises every emitted line
unconditionally, so no code path can defeat the rule by forgetting it.

The package imports nothing outside the standard library, because the target
machines are old, frequently air gapped, and often have no working package
index for their vintage.
"""

from __future__ import annotations

__all__ = [
    "PRODUCT_NAME", "PRODUCT_MAKER", "PRODUCT_FULL_NAME",
    "VERSION", "ASSIGNS_ADDRESSES",
]

#: The name, and who made it. Kept apart because they are set on two lines
#: wherever there is room for two -- the boot menu, the login screen, the
#: console masthead -- and joined into one where there is not.
PRODUCT_NAME = "Crossbar"
PRODUCT_MAKER = "by Kernel Project"
PRODUCT_FULL_NAME = f"{PRODUCT_NAME} {PRODUCT_MAKER}"

#: The release, named in words in keeping with Constraint Two.
VERSION = "one point two point zero"

#: Stated as a value so that a test can assert the product claim mechanically.
ASSIGNS_ADDRESSES = False
