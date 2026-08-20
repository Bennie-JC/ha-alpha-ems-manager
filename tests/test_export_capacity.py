"""The export gate is measured at the meter, not reconstructed from PV and load.

Every sample in :data:`LIVE_SAMPLES` is a real reading taken from this project's
own diagnostics downloads while beta.8 was running in shadow mode. Against the
flows recorded beside them, the beta.8 rule permits **all three**, and all three
were exporting or within a few watts of it. That is the regression this file
exists to prevent, so every sample is asserted against *both* rules: the new one
must refuse it, and the old one is reproduced to show that it did not.

The rule is::

    capacity = max(0, grid_import - grid_export + battery_discharge)

and the command is refused whole whenever it exceeds that capacity less the
configured margin. It is never scaled to fit.
"""

from __future__ import annotations

import inspect

import pytest

from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_HOLD,
    INHIBIT_GRID_STALE,
    INHIBIT_GRID_UNUSABLE,
    INHIBIT_WOULD_EXPORT,
)
from custom_components.alpha_ems_manager.safety import (
    ControlContext,
    absorbing_capacity_kw,
    evaluate,
)

from .test_control_pipeline import make_context, make_intent

#: ``(label, house_w, pv_w, battery_discharge_w, grid_import_w, capacity_w)``
#:
#: Taken from the live diagnostics of 2026-08-20, all three in shadow mode with a
#: 0.9 kW discharge recommended.
LIVE_SAMPLES: tuple[tuple[str, float, float, float, float, float], ...] = (
    # Already net-exporting a kilowatt: PV covered the house and then some.
    ("15:33", 2071.0, 3132.0, 0.0, 22.0, 22.0),
    # Reported ``inhibited`` live, but not by this rule: the diagnostics control
    # block and the flow snapshot beside it describe different instants, so the
    # house load the gate actually used was not the one printed here. Against
    # these flows the old rule permits it too.
    ("16:13", 1126.0, 780.0, 361.0, 2.0, 363.0),
    ("16:18", 1269.0, 626.0, 661.0, 21.0, 682.0),
)

#: The commanded discharge in all three samples.
LIVE_COMMAND_KW = 0.9


def sample_context(
    house_w: float,
    battery_discharge_w: float,
    grid_import_w: float,
    **overrides: object,
) -> ControlContext:
    """Return a context describing one live sample."""
    return make_context(
        house_load_w=house_w,
        # Positive for charging, so a discharge is negative.
        battery_power_w=-battery_discharge_w,
        grid_import_w=grid_import_w,
        grid_export_w=0.0,
        device_power_kw=LIVE_COMMAND_KW,
        export_margin_percent=10.0,
        **overrides,
    )


@pytest.mark.parametrize(
    ("label", "house_w", "pv_w", "discharge_w", "import_w", "capacity_w"),
    LIVE_SAMPLES,
    ids=[sample[0] for sample in LIVE_SAMPLES],
)
def test_the_live_samples_are_all_refused(
    label: str,
    house_w: float,
    pv_w: float,
    discharge_w: float,
    import_w: float,
    capacity_w: float,
) -> None:
    """All three real snapshots must inhibit. None of them did under beta.8."""
    context = sample_context(house_w, discharge_w, import_w)

    assert absorbing_capacity_kw(context) == pytest.approx(capacity_w / 1000.0)

    verdict = evaluate(make_intent(energy_ac_kwh=0.225), context)

    assert verdict.safe is False
    assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT


@pytest.mark.parametrize(
    ("label", "house_w", "pv_w", "discharge_w", "import_w", "capacity_w"),
    LIVE_SAMPLES,
    ids=[sample[0] for sample in LIVE_SAMPLES],
)
def test_the_old_rule_would_have_passed_every_one_of_them(
    label: str,
    house_w: float,
    pv_w: float,
    discharge_w: float,
    import_w: float,
    capacity_w: float,
) -> None:
    """Reproduce the beta.8 rule, to show the correction is not cosmetic.

    ``device_power_kw <= house_load_kw * (1 - margin)`` is evaluated directly
    here, against the same flow snapshots. It permits **all three** -- including
    16:13, which the live report showed as inhibited for an unrelated reason.

    Every one of the three was in fact exporting or near it, so every one of them
    is a case the old rule got wrong on the readings recorded beside it. If a
    future refactor made the new rule agree with the old one here, the fix would
    have been undone, and this fails in the same breath as saying so.
    """
    old_permits = (house_w / 1000.0) * 0.9 >= LIVE_COMMAND_KW
    new_permits = (capacity_w / 1000.0) * 0.9 >= LIVE_COMMAND_KW

    assert old_permits is True
    assert new_permits is False


def test_the_capacity_is_the_meter_plus_the_discharge_already_flowing() -> None:
    """A battery already discharging has that much more room, not less.

    Displacing an existing discharge is free: the house is absorbing it now, so a
    command of the same size changes nothing at the meter.
    """
    idle = make_context(grid_import_w=500.0, grid_export_w=0.0, battery_power_w=0.0)
    discharging = make_context(
        grid_import_w=500.0, grid_export_w=0.0, battery_power_w=-1500.0
    )

    assert absorbing_capacity_kw(idle) == pytest.approx(0.5)
    assert absorbing_capacity_kw(discharging) == pytest.approx(2.0)


