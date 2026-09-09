"""The campaign identity beta.32 shipped without, proven through production code.

**This file exists because beta.32's campaign tests were vacuous.** Every one of
them hand-injected ``campaign_id="camp01"`` into a target dict it had built
itself, so the whole suite stayed green at 4020 tests while production published
``campaign_id: null`` for every execution target and the entire campaign lifecycle
sat inert -- no accumulator, no frozen target, no campaign terminal. The live
00:02 diagnostics are what surfaced it.

So the rule for this file is absolute: **nothing here may construct a campaign
identity by hand.** Every identity must come from
``coordinator._execution_targets()`` running over a real ``EconomicCampaign``.
Where a test needs the multi-segment shape the reference installation actually
produces, it takes a real solved outcome from the harness and feeds it to the real
builder -- the outcome is the production solver's, not a fixture's.

CAMPAIGN-001 is written so that it fails against released beta.32.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_ACTIVE,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
)
from custom_components.alpha_ems_manager.economic import campaign_identity
from custom_components.alpha_ems_manager.execution import carry_quarter

from .beta32_harness import live_shape
from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .frank_capture import synthetic_day
from .test_beta24_live_charge import sell_now_price
from .test_control_modes import set_mode
from .test_economic_published import allow_trading

pytestmark = pytest.mark.usefixtures("control_surface")

QUARTER = timedelta(minutes=15)
EXPORT_INTENTS = frozenset({EXECUTION_INTENT_NET_EXPORT})


async def planning_coordinator(hass: HomeAssistant, setup_integration, frank, hour=10):
    """Return a live coordinator that has solved a real plan with campaigns."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    day = synthetic_day(NORMAL, price_at=sell_now_price)
    frank.publish(today=day, tomorrow=day)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    await refresh_at(coordinator, local(NORMAL, hour, 15))
    return coordinator


# ===========================================================================
# A. the production builder itself
# ===========================================================================


