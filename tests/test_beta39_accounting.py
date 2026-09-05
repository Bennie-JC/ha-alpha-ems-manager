"""beta.39 Gate 2: a total that is mathematically defensible, or no total.

**The question the sensor could not answer.** "What has today earned me, what is
still coming, and what is the honest total?" -- and the two figures that looked
closest to an answer were the two it would have been most wrong to use.
``decision_advantage_eur`` is a from-now comparison against a counterfactual, not
a realised quantity; ``net_cash_flow_eur`` is import less export, so a *negative*
value means money arrived. Neither is a profit, and adding one to the other would
straddle two counterfactuals over two different time frames.

What beta.39 publishes instead, on one basis::

    realised_today_eur
    + in_progress_interval_eur
    + remaining_expected_today_eur
    + forecast_revaluation_eur
    = total_economic_value_today_eur

and it telescopes. Writing ``R`` for cash realised in the closed part of the day,
``G`` for cash realised so far in the quarter in flight, ``P`` for cash the plan
still expects before midnight, ``V[now]`` for this refresh's value curve and
``V[open]`` for the curve as it stood when the day opened::

    realised_today_eur = R + V[now](e_close) - V[now](e_open)   (beta.38's identity)
    in_progress        = G
    remaining          = P
    revaluation        = V[now](e_open) - V[open](e_open)
    ----------------------------------------------------------
    total              = R + G + P + V[now](e_close) - V[open](e_open)

-- the day's cash, plus what the pack is worth now, less what it was worth when
the day opened. Every intermediate term cancels, so **no residual is hidden inside
any addend**: that is the property this file exists to pin.

Four things had to be proven before any of it could be published, and each has its
own section below:

1. the remaining-today avoidance can be rebuilt on the **same** no-battery
   counterfactual the realised ledger uses -- exactly, from the solved plan, with
   no second solve. This was the declared blocker;
2. the day's three slices are disjoint and exhaustive at 92, 96 **and** 100
   intervals, by construction rather than by which intervals have data;
3. a forecast revaluation term is mathematically *required*, not merely useful --
   it is precisely the residual between the position total and beta.38's identity;
4. it cannot be reconstructed from anything already persisted, which is why the
   release adds one optional field per civil day.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    ACCOUNTING_BASIS_POSITION,
    ACCOUNTING_OPENING_ENERGY_TOLERANCE_KWH,
    ACCOUNTING_RECONCILIATION_TOLERANCE_EUR,
    ACCOUNTING_UNAVAILABLE_HORIZON_SHORT_OF_MIDNIGHT,
    ACCOUNTING_UNAVAILABLE_NO_OPENING_VALUATION,
    ACCOUNTING_UNAVAILABLE_OPENING_ENERGY_MISMATCH,
    ACCOUNTING_UNAVAILABLE_REASONS,
    ACCOUNTING_UNAVAILABLE_VALUATION_REFERENCE_MOVED,
    AVOIDANCE_BASIS_NO_BATTERY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from custom_components.alpha_ems_manager.economic import day_block_for
from custom_components.alpha_ems_manager.realized import (
    RealizedWindow,
    day_accounting,
    day_partition,
    open_quarter_value_eur,
    realized_window,
)

from .beta34_shape import load_29aug, pv_29aug, solve_at
from .test_beta24_live_charge import LiveSurface


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


# ===========================================================================
# 1. the blocker: one counterfactual, rebuilt exactly
# ===========================================================================

#: Horizon shapes chosen so both ambient cases occur. ``no_ambient`` is the
#: control: with the model switched off every interval takes the plain idle
#: baseline, and the identity has to hold on that branch too.
SHAPES = {
    "sell": {"head": 28, "end": 96, "stored": 8.294},
    "buy": {"head": 8, "end": 96, "stored": 1.2, "allow_export": False},
    "mixed": {"head": 36, "end": 96, "stored": 4.0},
    "zero_pv": {"head": 20, "end": 96, "stored": 6.0, "pv_fn": lambda index: 0.0},
    "no_ambient": {"head": 36, "end": 96, "stored": 4.0},
    "survival": {"head": 68, "end": 96, "stored": 0.3},
}


def _solve(name: str):
    """Return the solved outcome for one named shape."""
    kwargs = dict(SHAPES[name])
    if name == "no_ambient":
        kwargs["ambient_self_consumption"] = False
    return solve_at(**kwargs).outcome


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_no_battery_counterfactual_is_exact_not_estimated(shape: str) -> None:
    """**The declared blocker, and it closes with zero error.**

    Realised load avoidance is ``max(0, max(0, load - pv) - import)`` -- measured
    against a household with no battery at all. The plan's ``avoided_import_eur``
    is measured against leaving the battery alone *this* interval, which since
    beta.31 includes the inverter serving residual load from the pack unbidden.
    Adding one to the other is the single dishonest move that was available here.

    So the accounting rebuilds the no-battery residual from the solved plan:
    ``idle_import_kwh + ambient_self_consumption_ac_kwh``. That is an identity in
    both branches, and this asserts it against the fixture's own load and
    production functions -- data the production code never sees.

    *Mutation: drop the ambient term and the ambient intervals fail; use
    ``marginal_grid_import_kwh`` instead and every hold interval fails.*
    """
    pv_fn = SHAPES[shape].get("pv_fn", pv_29aug)
    outcome = _solve(shape)

    for interval in outcome.desired.intervals:
        truth = max(0.0, load_29aug(interval.index) - pv_fn(interval.index))
        assert interval.no_battery_import_kwh == pytest.approx(truth, abs=1e-12), (
            interval.index,
            interval.counterfactual_basis,
        )
        # And the avoidance built on it, against the same independent truth. The
        # residual alone would leave the subtraction untested -- and substituting
        # ``marginal_grid_import_kwh`` for it is the one wrong answer that looks
        # right, because on a plain interval the two agree exactly.
        assert interval.avoided_import_no_battery_kwh == pytest.approx(
            max(0.0, truth - interval.grid_import_kwh), abs=1e-12
        ), (interval.index, interval.counterfactual_basis)


def test_the_witness_that_the_ambient_branch_is_actually_exercised() -> None:
    """An identity proven only on the branch where it is trivial proves nothing.

    On a plain interval ``ambient_self_consumption_ac_kwh`` is zero and the
    reconstruction is a no-op. The second branch -- where ``idle_import_kwh`` was
    *overwritten* with the ambient baseline -- is the one that needed the sum, so
    it has to be reached.
    """
    ambient = sum(
        1
        for shape in SHAPES
        for interval in _solve(shape).desired.intervals
        if interval.ambient_self_consumption_ac_kwh > 0.0
    )
    plain = sum(
        1
        for shape in SHAPES
        for interval in _solve(shape).desired.intervals
        if interval.ambient_self_consumption_ac_kwh == 0.0
    )

    assert ambient > 0, "no ambient interval: the reconstruction is untested"
    assert plain > 0, "no plain interval: the other branch is untested"


def test_the_avoidance_clamps_at_zero_exactly_as_the_measured_one_does() -> None:
    """A grid charge avoids nothing; it does not avoid a negative amount.

    ``avoided_import_eur`` is deliberately unclamped -- a negative marginal is a
    purchase the battery caused and the day block reports it as such. The
    accounting figure must clamp, because the measured side does: the purchase is
    already in ``grid_import_kwh`` and priced there, and letting a negative
    avoidance net against it would report one euro under two names.
    """
    outcome = _solve("buy")
    charging = [
        interval
        for interval in outcome.desired.intervals
        if interval.grid_import_kwh > interval.no_battery_import_kwh + 1e-9
    ]

    assert charging, "the witness: this horizon must actually buy from the grid"
    for interval in charging:
        assert interval.avoided_import_no_battery_kwh == 0.0, interval.index
        assert interval.marginal_grid_import_kwh > 0.0, interval.index

    # **The second witness, and without it this test is satisfied by an avoidance
    # that is always zero.** A clamp is only a clamp if something else gets
    # through it.
    saving = [
        interval
        for interval in outcome.desired.intervals
        if interval.avoided_import_no_battery_kwh > 0.0
    ]
    assert saving, "an avoidance that is never positive is not being clamped"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_remaining_figure_is_the_realised_construction(shape: str) -> None:
    """``export_revenue - import_cost + avoided``, term for term.

    That is exactly how ``RealizedWindow.realized_net_value_eur`` is built on
    measured flows, and it is what makes the two addable. Re-derived here from the
    intervals rather than read back from the block, so the block cannot pass by
    agreeing with itself.
    """
    outcome = _solve(shape)
    block = day_block_for(outcome.desired, today_interval_count=96)
    expected = sum(
        interval.grid_export_kwh * (interval.export_price_eur_kwh or 0.0)
        - interval.grid_import_kwh * (interval.import_price_eur_kwh or 0.0)
        + interval.avoided_import_no_battery_kwh
        * (interval.import_price_eur_kwh or 0.0)
        for interval in outcome.desired.intervals
        if interval.index < 96
    )

    assert block["avoidance_basis"] == AVOIDANCE_BASIS_NO_BATTERY
    assert block["no_battery_value_eur"] == pytest.approx(expected, abs=1e-4)


def test_the_remaining_figure_carries_no_model_term_and_no_inventory() -> None:
    """No hurdle rate, no wear proxy, no terminal credit, no stored value.

    Every one of those is either not cash or not attributable to a civil day, and
    the day block has excluded them since beta.37. This pins that the *new* figure
    inherited the exclusion rather than quietly reintroducing it.
    """
    outcome = _solve("mixed")
    plan = outcome.desired
    block = day_block_for(plan, today_interval_count=96)

    assert block["switching_cost_eur"] is not None, "the witness: a fee exists"
    assert block["switching_cost_eur"] > 0.0
    cash = block["export_revenue_eur"] - block["grid_import_cost_eur"]
    # Three independently rounded euro figures, so the identity is checked at the
    # resolution they are published at rather than tighter than it. It landed
    # exactly on 1e-4 after beta.41 moved the plan; the relationship is exact in
    # the unrounded quantities and this is what survives publication.
    assert block["no_battery_value_eur"] == pytest.approx(
        cash + block["avoided_import_no_battery_eur"], abs=3e-4
    )
    # And it is none of the four figures that would have been tempting.
    for wrong in (
        plan.objective_eur,
        plan.hold_cost_eur - plan.cost_eur,
        block["interval_value_eur"],
        block["avoided_import_eur"],
    ):
        assert block["no_battery_value_eur"] != pytest.approx(wrong, abs=1e-3), wrong


def test_the_two_avoidance_bases_actually_differ() -> None:
    """The reason the recomputation exists, measured.

    If the per-interval idle avoidance and the no-battery avoidance were equal,
    the whole basis argument would be decorative. They are not: wherever the
    inverter would have served the house from the pack by itself, the idle
    baseline already includes that service and the no-battery one does not.
    """
    outcome = _solve("mixed")
    block = day_block_for(outcome.desired, today_interval_count=96)

    assert block["avoided_import_eur"] != pytest.approx(
        block["avoided_import_no_battery_eur"], abs=1e-3
    ), block


# ===========================================================================
# 2. the partition, at every civil-day length
# ===========================================================================


@pytest.mark.parametrize("count", [92, 96, 100])
def test_the_day_partition_is_disjoint_and_exhaustive(count: int) -> None:
    """**The test that makes the identity a proof rather than a definition.**

    Every head position, on a short DST day, an ordinary day and a long DST day.
    The three slices must cover ``range(count)`` exactly once each -- and they are
    defined by the plan's head index, not by which intervals carry data, which is
    what makes that true when a sample is missing.

    *Mutation: shift the closed slice by one interval, or make the quarter in
    flight ``head`` instead of ``head - 1``, and this fails at every head.*
    """
    for head in range(-2, count + 3):
        closed, in_progress, remaining = day_partition(head=head, interval_count=count)
        covered = list(closed) + ([] if in_progress is None else [in_progress])
        covered += list(remaining)

        assert sorted(covered) == list(range(count)), (head, covered)
        assert len(covered) == len(set(covered)), (head, "overlap")


def test_the_quarter_in_flight_is_the_one_the_plan_does_not_plan() -> None:
    """Stage A's head is ``elapsed + 1``, so ``head - 1`` is the open quarter.

    This is the arithmetic behind the gap the live captures showed: at 21:00 the
    realised window covered 0-83 and the plan's today slice covered 85-95, and
    index 84 was in neither. It is now named.
    """
    closed, in_progress, remaining = day_partition(head=85, interval_count=96)

    assert list(closed) == list(range(0, 84))
    assert in_progress == 84
    assert list(remaining) == list(range(85, 96))


def test_the_first_refresh_after_midnight_has_nothing_closed() -> None:
    """``head`` of zero: no closed interval and no quarter in flight.

    A partition that invented an index ``-1`` here would price the last quarter of
    *yesterday* as today's, which is the reclassification the civil-day boundary
    exists to prevent.
    """
    closed, in_progress, remaining = day_partition(head=0, interval_count=96)

    assert list(closed) == []
    assert in_progress is None
    assert list(remaining) == list(range(96))


# ===========================================================================
# 3. the identity, and the refusal to fake it
# ===========================================================================


def _window(*, net: float, opening: float, closing: float) -> RealizedWindow:
    """Return a window whose position identity is exactly ``net + closing - opening``.

    Built through ``realized_window`` from one priced interval, so the fields the
    accounting reads are produced by the production function rather than assigned.
    """
    window = realized_window(
        grid_import_kwh=[1.0],
        grid_export_kwh=[0.0],
        import_price_eur_kwh=[0.5],
        export_price_eur_kwh=[0.1],
        load_kwh=[1.0],
        production_kwh=[0.0],
        opening_inventory_value_eur=opening,
        closing_inventory_value_eur=closing,
    )
    assert window.realized_net_value_eur is not None
    # The fixture's own net figure, so the caller's ``net`` is honoured exactly.
    return RealizedWindow(
        **{
            **{
                field: getattr(window, field)
                for field in window.__dataclass_fields__
                if field
                not in {
                    "realized_import_cost_eur",
                    "realized_export_revenue_eur",
                    "realized_load_avoidance_value_eur",
                }
            },
            "realized_import_cost_eur": 0.0,
            "realized_export_revenue_eur": 0.0,
            "realized_load_avoidance_value_eur": net,
        }
    )


def test_the_five_terms_sum_to_the_published_total() -> None:
    """The identity, on figures chosen so no two of them are equal.

    *Mutation: drop any addend from the sum, or sign-flip the revaluation, and the
    reconciliation error exceeds its tolerance and the total is withheld.*
    """
    result = day_accounting(
        realised=_window(net=1.25, opening=0.40, closing=0.90),
        in_progress_eur=0.0725,
        in_progress_index=57,
        in_progress_coverage=0.4,
        remaining_expected_eur=2.5,
        forecast_revaluation_eur=-0.0658,
    )

    assert result.realised_today_eur == pytest.approx(1.25 + 0.90 - 0.40)
    assert result.total_economic_value_today_eur == pytest.approx(
        result.realised_today_eur
        + result.in_progress_interval_eur
        + result.remaining_expected_today_eur
        + result.forecast_revaluation_eur
    )
    assert abs(result.reconciliation_error_eur) <= (
        ACCOUNTING_RECONCILIATION_TOLERANCE_EUR
    )
    assert result.unavailable_reason is None


def test_the_total_telescopes_to_a_sentence_a_person_can_state() -> None:
    """``R + G + P + V[now](close) - V[open](open)``, and nothing else survives.

    The intermediate ``V[now](open)`` appears twice with opposite signs. If it did
    not cancel, the total would double-count the opening position -- and the
    identity above would still pass, because it only checks the addends against
    their own sum. This is the assertion that pins what the total *means*.
    """
    net, v_now_open, v_now_close, v_open_open = 1.25, 0.40, 0.90, 0.31
    result = day_accounting(
        realised=_window(net=net, opening=v_now_open, closing=v_now_close),
        in_progress_eur=0.0725,
        in_progress_index=57,
        in_progress_coverage=0.4,
        remaining_expected_eur=2.5,
        forecast_revaluation_eur=v_now_open - v_open_open,
    )

    assert result.total_economic_value_today_eur == pytest.approx(
        net + 0.0725 + 2.5 + v_now_close - v_open_open, abs=1e-4
    )


@pytest.mark.parametrize(
    "missing",
    [
        "realised",
        "in_progress_eur",
        "remaining_expected_eur",
        "forecast_revaluation_eur",
    ],
)
def test_a_missing_addend_takes_the_total_with_it(missing: str) -> None:
    """A total missing a term is not a smaller total. **Never a zero.**"""
    kwargs = {
        "realised": _window(net=1.25, opening=0.40, closing=0.90),
        "in_progress_eur": 0.07,
        "in_progress_index": 57,
        "in_progress_coverage": 0.4,
        "remaining_expected_eur": 2.5,
        "forecast_revaluation_eur": -0.07,
    }
    kwargs[missing] = None

    result = day_accounting(**kwargs)

    assert result.total_economic_value_today_eur is None
    assert result.reconciliation_error_eur is None


def test_no_addend_is_ever_a_plug() -> None:
    """**The rule the brief names explicitly: never hide a residual to balance.**

    The failure mode this guards against is not a wrong total -- it is a *right*
    total achieved by quietly adjusting one of the terms to make the four add up.
    A plug is undetectable from the equation alone, which is exactly why it has to
    be pinned against the source each term came from.

    ``realised_today_eur`` is the one at risk, because it is the term with an
    independent definition to fall back on: beta.38's position identity. It must
    be that identity and nothing else, whatever the other three do.

    *Mutation: publish ``realised_today_eur`` as ``total`` less the other three,
    and this fails while every arithmetic identity in this file still passes.*
    """
    realised = _window(net=1.25, opening=0.40, closing=0.90)
    result = day_accounting(
        realised=realised,
        in_progress_eur=0.0725,
        in_progress_index=57,
        in_progress_coverage=0.4,
        remaining_expected_eur=2.5,
        forecast_revaluation_eur=-0.0658,
    )

    assert result.realised_today_eur == pytest.approx(
        realised.realized_plus_remaining_value_eur, abs=1e-9
    )
    # And the other three are the figures handed over, at their own precision.
    assert result.in_progress_interval_eur == pytest.approx(0.0725, abs=1e-9)
    assert result.remaining_expected_today_eur == pytest.approx(2.5, abs=1e-9)
    assert result.forecast_revaluation_eur == pytest.approx(-0.0658, abs=1e-9)

    # **And structurally, because a plug that computes the right answer is
    # invisible from the outside.** Where the identity holds -- which is always,
    # by construction -- ``total - the other three`` *equals* the realised term,
    # so no assertion on values can tell a derivation from a plug. What can is the
    # direction of the arithmetic: the total is derived from the addends and never
    # the reverse.
    import inspect

    from custom_components.alpha_ems_manager import realized as realized_module

    source = inspect.getsource(realized_module.day_accounting)
    published = source[source.index("return DayAccounting(") :]
    realised_line = published[
        published.index("realised_today_eur=") : published.index(
            "in_progress_interval_eur="
        )
    ]
    assert "realised_today" in realised_line, realised_line
    assert "total" not in realised_line, realised_line
    for name in ("in_progress_eur", "remaining_expected_eur"):
        assert name not in realised_line, (name, realised_line)


def test_the_reconciliation_tolerance_is_above_what_rounding_can_reach() -> None:
    """**The bound is derived, so a correct reconciliation can never trip it.**

    Four addends rounded at four decimals, plus the sum's own rounding, is at most
    2.5e-4 of pure rounding. A tolerance at or below that would refuse totals that
    are arithmetically right -- which is how a guard against dishonesty becomes a
    permanently unavailable figure.

    *Mutation: tighten the tolerance to 1e-4 and this fails.*
    """
    assert ACCOUNTING_RECONCILIATION_TOLERANCE_EUR > 5 * 5e-5

    # Walked, not argued: three terms each a hair over the rounding boundary is
    # the worst case three addends can construct, and it must reconcile.
    hair = 0.00005000001
    result = day_accounting(
        realised=_window(net=1.0, opening=0.0, closing=0.0),
        in_progress_eur=hair,
        in_progress_index=1,
        in_progress_coverage=0.0,
        remaining_expected_eur=hair,
        forecast_revaluation_eur=hair,
    )

    assert result.total_economic_value_today_eur is not None, result
    assert abs(result.reconciliation_error_eur) <= (
        ACCOUNTING_RECONCILIATION_TOLERANCE_EUR
    )


def test_a_non_finite_addend_withholds_the_total() -> None:
    """**Finite, not merely present.**

    A ``nan`` propagates through the sum *and* through the tolerance test --
    ``abs(nan) > tol`` is false -- so a single non-finite term would have
    published a total reading ``nan`` and passed its own reconciliation check.

    *Mutation: check only for ``None`` and this fails.*
    """
    window = _window(net=1.25, opening=0.40, closing=0.90)
    broken = RealizedWindow(
        **{
            **{
                field: getattr(window, field)
                for field in window.__dataclass_fields__
                if field != "closing_inventory_value_eur"
            },
            "closing_inventory_value_eur": float("nan"),
        }
    )

    result = day_accounting(
        realised=broken,
        in_progress_eur=0.0,
        in_progress_index=1,
        in_progress_coverage=0.0,
        remaining_expected_eur=0.0,
        forecast_revaluation_eur=0.0,
    )

    assert result.total_economic_value_today_eur is None


def test_the_quarter_in_flight_is_priced_by_the_realised_rule() -> None:
    """One arithmetic for the open quarter and the closed ones.

    ``open_quarter_value_eur`` has to agree with what ``realized_window`` would
    make of the same interval once it closes, or the term would change value at
    the moment it moved into history.
    """
    flows = {
        "grid_import_kwh": 0.2,
        "grid_export_kwh": 1.4,
        "load_kwh": 0.35,
        "production_kwh": 0.0,
        "import_price_eur_kwh": 0.31,
        "export_price_eur_kwh": 0.24,
    }
    live = open_quarter_value_eur(**flows)
    closed = realized_window(
        grid_import_kwh=[flows["grid_import_kwh"]],
        grid_export_kwh=[flows["grid_export_kwh"]],
        import_price_eur_kwh=[flows["import_price_eur_kwh"]],
        export_price_eur_kwh=[flows["export_price_eur_kwh"]],
        load_kwh=[flows["load_kwh"]],
        production_kwh=[flows["production_kwh"]],
    )

    assert live == pytest.approx(closed.realized_net_value_eur, abs=1e-4)
    # The witness: a quarter that exported at a profit is worth something.
    assert live > 0.0


def test_an_unpriceable_open_quarter_is_unknown_and_not_zero() -> None:
    """A flow with no price is skipped everywhere else, and here too."""
    assert (
        open_quarter_value_eur(
            grid_import_kwh=0.2,
            grid_export_kwh=0.0,
            load_kwh=0.35,
            production_kwh=0.0,
            import_price_eur_kwh=None,
            export_price_eur_kwh=0.24,
        )
        is None
    )


def test_the_published_reasons_are_a_closed_vocabulary() -> None:
    """Every reason the accounting can state is in the published tuple.

    So a reader can enumerate them, and a new one cannot be added without
    appearing in the vocabulary a dashboard reads.
    """
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module.AlphaEmsCoordinator._today_accounting)
    source += inspect.getsource(module.AlphaEmsCoordinator._forecast_revaluation_eur)
    source += inspect.getsource(module.AlphaEmsCoordinator._remaining_expected_eur)

    named = {
        name
        for name in dir(module)
        if name.startswith("ACCOUNTING_UNAVAILABLE_") and name.isupper()
    }
    used = {name for name in named if name in source}

    assert used, "the accounting names no reason at all"
    for name in used:
        assert getattr(module, name) in ACCOUNTING_UNAVAILABLE_REASONS, name


# ===========================================================================
# 4. the revaluation: required, and not reconstructible
# ===========================================================================


def test_the_revaluation_is_exactly_the_residual_the_identity_leaves() -> None:
    """**The proof that it is required rather than useful.**

    Subtract beta.38's operational identity from the position total and what is
    left is ``V[now](e_open) - V[open](e_open)`` -- nothing else. So a total without
    it does not merely omit a term: it attributes forecast movement to today's
    operation, under a name that says operation.
    """
    net, v_now_open, v_now_close, v_open_open = 1.25, 0.40, 0.90, 0.31
    realised = _window(net=net, opening=v_now_open, closing=v_now_close)
    result = day_accounting(
        realised=realised,
        in_progress_eur=0.0,
        in_progress_index=1,
        in_progress_coverage=0.0,
        remaining_expected_eur=0.0,
        forecast_revaluation_eur=v_now_open - v_open_open,
    )

    operational = realised.realized_plus_remaining_value_eur
    assert result.total_economic_value_today_eur - operational == pytest.approx(
        v_now_open - v_open_open, abs=1e-4
    )


def test_the_marginal_shortcut_is_not_the_position_value() -> None:
    """**Why a new persisted field was unavoidable.**

    The tempting reconstruction is ``marginal_value_eur_per_kwh * stored_energy``,
    from figures ``EconomicSnapshot`` already keeps. It is wrong by construction:
    the marginal figure is the slope at the head bucket, and the position value is
    the integral of that slope from the floor upward over a curve the model itself
    reports as kinked at every switching-fee boundary.

    Measured on a solved horizon rather than argued, and the discrepancy is a
    material fraction rather than a rounding difference.
    """
    from custom_components.alpha_ems_manager.economic import economic_value_summary

    outcome = _solve("sell")
    summary = economic_value_summary(outcome, today_interval_count=96)
    stored = summary["stored_value"]
    marginal = stored["marginal_value_down_eur_kwh"]
    position = stored["stored_value_eur"]

    assert marginal is not None and position is not None, summary
    shortcut = marginal * summary["stored_energy_kwh"]
    assert position > 0.0, "the witness: the pack is worth something"
    assert abs(shortcut - position) / position > 0.05, (shortcut, position)


def test_the_revaluation_is_zero_on_a_curve_that_has_not_moved() -> None:
    """Same curve, same energy: nothing to revalue.

    Half of the pair. A revaluation that were non-zero here would be measuring
    something other than the curve.
    """
    result = day_accounting(
        realised=_window(net=1.0, opening=0.40, closing=0.40),
        in_progress_eur=0.0,
        in_progress_index=1,
        in_progress_coverage=0.0,
        remaining_expected_eur=0.0,
        forecast_revaluation_eur=0.0,
    )

    assert result.forecast_revaluation_eur == 0.0
    assert result.total_economic_value_today_eur == pytest.approx(1.0)


def test_the_revaluation_is_non_zero_on_a_curve_that_has_moved() -> None:
    """The other half, on the live figures that motivated the term.

    The 2026-09-02 downloads valued the *same* 12.269 kWh at 2.3001 EUR at 20:45
    and 2.3659 EUR at 21:00 -- 6.6 cents of pure curve movement in a quarter, on
    unchanged energy. Held at the recorded precision so a term that silently
    collapsed to zero could not pass.
    """
    result = day_accounting(
        realised=_window(net=0.0, opening=2.3659, closing=2.3659),
        in_progress_eur=0.0,
        in_progress_index=1,
        in_progress_coverage=0.0,
        remaining_expected_eur=0.0,
        forecast_revaluation_eur=2.3659 - 2.3001,
    )

    assert result.forecast_revaluation_eur == pytest.approx(0.0658, abs=1e-4)
    assert result.total_economic_value_today_eur == pytest.approx(0.0658, abs=1e-4)


# ===========================================================================
# 5. the one new persisted field
# ===========================================================================


def _day():
    """Return a day record with a state-of-charge series, as production files one."""
    from .forecast_helpers import NORMAL
    from .test_beta35_ledger import _measured_day

    return _measured_day(NORMAL)


def test_the_opening_valuation_is_written_once_and_never_revised() -> None:
    """**Idempotent, and the guard is the field's own absence.**

    A reload, a restart or an extra refresh must not be able to re-open a civil
    day: the second write would move the reference the whole day's revaluation is
    measured from, and the figure would reset mid-day with nothing to notice it.

    *Mutation: remove the ``is not None`` guard and the second write wins.*
    """
    record = _day()

    assert record.open_value is None
    assert record.note_opening_valuation(
        valued_at="2026-09-02T00:15:00+02:00",
        stored_energy_kwh=12.269,
        position_value_eur=2.3001,
        floor_kwh=4.216,
        bucket_kwh=0.35,
    )
    first = dict(record.open_value)

    assert not record.note_opening_valuation(
        valued_at="2026-09-02T12:00:00+02:00",
        stored_energy_kwh=3.0,
        position_value_eur=99.0,
        floor_kwh=4.216,
        bucket_kwh=0.35,
    )
    assert record.open_value == first


def test_the_opening_valuation_survives_a_round_trip() -> None:
    """It is persisted, or the revaluation dies at every restart."""
    from custom_components.alpha_ems_manager.storage import DayRecord

    record = _day()
    record.note_opening_valuation(
        valued_at="2026-09-02T00:15:00+02:00",
        stored_energy_kwh=12.269,
        position_value_eur=2.3001,
        floor_kwh=4.216,
        bucket_kwh=0.35,
    )
    payload = record.to_dict()
    restored = DayRecord.from_dict(record.day, payload, record.tz_key)

    assert "ov" in payload
    assert restored is not None
    assert restored.open_value == record.open_value


def test_a_beta_thirty_eight_document_reads_back_without_it() -> None:
    """**Additive: no migration and no reset.**

    A document written before beta.39 has no ``ov`` key, and that is a defined
    state -- the revaluation publishes ``None`` with a reason until the next civil
    day writes one. It must not be an error and must not be a zero.
    """
    from custom_components.alpha_ems_manager.storage import DayRecord

    record = _day()
    payload = record.to_dict()
    assert "ov" not in payload

    restored = DayRecord.from_dict(record.day, payload, record.tz_key)
    assert restored is not None
    assert restored.open_value is None


@pytest.mark.parametrize(
    "damaged",
    [
        {"at": "2026-09-02T00:15:00+02:00", "e": 12.269},
        {"e": 12.269, "v": 2.3, "f": 4.2, "b": 0.35},
        {"at": "x", "e": 12.269, "v": None, "f": 4.2, "b": 0.35},
        {"at": "x", "e": "12.269", "v": 2.3, "f": 4.2, "b": 0.35},
        {"at": "x", "e": 12.269, "v": True, "f": 4.2, "b": 0.35},
        "not a dict",
    ],
)
def test_a_damaged_opening_valuation_degrades_to_absent(damaged) -> None:
    """All five numbers or none of them.

    Half a valuation is not a smaller one: a record missing the energy it valued,
    or the lattice it was measured against, cannot be compared with anything. It
    is read as absent -- which has a published reason -- rather than as a
    plausible-looking partial figure.
    """
    from custom_components.alpha_ems_manager.storage import DayRecord

    record = _day()
    payload = record.to_dict()
    payload["ov"] = damaged

    restored = DayRecord.from_dict(record.day, payload, record.tz_key)
    assert restored is not None
    assert restored.open_value is None, damaged


def test_only_the_storage_minor_version_moved() -> None:
    """The additive change, and the only schema movement in the release.

    Schemas have been frozen since beta.33 and a moving one is worth a test rather
    than a line in a changelog. beta.39 moved the learning store's minor to 7 for the
    opening valuation; beta.42 moved it to 8 for the sealed per-day benefit and the
    lifetime cursor. Every other schema here is still where beta.33 left it, which is
    the part this test exists to notice.
    """
    from custom_components.alpha_ems_manager.const import (
        CLAIM_SCHEMA_VERSION,
        CONFIG_ENTRY_VERSION,
        FORECAST_STORAGE_MINOR_VERSION,
        FORECAST_STORAGE_VERSION,
    )

    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (2, 8)
    assert CONFIG_ENTRY_VERSION == 2
    assert CLAIM_SCHEMA_VERSION == 2
    assert (FORECAST_STORAGE_VERSION, FORECAST_STORAGE_MINOR_VERSION) == (1, 8)


# ===========================================================================
# 6. through the coordinator, on a priced civil day
# ===========================================================================


async def a_fully_priced_day(hass, config_data, frank, live_surface, monkeypatch):
    """Return a coordinator whose whole civil day is priced, planned and valued.

    **Both market days are published, and that is not decoration.** The source
    publishes a *market* day and the plan is a local civil day; where Home
    Assistant runs outside the market's timezone -- which this harness does -- one
    published day cannot span the other, and roughly a third of the local day is
    then neither realised nor planned. That is a real installation shape, and
    beta.39 refuses a total over it, so a fixture that wants a *total* has to
    price the whole day first.

    The opening valuation is written by the refresh itself, through
    ``_note_opening_valuation``, because the day record exists by then. Nothing
    here pokes the record.
    """
    from datetime import timedelta

    from .beta38_trace import step_clock
    from .forecast_helpers import NORMAL
    from .frank_capture import synthetic_day
    from .test_beta24_live_charge import charge_now_price, step_once
    from .test_beta38_ledger import a_priced_day

    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    frank.publish(
        today=synthetic_day(NORMAL, price_at=charge_now_price),
        tomorrow=synthetic_day(NORMAL + timedelta(days=1), price_at=charge_now_price),
    )
    await step_once(hass, coordinator, live_surface, **step_clock(1))

    plan = (coordinator.data or {}).get("battery_plan")
    record = coordinator.store.days[plan.target_day]
    assert record.open_value is not None, "the refresh must record an opening value"
    return coordinator


def inside_the_day(coordinator):
    """Return an instant inside the priced civil day, as production always is.

    The harness's wall clock is a different day from the fixture's priced one, so
    ``current_prices`` finds nothing and the quarter in flight cannot be valued --
    which is honest, and is not a state a live installation is ever in. Every
    coordinator-level assertion below therefore asks about an instant in the day
    it is asking about.
    """
    from datetime import timedelta

    import homeassistant.util.dt as dt_util

    from custom_components.alpha_ems_manager.storage import interval_start_utc

    plan = (coordinator.data or {}).get("battery_plan")
    outcome = (coordinator.data or {}).get("economic")
    head = outcome.desired.intervals[0].index
    tz = dt_util.get_default_time_zone()
    # The quarter in flight is ``head - 1``, and a minute into it.
    start = interval_start_utc(plan.target_day, max(0, head - 1), tz)
    return dt_util.as_local(start + timedelta(minutes=1))


def accounting_of(coordinator) -> dict:
    """Return the day-accounting block as the entity publishes it."""
    payload = coordinator.economic_value(inside_the_day(coordinator))
    assert payload.get("available") is True, payload
    block = payload.get("today_accounting")
    assert isinstance(block, dict), payload
    return block


async def test_the_entity_publishes_the_identity_and_it_reconciles(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """**End to end, on the payload a user downloads.**

    Asserted on the published block rather than on coordinator state, which is the
    assertion beta.38 got wrong twice: a figure correct in memory and stale in the
    payload is a figure nobody can read.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    block = accounting_of(coordinator)

    assert block["unavailable_reason"] is None, block
    assert block["accounting_basis"] == ACCOUNTING_BASIS_POSITION
    assert block["avoidance_basis"] == AVOIDANCE_BASIS_NO_BATTERY
    addends = (
        block["realised_today_eur"],
        block["in_progress_interval_eur"],
        block["remaining_expected_today_eur"],
        block["forecast_revaluation_eur"],
    )
    assert all(value is not None for value in addends), block
    assert block["total_economic_value_today_eur"] == pytest.approx(
        sum(addends), abs=ACCOUNTING_RECONCILIATION_TOLERANCE_EUR
    )
    assert abs(block["reconciliation_error_eur"]) <= (
        ACCOUNTING_RECONCILIATION_TOLERANCE_EUR
    )

    # **And the remaining term is the day block's own no-battery figure**, not one
    # of the two planner comparisons sitting beside it. Named here because every
    # arithmetic assertion above would still balance if it were the wrong one --
    # the total would simply be measuring something that does not exist.
    outcome = (coordinator.data or {}).get("economic")
    plan = (coordinator.data or {}).get("battery_plan")
    count = coordinator.store.days[plan.target_day].interval_count
    expected = day_block_for(outcome.desired, today_interval_count=count)
    assert block["remaining_expected_today_eur"] == pytest.approx(
        expected["no_battery_value_eur"], abs=1e-4
    ), (block["remaining_expected_today_eur"], expected)


