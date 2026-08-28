"""The dead-man is internal, alternating, and the only duration ownership sees.

**Written because a configuration audit got this wrong.** beta.32's audit traced
`control_horizon_minutes` into `device_duration_minutes()`, compared the result
against `DISPATCH_DEADMAN_MINUTES`, and concluded that a configured horizon above
25 minutes would break dispatch ownership. It would not: those two values never
meet in production. Every writer of the Dispatch duration register is
`deadman_minutes()`, which returns 20 or 25, and the cleanup, which returns the
helper's own minimum.

The setting has since been withdrawn (it governed nothing a Live run did), but the
guarantee it was wrongly accused of breaking is worth pinning permanently: **the
duration the inverter is asked to hold is always one Alpha EMS is willing to
command, whatever anything upstream believes.**
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.alphaess_device import (
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_DURATION,
    DISPATCH_MODE_SOC_CONTROL,
    device_duration_minutes,
    plan_dispatch_arm,
    plan_dispatch_cleanup,
    plan_dispatch_rearm,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_HORIZON_MINUTES,
    CONTROL_MIN_DURATION_MINUTES,
    MAX_CONTROL_HORIZON_MINUTES,
    MIN_CONTROL_HORIZON_MINUTES,
)
from custom_components.alpha_ems_manager.dispatch import deadman_minutes


def written_durations(steps) -> list[float]:
    """Return every duration written to the Dispatch register by these steps."""
    return [step.value for step in steps if step.entity_id == DISPATCH_DURATION]


# ===========================================================================
# CONTROL-001 / CONTROL-009 — the register only ever holds a permitted value
# ===========================================================================


def test_control_001_every_arm_and_rearm_writes_a_permitted_duration() -> None:
    """CONTROL-001 and CONTROL-009 together.

    Ownership asks one question of the duration: *is the running dispatch on a
    duration Alpha EMS is willing to command?* It deliberately does **not** ask
    whether the value equals the claim's, because the alternation makes that false
    at every re-arm. So the invariant that keeps ownership provable is this one --
    every duration that reaches the register is in the permitted set.
    """
    arm = plan_dispatch_arm(
        mode=DISPATCH_MODE_SOC_CONTROL,
        power_kw=-1.0,
        cutoff_soc_percent=21,
        duration_minutes=deadman_minutes(None),
        pv_enabled=True,
    )
    assert written_durations(arm) == [float(DISPATCH_DEADMAN_MINUTES[0])]

    # Every reachable re-arm, from every value the register can be holding.
    for previous in (None, *DISPATCH_DEADMAN_MINUTES, CONTROL_MIN_DURATION_MINUTES):
        written = written_durations(plan_dispatch_rearm(deadman_minutes(previous)))
        assert len(written) == 1
        assert int(written[0]) in DISPATCH_DEADMAN_MINUTES, previous


def test_control_009_the_resting_value_is_the_helper_minimum_not_zero() -> None:
    """The cleanup rests the register at the helper's own minimum.

    Not zero: the helper refuses values below its range, and a refused write is
    not a cleared field. This is why a live diagnostics download taken while idle
    shows a duration of 5 -- that reading is correct and is not a stale dead-man.

    Ownership is unaffected, because the duration factor is only consulted while a
    dispatch is active, and a rested register means none is.
    """
    written = written_durations(plan_dispatch_cleanup())
    assert written == [float(CONTROL_MIN_DURATION_MINUTES)]
    assert int(written[0]) not in DISPATCH_DEADMAN_MINUTES


# ===========================================================================
# CONTROL-002 / CONTROL-003 — the alternation, at the write boundary
# ===========================================================================


def test_control_003_arm_writes_low_then_sustains_alternate() -> None:
    """CONTROL-003. Arm 20, first sustain 25, second sustain 20.

    The alternation is a workaround, not a policy: the vendor automation triggers
    on the helper *changing state*, so writing the same duration twice re-arms
    nothing and the run would expire silently mid-charge. Twenty-five is not a
    longer run, not more energy and not a different horizon.
    """
    low, high = DISPATCH_DEADMAN_MINUTES

    register = written_durations(
        plan_dispatch_arm(
            mode=DISPATCH_MODE_SOC_CONTROL,
            power_kw=-1.0,
            cutoff_soc_percent=21,
            duration_minutes=deadman_minutes(None),
            pv_enabled=True,
        )
    )[0]
    assert register == float(low)

    sequence = [register]
    for _sustain in range(4):
        register = written_durations(plan_dispatch_rearm(deadman_minutes(register)))[0]
        sequence.append(register)

    assert sequence == [float(low), float(high), float(low), float(high), float(low)]


def test_control_002_consecutive_writes_never_repeat_a_value() -> None:
    """CONTROL-002. The property the alternation exists for, stated directly.

    Whatever the register currently holds, the next write differs from it -- which
    is the only thing that makes the vendor automation fire.
    """
    register: float | None = None
    for _step in range(12):
        nxt = float(deadman_minutes(register))
        assert nxt != register, "a repeated duration re-arms nothing"
        register = nxt


def test_the_alternation_stays_inside_the_planning_range() -> None:
    """Both values must satisfy the gate that gets to refuse them.

    ``INHIBIT_DURATION_OUT_OF_RANGE`` bounds the commanded duration by the
    *planning* range: a command shorter than one planning interval would lapse
    before the next refresh could renew it. Since beta.33 that gate is fed the
    duration actually about to be written, so the alternation has to sit inside it
    or Live control would refuse itself.
    """
    for minutes in DISPATCH_DEADMAN_MINUTES:
        assert MIN_CONTROL_HORIZON_MINUTES <= minutes <= MAX_CONTROL_HORIZON_MINUTES


# ===========================================================================
# The withdrawn setting: what replaced it, and what it never touched
# ===========================================================================


def test_the_internal_horizon_is_a_constant_and_not_a_user_setting() -> None:
    """CFG-001a. The horizon is internal, and the dead-man is not exposed.

    A setting that cannot change what the battery does is worse than no setting.
    ``control_horizon_minutes`` was shown as "Command duration" while every Live
    command's duration came from the alternation instead.
    """
    from custom_components.alpha_ems_manager import config_flow
    from custom_components.alpha_ems_manager.coordinator import SourceConfig

    assert CONTROL_HORIZON_MINUTES == 20

    source = pathlib_read(config_flow.__file__)
    assert "control_horizon_minutes" not in source
    assert "CONF_CONTROL_HORIZON_MINUTES" not in source
    # And the dead-man is not offered in its place, under any name.
    assert "DISPATCH_DEADMAN_MINUTES" not in source
    assert "deadman" not in source.lower()

    assert "control_horizon_minutes" not in SourceConfig.__dataclass_fields__


def test_the_advisory_command_duration_never_reaches_the_dispatch_register() -> None:
    """The separation the beta.32 audit missed, pinned as a property.

    ``device_duration_minutes`` sizes the *advisory helper-family* command. It is a
    different register from the Dispatch dead-man, and conflating the two is what
    produced a false high-severity finding.
    """
    for horizon in (20, 25, 30, 45, 60):
        advisory = device_duration_minutes(horizon)
        assert advisory == horizon, "the advisory command carries the figure given"

    # The Dispatch register, by contrast, only ever receives the alternation.
    for previous in (None, 20.0, 25.0, 5.0):
        assert int(deadman_minutes(previous)) in DISPATCH_DEADMAN_MINUTES


def pathlib_read(path: str) -> str:
    """Return a module's source text."""
    import pathlib

    return pathlib.Path(path).read_text(encoding="utf-8")