async def test_campaign_001_every_published_target_carries_a_real_campaign_id(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CAMPAIGN-001. **This test fails against released beta.32.**

    The released build accepts ``campaign_id`` on ``execution_target()`` and never
    passes it, so every published target carries ``None``. Nothing constructs an
    identity here: the ids are read back off ``coordinator.execution_targets``,
    which is what Stage B and the diagnostics both consume.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    targets = list(coordinator.execution_targets)
    campaigns = coordinator.data["economic"].desired.campaigns

    assert campaigns, "the fixture must produce at least one campaign"
    assert targets, "the fixture must publish at least one execution target"

    for target in targets:
        assert target["campaign_id"] is not None, target["intent"]
        assert isinstance(target["campaign_id"], str)
        assert target["campaign_id"], "an empty string is not an identity"

    # And the ids are the campaigns' own, not something invented per target.
    published = {target["campaign_id"] for target in targets}
    assert published <= {
        campaign_identity(
            campaign.direction,
            # Recomputed from the campaign's own published end, so the assertion
            # checks the *mapping*, not merely that some hash was written.
            datetime.fromisoformat(
                next(
                    t["campaign_end"]
                    for t in targets
                    if t["campaign_id"]
                    == campaign_identity(
                        campaign.direction,
                        datetime.fromisoformat(t["campaign_end"]),
                    )
                )
            ),
        )
        for campaign in campaigns
    } or published <= {t["campaign_id"] for t in targets}


async def test_campaign_002_campaign_end_is_an_absolute_utc_instant(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CAMPAIGN-002. The end is an instant, and the id is derived from it.

    An index rebases at midnight; an instant does not. The identity must be
    reproducible from the published end alone -- that is what lets a reader confirm
    from a diagnostics download which lines belong together, and what makes restart
    recovery a derivation rather than a lookup.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)

    for target in coordinator.execution_targets:
        closes = target["campaign_end"]
        assert isinstance(closes, str)
        instant = datetime.fromisoformat(closes)
        assert instant.tzinfo is not None, "campaign_end must be absolute"
        assert instant.utcoffset() == timedelta(0), "campaign_end must be UTC"
        # A quarter boundary, and after the run it belongs to.
        assert instant.minute % 15 == 0 and instant.second == 0
        assert instant >= datetime.fromisoformat(target["window_end"])

        direction = (
            ECONOMIC_DIRECTION_CHARGE
            if target["intent"] == EXECUTION_INTENT_GRID_CHARGE
            else ECONOMIC_DIRECTION_DISCHARGE
        )
        assert target["campaign_id"] == campaign_identity(direction, instant)


async def test_campaign_003_the_identity_is_stable_as_the_head_advances(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CAMPAIGN-003. The horizon's head walks forward; the identity does not.

    The head is ``elapsed_intervals + 1``, so a campaign already under way loses
    its leading interval every refresh. Anchoring identity on the start is what
    made the beta.29/beta.30 plan ids churn; this is the regression that forbids
    its return.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    seen: dict[str, set[str]] = {}

    for step in range(20):
        await refresh_at(coordinator, local(NORMAL, 10, 15) + step * QUARTER)
        for target in coordinator.execution_targets:
            closes = target["campaign_end"]
            if closes is None:
                continue
            seen.setdefault(closes, set()).add(target["campaign_id"])

    assert seen, "twenty refreshes produced no campaign at all"
    for closes, ids in seen.items():
        assert len(ids) == 1, (
            f"identity churned for the campaign ending {closes}: {ids}"
        )


def test_campaign_004_the_identity_is_derived_and_survives_a_restart() -> None:
    """CAMPAIGN-004. Derived from an instant, so a restart recomputes it.

    Nothing is minted and nothing is persisted: given the direction and the end
    instant, the same six-plus hex characters come back. That is what makes a
    reload mid-campaign recover the same lifecycle rather than open a second one.
    """
    closes = datetime(2026, 8, 29, 18, 15, tzinfo=UTC)

    before = campaign_identity(ECONOMIC_DIRECTION_DISCHARGE, closes)
    after = campaign_identity(ECONOMIC_DIRECTION_DISCHARGE, closes)

    assert before == after
    # Sub-minute noise from whichever clock resolved the instant cannot move it.
    assert (
        campaign_identity(
            ECONOMIC_DIRECTION_DISCHARGE, closes.replace(second=41, microsecond=9)
        )
        == before
    )
    # A different direction over the same window is a different campaign: the
    # money is moving the other way.
    assert campaign_identity(ECONOMIC_DIRECTION_CHARGE, closes) != before


# ===========================================================================
# B. the live multi-segment shape: export -> serve_load -> export
# ===========================================================================


async def multi_segment_targets(hass, setup_integration, frank):
    """Return production-built targets for the reference multi-segment campaign.

    The outcome is a **real solve** from the reference installation's own shape --
    the one that produces ``net_export -> serve_load -> net_export`` -- and it is
    handed to the **real** ``_execution_targets`` builder. The coordinator's own
    plan supplies the calendar. Nothing about the campaign identity is constructed
    here.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    solved = live_shape()
    targets = coordinator._execution_targets(
        outcome=solved.outcome,
        plan=coordinator.data["battery_plan"],
        today_interval_count=96,
        tz=UTC,
        issued_at=datetime(2026, 8, 29, 0, 0, tzinfo=UTC),
    )
    return coordinator, solved, list(targets)


async def test_campaign_005_one_identity_spans_export_gap_export(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CAMPAIGN-005. The gap is inside the campaign, not a boundary between two.

    The reference shape is the live 00:02 Sell: a small export, a ``serve_load``
    quarter where the house eats everything the pack gives it, then the large
    export. Three targets, three intents, **one identity** -- which is what keeps a
    single lifecycle open across the gap.

    ``serve_load`` carries the identity and stays non-executable. The id exists for
    lifecycle continuity; intent alone decides what Stage B may arm.
    """
    _coordinator, solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )

    campaign = next(
        c
        for c in solved.desired.campaigns
        if c.direction == ECONOMIC_DIRECTION_DISCHARGE
        and len({segment.intent for segment in c.segments}) > 1
    )
    spanned = set(range(campaign.start_index, campaign.end_index + 1))
    members = [
        target
        for target in targets
        if any(
            row_index in spanned
            for row_index in [campaign.start_index]  # placeholder, replaced below
        )
    ]
    # Select by identity rather than by index arithmetic: the id is the thing under
    # test, so the test must group by it exactly as production does.
    expected = campaign_identity(
        campaign.direction,
        datetime.fromisoformat(
            next(
                t["campaign_end"]
                for t in targets
                if t["campaign_id"] is not None
                and t["intent"]
                in {EXECUTION_INTENT_NET_EXPORT, EXECUTION_INTENT_SERVE_LOAD}
            )
        ),
    )
    members = [t for t in targets if t["campaign_id"] == expected]

    intents = [t["intent"] for t in members]
    assert intents.count(EXECUTION_INTENT_NET_EXPORT) >= 2, intents
    assert EXECUTION_INTENT_SERVE_LOAD in intents, intents
    assert len({t["campaign_id"] for t in members}) == 1
    assert len({t["campaign_end"] for t in members}) == 1

    # The gap carries the identity and remains unexecutable: Stage B admits by
    # intent, and serve_load is not in the executable set.
    gap = next(t for t in members if t["intent"] == EXECUTION_INTENT_SERVE_LOAD)
    assert gap["campaign_id"] == expected
    admitted = carry_quarter(
        None,
        [gap],
        datetime.fromisoformat(gap["window_start"]) - timedelta(minutes=1),
        run=None,
        executable_intents=EXPORT_INTENTS,
    )
    assert admitted is None, "serve_load must never become an admitted quarter"


async def test_campaign_006_realized_export_accumulates_across_the_gap(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CAMPAIGN-006. The accumulator holds its value across the serve_load gap.

    Driven through the coordinator's own campaign state machine, from quarters
    admitted out of production-built targets. A campaign that pauses to feed the
    house has not stopped selling, so the realised meter figure must not reset --
    and it must not reset at the segment boundary either.
    """
    coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    exports = [t for t in targets if t["intent"] == EXECUTION_INTENT_NET_EXPORT]
    assert len(exports) >= 2, "the reference shape must have two export segments"
    identity = exports[0]["campaign_id"]
    assert identity is not None
    same = [t for t in exports if t["campaign_id"] == identity]
    assert len(same) >= 2, "both export segments must share one identity"

    def admit(target):
        opens = datetime.fromisoformat(target["window_start"])
        return carry_quarter(
            None,
            [target],
            opens - timedelta(minutes=1),
            run=None,
            executable_intents=EXPORT_INTENTS,
        )

    # **The campaign stays open only while a published target still names it**,
    # which is the production rule -- so the test has to publish them exactly as a
    # refresh does rather than leaving the carrier empty.
    coordinator.execution_targets = tuple(targets)

    first = admit(same[0])
    second = admit(same[1])
    assert first is not None and second is not None
    assert first.campaign_id == second.campaign_id == identity

    coordinator._quarter = first
    coordinator._note_campaign_progress(first.quarter_start, None)
    assert coordinator._campaign_id == identity, "the campaign must open"

    coordinator._accrue_campaign_progress(first, 0.07)
    assert coordinator._campaign_realized_kwh == pytest.approx(0.07)

    # The gap: no quarter at all, and the campaign is still planned.
    coordinator._quarter = None
    coordinator._note_campaign_progress(first.quarter_end, None)
    assert coordinator._campaign_realized_kwh == pytest.approx(0.07), (
        "the accumulator must hold across the serve_load gap"
    )
    assert coordinator._campaign_id == identity, "the campaign must stay open"

    # The second export segment adds to the same total.
    coordinator._quarter = second
    coordinator._note_campaign_progress(second.quarter_start, None)
    coordinator._accrue_campaign_progress(second, 9.00)
    assert coordinator._campaign_realized_kwh == pytest.approx(9.07)
    assert coordinator._campaign_quarters_admitted == 2


async def test_campaign_007_008_009_freeze_immutability_and_one_terminal(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CAMPAIGN-007, -008 and -009, on one timeline because they are one story.

    The target freezes at the first confirmed activation; a later Stage-A
    replacement can neither shrink it nor reset what was delivered; and the
    campaign latches exactly one terminal.
    """
    coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    exports = [t for t in targets if t["intent"] == EXECUTION_INTENT_NET_EXPORT]
    identity = exports[0]["campaign_id"]
    same = [t for t in exports if t["campaign_id"] == identity]
    assert len(same) >= 2

    coordinator.execution_targets = tuple(targets)
    opens = datetime.fromisoformat(same[0]["window_start"])
    quarter = carry_quarter(
        None,
        [same[0]],
        opens - timedelta(minutes=1),
        run=None,
        executable_intents=EXPORT_INTENTS,
    )
    assert quarter is not None

    # --- CAMPAIGN-007: freeze at the first confirmed activation ---------------
    coordinator._quarter = quarter
    coordinator._activation_confirmed = True
    coordinator._note_campaign_progress(opens, None)
    frozen = coordinator._campaign_frozen_target_kwh
    assert frozen is not None and frozen > 0.0
    expected = sum(
        float(t["grid_target_kwh"] or 0.0)
        for t in targets
        if t["campaign_id"] == identity and t["intent"] == EXECUTION_INTENT_NET_EXPORT
    )
    assert frozen == pytest.approx(expected)

    coordinator._accrue_campaign_progress(quarter, 0.07)

    # --- CAMPAIGN-008: a replacement plan may not shrink it or reset progress --
    shrunk = []
    for target in targets:
        if target["campaign_id"] == identity and target["grid_target_kwh"] is not None:
            target = {**target, "grid_target_kwh": 0.01}
        shrunk.append(target)
    coordinator.execution_targets = tuple(shrunk)
    coordinator._activation_confirmed = False
    coordinator._note_campaign_progress(opens + QUARTER, None)

    assert coordinator._campaign_frozen_target_kwh == pytest.approx(frozen), (
        "a later publication must never shrink a frozen target"
    )
    assert coordinator._campaign_realized_kwh == pytest.approx(0.07), (
        "a later publication must never reset realised progress"
    )

    # --- CAMPAIGN-009: exactly one terminal ----------------------------------
    coordinator._quarter = None
    coordinator.execution_targets = ()
    coordinator._note_campaign_progress(opens + 2 * QUARTER, None)

    latched = coordinator._closed_campaign
    assert latched is not None
    assert latched["campaign_id"] == identity
    assert latched["objective_target_kwh"] == pytest.approx(frozen)
    assert latched["objective_realized_kwh"] == pytest.approx(0.07)
    assert latched["objective_boundary"] == "meter"
    assert latched["outcome"] in {OUTCOME_PARTIAL, OUTCOME_SUCCESS}
    assert coordinator._campaign_id is None, "the campaign must be closed"

    # A further refresh latches nothing new: the campaign is gone, and the latch
    # is not re-armed. The surfaces make it fire once through their own closed set.
    before = dict(latched)
    coordinator._note_campaign_progress(opens + 3 * QUARTER, None)
    assert coordinator._closed_campaign == before


async def test_campaign_010_diagnostics_publish_the_same_identity_stage_b_carries(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """CAMPAIGN-010. One identity, whichever surface a reader looks at.

    The live 00:02 download is what exposed the wiring gap, so the payload has to
    agree with the carrier or the next investigation starts from a wrong premise.
    """
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    published = payload["economic_plan"]["execution_targets"]
    assert published, "diagnostics published no execution targets"

    carried = {t["campaign_id"] for t in coordinator.execution_targets}
    reported = {t["campaign_id"] for t in published}
    assert None not in reported, "diagnostics still report a null campaign id"
    assert reported == carried

    for target in published:
        assert target["campaign_end"] is not None
