"""beta.39 Gate 3: one charge run with two reasons says so.

**What the live evidence showed.** A planned charge campaign with

```
battery_target_kwh: 8.06
safety_buy_kwh: 0.83
economic_buy_kwh: 7.22
```

published ``purchase.classification: mixed`` -- correctly, since beta.32 -- beside
a run-level ``purpose: safety_buy``. So the figures a reader audits said one thing
and the word a *user* reads said another: seven of the campaign's eight
kilowatt-hours were presented as compelled survival energy when they were a
deliberate trade the optimiser chose on price.

The economics were already right. Nothing here changes a quantity, a window, a
schedule, an authority or a command. What changes is one word on three
presentation surfaces.

**What ``mixed_buy`` means, and what it does not.** Physical reachability made a
compulsory component exist; the optimiser then found *additional* charging worth
doing in the same run, independently. It does **not** mean the economic component
became safety energy, and it does not mean a Safety Buy grew: only physical
reachability may initiate a compulsory purchase, and section 4 below pins that
that rule did not move.

The predicate is deliberately in two places. ``economic.purchase_purpose`` owns
the run's purpose; ``activity.category_of`` has owned the Activity category since
beta.32 and **cannot call it**, because ``activity`` imports only ``const`` so the
surface a user reads cannot reach the optimiser. Section 2 holds the two to one
answer over all four quadrants rather than letting a shared import do it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.activity import category_of, direction_of
from custom_components.alpha_ems_manager.const import (
    ACTIVITY_CATEGORY_ECONOMIC_BUY,
    ACTIVITY_CATEGORY_MIXED_BUY,
    ACTIVITY_CATEGORY_SAFETY_BUY,
    ACTIVITY_PURPOSE_ECONOMIC,
    ACTIVITY_PURPOSE_MIXED,
    ACTIVITY_PURPOSE_SAFETY,
    ACTIVITY_PURPOSES,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_MIXED_BUY,
    ECONOMIC_ACTION_OPTIONS,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_DIRECTION_CHARGE,
    EXECUTION_INTENT_GRID_CHARGE,
)
from custom_components.alpha_ems_manager.economic import (
    EconomicRun,
    execution_target,
    purchase_purpose,
)

from .test_beta24_live_charge import LiveSurface


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


# ===========================================================================
# 1. the three cases, on the predicate itself
# ===========================================================================


def test_a_pure_safety_buy_is_still_a_safety_buy() -> None:
    """Case A: compulsory energy, none discretionary. Unchanged from beta.38."""
    assert (
        purchase_purpose(
            ECONOMIC_ACTION_CHARGE,
            safety_buy=True,
            safety_buy_kwh=0.83,
            economic_buy_kwh=0.0,
        )
        == ECONOMIC_ACTION_SAFETY_BUY
    )


def test_a_pure_economic_buy_keeps_the_runs_own_action() -> None:
    """Case B: nothing compulsory. The run is a charge and says so.

    Keyed on ``safety_buy`` being false, which is what "the reachability solve
    compelled nothing in this run" means. The run's own action is returned rather
    than a purchase word, exactly as it was, so a discharge or an export reaching
    this function is unaffected.
    """
    assert (
        purchase_purpose(
            ECONOMIC_ACTION_CHARGE,
            safety_buy=False,
            safety_buy_kwh=0.0,
            economic_buy_kwh=7.22,
        )
        == ECONOMIC_ACTION_CHARGE
    )
    assert (
        purchase_purpose(
            ECONOMIC_ACTION_EXPORT,
            safety_buy=False,
            safety_buy_kwh=None,
            economic_buy_kwh=None,
        )
        == ECONOMIC_ACTION_EXPORT
    )


def test_both_components_present_is_a_mixed_buy() -> None:
    """Case C, and the release.

    *Mutation: collapse the mixed branch back into ``safety_buy`` and this fails.*
    """
    assert (
        purchase_purpose(
            ECONOMIC_ACTION_CHARGE,
            safety_buy=True,
            safety_buy_kwh=0.83,
            economic_buy_kwh=7.22,
        )
        == ECONOMIC_ACTION_MIXED_BUY
    )


def test_an_unattributable_run_falls_back_and_never_invents_mixed() -> None:
    """**``None`` is not zero, and it is not "mixed" either.**

    Where reachability was not computed this refresh, or the record predates the
    attribution, the split is genuinely unknown. "We cannot tell how this run
    splits" must not render as "it is mixed", which would be an invention, nor as
    a discretionary charge, which would hide a compulsion. It falls back to the
    binary beta.38 answer.
    """
    for safety, economic in ((None, None), (0.83, None), (None, 7.22)):
        assert (
            purchase_purpose(
                ECONOMIC_ACTION_CHARGE,
                safety_buy=True,
                safety_buy_kwh=safety,
                economic_buy_kwh=economic,
            )
            == ECONOMIC_ACTION_SAFETY_BUY
        ), (safety, economic)


def test_a_zero_discretionary_component_is_not_mixed() -> None:
    """Strictly greater than zero on both sides.

    A run whose discretionary share rounds to nothing is a Safety Buy, and calling
    it mixed would put a second reason on a purchase that had one.
    """
    assert (
        purchase_purpose(
            ECONOMIC_ACTION_CHARGE,
            safety_buy=True,
            safety_buy_kwh=0.83,
            economic_buy_kwh=0.0,
        )
        == ECONOMIC_ACTION_SAFETY_BUY
    )


# ===========================================================================
# 2. two modules, one answer
# ===========================================================================

#: The four quadrants of ``(compelled, discretionary)``, at the live magnitudes.
QUADRANTS = (
    (0.0, 0.0),
    (0.83, 0.0),
    (0.0, 7.22),
    (0.83, 7.22),
)


@pytest.mark.parametrize(("compelled", "discretionary"), QUADRANTS)
def test_the_purpose_and_the_activity_category_cannot_disagree(
    compelled: float, discretionary: float
) -> None:
    """**One predicate in two isolated modules, held to one answer.**

    ``activity`` imports only ``const`` -- deliberately, so the surface a user
    reads cannot reach the optimiser -- so ``category_of`` cannot call
    ``purchase_purpose``. That isolation is worth keeping and the duplication is
    not, so the agreement is pinned instead: for every possible split, the run's
    purpose and the Activity category must describe the same shape.

    *Mutation: change either branch alone and this fails on the quadrant it moved.*
    """
    purpose = purchase_purpose(
        ECONOMIC_ACTION_CHARGE,
        safety_buy=compelled > 0.0,
        safety_buy_kwh=compelled,
        economic_buy_kwh=discretionary,
    )
    category = category_of(ECONOMIC_ACTION_CHARGE, (compelled, discretionary))

    expected = {
        (False, False): (ECONOMIC_ACTION_CHARGE, ACTIVITY_CATEGORY_ECONOMIC_BUY),
        (True, False): (ECONOMIC_ACTION_SAFETY_BUY, ACTIVITY_CATEGORY_SAFETY_BUY),
        (False, True): (ECONOMIC_ACTION_CHARGE, ACTIVITY_CATEGORY_ECONOMIC_BUY),
        (True, True): (ECONOMIC_ACTION_MIXED_BUY, ACTIVITY_CATEGORY_MIXED_BUY),
    }[(compelled > 0.0, discretionary > 0.0)]

    assert (purpose, category) == expected, (compelled, discretionary)


def test_the_activity_purpose_distinguishes_all_three() -> None:
    """``safety`` / ``economic`` / ``mixed``, and the third is new in beta.39.

    ``ACTIVITY_CATEGORY_MIXED_BUY`` has existed since beta.32 and mapped to
    ``economic``, so a campaign with a real compulsory component reported a purpose
    saying the purchase was entirely a choice.

    *Mutation: drop the mixed branch from ``_purpose_for`` and this fails.*
    """
    from custom_components.alpha_ems_manager.activity import _purpose_for

    assert _purpose_for(ACTIVITY_CATEGORY_SAFETY_BUY) == ACTIVITY_PURPOSE_SAFETY
    assert _purpose_for(ACTIVITY_CATEGORY_ECONOMIC_BUY) == ACTIVITY_PURPOSE_ECONOMIC
    assert _purpose_for(ACTIVITY_CATEGORY_MIXED_BUY) == ACTIVITY_PURPOSE_MIXED
    # An adopted lifecycle's category is genuinely unknown, and guessing would be
    # a claim about why the user's money was spent.
    assert _purpose_for("") is None
    assert ACTIVITY_PURPOSE_MIXED in ACTIVITY_PURPOSES


def test_a_mixed_buy_still_moves_the_battery_one_way() -> None:
    """``direction_of`` must call it a charge.

    The Battery Recommendation entity reads this, and an action label missing here
    is returned *as* a direction -- so ``mixed_buy`` would have been published as
    a battery direction.

    *Mutation: remove ``mixed_buy`` from the charge tuple and this fails.*
    """
    assert direction_of(ECONOMIC_ACTION_MIXED_BUY) == ECONOMIC_DIRECTION_CHARGE
    assert direction_of(ECONOMIC_ACTION_SAFETY_BUY) == ECONOMIC_DIRECTION_CHARGE
    assert direction_of(ECONOMIC_ACTION_CHARGE) == ECONOMIC_DIRECTION_CHARGE


def test_the_new_word_is_publishable_by_the_entities() -> None:
    """In the option set, so the enum sensors may actually state it.

    Without this the value is computed, refused by
    ``action if action in ECONOMIC_ACTION_OPTIONS else None``, and published as
    ``unknown`` -- which is worse than the defect it replaces.
    """
    assert ECONOMIC_ACTION_MIXED_BUY in ECONOMIC_ACTION_OPTIONS


# ===========================================================================
# 3. the live shape, through the real target builder
# ===========================================================================


def _run(*, battery_kwh: float, action: str = ECONOMIC_ACTION_CHARGE) -> EconomicRun:
    """Return a real ``EconomicRun`` at the live campaign's battery target.

    **The production dataclass, not a stand-in.** A hand-written stub with the
    fields ``execution_target`` happens to read is a double that drifts: it grew
    three missing attributes in as many minutes here, and each one it lacked was a
    field a published target carries. The split is a *parameter* of
    ``execution_target`` rather than a property of the run, so a genuine run at
    the live energy plus the live attribution reproduces the shape exactly.
    """
    return EconomicRun(
        action=action,
        start_index=40,
        end_index=47,
        interval_count=8,
        battery_charge_ac_kwh=battery_kwh,
        battery_discharge_ac_kwh=0.0,
        grid_import_kwh=battery_kwh,
        grid_export_kwh=0.0,
        pv_curtailed_kwh=0.0,
        first_power_kw=4.0,
        net_cash_flow_eur=-0.4,
        min_price_eur_kwh=0.09,
        max_price_eur_kwh=0.13,
        average_price_eur_kwh=0.11,
        marginal_grid_import_kwh=battery_kwh,
        marginal_cost_eur=-0.4,
        direction=ECONOMIC_DIRECTION_CHARGE,
    )


def _target(*, safety_kwh, economic_kwh, battery_kwh=8.06, safety_buy=True):
    """Return a published execution target for one known purchase split."""
    start = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    return execution_target(
        _run(battery_kwh=battery_kwh),
        window_start=start,
        window_end=start + timedelta(hours=2),
        reserve_floor_kwh=2.0,
        issued_at=start,
        stale_after=start + timedelta(minutes=20),
        safety_buy=safety_buy,
        safety_buy_kwh=safety_kwh,
        economic_buy_kwh=economic_kwh,
    )


def test_the_live_campaign_now_publishes_a_mixed_purpose() -> None:
    """**The evidence shape, reproduced: 8.06 = 0.83 compelled + 7.22 chosen.**

    *Mutation: collapse the mixed branch and this fails at the live magnitudes.*
    """
    target = _target(safety_kwh=0.83, economic_kwh=7.22)

    assert target["purpose"] == ECONOMIC_ACTION_MIXED_BUY
    assert target["battery_target_kwh"] == pytest.approx(8.06)
    # The numerical source of truth is unchanged and still published beside it.
    assert target["safety_buy_kwh"] == pytest.approx(0.83)
    assert target["economic_buy_kwh"] == pytest.approx(7.22)
    # **The components do not sum exactly, and that is left alone deliberately.**
    # 0.83 + 7.22 is 8.05 against a target of 8.06: the two components as the live
    # download quotes them are two-decimal figures, the battery target is a
    # three-decimal one, and each is rounded independently by the existing
    # ``_round_kwh``. A centimetre of kilowatt-hour is the display rounding that
    # was already there. Changing the energy arithmetic to make rounded figures
    # add up would be adjusting a measurement to flatter a presentation, so the
    # tolerance names the rounding instead.
    residual = abs(
        target["safety_buy_kwh"]
        + target["economic_buy_kwh"]
        - target["battery_target_kwh"]
    )
    assert residual == pytest.approx(0.01, abs=1e-9), residual
    # And the classification did not touch either component or the target.
    assert (target["safety_buy_kwh"], target["economic_buy_kwh"]) == (0.83, 7.22)


def test_the_classification_changes_nothing_a_decision_reads() -> None:
    """**The whole safety argument of the change, field by field.**

    Two targets built from one run and one window, differing *only* in the
    purchase split. Every figure Stage B, the authority, the campaign or the
    frozen schedule reads must be identical -- so the classification cannot have
    fed back into any of them.

    *Mutation: derive ``intent`` from the purpose, or let the purpose reach the
    battery target, and this fails.*
    """
    mixed = _target(safety_kwh=0.83, economic_kwh=7.22)
    pure = _target(safety_kwh=8.06, economic_kwh=0.0)

    assert mixed["purpose"] != pure["purpose"], "the witness: the word did move"
    for field in (
        "intent",
        "plan_id",
        "campaign_id",
        "campaign_end",
        "window_start",
        "window_end",
        "issued_at",
        "stale_after",
        "battery_target_kwh",
        "grid_target_kwh",
        "desired_grid_kw",
        "reserve_floor_kwh",
        "quarter_schedule",
        "required_headroom_kwh",
        "max_end_energy_kwh",
        "headroom_until",
        "average_power_kw",
        "first_power_kw",
    ):
        assert mixed.get(field) == pure.get(field), field
    # And the intent is the executable one either way, which is what Stage B acts
    # on. The purpose is a word beside it.
    assert mixed["intent"] == EXECUTION_INTENT_GRID_CHARGE


def test_a_mixed_target_is_admitted_exactly_as_a_charge_target_is() -> None:
    """A mixed run remains executable, on the production admission path.

    The new word reaches ``purpose``; ``admit`` keys on ``intent``, and an intent
    it does not recognise admits nothing. If the classification had leaked into
    the intent, a mixed campaign would silently stop executing -- which is the
    worst outcome available from an observability change.
    """
    from custom_components.alpha_ems_manager.execution import admit, parse_target

    mixed = parse_target(_target(safety_kwh=0.83, economic_kwh=7.22))
    pure = parse_target(_target(safety_kwh=8.06, economic_kwh=0.0))

    assert mixed is not None and pure is not None
    assert mixed.intent == pure.intent == EXECUTION_INTENT_GRID_CHARGE
    assert mixed.battery_target_kwh == pure.battery_target_kwh
    assert mixed.purpose == ECONOMIC_ACTION_MIXED_BUY
    assert pure.purpose == ECONOMIC_ACTION_SAFETY_BUY

    # And the admission decision is the same on both, which is what keeps a mixed
    # campaign executable: ``admit`` keys on the intent, never on the purpose.
    now = datetime(2026, 9, 3, 10, 1, tzinfo=UTC)
    one = admit(mixed, now)
    two = admit(pure, now)
    assert one.target.intent == two.target.intent
    assert one.target.battery_target_kwh == two.target.battery_target_kwh
    assert one.actionable_at(now) == two.actionable_at(now) is True
    # The one thing that differs is the word.
    assert one.target.purpose != two.target.purpose


# ===========================================================================
# 4. the Safety Buy invariant, which does not move
# ===========================================================================


def test_an_economic_buy_can_never_initiate_a_compulsory_purchase() -> None:
    """A45, restated for beta.39. **Only physical reachability may initiate.**

    A mixed run means reachability compelled *something* and the optimiser
    independently wanted more. It does not mean the economic component became
    safety energy, and a run with no compulsory component may not acquire one by
    being economically attractive -- however attractive.
    """
    from .beta34_shape import solve_at

    # A horizon that buys hard on price alone: cheap now, dear later, plenty of
    # stored energy so nothing is physically compelled.
    #
    # **beta.41 moved the witness from 6.0 kWh to 10.0, and the claim is
    # untouched.** Once holding depletes the pack honestly, the reachability band
    # -- the hard floor plus the uncertainty margin, 5.33 kWh here -- genuinely
    # confines the plan at 6.0 where the bare hard floor does not, so the reserve
    # really does compel charging there and 6.0 no longer isolates "economic
    # only". At 10.0 the band never binds, ``bridge_kwh_now`` is zero, and the
    # plan still buys 12.5 kWh on price alone: a *stronger* witness than the one
    # it replaces, not a weaker assertion.
    outcome = solve_at(head=8, end=96, stored=10.0, allow_export=False).outcome

    assert outcome.safety_buy_ac_kwh == pytest.approx(0.0), (
        "the witness: reachability compels nothing on this horizon"
    )
    assert outcome.safety_buy_runs == frozenset() or not outcome.safety_buy_runs
    charging = [
        run
        for run in outcome.desired.runs
        if run.action == ECONOMIC_ACTION_CHARGE and run.energy_kwh > 0.0
    ]
    assert charging, "the witness: it does buy, and only on price"
    for run in charging:
        assert (
            purchase_purpose(
                run.action,
                safety_buy=run.start_index in outcome.safety_buy_runs,
                safety_buy_kwh=outcome.safety_buy_attribution.get(
                    run.start_index, (None, None)
                )[0],
                economic_buy_kwh=outcome.safety_buy_attribution.get(
                    run.start_index, (None, None)
                )[1],
            )
            == ECONOMIC_ACTION_CHARGE
        ), run.start_index


def test_the_safety_buy_quantity_is_untouched_by_the_classification() -> None:
    """The frozen figures, again, because this release must not have moved them.

    Duplicated from the neutrality suite on purpose: a change whose whole claim is
    "no quantity moves" should fail in the file that introduces it, not only in
    the one that audits it.
    """
    from .beta34_shape import solve_at

    outcome = solve_at(head=68, end=96, stored=0.3).outcome

    # **beta.41: the run grew, the compelled part did not.** ``bridge_kwh_now``
    # is unchanged at 4.5458 and the safety component is unchanged at 4.7917 --
    # identical at 2 c/kWh and at 90 c/kWh, which the test below proves. What grew
    # is the *economic* share, from 0.208 to 1.597, because once the pack is
    # modelled as depleting the optimiser buys more on this horizon than it used
    # to. The quantity that is compulsory is the invariant here; the size of the
    # run that contains it is not.
    # **Phase 2 grew the run and left the compelled part untouched.** The
    # compelled quantity is still 4.7917 and still identical at 2 c/kWh and at
    # 90 c/kWh -- coverage cannot make a purchase compulsory, and does not try to.
    # What grew is the run around it: on this horizon the pack is empty, the
    # household will import 15.3 kWh regardless, and coverage buys part of it at
    # the cheapest quarters available. Three further single-interval runs appear
    # for the same reason, carrying no compelled share at all.
    assert outcome.safety_buy_ac_kwh == pytest.approx(7.777777777777779)
    assert outcome.bridge_kwh_now == pytest.approx(4.545823529332798)
    # **beta.41 re-records the split, not the quantity.** ``safety_buy_ac_kwh``
    # and ``bridge_kwh_now`` above are unchanged, and they are the invariants: how
    # much was bought, and how much was compulsory. What moved is the *reason*
    # attached to it.
    #
    # The old split came from differencing this solve against one whose reserve is
    # relaxed to the hard floor. At 0.3 kWh the pack is below that floor, so the
    # relaxed solve is in violation too, both solves are minimising violation
    # rather than cost, and their difference reflects tie-breaks instead of
    # motives. Below the floor the compelled quantity is the bridge itself --
    # ``max(0, reachability_now - stored)``, converted to the AC boundary runs are
    # measured at -- and anything beyond it is economic even down here, because at
    # a dear price the plan legitimately holds more than the bridge so the inverter
    # can self-consume rather than import.
    #
    # That is what makes the figure price-blind again: 4.7917 at 2 c/kWh and at
    # 90 c/kWh alike, which the test below now proves on two solves that are no
    # longer identical.
    # **Three headings, and the run adds up under them.** The published pair is
    # compelled and *discretionary*, so the coverage share is subtracted out of the
    # second rather than left sitting in it: 4.7917 compelled, 1.3889 coverage and
    # 1.5972 discretionary make the run's 7.7778 exactly. The three single-interval
    # runs are coverage entire, which is why their discretionary share is zero and
    # not 0.2778.
    #
    # 1.5972 is the figure beta.40 published for the discretionary half of this run
    # before any of this, and it returning unchanged is the useful part: the energy
    # Phase 2 added to the run is coverage, none of it was a trade, and none of it
    # was compulsory.
    assert outcome.safety_buy_attribution == {
        68: (pytest.approx(4.791718731292308), pytest.approx(1.5971701575965849)),
        88: (pytest.approx(0.0), pytest.approx(0.0)),
        90: (pytest.approx(0.0), pytest.approx(0.0)),
        92: (pytest.approx(0.0), pytest.approx(0.0)),
    }
    assert outcome.coverage_buy_attribution == {
        68: pytest.approx(1.3888888888888928),
        88: pytest.approx(0.2777777777777785),
        90: pytest.approx(0.2777777777777785),
        92: pytest.approx(0.2777777777777785),
    }
    for run in outcome.desired.runs:
        if run.action != "charge":
            continue
        compelled, discretionary = outcome.safety_buy_attribution[run.start_index]
        covered = outcome.coverage_buy_attribution[run.start_index]
        assert compelled + covered + discretionary == pytest.approx(
            run.battery_charge_ac_kwh, abs=1e-9
        ), run.start_index
    # And it is coverage rather than arbitrage, structurally: nothing is exported.
    assert outcome.desired.planned_grid_export_kwh == pytest.approx(0.0, abs=1e-9)


def test_the_solved_survival_run_is_itself_a_mixed_buy() -> None:
    """**The shape is not hypothetical: the reference survival horizon is mixed.**

    ``safety_buy_attribution`` splits interval 68 as 0.278 compelled and 4.722
    discretionary -- the same shape as the live 0.83/7.22 campaign, at different
    magnitudes. So the branch this release adds is reached by the project's own
    long-standing fixture, and a test that only ever saw hand-built numbers would
    not have shown that.
    """
    from .beta34_shape import solve_at

    outcome = solve_at(head=68, end=96, stored=0.3).outcome
    compelled, discretionary = outcome.safety_buy_attribution[68]

    assert compelled > 0.0 and discretionary > 0.0
    assert (
        purchase_purpose(
            ECONOMIC_ACTION_CHARGE,
            safety_buy=68 in outcome.safety_buy_runs,
            safety_buy_kwh=compelled,
            economic_buy_kwh=discretionary,
        )
        == ECONOMIC_ACTION_MIXED_BUY
    )
    assert category_of(ECONOMIC_ACTION_CHARGE, (compelled, discretionary)) == (
        ACTIVITY_CATEGORY_MIXED_BUY
    )


# ===========================================================================
# 5. the presentation surfaces
# ===========================================================================


def test_the_economic_action_entity_relays_the_mixed_word() -> None:
    """It reads the target's purpose, and must not flatten it.

    *Mutation: drop ``mixed_buy`` from the relayed pair in ``_executing_action``
    and this fails.*
    """
    from custom_components.alpha_ems_manager.sensor import _executing_action

    for purpose, expected in (
        (ECONOMIC_ACTION_MIXED_BUY, ECONOMIC_ACTION_MIXED_BUY),
        (ECONOMIC_ACTION_SAFETY_BUY, ECONOMIC_ACTION_SAFETY_BUY),
        (ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_CHARGE),
    ):
        view = {"purpose": purpose, "intent": EXECUTION_INTENT_GRID_CHARGE}
        assert _executing_action(view) == expected, purpose


def test_the_next_planned_action_entity_does_not_overwrite_the_mixed_word() -> None:
    """**The ordering trap, and it is the one this surface had.**

    The fallback below the purpose knows only whether the run is *in* the
    Safety-Buy set -- which is true of a mixed run too. Consulting it first would
    overwrite a truthful ``mixed_buy`` with ``safety_buy`` and reinstate the whole
    defect on this entity alone, while the Economic Action entity beside it read
    correctly.

    *Mutation: put the ``safety_buy_runs`` fallback back in front and this fails.*
    """
    import types

    from custom_components.alpha_ems_manager import sensor as module

    run = _run(battery_kwh=8.06)
    target = _target(safety_kwh=0.83, economic_kwh=7.22)
    outcome = types.SimpleNamespace(available=True, safety_buy_runs={run.start_index})

    stub = types.SimpleNamespace()
    original_outcome = module._economic_outcome
    original_next = module._next_planned_run
    try:
        module._economic_outcome = lambda coordinator: outcome
        module._next_planned_run = lambda coordinator: (run, target)
        assert module._next_planned_action_value(stub) == ECONOMIC_ACTION_MIXED_BUY
    finally:
        module._economic_outcome = original_outcome
        module._next_planned_run = original_next


async def test_both_enum_entities_can_state_the_new_word(
    hass: HomeAssistant, config_data: dict, source_entities: None, setup_integration
) -> None:
    """The options a user's dashboard sees, on the live entities.

    An enum sensor whose state is outside its declared options is published as
    ``unknown``, so the word being computable is not enough -- the entity has to
    be allowed to say it.
    """
    for entity_id in (
        "sensor.alpha_ems_economic_action",
        "sensor.alpha_ems_next_planned_action",
    ):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert ECONOMIC_ACTION_MIXED_BUY in state.attributes["options"], entity_id


def test_both_languages_label_the_new_word() -> None:
    """A published state with no label renders as a raw slug to the user.

    The label matches Activity's own ``Mixed Buy`` since beta.32, so the entity
    and the Activity line read alike.
    """
    import json
    import pathlib

    for language, expected in (("en", "Mixed Buy"), ("nl", "Gemengde inkoop")):
        path = pathlib.Path(
            f"custom_components/alpha_ems_manager/translations/{language}.json"
        )
        sensors = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
        for key in ("economic_action", "next_planned_action"):
            states = sensors[key]["state"]
            assert states[ECONOMIC_ACTION_MIXED_BUY] == expected, (language, key)


# ===========================================================================
# 6. the change reaches no decision
# ===========================================================================


def test_the_purpose_is_derived_after_attribution_and_feeds_nothing_back() -> None:
    """**Structural, because the argument is about reachability, not values.**

    ``purchase_purpose`` takes an action and two already-computed energies and
    returns a string. It cannot solve, cannot admit, cannot claim and cannot see
    a price -- so there is no path by which the classification could reach
    Stage-A optimisation, trade admission, the reserve, Safety-Buy triggering, a
    charge quantity, a campaign window, Stage-B execution, authority or the frozen
    schedule.
    """
    import inspect

    source = inspect.getsource(purchase_purpose)
    body = source[source.index('"""', source.index('"""') + 3) + 3 :]

    for forbidden in (
        "solve",
        "admit",
        "carry",
        "price",
        "reserve",
        "bridge",
        "reachab",
        "self.",
        "import ",
    ):
        assert forbidden not in body, forbidden
    # Three parameters and no state: everything it can see was handed to it.
    assert set(inspect.signature(purchase_purpose).parameters) == {
        "action",
        "safety_buy",
        "safety_buy_kwh",
        "economic_buy_kwh",
    }


def test_no_schema_version_moved_for_the_classification() -> None:
    """A word on a published target is not persisted state.

    beta.39's only schema movement is the opening valuation's ``STORAGE_MINOR``
    bump, and Mixed Buy must not have added a second.
    """
    from custom_components.alpha_ems_manager.const import (
        CLAIM_SCHEMA_VERSION,
        CONFIG_ENTRY_VERSION,
        FORECAST_STORAGE_MINOR_VERSION,
        FORECAST_STORAGE_VERSION,
        STORAGE_MINOR_VERSION,
        STORAGE_VERSION,
    )

    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (2, 7)
    assert CONFIG_ENTRY_VERSION == 2
    assert CLAIM_SCHEMA_VERSION == 2
    assert (FORECAST_STORAGE_VERSION, FORECAST_STORAGE_MINOR_VERSION) == (1, 8)