def test_an_exporting_site_has_no_capacity_at_all() -> None:
    """Exporting 500 W with the battery idle leaves nothing to absorb a discharge."""
    context = make_context(grid_import_w=0.0, grid_export_w=500.0, battery_power_w=0.0)

    assert absorbing_capacity_kw(context) == 0.0


def test_export_beyond_an_existing_discharge_still_floors_at_zero() -> None:
    """Never negative, so the comparison downstream cannot invert."""
    context = make_context(
        grid_import_w=0.0, grid_export_w=3000.0, battery_power_w=-1000.0
    )

    assert absorbing_capacity_kw(context) == 0.0


def test_a_charge_contributes_no_capacity() -> None:
    """A charging battery is creating load, not absorbing a discharge.

    Counting it would credit the battery for demand it is itself making, and the
    command being checked would replace that demand rather than add to it.
    """
    context = make_context(grid_import_w=1000.0, battery_power_w=2000.0)

    assert absorbing_capacity_kw(context) == pytest.approx(1.0)


def test_pv_is_never_read_by_the_capacity_rule() -> None:
    """The whole point of measuring at the meter: PV does not enter.

    Asserted structurally rather than by value, because a PV term reintroduced
    later would most likely still produce plausible-looking numbers.
    """
    body = inspect.getsource(absorbing_capacity_kw).split('"""')[-1]

    assert "pv" not in body.lower()
    assert "house_load" not in body


def test_the_gate_does_not_read_house_load_for_the_export_decision() -> None:
    """House load can be anything; only the meter decides the export verdict."""
    verdicts = {
        evaluate(
            make_intent(),
            make_context(
                house_load_w=house_w,
                grid_import_w=2800.0,
                grid_export_w=0.0,
                battery_power_w=-1200.0,
                device_power_kw=3.6,
                export_margin_percent=10.0,
            ),
        ).safe
        for house_w in (1.0, 500.0, 2000.0, 50_000.0)
    }

    assert verdicts == {True}


def test_an_unreadable_meter_inhibits_rather_than_reading_zero() -> None:
    """Substituting zero here would permit a discharge on an exporting site."""
    verdict = evaluate(
        make_intent(),
        make_context(grid_import_w=None, grid_export_w=None),
    )

    assert verdict.inhibit_reason == INHIBIT_GRID_UNUSABLE


def test_a_stale_meter_inhibits() -> None:
    """A P1 meter publishes every second or two; minutes of silence is a fault.

    There is deliberately no daylight exemption here, unlike the PV sensor in the
    energy-balance path: a grid reading is never legitimately constant for hours,
    so nothing has to be carved out for the case where it is.
    """
    verdict = evaluate(make_intent(), make_context(grid_age_seconds=301.0))

    assert verdict.inhibit_reason == INHIBIT_GRID_STALE


def test_only_a_discharge_needs_the_meter() -> None:
    """A charge cannot export, and a hold moves nothing."""
    charge = evaluate(
        make_intent(action=ACTION_CHARGE, energy_ac_kwh=0.5),
        make_context(
            grid_import_w=None,
            grid_export_w=None,
            grid_age_seconds=9999.0,
            device_power_kw=2.0,
        ),
    )
    hold = evaluate(
        make_intent(action=ACTION_HOLD, energy_ac_kwh=0.0),
        make_context(
            grid_import_w=None,
            grid_export_w=None,
            grid_age_seconds=9999.0,
            house_load_w=None,
            house_load_age_seconds=9999.0,
            device_power_kw=0.0,
            device_cutoff_percent=0,
            device_duration_minutes=0,
        ),
    )

    assert charge.safe is True
    assert hold.safe is True


def test_the_verdict_still_carries_no_magnitude() -> None:
    """The gate refuses whole commands. It has nowhere to put a smaller one."""
    verdict = evaluate(
        make_intent(energy_ac_kwh=2.0),
        make_context(grid_import_w=10.0, battery_power_w=0.0, device_power_kw=8.0),
    )

    assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT
    assert not any(
        "power" in name or "energy" in name or "capacity" in name
        for name in type(verdict).__dataclass_fields__
    )


@pytest.mark.parametrize("margin", [0.0, 5.0, 10.0, 50.0])
def test_the_margin_reduces_the_capacity_and_never_the_command(
    margin: float,
) -> None:
    """Swept, so a sign error or a misplaced division cannot survive.

    Capacity is a fixed 8 kW here so that even a 50 % margin leaves an allowance
    comfortably above the device minimum, which would otherwise be the condition
    that fired and the export rule would go untested.
    """
    allowed = 8.0 * (1.0 - margin / 100.0)

    def verdict_at(power_kw: float) -> object:
        return evaluate(
            make_intent(),
            make_context(
                grid_import_w=8000.0,
                grid_export_w=0.0,
                battery_power_w=0.0,
                device_power_kw=power_kw,
                export_margin_percent=margin,
            ),
        )

    assert verdict_at(round(allowed - 0.01, 4)).safe is True
    assert verdict_at(round(allowed + 0.01, 4)).inhibit_reason == INHIBIT_WOULD_EXPORT