# ===========================================================================
# CONTROL-004..008 — ownership must survive the alternation
# ===========================================================================


def test_control_005_ownership_holds_across_an_alternating_rearm() -> None:
    """CONTROL-005. The re-arm changes the register; ownership must not blink.

    This is the exact reason the duration factor asks "is it permitted" rather
    than "does it equal the claim": comparing against the claim would fail
    ownership every fifteen minutes, which is the class of periodic failure
    beta.30 exists to end.
    """
    from custom_components.alpha_ems_manager.execution import OwnershipEvidence

    for register in DISPATCH_DEADMAN_MINUTES:
        evidence = OwnershipEvidence(
            dispatch_active=True,
            marker_on=True,
            readback_compatible=True,
            readback_duration_minutes=float(register),
            readback_duration_permitted=int(register) in DISPATCH_DEADMAN_MINUTES,
        )
        assert evidence.readback_duration_permitted is True
        assert evidence.readback_factor_failure is None, register


def test_control_007_a_foreign_duration_still_fails_the_factor() -> None:
    """CONTROL-007. Widening the permitted set is exactly what must not happen.

    The factor is not "any plausible duration is ours". A dispatch running on a
    duration Alpha EMS would never command is somebody else's, and stays
    untouchable.
    """
    from custom_components.alpha_ems_manager.const import OWNERSHIP_FACTOR_DURATION
    from custom_components.alpha_ems_manager.execution import OwnershipEvidence

    for foreign in (7.0, 30.0, 45.0, 60.0, 120.0):
        assert int(foreign) not in DISPATCH_DEADMAN_MINUTES
        evidence = OwnershipEvidence(
            dispatch_active=True,
            marker_on=True,
            readback_compatible=True,
            readback_duration_minutes=foreign,
            readback_duration_permitted=False,
        )
        assert evidence.readback_factor_failure == OWNERSHIP_FACTOR_DURATION
        assert evidence.readback_reflects_claim is False


@pytest.mark.parametrize("permitted", [None, True, False])
def test_an_unsupplied_duration_verdict_is_reported_not_judged(permitted) -> None:
    """``None`` means the caller did not say, and then duration is not judged.

    Failing closed on a verdict nobody supplied would make ownership depend on
    which call site built the evidence.
    """
    from custom_components.alpha_ems_manager.const import OWNERSHIP_FACTOR_DURATION
    from custom_components.alpha_ems_manager.execution import OwnershipEvidence

    evidence = OwnershipEvidence(
        dispatch_active=True,
        marker_on=True,
        readback_compatible=True,
        readback_duration_permitted=permitted,
    )
    failed = evidence.readback_factor_failure == OWNERSHIP_FACTOR_DURATION
    assert failed is (permitted is False)
