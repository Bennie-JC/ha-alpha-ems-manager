"""One statement of what this release may physically execute.

**Every test that used to assert ``CONTROL_EXECUTION_AVAILABLE is False`` now asks
here instead**, and that centralisation is the point rather than a convenience.
Through beta.23 "executes nothing" was a single boolean, so a boolean assertion
said everything there was to say. beta.24 executes exactly one action, which makes
the interesting claim *which* one -- and a claim repeated in fifteen files is a
claim that can be relaxed in fourteen of them without anyone noticing.

So the invariant lives once. Widening the barrier fails every caller at the same
moment, which is what a safety invariant should do.
"""

from __future__ import annotations

from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_HOLD,
    CONTROL_EXECUTABLE_ACTIONS,
    CONTROL_EXECUTION_AVAILABLE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_EXPORT,
)


def assert_charge_only_capability() -> None:
    """Assert that this release executes a charge and nothing else.

    Both halves matter. The first says beta.24 is not a zero-actuation release any
    more, so a test that quietly went on believing it would be testing a world that
    no longer exists. The second is the one that protects a battery: discharge,
    export and curtailment are named explicitly rather than left to "everything
    else", because a set that grew a member would otherwise pass silently.
    """
    assert CONTROL_EXECUTION_AVAILABLE is True
    assert frozenset({ACTION_CHARGE}) == CONTROL_EXECUTABLE_ACTIONS
    for action in (
        ACTION_DISCHARGE,
        ACTION_HOLD,
        ECONOMIC_ACTION_EXPORT,
        ECONOMIC_ACTION_CURTAIL,
    ):
        assert action not in CONTROL_EXECUTABLE_ACTIONS, action
