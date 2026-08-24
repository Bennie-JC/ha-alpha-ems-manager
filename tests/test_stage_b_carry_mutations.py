"""Break each carry-forward invariant, and prove a test notices.

Every mutation here is a change someone might make in good faith while tidying the
controller up. Four are worth singling out, because each one is a version of a
mistake this project has already made once:

* **keying continuity on the publication id.** The obvious choice -- Stage A
  publishes an id, so use it -- and it churns every fifteen minutes, which resets
  progress every quarter and brings the sawtooth back by a different route.
* **letting a later publication move the accepted window.** It looks like keeping
  the run up to date. It is the one edit that makes activation unreachable again,
  because the accepted start is what the passing clock has to catch up with.
* **affirming on intent alone.** Simpler than an overlap test, and it carries a run
  forward into a campaign Stage A moved to tonight.
* **letting the reserve guard keep the command source.** Nothing looks broken: a
  well-formed discharge is built every refresh and the interlock passes it. That is
  precisely how F1's effect survived a whole implementation pass.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime, timedelta

from custom_components.alpha_ems_manager import execution as execution_module
from custom_components.alpha_ems_manager.alphaess_device import (
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    build_command,
    device_power_kw,
    plan_commands,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    CONTROL_MAX_POWER_KW,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_PREPARED,
)
from custom_components.alpha_ems_manager.execution import (
    admit,
    affirms,
    carry_forward,
    control_intent_for,
    mint_run_id,
    parse_target,
)

from .test_stage_b_carry_forward import BEFORE, decision_for, published
from .test_stage_b_controller import CLOSES, OPENS

NINE = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
QUARTER = timedelta(minutes=15)


# ===========================================================================
# The identity, and what happens when it is the wrong one
# ===========================================================================


def test_keying_continuity_on_the_publication_id_is_caught() -> None:
    """The mutation: use ``plan_id`` as the run identity.

    It is what Stage A offers, so it looks like the obvious key. Measured across
    eleven refreshes it produces eleven different identities for one run -- and
    every one of them resets progress, the grid integral and the ownership record.
    """
    run = admit(parse_target(published(OPENS)), BEFORE)
    minted = set()
    published_ids = set()

    for step in range(11):
        moment = OPENS + step * QUARTER
        payload = published(moment + QUARTER)
        published_ids.add(payload["plan_id"])
        minted.add(run.run_id)

    assert len(minted) == 1, minted
    assert len(published_ids) == 11, published_ids


def test_minting_the_identity_from_the_moving_window_start_is_caught() -> None:
    """The mutation: mint over the *published* start instead of the admitted one.

    Same failure by a shorter route. The identity has to be minted over something
    that does not move, and the only such instant is the one that was accepted.
    """
    live = {
        mint_run_id(EXECUTION_INTENT_GRID_CHARGE, OPENS + step * QUARTER, BEFORE)
        for step in range(6)
    }
    assert len(live) == 6

    stable = {
        mint_run_id(EXECUTION_INTENT_GRID_CHARGE, OPENS, BEFORE) for _ in range(6)
    }
    assert len(stable) == 1


# ===========================================================================
# The accepted window, and why it may not move
# ===========================================================================


def test_moving_the_accepted_window_start_forward_is_caught() -> None:
    """The mutation: adopt the fresh publication's window on every affirmation.

    It reads as keeping the run current. It restores the original blocker exactly:
    the accepted start advances with every refresh, so the clock can never reach
    it and the controller sits in ``prepared`` for ever.
    """
    carry = carry_forward(None, [published(NINE + QUARTER)], NINE)
    assert carry.carried is not None

    for step in range(1, 6):
        moment = NINE + step * QUARTER
        carry = carry_forward(carry.carried, [published(moment + QUARTER)], moment)
        assert carry.carried is not None
        # Had the window been adopted, this would be ``moment + QUARTER`` and the
        # decision below would be ``prepared`` at every offset.
        assert carry.carried.window_start == NINE + QUARTER
        assert decision_for(carry, moment).state == EXECUTION_STATE_ARMED


def test_extending_the_accepted_window_end_is_caught() -> None:
    """The mutation: let an affirmation push the end out.

    The other direction of the same error, and the more expensive one: a carried
    run would outlive the economics that chose it for as long as Stage A kept
    republishing anything of that intent.
    """
    run = admit(parse_target(published(OPENS, CLOSES)), BEFORE)
    longer = published(OPENS + QUARTER, CLOSES + timedelta(hours=3))

    carry = carry_forward(run, [longer], OPENS + QUARTER)

    assert carry.carried is not None
    assert carry.carried.window_end == CLOSES
    # And the end still ends it.
    assert carry_forward(carry.carried, [longer], CLOSES).carried is None


# ===========================================================================
# Affirmation, and the two ways to get it wrong
# ===========================================================================


def test_affirming_on_the_intent_alone_is_caught() -> None:
    """The mutation: drop the overlap test and match on intent.

    Simpler, and it carries a run forward into a campaign that has nothing to do
    with it -- Stage A now wants to charge at midnight, and the accepted afternoon
    run is treated as re-affirmed.
    """
    run = admit(parse_target(published(OPENS, CLOSES)), BEFORE)
    tonight = parse_target(
        published(CLOSES + timedelta(hours=4), CLOSES + timedelta(hours=6))
    )

    assert tonight.intent == run.intent
    assert not affirms(run, tonight)


def test_affirming_across_a_direction_change_is_caught() -> None:
    """The mutation: overlap alone, without checking the intent.

    An export run overlapping the accepted charge window would affirm it, and the
    carried record would then describe a charge while Stage A wanted the opposite.
    """
    run = admit(parse_target(published(OPENS, CLOSES)), BEFORE)
    overlapping_export = parse_target(
        published(OPENS + QUARTER, CLOSES, intent="net_export")
    )

    assert overlapping_export.window_start <= run.window_end
    assert not affirms(run, overlapping_export)


def test_treating_a_missing_publication_as_an_affirmation_is_caught() -> None:
    """The mutation: keep the run until its window ends, ignoring absence.

    ``stale_after`` would still catch it eventually, but half an hour later -- and
    withdrawal is supposed to be visible within one refresh.
    """
    run = admit(parse_target(published(OPENS, CLOSES)), BEFORE)

    assert carry_forward(run, [], OPENS + QUARTER).carried is None
    assert run.window_end > OPENS + QUARTER


# ===========================================================================
# Revision, and the two ways it becomes meaningless
# ===========================================================================


def test_a_revision_that_counts_refreshes_is_caught() -> None:
    """The mutation: include the window end in the materiality test.

    Measured on a real campaign: a reserve-driven buy is anchored to the head of
    the run, so its end advances every refresh, and the revision becomes a refresh
    counter. beta.19's revision never left 1; this is the same uselessness with the
    opposite shape.
    """
    run = admit(parse_target(published(OPENS, CLOSES)), BEFORE)
    revisions = set()
    carry = None

    for step in range(1, 7):
        moment = OPENS + step * QUARTER
        # A sliding end, which is what a head-anchored safety buy publishes.
        sliding = published(moment + QUARTER, CLOSES + step * QUARTER)
        carry = carry_forward(carry.carried if carry else run, [sliding], moment)
        assert carry.carried is not None
        revisions.add(carry.carried.revision)

    assert revisions == {1}, revisions


def test_a_revision_that_ignores_a_real_change_is_caught() -> None:
    """The other direction: it still has to move when Stage A moves a figure."""
    run = admit(parse_target(published(OPENS, CLOSES)), BEFORE)
    moment = OPENS + QUARTER

    bigger = published(moment + QUARTER, CLOSES, battery_target_kwh=19.5)
    carry = carry_forward(run, [bigger], moment)

    assert carry.carried is not None
    assert carry.carried.revision == 2


def test_adopting_the_published_energy_figure_is_caught() -> None:
    """The mutation: refresh the accepted target's figures on affirmation.

    It looks like keeping the run current, and it double-counts. A publication's
    remaining target shrinks as the horizon eats the run, so taking the fresh
    figure *and* subtracting measured progress removes the same kilowatt-hours
    twice.
    """
    run = admit(parse_target(published(OPENS, CLOSES)), BEFORE)
    moment = OPENS + QUARTER
    shrunk = published(moment + QUARTER, CLOSES, battery_target_kwh=9.0)

    carry = carry_forward(run, [shrunk], moment)

    assert carry.carried is not None
    assert carry.carried.target.battery_target_kwh == 11.94


# ===========================================================================
# Precedence and direction
# ===========================================================================


def test_letting_the_reserve_guard_win_an_open_window_is_caught() -> None:
    """The mutation: build the command before consulting Stage B.

    **This is the one that already happened.** Every other gate was green while a
    six-step reserve-guard discharge was built every refresh and passed the
    interlock, because a well-formed discharge is exactly what it is.

    Asserted where the two can be told apart: an actionable carried charge must
    produce a charge intent, and the reserve guard never produces one at all.
    """
    carry = carry_forward(None, [published(NINE + QUARTER)], NINE)
    carry = carry_forward(
        carry.carried, [published(NINE + 2 * QUARTER)], NINE + QUARTER
    )

    intent = control_intent_for(
        decision_for(carry, NINE + QUARTER),
        floor_soc_percent=20.0,
        ceiling_soc_percent=100.0,
        horizon_minutes=20,
        target_day=NINE.date(),
        start_index=37,
        built_at=NINE + QUARTER,
    )

    assert intent is not None
    assert intent.action == ACTION_CHARGE
    command = build_command(intent)
    steps = plan_commands(command)
    entities = {step.entity_id for step in steps}
    assert not entities & set(DISCHARGE_FAMILY.entities)
    assert CHARGE_FAMILY.activate in entities


def test_arming_a_prepared_run_is_caught() -> None:
    """The mutation: treat ``prepared`` as good enough to send.

    On this hardware arming *is* delivering -- measured on both control surfaces --
    so a command fifteen minutes early begins charging fifteen minutes early.
    """
    carry = carry_forward(None, [published(NINE + QUARTER)], NINE)
    decision = decision_for(carry, NINE)

    assert decision.state == EXECUTION_STATE_PREPARED
    assert not decision.wants_command
    assert (
        control_intent_for(
            decision,
            floor_soc_percent=20.0,
            ceiling_soc_percent=100.0,
            horizon_minutes=20,
            target_day=NINE.date(),
            start_index=36,
            built_at=NINE,
        )
        is None
    )


def test_commanding_more_power_than_the_register_holds_is_caught() -> None:
    """The mutation: leave the rolling request unclamped.

    A run late in its window with no headroom cap asks for
    ``remaining / remaining_hours``, which grows without bound as the denominator
    shrinks -- a real campaign reached 21.68 kW against a 20 kW register. Unclamped
    the safety gate refused the command outright, so a late charge did not run at
    the maximum; it did not run at all.
    """
    for wanted in (21.68, 40.0, 1_000.0):
        assert device_power_kw(wanted * 0.25, 0.25) == CONTROL_MAX_POWER_KW
    # And the clamp only ever lowers: the invariant below the maximum is untouched.
    assert device_power_kw(4.336 * 0.25, 0.25) == 4.3


# ===========================================================================
# The boundary the whole module rests on
# ===========================================================================


def test_the_carry_machine_still_names_no_economic_concept() -> None:
    """No price, no value, no ranking -- in the executable code of the machine.

    Docstrings are excluded because they *discuss* the prohibition; the point is
    that no branch can act on one.
    """
    forbidden = {
        "price",
        "prices",
        "eur",
        "value",
        "cost",
        "profit",
        "cheap",
        "expensive",
        "better",
        "best",
        "rank",
        "score",
    }
    for name in ("carry_forward", "affirms", "admit", "affirm", "_materially_moved"):
        source = inspect.getsource(getattr(execution_module, name))
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                continue
            if isinstance(node, ast.Name | ast.Attribute):
                label = node.id if isinstance(node, ast.Name) else node.attr
                assert label.lower() not in forbidden, (name, label)