async def test_the_total_is_not_any_of_the_figures_it_must_not_be(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """The four wrong answers, named individually.

    ``decision_advantage_eur`` is a from-now counterfactual comparison; the day
    split is each interval against its own idle baseline; ``realised_net_value_eur``
    omits the position entirely; and beta.38's position identity omits both the
    revaluation and the rest of the day. A rename or a copy-paste that substituted
    any of them would satisfy every identity test above and still be wrong.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    payload = coordinator.economic_value(inside_the_day(coordinator))
    total = payload["today_accounting"]["total_economic_value_today_eur"]
    plan = (coordinator.data or {}).get("battery_plan")
    ledger = coordinator.realized_today(plan)["ledger"]

    assert total is not None
    for wrong in (
        payload["decision_advantage_eur"],
        payload["today_interval_value_eur"],
        ledger["realised_net_value_eur"],
        ledger["realised_plus_remaining_value_eur"],
    ):
        assert total != pytest.approx(wrong, abs=1e-3), wrong


async def test_the_identity_holds_with_a_non_zero_revaluation(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """**The identity, on the case that motivated the term.**

    The fixture writes its opening valuation on the same curve the accounting then
    reads, so the natural revaluation is exactly zero -- which is correct, and
    which makes every arithmetic assertion above satisfied by a term that is
    always zero. A day whose curve has moved is the case the release exists for,
    and it is produced here the way a real day produces it: the persisted opening
    valuation stays where it was and the current curve values the same energy
    differently.

    Three things are pinned at once, and each is a separate mutation: the term is
    non-zero, its **sign** is current-less-persisted, and the total still
    reconciles with it in.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    record = coordinator.store.days[plan.target_day]
    drift = 0.0658  # the movement the 2026-09-02 captures showed in one quarter
    record.open_value = {**record.open_value, "v": record.open_value["v"] - drift}

    block = accounting_of(coordinator)

    assert block["forecast_revaluation_eur"] == pytest.approx(drift, abs=1e-4), block
    assert block["position"]["opening_inventory_value_eur"] == pytest.approx(
        block["position"]["opening_valuation_eur"] + drift, abs=1e-4
    ), block["position"]
    addends = (
        block["realised_today_eur"],
        block["in_progress_interval_eur"],
        block["remaining_expected_today_eur"],
        block["forecast_revaluation_eur"],
    )
    assert all(value is not None for value in addends), block
    assert block["total_economic_value_today_eur"] == pytest.approx(
        sum(addends), abs=ACCOUNTING_RECONCILIATION_TOLERANCE_EUR
    )
    # The witness: the total genuinely moved by the revaluation, so a released
    # build that dropped the term would publish a different number.
    without = sum(addends[:3])
    assert block["total_economic_value_today_eur"] != pytest.approx(without, abs=1e-3)


async def test_the_partition_covers_the_whole_civil_day_on_a_real_plan(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """The three counts add to the day's own interval count, through the coordinator."""
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    partition = accounting_of(coordinator)["partition"]

    in_flight = 0 if partition["in_progress_index"] is None else 1
    assert (
        partition["realised_intervals"] + in_flight + partition["remaining_intervals"]
        == partition["interval_count"]
    ), partition
    # The quarter in flight sits exactly between the two slices.
    assert partition["in_progress_index"] == partition["realised_intervals"]
    # And the plan's priced horizon reaches midnight, which is what makes the
    # total publishable at all.
    assert partition["remaining_unpriced_intervals"] == 0, partition
    # **The realised window is priced over exactly the closed slice**, which is
    # what stops the quarter in flight being counted twice: once here and once as
    # its own term. Priced plus skipped is the window's own account of how many
    # intervals it was handed.
    assert (
        partition["realised_intervals_priced"] + partition["realised_intervals_skipped"]
        == partition["realised_intervals"]
    ), partition


async def test_a_day_block_on_another_basis_publishes_no_total(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """**Fail closed on a basis change, and the guard is reachable.**

    No release can reach this today -- ``_day_block`` publishes the no-battery
    basis unconditionally -- and that is exactly why the guard exists: if a later
    release re-bases the day block, the remaining term stops being addable to a
    measured one, and the honest answer is no total rather than a mixed one. Sent
    through the real refusal path rather than argued in a comment.
    """
    from custom_components.alpha_ems_manager import coordinator as module
    from custom_components.alpha_ems_manager.const import (
        ACCOUNTING_UNAVAILABLE_AVOIDANCE_BASIS,
        AVOIDANCE_BASIS_INTERVAL_IDLE,
    )

    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    original = module.day_block_for

    def rebased(plan, **kwargs):
        block = dict(original(plan, **kwargs))
        block["avoidance_basis"] = AVOIDANCE_BASIS_INTERVAL_IDLE
        return block

    monkeypatch.setattr(module, "day_block_for", rebased)
    block = accounting_of(coordinator)

    assert block["remaining_expected_today_eur"] is None
    assert block["total_economic_value_today_eur"] is None
    assert block["unavailable_reason"] == ACCOUNTING_UNAVAILABLE_AVOIDANCE_BASIS


async def test_a_horizon_short_of_midnight_publishes_no_total(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """**A day with a hole in it has no day total.**

    The single-market-day fixture is that shape: Home Assistant outside the
    market's timezone, so the published day cannot span the local one and part of
    the civil day is neither realised nor planned. Adding up what is left would
    publish a figure that looks like the day and is not.
    """
    import homeassistant.util.dt as dt_util

    from .test_beta38_ledger import a_priced_day

    coordinator = await a_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    outcome = (coordinator.data or {}).get("economic")
    coordinator._note_opening_valuation(
        outcome=outcome, plan=plan, now=dt_util.now(), today=plan.target_day
    )
    block = accounting_of(coordinator)

    assert block["partition"]["remaining_unpriced_intervals"] > 0, block["partition"]
    assert block["remaining_expected_today_eur"] is None
    assert block["total_economic_value_today_eur"] is None
    assert (
        block["unavailable_reason"] == ACCOUNTING_UNAVAILABLE_HORIZON_SHORT_OF_MIDNIGHT
    )


async def test_a_day_with_no_opening_valuation_says_so(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """No record, no zero. The reason is published and the total is withheld."""
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    coordinator.store.days[plan.target_day].open_value = None

    block = accounting_of(coordinator)

    assert block["forecast_revaluation_eur"] is None
    assert block["total_economic_value_today_eur"] is None
    assert block["unavailable_reason"] == ACCOUNTING_UNAVAILABLE_NO_OPENING_VALUATION


async def test_a_moved_lattice_refuses_rather_than_fudges(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """Two integrals over two different lattices are not a revaluation.

    The reserve floor is deliberately *not* a refusal -- it moves with the load and
    production forecasts, and its effect on what the position is worth is forecast
    revaluation in the plainest sense, so both floors are published beside the
    figure instead. The lattice pitch is different in kind: it is
    ``quarter_dc / k`` for integer ``k`` and depends only on the configured limits,
    so a changed pitch is a changed *system* rather than a changed forecast.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    record = coordinator.store.days[plan.target_day]
    record.open_value = {**record.open_value, "b": record.open_value["b"] * 2.0}

    block = accounting_of(coordinator)

    assert block["forecast_revaluation_eur"] is None
    assert block["total_economic_value_today_eur"] is None
    assert (
        block["unavailable_reason"] == ACCOUNTING_UNAVAILABLE_VALUATION_REFERENCE_MOVED
    )


async def test_a_stable_lattice_is_not_mistaken_for_a_moved_one(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """**The rounding trap, encoded.**

    The pitch is persisted at six decimals for compactness. An exact float
    comparison against the live figure therefore fails by about 3e-7 on an
    entirely unchanged lattice -- and did, publishing
    ``valuation_reference_moved`` on every refresh of a stable installation and
    making the total permanently unavailable. It is compared at the precision it
    is stored at instead.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    outcome = (coordinator.data or {}).get("economic")
    stored = coordinator.store.days[plan.target_day].open_value

    # The witness: the two figures genuinely differ as floats.
    assert stored["b"] != outcome.bucket_kwh
    assert accounting_of(coordinator)["forecast_revaluation_eur"] is not None


async def test_a_mismatched_opening_energy_refuses_rather_than_fudges(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """The two valuations must be of one energy, or their difference means nothing.

    The persisted figure comes from the live snapshot at the day's first usable
    refresh and the ledger's comes from the state-of-charge series, stored to 0.4 %
    of capacity -- so they always differ a little, and the tolerance is one quantum
    of that. Beyond it they are two different positions.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = (coordinator.data or {}).get("battery_plan")
    record = coordinator.store.days[plan.target_day]
    drift = ACCOUNTING_OPENING_ENERGY_TOLERANCE_KWH * 10.0
    record.open_value = {**record.open_value, "e": record.open_value["e"] + drift}

    block = accounting_of(coordinator)

    assert block["forecast_revaluation_eur"] is None
    assert block["unavailable_reason"] == ACCOUNTING_UNAVAILABLE_OPENING_ENERGY_MISMATCH


async def test_the_quarter_in_flight_never_joins_the_closed_history(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """**The requirement in the brief: an explicit third term, not a fold.**

    The realised slice is ``[0, h-1)`` and the quarter in flight is ``h-1``, so the
    interval whose measurement is still accruing is structurally outside the
    realised window -- which is also why it had to be published separately: it is
    in neither the realised window nor the plan's remaining slice, and that is the
    quarter the live captures lost.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    block = accounting_of(coordinator)
    partition = block["partition"]
    index = partition["in_progress_index"]

    assert index is not None
    assert index == partition["realised_intervals"], partition
    assert index not in range(
        partition["interval_count"] - partition["remaining_intervals"],
        partition["interval_count"],
    ), partition
    # Measured from the live integrators, with its coverage stated, because a term
    # computed two seconds into a quarter is honestly near zero and a reader has
    # to be able to tell that from nothing having happened.
    assert block["in_progress_interval_eur"] is not None
    assert partition["in_progress_coverage"] is not None


async def test_the_open_quarter_prices_the_baseline_not_the_meter(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """The flexible load is subtracted, exactly as every closed interval's is.

    ``DayRecord.baseline_at`` is ``measured - ev`` for every persisted interval,
    and an open one must not be the exception: an EV session folded into the house
    baseline would inflate the no-battery counterfactual and credit the battery
    with avoiding an import nobody was going to make.

    The integrators are stubbed because the harness configures no flexible load,
    and a term that is structurally zero cannot test a subtraction.
    """
    from dataclasses import dataclass

    @dataclass
    class Stub:
        open_energy_kwh: float
        open_coverage: float = 0.5

    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    moment = inside_the_day(coordinator)
    coordinator._accumulator = Stub(open_energy_kwh=0.90)
    coordinator._ev_accumulator = Stub(open_energy_kwh=0.60)
    coordinator._pv_accumulator = Stub(open_energy_kwh=0.0)
    coordinator._grid_import_accumulator = Stub(open_energy_kwh=0.10)
    coordinator._grid_export_accumulator = Stub(open_energy_kwh=0.0)

    value, coverage = coordinator._open_quarter_value_eur(moment)
    buy, _sell = coordinator.current_prices(moment)

    assert buy is not None, "the witness: the instant is priced"
    assert coverage == pytest.approx(0.5)
    # baseline 0.90 - 0.60 = 0.30; avoided = max(0, 0.30 - 0.10) = 0.20
    assert value == pytest.approx(0.20 * buy - 0.10 * buy, abs=1e-4)
    # And the meter reading would have given a different, larger answer.
    assert value != pytest.approx(0.90 * buy - 0.10 * buy, abs=1e-4)


async def test_a_reload_does_not_double_count_the_day(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """Reading it twice, and writing it again, changes nothing.

    Every realised figure in this project is recomputed from persisted history on
    every call -- there is no accumulator to double -- and the one new persisted
    field is write-once. Together those are what make a reload safe, and this
    exercises both.
    """
    import homeassistant.util.dt as dt_util

    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    first = accounting_of(coordinator)
    plan = (coordinator.data or {}).get("battery_plan")
    outcome = (coordinator.data or {}).get("economic")

    assert not coordinator._note_opening_valuation(
        outcome=outcome, plan=plan, now=dt_util.now(), today=plan.target_day
    )
    second = accounting_of(coordinator)

    assert second == first


async def test_the_accounting_block_is_publish_only(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """**Reading the entity must not move anything.**

    The block is assembled on every state update, so if it wrote, an entity read
    would be a side effect -- and the one write the release adds runs on the
    refresh instead. Pinned on the plan, the carried run, the campaign, the
    execution record, the persisted valuation, the lifecycle and the write
    surface, which is everything a decision could turn on.
    """
    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    day = (coordinator.data or {})["battery_plan"].target_day

    def snapshot():
        return (
            coordinator._plan,
            coordinator._carried,
            coordinator._campaign_id,
            coordinator.store.execution_record,
            dict(coordinator.store.days[day].open_value),
            len(live_surface.calls),
            coordinator._lifecycle,
        )

    before = snapshot()
    for _ in range(3):
        accounting_of(coordinator)

    assert snapshot() == before


async def test_there_is_still_exactly_one_euro_entity(
    hass, config_data: dict, source_entities: None, setup_integration
) -> None:
    """**One EUR entity, and it stays one.**

    A family of economic sensors is exactly how two of them come to disagree, so
    the day accounting lands on the entity that already exists rather than on a
    second one -- and the state is unchanged, so no history breaks.
    """
    from homeassistant.components.sensor import SensorDeviceClass

    monetary = [
        entity_id
        for entity_id in hass.states.async_entity_ids("sensor")
        if hass.states.get(entity_id).attributes.get("device_class")
        == SensorDeviceClass.MONETARY
    ]

    assert monetary == ["sensor.alpha_ems_economic_value"], monetary


async def test_an_unavailable_refresh_still_publishes_its_reason(
    hass, config_data: dict, source_entities: None, setup_integration
) -> None:
    """No plan, no figures -- and the basis and the reason are still there.

    The accounting attributes are gated on the same predicate as the state, so the
    two can never describe different refreshes. What a reader gets instead is why,
    which is the question they have.
    """
    state = hass.states.get("sensor.alpha_ems_economic_value")

    assert state is not None
    assert "both sides are metered cash" in state.attributes["basis"]
    assert "unavailable_reason" in state.attributes
    assert "total_economic_value_today_eur" not in state.attributes


async def test_the_entity_attributes_carry_the_five_figures_and_the_audit_block(
    hass, config_data: dict, source_entities: None, frank, live_surface, monkeypatch
) -> None:
    """Flat where a card reads them, nested where a person auditing them does.

    Read through the entity's own attribute function rather than by reformatting
    the payload, so the projection from the nested block onto the flat names is
    what is being tested.
    """
    from custom_components.alpha_ems_manager.sensor import (
        _economic_value_attributes,
    )

    coordinator = await a_fully_priced_day(
        hass, config_data, frank, live_surface, monkeypatch
    )
    attributes = _economic_value_attributes(coordinator)

    for name in (
        "realised_today_eur",
        "in_progress_interval_eur",
        "in_progress_interval_index",
        "remaining_expected_today_eur",
        "forecast_revaluation_eur",
        "total_economic_value_today_eur",
        "accounting_basis",
        "accounting_reconciliation_error_eur",
        "accounting_unavailable_reason",
    ):
        assert name in attributes, name
    assert isinstance(attributes["today_accounting"], dict)
    assert "no residual is absorbed" in attributes["accounting_rule"]
    # The state is untouched: still the cash advantage, so no history breaks.
    assert attributes["decision_advantage_eur"] == coordinator.economic_value()["state"]
    # And the flat names are a projection of the block, never a second derivation.
    block = attributes["today_accounting"]
    assert attributes["realised_today_eur"] == block["realised_today_eur"]
    assert (
        attributes["total_economic_value_today_eur"]
        == (block["total_economic_value_today_eur"])
    )


def test_the_accounting_adds_no_solve() -> None:
    """It reads the plan the refresh already produced, and a persisted scalar.

    Asserted structurally: the accounting helpers may not name the solver's entry
    points. A figure that re-solved would make an entity read cost a dynamic
    programme, and would also make the published total describe a different plan
    from the one beside it.
    """
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = "".join(
        inspect.getsource(getattr(module.AlphaEmsCoordinator, name))
        for name in (
            "_today_accounting",
            "_remaining_expected_eur",
            "_forecast_revaluation_eur",
            "_open_quarter_value_eur",
            "_note_opening_valuation",
        )
    )

    for forbidden in ("solve_economic", "build_physics_table", "_walk_forward"):
        assert forbidden not in source, forbidden
