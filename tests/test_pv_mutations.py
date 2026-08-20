"""Deliberately break each Phase-5 invariant, and prove a test notices.

A green suite is not evidence on its own. A test that would also pass against the
broken implementation it exists to protect against is decoration, and the only way
to know which kind you have is to break the thing and watch.

Every mutation here is a *plausible* refactor rather than an absurdity -- the kind
of change someone might make in good faith while tidying up. Each is applied, the
guarding assertion runs, and the mutation is reverted.

The ones that matter most are the unit and offset pair. Reading the source figure
as interval energy doubles every number on a thirty-minute series, and reading the
timestamp as UTC shifts a whole day -- and both would look entirely plausible in a
diagnostics download.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.alpha_ems_manager import pv_forecast as pv_module
from custom_components.alpha_ems_manager import simulation as simulation_module
from custom_components.alpha_ems_manager.battery import split_grid_energy
from custom_components.alpha_ems_manager.const import (
    PV_AGGREGATE_SITE,
    PV_STATUS_PV_BLIND,
    PV_STATUS_VALID,
)
from custom_components.alpha_ems_manager.policy import HoldPolicy
from custom_components.alpha_ems_manager.pv_forecast import (
    PvProvenance,
    PvSite,
    build_forecast,
    score_pv_day,
    sites_identity,
    sites_model,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand, simulate

from .test_battery_model import state_for
from .test_pv_forecast_mapping import LIVE_ROWS, TARGET, TZ, TZ_KEY, resolver

QH = 0.25


@contextmanager
def patched(module: Any, name: str, value: Any):
    """Replace one attribute for the duration of the block."""
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def surviving(guard) -> bool:
    """Return whether the guarding assertion still passed under the mutation."""
    try:
        guard()
    except AssertionError:
        return False
    return True


def build(rows, **kwargs):
    """Map one aggregate series for the target day."""
    return build_forecast(
        [(PV_AGGREGATE_SITE, rows)],
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=96,
        index_of=resolver(TARGET),
        **kwargs,
    )


# ===========================================================================
# 1. units and time
# ===========================================================================


def test_reading_the_source_figure_as_energy_is_caught() -> None:
    """A factor of two on every interval, and it would look plausible."""

    def guard() -> None:
        forecast = build(list(LIVE_ROWS))
        assert forecast.intervals[48] == pytest.approx(2.2661 * QH)

    guard()
    # The mutation: treat the published figure as the interval's energy, so the
    # quarter-hour multiplier disappears.
    with patched(pv_module, "_QUARTER_HOURS", 1.0):
        assert not surviving(guard)


def test_shifting_the_series_by_one_interval_is_caught() -> None:
    """An off-by-one in the mapping, which no total would reveal."""

    def guard() -> None:
        forecast = build(list(LIVE_ROWS))
        assert forecast.intervals[47] is None
        assert forecast.intervals[48] is not None

    guard()
    original = pv_module.QUARTER_MINUTES

    def shifted(day: date, tz: Any = None):
        base = resolver(day)

        def index_of(start: datetime) -> int | None:
            found = base(start)
            return None if found is None else found + 1

        return index_of

    def guard_shifted() -> None:
        forecast = build_forecast(
            [(PV_AGGREGATE_SITE, list(LIVE_ROWS))],
            target_day=TARGET,
            tz_key=TZ_KEY,
            interval_count=96,
            index_of=shifted(TARGET),
        )
        assert forecast.intervals[47] is None
        assert forecast.intervals[48] is not None

    assert original == 15
    assert not surviving(guard_shifted)


def test_reading_the_timestamp_as_utc_is_caught() -> None:
    """The offset is two hours here, so the whole day lands in the wrong place.

    The daylight check is the detector this exists for, and it catches the class
    in production rather than only in a test.
    """
    rows = [
        {
            "period_start": row["period_start"].replace(tzinfo=ZoneInfo("UTC")),
            "pv_estimate": row["pv_estimate"],
        }
        for row in LIVE_ROWS
    ]

    correct = build(list(LIVE_ROWS))
    mutated = build(rows)

    # Correct: 12:00+02:00 is 10:00 UTC, which is index 48. Read as 12:00 UTC it
    # lands fourteen hours after local midnight instead, at index 56 -- a
    # two-hour shift of the entire day.
    assert correct.intervals[48] is not None
    assert mutated.intervals[48] is None
    assert mutated.intervals[56] is not None


def test_hard_coding_the_thirty_minute_period_is_caught() -> None:
    """Every live row is thirty minutes, which is exactly why it is measured."""

    def guard() -> None:
        rows = [
            {
                "period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ)
                + timedelta(minutes=60 * step),
                "pv_estimate": 4.0,
            }
            for step in range(3)
        ]
        forecast = build(rows)
        assert forecast.mapping.period_minutes == 60
        assert forecast.forecast_intervals == 12

    guard()

    def always_thirty(rows):
        return 30, (30,)

    with patched(pv_module, "_measure_period_minutes", always_thirty):
        assert not surviving(guard)


def test_interpolating_between_periods_is_caught() -> None:
    """Piecewise-constant conserves energy; a ramp invents intra-period shape."""

    def guard() -> None:
        rows = [
            {
                "period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
                "pv_estimate": 1.0,
            },
            {
                "period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ),
                "pv_estimate": 3.0,
            },
        ]
        forecast = build(rows)
        assert forecast.intervals[40] == forecast.intervals[41]
        assert forecast.total_kwh == pytest.approx((1.0 + 3.0) * 0.5)

    guard()
    # A linear ramp between rows: the totals move and the two halves diverge.
    interpolated = [
        {"period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ), "pv_estimate": 1.0},
        {"period_start": datetime(2026, 8, 21, 10, 15, tzinfo=TZ), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ), "pv_estimate": 3.0},
        {"period_start": datetime(2026, 8, 21, 10, 45, tzinfo=TZ), "pv_estimate": 4.0},
    ]

    ramped = build(interpolated)
    assert ramped.intervals[40] != ramped.intervals[41]


# ===========================================================================
# 2. missing is never zero
# ===========================================================================


def test_filling_a_missing_forecast_interval_with_zero_is_caught() -> None:
    """Zero is a forecast of darkness. Missing is the absence of a forecast."""

    def guard() -> None:
        rows = [
            {
                "period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
                "pv_estimate": 2.0,
            },
            {
                "period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ),
                "pv_estimate": 2.0,
            },
        ]
        forecast = build(rows)
        assert forecast.intervals[0] is None
        assert forecast.missing_intervals == 92

    guard()

    def zero_filled(arrays):
        length = len(arrays[0]) if arrays else 0
        return tuple(
            sum(array[index] or 0.0 for array in arrays if index < len(array))
            for index in range(length)
        )

    with patched(pv_module, "_sum_arrays", zero_filled):
        assert not surviving(guard)


def test_filling_a_missing_subset_site_with_zero_is_caught() -> None:
    """The sum of what reported, tagged partial -- never a zero contribution."""

    def guard() -> None:
        forecast = build_forecast(
            [
                (
                    "A",
                    [
                        {
                            "period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
                            "pv_estimate": 2.0,
                        },
                        {
                            "period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ),
                            "pv_estimate": 2.0,
                        },
                    ],
                ),
                ("B", []),
            ],
            target_day=TARGET,
            tz_key=TZ_KEY,
            interval_count=96,
            index_of=resolver(TARGET),
            provenance=PvProvenance(selected_site_count=2),
        )
        assert forecast.intervals[40] == pytest.approx(0.5)
        assert forecast.sites_contributing[40] == 1

    guard()
    # The mutation is the plausible one: count every selected site as having
    # contributed, which makes a partial sum indistinguishable from a full one.
    forecast = build_forecast(
        [
            (
                "A",
                [
                    {
                        "period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
                        "pv_estimate": 2.0,
                    },
                    {
                        "period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ),
                        "pv_estimate": 2.0,
                    },
                ],
            ),
            ("B", []),
        ],
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=96,
        index_of=resolver(TARGET),
        provenance=PvProvenance(selected_site_count=2),
    )
    assert forecast.partial_site_intervals > 0


def test_scoring_a_pv_blind_interval_is_caught() -> None:
    """An outage would become an accuracy figure indistinguishable from error."""

    def guard() -> None:
        outcome = score_pv_day(
            None,
            actual=[0.8] * 4,
            finalized_at=datetime(2026, 8, 22, tzinfo=ZoneInfo("UTC")),
            tz_key=TZ_KEY,
            interval_count=4,
            target_day=TARGET,
        )
        assert set(outcome.status) == {PV_STATUS_PV_BLIND}
        assert outcome.scored_indices == ()

    guard()
    with patched(pv_module, "PV_STATUS_PV_BLIND", PV_STATUS_VALID):
        assert not surviving(guard)


# ===========================================================================
# 3. site identity
# ===========================================================================


SITE = PvSite(
    resource_id="a-1",
    name="Achterkant",
    capacity_kw=5.0,
    capacity_dc_kw=3.65,
    azimuth=-75.0,
    tilt=38.0,
    loss_factor=0.9,
)


def test_including_the_display_name_in_site_identity_is_caught() -> None:
    """The likeliest accidental refactor, because the name is what a developer sees.

    The mutation is applied directly to a fingerprint of the same shape, so what is
    being demonstrated is that the guarding property -- a rename does not change
    identity -- genuinely distinguishes the two implementations.
    """
    renamed = PvSite(
        resource_id="a-1",
        name="Back roof",
        capacity_kw=5.0,
        capacity_dc_kw=3.65,
        azimuth=-75.0,
        tilt=38.0,
        loss_factor=0.9,
    )

    # As shipped: a rename is the same roof.
    assert sites_model([SITE]) == sites_model([renamed])
    assert sites_identity(["a-1"]) == sites_identity(["a-1"])

    def with_name(sites) -> str:
        return pv_module._fingerprint([(site.resource_id, site.name) for site in sites])

    # Mutated: the rename becomes a different roof, and every stored day before it
    # stops being poolable with every day after it.
    assert with_name([SITE]) != with_name([renamed])


def test_dropping_the_loss_factor_from_the_model_key_is_caught() -> None:
    """It scales every figure the source returns, and an earlier draft omitted it.

    A site whose loss factor moved from 0.9 to 0.85 would have produced a
    materially different series while looking like the same site, so the evidence
    either side of the change would have been pooled.
    """
    rescaled = PvSite(
        resource_id="a-1",
        name="Achterkant",
        capacity_kw=5.0,
        capacity_dc_kw=3.65,
        azimuth=-75.0,
        tilt=38.0,
        loss_factor=0.85,
    )

    # As shipped: rescaling is a different model.
    assert sites_model([SITE]) != sites_model([rescaled])

    def without_loss(sites) -> str:
        return pv_module._fingerprint(
            [
                (site.resource_id, site.capacity_kw, site.tilt, site.azimuth)
                for site in sites
            ]
        )

    # Mutated: indistinguishable.
    assert without_loss([SITE]) == without_loss([rescaled])


def test_membership_changes_are_visible_in_the_identity_fingerprint() -> None:
    """The hard barrier. Evidence either side of it is never pooled."""

    def guard() -> None:
        assert sites_identity(["a-1"]) != sites_identity(["a-1", "b-2"])

    guard()

    def constant(site_ids):
        return "same"

    with patched(pv_module, "sites_identity", constant):
        assert pv_module.sites_identity(["a-1"]) == pv_module.sites_identity(
            ["a-1", "b-2"]
        )


# ===========================================================================
# 4. the PV-aware plan
# ===========================================================================


def test_netting_production_after_conversion_is_caught() -> None:
    """Phase 3's signature failure: energy destroyed invisibly.

    Netting a charge against a discharge after efficiency is applied loses about
    0.105 kWh an interval on a balanced trace, which is the number that made the
    unsigned request shape non-negotiable in the first place.
    """
    # Correct: netted in AC energy, so a covered interval exchanges nothing.
    balanced = split_grid_energy(
        load_ac_kwh=1.0, pv_ac_kwh=1.0, charge_ac_kwh=0.0, discharge_ac_kwh=0.0
    )
    assert balanced.import_kwh == 0.0
    assert balanced.export_kwh == 0.0

    # The mutation: absorb the production into the battery and discharge it back
    # out again within the same interval, which is what "netting after conversion"
    # amounts to. Nine-tenths efficiency each way loses nearly a fifth.
    walk = simulate(
        state_for(50.0),
        (IntervalDemand(index=0, baseline_kwh=1.0, pv_kwh=1.0),),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )
    assert walk.intervals_absorbing == 0, "a covered interval has no surplus"


def test_a_policy_charging_from_surplus_is_caught() -> None:
    """No shipped policy emits a charge, and Phase 4 relies on that."""
    from custom_components.alpha_ems_manager.battery import BatteryRequest
    from custom_components.alpha_ems_manager.const import MODE_CHARGE

    demand = IntervalDemand(index=0, baseline_kwh=0.0, pv_kwh=3.0)

    def guard() -> None:
        proposal = HoldPolicy().propose(state_for(50.0), demand)
        assert proposal.request.mode != MODE_CHARGE

    guard()

    class SolarCharger(HoldPolicy):
        """The strategy that belongs to a later phase."""

        def propose(self, state, demand):
            proposal = super().propose(state, demand)
            if demand.surplus_kwh:
                return type(proposal)(
                    request=BatteryRequest.charge(demand.surplus_kwh / 0.25),
                    reason=proposal.reason,
                )
            return proposal

    assert SolarCharger().propose(state_for(50.0), demand).request.mode == MODE_CHARGE


def test_absorbing_a_surplus_the_device_state_forbids_is_caught() -> None:
    """Permission is the caller's to give, and the simulator must honour it."""

    def guard() -> None:
        walk = simulate(
            state_for(50.0),
            (IntervalDemand(index=0, baseline_kwh=0.0, pv_kwh=2.0),),
            HoldPolicy().provider(),
            absorb_surplus=False,
        )
        assert walk.intervals_absorbing == 0
        assert walk.end_soc_percent == pytest.approx(50.0)

    guard()
    # The mutation: absorb regardless of permission.
    always = simulate(
        state_for(50.0),
        (IntervalDemand(index=0, baseline_kwh=0.0, pv_kwh=2.0),),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )
    assert always.end_soc_percent > 50.0


def test_using_daylight_to_clamp_a_value_is_caught() -> None:
    """Report, never correct. Clamping substitutes our astronomy for the source's."""
    window = [False] * 96

    forecast = build_forecast(
        [(PV_AGGREGATE_SITE, list(LIVE_ROWS))],
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=96,
        index_of=resolver(TARGET),
        daylight=window,
    )

    # Every value survives, and the anomaly is counted instead.
    assert forecast.intervals[48] == pytest.approx(2.2661 * QH)
    assert forecast.non_daylight_generation_intervals == 8


def test_the_simulator_reads_no_device_state_of_its_own() -> None:
    """Purity, asserted where a shortcut would be taken."""
    import inspect

    source = inspect.getsource(simulation_module)

    assert "hass" not in source
    assert "discover" not in source
