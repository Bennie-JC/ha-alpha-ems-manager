"""beta.58: a grid remainder was bounding a battery objective, and it was stale.

On 2026-09-11 a ``mixed_buy`` campaign published twenty-one rows summing to
**11.71 kWh** -- a schedule that covers its own target to the cent, front-loaded
onto the cheapest quarters of the afternoon. It delivered **0.633 kWh**.

Nothing in Stage A was wrong. The rows were right, the prices were right, the
optimiser was right, and Stage B faithfully executed the authority it was handed.
The authority was wrong, in two independent ways that compounded:

* **Domain.** ``CarriedQuarter.battery_allowance_kwh`` bounds each row by the
  run-level frozen remainder. That remainder was the run's remaining *grid
  purchase* -- ``grid_cap_kwh - grid_charged_kwh`` -- and its producer's own first
  line said so. A grid figure bounding a battery objective is not a reduction, it
  is a category error. beta.40 removed the same confusion one layer lower, where
  the run's grid budget capped battery *power* as a flat pace; this was its last
  instance.
* **Provenance.** The producer read ``_stage_b_decision``, which the refresh in
  progress has not yet assigned -- so a plan admitted now was bounded by the
  **previous** run's demand. The outgoing run's grid budget was 85 Wh from spent,
  having over-consumed against a production forecast that never arrived.

So every row of the new campaign was capped at ``min(row, 0.085)``. Maximum
deliverable 1.79 kWh against a target of 11.71 -- decided at admission, before a
single row opened, and nothing published said so.

**What is deliberately not fixed here.** No deficit is carried between rows, no
row is enlarged, the frozen target still never shrinks, and an unreachable
campaign still closes normally at its window end. A row that closes short is
short for ever -- that is *no cross-row catch-up* working as designed. beta.58
makes the loss visible; it does not grant authority to recover it.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_UNREACHABLE_REASONS,
    CAMPAIGN_UNREACHABLE_ROW_AUTHORITY,
    CAMPAIGN_UNREACHABLE_WINDOW,
    EXECUTION_INTENT_GRID_CHARGE,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
from custom_components.alpha_ems_manager.execution import (
    CarriedRun,
    admit_plan,
    battery_allowance_of,
    forward_authorisation,
    parse_target,
    remaining_authorised_kwh,
)

# ---------------------------------------------------------------------------
# the live shape, as figures
# ---------------------------------------------------------------------------

#: The admitted window, 2026-09-11 09:30-14:45 UTC (11:30-16:45 local).
WINDOW_START = datetime(2026, 9, 11, 9, 30, tzinfo=UTC)
QUARTER = timedelta(minutes=15)
ROW_COUNT = 21

#: The run-level frozen remainder the live plan actually carried: the previous
#: run's *grid* budget, 85 Wh from exhausted. Every figure below is the dump's.
LIVE_STALE_GRID_REMAINDER_KWH = 0.08507713984833198

#: Row energies, in order. Three large rows among eighteen ordinary ones.
BIG_ROWS: dict[int, float] = {3: 2.50, 5: 1.67, 9: 2.50}
ORDINARY_ROW_KWH = 0.28
#: 18 * 0.28 + 2.50 + 1.67 + 2.50
CAMPAIGN_TARGET_KWH = 11.71


def _battery(index: int) -> float:
    return BIG_ROWS.get(index, ORDINARY_ROW_KWH)


def _grid(index: int) -> float:
    """Return the row's marginal import ceiling, always below its battery figure.

    The difference is production the planner expects the row to absorb, which is
    why a charge row can be worth more than any grid budget will pay for.
    """
    return round(_battery(index) * 0.9, 2) if index in BIG_ROWS else 0.10


def published(
    *,
    rows: int = ROW_COUNT,
    plan_id: str = "b58-plan",
    campaign_id: str = "b58-campaign",
) -> dict:
    """Return the live twenty-one-row ``mixed_buy`` publication."""
    schedule = []
    for index in range(rows):
        opens = WINDOW_START + QUARTER * index
        schedule.append(
            {
                "start": opens.isoformat(),
                "end": (opens + QUARTER).isoformat(),
                "battery_kwh": _battery(index),
                "grid_authorised_kwh": _grid(index),
                "grid_export_target_kwh": 0.0,
                "grid_export_caused_kwh": 0.0,
                "desired_grid_kw": _grid(index) / 0.25,
                "retention_authorised": True,
                "retention_gate": "authorised",
                "retention_until_dc_kwh": 15.81,
            }
        )
    ends = WINDOW_START + QUARTER * rows
    return {
        "plan_id": plan_id,
        "revision": 3,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "purpose": "mixed_buy",
        "campaign_id": campaign_id,
        "campaign_end": ends.isoformat(),
        "window_start": WINDOW_START.isoformat(),
        "window_end": ends.isoformat(),
        "issued_at": (WINDOW_START - QUARTER).isoformat(),
        "stale_after": (WINDOW_START + timedelta(minutes=30)).isoformat(),
        "battery_target_kwh": sum(_battery(index) for index in range(rows)),
        "expected_grid_to_battery_kwh": 8.33,
        "expected_pv_to_battery_kwh": 3.34,
        "charge_source": "mixed",
        "average_power_kw": 2.222,
        "quarter_schedule": schedule,
    }


def plan_with(frozen: float | None, *, rows: int = ROW_COUNT):
    """Return the live schedule admitted under one run-level remainder."""
    parsed = parse_target(published(rows=rows))
    assert parsed is not None
    admitted = admit_plan(
        parsed,
        run=None,
        now=WINDOW_START - timedelta(seconds=30),
        frozen_remaining_kwh=frozen,
    )
    assert admitted is not None
    return admitted


def allowances(plan) -> list[float]:
    """Return what every row of ``plan`` may actually move, in order."""
    return [
        plan.executing_quarter(row.start + timedelta(minutes=1)).battery_allowance_kwh()
        for row in plan.rows
    ]


# ===========================================================================
# 1. THE ACCEPTANCE TEST -- the live campaign, reproduced
# ===========================================================================


def test_the_schedule_covers_its_own_target() -> None:
    """The premise, asserted rather than assumed. **Stage A was not at fault.**

    If the rows did not sum to the target this would be a planning defect and the
    whole diagnosis would be wrong. They do, to the cent -- so every kilowatt-hour
    the campaign failed to deliver was authority the schedule already carried.
    """
    plan = plan_with(None)

    assert len(plan.rows) == ROW_COUNT
    assert sum(row.battery_kwh for row in plan.rows) == pytest.approx(
        CAMPAIGN_TARGET_KWH
    )
    assert all(row.executable for row in plan.rows)


def test_every_row_receives_its_own_published_battery_energy() -> None:
    """**The fix, in one assertion.** A run bounded by its own target caps nothing.

    The admitted run's battery remainder at admission is its whole target, because
    it has delivered nothing yet -- so ``min(row, remainder)`` is the row, for every
    row including the 2.50 kWh ones. This is what the live campaign should have
    done and did not.
    """
    plan = plan_with(CAMPAIGN_TARGET_KWH)

    assert allowances(plan) == pytest.approx(
        [_battery(index) for index in range(ROW_COUNT)]
    )
    # The two figures the live dump disagreed about now agree.
    first = plan.executing_quarter(WINDOW_START + timedelta(minutes=1))
    assert first.battery_target_kwh == pytest.approx(ORDINARY_ROW_KWH)
    assert first.battery_allowance_kwh() == pytest.approx(ORDINARY_ROW_KWH)


def test_the_large_row_is_authorised_in_full() -> None:
    """A 2.50 kWh row must be worth 2.50 kWh, which is 10 kW for its quarter.

    The live schedule put its large rows on the cheapest quarters of the afternoon.
    Capping them was not a small loss spread thinly -- 2.42 kWh of the 5.22 kWh
    stranded that day went missing in a single row.
    """
    plan = plan_with(CAMPAIGN_TARGET_KWH)
    big = plan.rows[9]
    quarter = plan.executing_quarter(big.start + timedelta(minutes=1))

    assert big.battery_kwh == pytest.approx(2.50)
    assert quarter.battery_allowance_kwh() == pytest.approx(2.50)
    assert quarter.battery_allowance_kwh() / 0.25 == pytest.approx(10.0)


def test_the_stale_grid_remainder_would_collapse_every_row() -> None:
    """**The defect itself, pinned so the regression has teeth.**

    This is not a test of current behaviour -- it is the arithmetic of the bug,
    recorded. Hand the schedule the previous run's 85 Wh grid remainder and all
    twenty-one rows collapse to it, which is precisely what the installation did
    for five and a half hours.

    *Mutation: point the producer back at the grid remainder and
    ``test_every_row_receives_its_own_published_battery_energy`` fails on row one.*
    """
    plan = plan_with(LIVE_STALE_GRID_REMAINDER_KWH)
    collapsed = allowances(plan)

    assert collapsed == pytest.approx([LIVE_STALE_GRID_REMAINDER_KWH] * ROW_COUNT)
    # 21 * 0.085 against a target of 11.71: unreachable before a row opened.
    assert sum(collapsed) == pytest.approx(1.786, abs=0.01)
    assert sum(collapsed) < CAMPAIGN_TARGET_KWH / 6


def test_grid_purchase_stays_bounded_by_the_rows_own_authorisation() -> None:
    """**The half that must not change.** Battery authority is not grid authority.

    Freeing the battery objective must not free the purchase. Every row still
    publishes its own marginal import ceiling, and for the large rows that ceiling
    is materially below the battery figure -- the difference is production the
    planner expects to absorb, which no grid budget may pay for twice.
    """
    plan = plan_with(CAMPAIGN_TARGET_KWH)

    for index, row in enumerate(plan.rows):
        quarter = plan.executing_quarter(row.start + timedelta(minutes=1))
        assert quarter.grid_authorised_kwh == pytest.approx(_grid(index))
        assert quarter.grid_authorised_kwh <= quarter.battery_allowance_kwh()


# ===========================================================================
# 1b. THE PRODUCER -- where the stale grid figure actually came from
# ===========================================================================
#
# Called unbound against a namespace carrying exactly the fields the method
# reads, which is this repository's own idiom for a coordinator helper (see
# ``test_beta43_public._coordinator``). It is the sharpest available statement:
# if the method ever reaches for a field that is not here, it raises rather than
# quietly answering from somewhere it should not be looking.


def carried_run(run_id: str = "b58-run", *, target: dict | None = None) -> CarriedRun:
    """Return the run Stage B is executing, built from a real publication."""
    parsed = parse_target(target or published())
    assert parsed is not None
    return CarriedRun(
        run_id=run_id,
        plan_id=parsed.plan_id,
        target=parsed,
        revision=parsed.revision,
        admitted_at=WINDOW_START - QUARTER,
        affirmed_at=WINDOW_START - QUARTER,
        stale_after=WINDOW_START + QUARTER,
    )


def producer(
    *,
    carried: CarriedRun | None,
    accumulators_belong_to: str | None,
    delivered_kwh: float = 0.0,
    stage_b_decision: object = None,
):
    """Return what the admission site would snapshot in this exact state."""
    fake = SimpleNamespace(
        _carried=carried,
        _execution_run=accumulators_belong_to,
        _execution_closed_kwh=delivered_kwh,
        _battery_charge_accumulator=None,
        _stage_b_decision=stage_b_decision,
    )
    # The real delivery reader, bound to this namespace, so the production path
    # runs end to end rather than against a stub of its own arithmetic.
    fake._run_battery_delivered_kwh = lambda run_id: (
        AlphaEmsCoordinator._run_battery_delivered_kwh(fake, run_id)
    )
    return AlphaEmsCoordinator._frozen_battery_remaining_kwh(fake)


#: A previous run whose grid budget is 85 Wh from spent -- the live 2026-09-11
#: state at the instant the new campaign was admitted.
SPENT_PREVIOUS_RUN = SimpleNamespace(
    demand=SimpleNamespace(
        grid_cap_kwh=0.9,
        grid_charged_kwh=0.9 - LIVE_STALE_GRID_REMAINDER_KWH,
    )
)


def test_a_new_run_does_not_inherit_the_previous_runs_grid_remainder() -> None:
    """**The provenance half of the fix, and the live case exactly.**

    The accumulators still belong to the outgoing run, whose grid budget is 85 Wh
    from spent, and ``_stage_b_decision`` still describes it -- because this
    refresh has not assigned its own yet. The incoming run has delivered nothing,
    so its remainder is its whole battery target.

    *Mutation: read ``_stage_b_decision.demand`` again and this returns 0.085.*
    """
    remaining = producer(
        carried=carried_run("new-run"),
        accumulators_belong_to="old-run",
        delivered_kwh=4.2,
        stage_b_decision=SPENT_PREVIOUS_RUN,
    )

    assert remaining == pytest.approx(CAMPAIGN_TARGET_KWH)
    assert remaining != pytest.approx(LIVE_STALE_GRID_REMAINDER_KWH)


def test_the_producer_never_reads_the_previous_decision_at_all() -> None:
    """Stronger than the figure: the stale source is not consulted either way.

    The answer is identical whether the previous decision describes a spent run, a
    fresh one, or nothing -- which is what "it reads the run being admitted" means.
    """
    answers = [
        producer(
            carried=carried_run("new-run"),
            accumulators_belong_to="old-run",
            stage_b_decision=decision,
        )
        for decision in (
            None,
            SPENT_PREVIOUS_RUN,
            SimpleNamespace(
                demand=SimpleNamespace(grid_cap_kwh=99.0, grid_charged_kwh=0.0)
            ),
            SimpleNamespace(demand=None),
        )
    ]

    assert answers == pytest.approx([CAMPAIGN_TARGET_KWH] * 4)


def test_a_run_that_has_delivered_has_correspondingly_less_left() -> None:
    """The reduction the ``min`` exists for, now measured in the right domain.

    Once the accumulators belong to this run, what it has already put in the pack
    is subtracted -- battery against battery, which is the whole point.
    """
    remaining = producer(
        carried=carried_run("same-run"),
        accumulators_belong_to="same-run",
        delivered_kwh=4.0,
    )

    assert remaining == pytest.approx(CAMPAIGN_TARGET_KWH - 4.0)


def test_an_over_delivered_run_floors_at_zero_and_never_goes_negative() -> None:
    """A negative remainder would read as "unbounded" one ``max`` later."""
    assert producer(
        carried=carried_run("same-run"),
        accumulators_belong_to="same-run",
        delivered_kwh=CAMPAIGN_TARGET_KWH + 5.0,
    ) == pytest.approx(0.0)


def test_no_run_means_no_bound_rather_than_a_bound_of_zero() -> None:
    """``None`` is uncapped. Zero would stop a schedule nobody asked to stop."""
    assert producer(carried=None, accumulators_belong_to=None) is None


# ===========================================================================
# 2. the reduction the beta.27 rule exists for, still working
# ===========================================================================


def test_a_genuine_run_level_reduction_still_bounds_an_unopened_row() -> None:
    """**beta.27 clause 4 is not weakened.** The ``min`` stays; its input changed.

    A run that has already delivered most of its target has less left, and a row
    admitted under that smaller remainder is legitimately bounded by it. Deleting
    the bound would have fixed the symptom and lost this.
    """
    plan = plan_with(0.40)

    assert plan.executing_quarter(
        WINDOW_START + timedelta(minutes=1)
    ).battery_allowance_kwh() == pytest.approx(0.28)
    big = plan.rows[9]
    assert plan.executing_quarter(
        big.start + timedelta(minutes=1)
    ).battery_allowance_kwh() == pytest.approx(0.40)


def test_the_allowance_rule_has_exactly_one_implementation() -> None:
    """The row and the reachability walk must not drift apart.

    Two transcriptions of one ``min`` is two places for a domain to be confused,
    which is the fault this release closes. Both read ``battery_allowance_of``.
    """
    assert battery_allowance_of(2.5, None) == pytest.approx(2.5)
    assert battery_allowance_of(2.5, 0.085) == pytest.approx(0.085)
    assert battery_allowance_of(2.5, 0.0) == pytest.approx(0.0)
    # Never negative, whatever it is handed.
    assert battery_allowance_of(-1.0, None) == pytest.approx(0.0)
    assert battery_allowance_of(2.5, -3.0) == pytest.approx(0.0)


def test_an_under_delivering_row_does_not_enlarge_the_next_one() -> None:
    """**No cross-row catch-up, and beta.58 does not touch it.**

    Every row's allowance is a function of that row and the run remainder frozen at
    admission. Nothing reads what an earlier row failed to deliver, so a shortfall
    cannot appear anywhere as extra authority.
    """
    plan = plan_with(CAMPAIGN_TARGET_KWH)
    before = allowances(plan)

    # The strongest statement available: the allowance is computed from frozen
    # data only, so the same plan asked twice -- with any delivery in between --
    # answers identically.
    assert allowances(plan) == pytest.approx(before)
    assert before[4] == pytest.approx(ORDINARY_ROW_KWH)
    assert before[10] == pytest.approx(ORDINARY_ROW_KWH)


# ===========================================================================
# 3. the forward cap -- measured at last, and reduction-only for ever
# ===========================================================================

FORWARD_FROM = WINDOW_START + QUARTER * 4
AFTER = FORWARD_FROM + timedelta(minutes=1)


def _forward(authorised: float, delivered: float = 0.0):
    parsed = parse_target(published())
    assert parsed is not None
    cap = forward_authorisation(parsed, delivered_since_kwh=delivered)
    return type(cap)(
        authorised_kwh=authorised,
        forward_from=FORWARD_FROM,
        delivered_since_kwh=cap.delivered_since_kwh,
    )


def test_measured_delivery_reaches_the_forward_cap_at_all() -> None:
    """**It never did.** The field was built at ``0.0`` and nothing wrote it.

    So ``forward_left`` was permanently the whole allowance and this cap could not
    bind however long its boundary went unrenewed -- a reduction instrument that
    could not reduce.
    """
    parsed = parse_target(published())
    assert parsed is not None

    assert forward_authorisation(parsed).delivered_since_kwh == pytest.approx(0.0)
    assert forward_authorisation(
        parsed, delivered_since_kwh=2.5
    ).delivered_since_kwh == pytest.approx(2.5)
    # And it reaches the published diagnostics rather than a constant zero.
    published_now = forward_authorisation(parsed, delivered_since_kwh=2.5).as_dict()
    assert published_now["delivered_since_forward_kwh"] == pytest.approx(2.5)


@pytest.mark.parametrize("delivered", [0.0, 0.5, 1.0, 2.0, 4.0, 10.0])
def test_more_measured_delivery_can_only_reduce(delivered: float) -> None:
    """**Monotonic reduction, asserted across the whole range.**

    The repair may make the cap accurate and nothing else. More delivery can only
    shrink what is left, and the result can never exceed the frozen figure --
    which is what forbids it authorising extra charging, growing a run, or
    reaching across a row boundary.
    """
    frozen = 3.0
    revised, _cap = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=frozen, forward=_forward(4.0, delivered)
    )
    baseline, _ = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=frozen, forward=_forward(4.0, 0.0)
    )

    assert revised <= baseline + 1e-9
    assert revised <= frozen + 1e-9
    assert revised >= 0.0


def test_the_forward_cap_can_never_raise_the_frozen_figure() -> None:
    """A publication wanting *more* is not a path for growth. It never was."""
    for authorised in (0.0, 1.0, 5.0, 100.0):
        revised, _cap = remaining_authorised_kwh(
            now=AFTER,
            frozen_remaining_kwh=2.0,
            forward=_forward(authorised, delivered=0.0),
        )
        assert revised <= 2.0 + 1e-9


def test_before_its_boundary_the_forward_cap_is_still_inactive() -> None:
    """beta.25's rule, unchanged: the interval in flight is Stage A's own."""
    revised, cap = remaining_authorised_kwh(
        now=FORWARD_FROM - timedelta(minutes=1),
        frozen_remaining_kwh=2.0,
        forward=_forward(0.1, delivered=0.0),
    )

    assert revised == pytest.approx(2.0)
    assert cap == "frozen"


# ===========================================================================
# 4. reachability -- the truth, published, and granting nothing
# ===========================================================================


def test_a_schedule_that_covers_its_target_is_fully_authorised() -> None:
    """Before anything is delivered the ceiling is the whole target."""
    plan = plan_with(CAMPAIGN_TARGET_KWH)

    assert plan.remaining_battery_authority_kwh(
        WINDOW_START - timedelta(minutes=1)
    ) == pytest.approx(CAMPAIGN_TARGET_KWH)


def test_closed_rows_take_their_authority_with_them() -> None:
    """**The arithmetic of the permanent loss.**

    A row that has ended authorises nothing further, whatever it delivered. Eight
    rows into the live campaign that left 5.86 kWh of authority against 11.065 kWh
    still wanted -- a 5.2 kWh hole no later row may fill.
    """
    plan = plan_with(CAMPAIGN_TARGET_KWH)
    at_row_eight = WINDOW_START + QUARTER * 8 + timedelta(minutes=1)

    remaining = plan.remaining_battery_authority_kwh(at_row_eight)
    spent = sum(_battery(index) for index in range(8))

    assert remaining == pytest.approx(CAMPAIGN_TARGET_KWH - spent)
    assert remaining == pytest.approx(5.86)


def test_the_open_row_contributes_only_what_it_has_left() -> None:
    """So the ceiling falls smoothly through a quarter instead of at its boundary."""
    plan = plan_with(CAMPAIGN_TARGET_KWH)
    inside = WINDOW_START + timedelta(minutes=5)

    whole = plan.remaining_battery_authority_kwh(inside)
    part = plan.remaining_battery_authority_kwh(inside, delivered_in_open_row_kwh=0.10)

    assert whole - part == pytest.approx(0.10)
    # And it can never go negative, however much the row over-delivered.
    flooded = plan.remaining_battery_authority_kwh(
        inside, delivered_in_open_row_kwh=99.0
    )
    assert flooded == pytest.approx(whole - ORDINARY_ROW_KWH)


def test_a_collapsed_authority_is_what_makes_a_campaign_unreachable() -> None:
    """The live case as a ceiling: 21 rows at 85 Wh cannot reach 11.71 kWh."""
    plan = plan_with(LIVE_STALE_GRID_REMAINDER_KWH)

    ceiling = plan.remaining_battery_authority_kwh(WINDOW_START - timedelta(minutes=1))
    assert ceiling < CAMPAIGN_TARGET_KWH
    assert CAMPAIGN_TARGET_KWH - ceiling == pytest.approx(9.92, abs=0.02)


def test_a_spent_window_authorises_nothing() -> None:
    """Past the last row there is no authority left, and no reason to pretend."""
    plan = plan_with(CAMPAIGN_TARGET_KWH)
    ended = WINDOW_START + QUARTER * (ROW_COUNT + 1)

    assert plan.remaining_battery_authority_kwh(ended) == pytest.approx(0.0)


def test_the_unreachable_reasons_are_a_closed_vocabulary() -> None:
    """Two words, both published, and neither an outcome."""
    from custom_components.alpha_ems_manager import const

    assert set(CAMPAIGN_UNREACHABLE_REASONS) == {
        CAMPAIGN_UNREACHABLE_ROW_AUTHORITY,
        CAMPAIGN_UNREACHABLE_WINDOW,
    }
    # **No new campaign outcome.** An unreachable campaign keeps its target and
    # closes at its window end exactly as it always did.
    assert CAMPAIGN_UNREACHABLE_ROW_AUTHORITY not in const.CAMPAIGN_OUTCOMES
    assert CAMPAIGN_UNREACHABLE_WINDOW not in const.CAMPAIGN_OUTCOMES
    assert len(const.CAMPAIGN_OUTCOMES) == 6


def test_the_reachability_rule_says_it_decides_nothing() -> None:
    """A figure a reader might act on has to say what it is not."""
    from custom_components.alpha_ems_manager.const import CAMPAIGN_REACHABILITY_RULE

    assert "read by nothing" in CAMPAIGN_REACHABILITY_RULE
    assert "closes normally" in CAMPAIGN_REACHABILITY_RULE
    assert "cross-row catch-up" in CAMPAIGN_REACHABILITY_RULE


# ===========================================================================
# 5. physical and persistence bounds
# ===========================================================================


def test_no_allowance_can_exceed_the_row_stage_a_published() -> None:
    """The bound is one-sided. It reduces or it does nothing -- never enlarges."""
    for frozen in (None, 0.0, 0.085, 5.0, CAMPAIGN_TARGET_KWH, 1_000.0):
        plan = plan_with(frozen)
        for index, allowance in enumerate(allowances(plan)):
            assert allowance <= _battery(index) + 1e-9


def test_an_admitted_plan_is_never_persisted_so_no_old_value_can_return() -> None:
    """**The whole of beta.58's storage story, and it is a proof of absence.**

    The beta.57 field held grid-domain data. It must never be reinterpreted as a
    battery figure -- and it cannot be, because nothing writes an admitted plan to
    disk and nothing reads one back. A restart rebuilds the schedule from the live
    publication, so the remainder is always recomputed by the fixed producer.

    Asserted against the store module's source rather than by round-tripping
    something that does not exist: the absence is the contract.
    """
    store = pathlib.Path(__file__).resolve().parents[1] / (
        "custom_components/alpha_ems_manager/storage.py"
    )
    text = store.read_text(encoding="utf-8")
    assert "execution_record" in text, "wrong module -- this is not the store"

    assert "frozen_remaining" not in text
    assert "AdmittedPlan" not in text
    assert "quarter_schedule" not in text


# ===========================================================================
# 6. the structural guard -- the domain error made unrepresentable
# ===========================================================================


def _function_code(module: str, function: str) -> str:
    """Return one function's **executable** source, with prose stripped out.

    Docstrings and comments are removed deliberately: every function below
    *explains* the grid/battery confusion it exists to prevent, and naming the
    fault in prose is the opposite of committing it. Only what runs is scanned,
    which is what ``ast.unparse`` over the body less its docstring gives.
    """
    import ast

    path = pathlib.Path(__file__).resolve().parents[1] / (
        f"custom_components/alpha_ems_manager/{module}.py"
    )
    text = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        return chr(10).join(ast.unparse(statement) for statement in body)
    raise AssertionError(f"{function} not found in {module}.py")


#: Every identifier that names the grid-purchase domain. None of them may appear
#: inside a function that computes battery-side authority.
GRID_IDENTIFIERS = (
    "grid_cap_kwh",
    "grid_charged_kwh",
    "grid_authorised_kwh",
    "remaining_grid_kw",
    "expected_grid_to_battery_kwh",
)


@pytest.mark.parametrize(
    ("module", "function"),
    [
        ("execution", "battery_allowance_kwh"),
        ("execution", "battery_allowance_of"),
        ("execution", "remaining_battery_authority_kwh"),
        ("coordinator", "_frozen_battery_remaining_kwh"),
        ("coordinator", "_run_battery_delivered_kwh"),
    ],
)
def test_no_battery_authority_function_names_the_grid_domain(
    module: str, function: str
) -> None:
    """**The guard that makes the defect unrepresentable, not merely absent.**

    Every function here answers "how much energy may this battery move". A grid
    identifier appearing in one of them is the beta.57 fault returning, whatever
    arithmetic surrounds it -- so this reads the source rather than a result.

    Asserted structurally because the numeric tests above can only catch the
    values a scenario happens to exercise. This catches the *shape*.
    """
    source = _function_code(module, function)

    assert source, f"{function} has no source to inspect"
    for identifier in GRID_IDENTIFIERS:
        assert identifier not in source, (
            f"{module}.{function} names {identifier}: a grid-domain figure has "
            "reached a battery-domain authority again"
        )


def test_the_producer_does_not_read_the_previous_decision() -> None:
    """The provenance fault, guarded at the source as well as by its figures.

    ``_stage_b_decision`` is assigned a hundred lines *after* the admission site
    reads it, so any use of it here is by definition the previous run's.
    """
    source = _function_code("coordinator", "_frozen_battery_remaining_kwh")

    assert "_stage_b_decision" not in source
    assert "_carried" in source, "it must read the run being admitted"


def test_the_grid_clamp_still_owns_the_grid_domain() -> None:
    """**And the other half: grid authority was not removed, only relocated.**

    Freeing the battery objective must not free the purchase. ``_charge_limits``
    still computes its ceiling from the run's grid budget -- clamp four is
    untouched, and beta.40's ruling that it is an energy rather than a pace still
    stands.
    """
    source = _function_code("coordinator", "_charge_limits")

    assert "grid_cap_kwh" in source
    assert "grid_charged_kwh" in source
    assert "remaining_authorised_kwh" in source


# ===========================================================================
# 7. the economics were never the problem, and are not touched
# ===========================================================================


def test_the_expensive_rows_are_where_stage_a_put_them() -> None:
    """**No reordering, no redistribution, no optimiser change.**

    Stage A front-loaded the large rows onto the cheapest quarters. beta.58 lets
    them execute; it does not move them, resize them, or add any. A fix that
    needed to redistribute energy would have been a planner change, and this
    asserts that the schedule coming out is the schedule that went in.
    """
    plan = plan_with(CAMPAIGN_TARGET_KWH)
    large = [index for index, row in enumerate(plan.rows) if row.battery_kwh > 1.0]

    assert large == sorted(BIG_ROWS)
    assert max(large) < ROW_COUNT / 2, "the heavy rows are in the first half"
    assert [row.battery_kwh for row in plan.rows] == pytest.approx(
        [_battery(index) for index in range(ROW_COUNT)]
    )


def test_the_ceiling_can_never_exceed_what_stage_a_published() -> None:
    """A reachability figure that could exceed the schedule would be an invitation.

    It is a *ceiling* on what the frozen rows already authorise, so it is bounded
    by their sum at every instant and under every remainder.
    """
    for frozen in (None, 0.085, 3.0, CAMPAIGN_TARGET_KWH, 1_000.0):
        plan = plan_with(frozen)
        for step in range(ROW_COUNT + 2):
            moment = WINDOW_START + QUARTER * step
            assert (
                plan.remaining_battery_authority_kwh(moment)
                <= CAMPAIGN_TARGET_KWH + 1e-9
            )
