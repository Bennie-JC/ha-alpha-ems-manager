"""beta.40 Gate 2: an opened row keeps the verdict it opened with.

**The invariant beta.38 was released to establish, extended to one new field and
not weakened by it.** An opened frozen row has execution authority; ordinary
Stage-A replanning may revise the future and not the present.

The temptation this file forbids is specific. Stage B already carries two
downward-only caps -- `frozen_remaining_at_admission_kwh` on the row and the
run-level forward allowance -- and both exist to bound **grid purchase**: to stop a
later publication growing a buy, and to let a replan shrink one that has not
happened yet. Inheriting them for the retention verdict would look like consistency
and would be a defect: the authority is over production that is free by
measurement, the controller bounds it by the measured surplus, so it provably
causes no import. Reducing it cannot save a cent and can only throw away energy the
tariff already said was worth keeping -- and a rolling replan carrying a slightly
different production forecast would silently revoke an open row's authority, which
is exactly the 2026-09-01 class of defect.

The live diagnostic also shows that machinery inert on the reference installation
(`frozen_remaining_at_admission_kwh: null`, `authorisation_reduced_by_replan: 0.0`),
so adopting it here would have been shipping untested behaviour on top of an
untested field.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    RETENTION_GATE_AUTHORISED,
    RETENTION_GATE_EXPORT_SUPERIOR,
    RETENTION_GATES,
)
from custom_components.alpha_ems_manager.execution import (
    AdmittedPlan,
    CarriedQuarter,
    QuarterRow,
    admit_quarter,
    parse_target,
    target_as_published,
)

from .beta40_trace import ROW_BATTERY_KWH, ROW_GRID_AUTHORISED_KWH
from .forecast_helpers import NORMAL, local

OPENS = local(NORMAL, 12, 0)


def row_at(
    minute: int,
    *,
    authorised: bool,
    gate: str | None = None,
    until: float | None = None,
) -> QuarterRow:
    """Return one published row, with or without Stage A's retention verdict."""
    start = local(NORMAL, 12, minute)
    return QuarterRow(
        start=start,
        end=start + timedelta(minutes=15),
        battery_kwh=ROW_BATTERY_KWH,
        grid_authorised_kwh=ROW_GRID_AUTHORISED_KWH,
        grid_export_target_kwh=0.0,
        grid_export_caused_kwh=0.0,
        desired_grid_kw=0.15,
        retention_authorised=authorised,
        retention_gate=(
            gate
            if gate is not None
            else (
                RETENTION_GATE_AUTHORISED
                if authorised
                else RETENTION_GATE_EXPORT_SUPERIOR
            )
        ),
        retention_until_dc_kwh=until,
    )


def plan_of(*rows: QuarterRow, admitted_at=None) -> AdmittedPlan:
    """Return a frozen schedule of ``rows``."""
    return AdmittedPlan(
        plan_id="plan-1",
        revision=1,
        run_id="run-1",
        intent=EXECUTION_INTENT_GRID_CHARGE,
        purpose=EXECUTION_INTENT_GRID_CHARGE,
        admitted_at=admitted_at or (OPENS - timedelta(minutes=15)),
        rows=rows,
    )


# == 1. the open row is not re-decided ====================================


def test_a_later_publication_cannot_revoke_the_open_row_verdict() -> None:
    """**The whole gate.** A replan reaches the future and not the present.

    The row covering now was admitted authorised. A fresh publication of the same
    window refuses -- because the forecast moved, or the price did -- and the row
    already executing keeps what it opened with.

    *Mutation: derive the quarter's verdict from the live publication instead of the
    frozen row and this fails.*
    """
    opened = plan_of(row_at(0, authorised=True), row_at(15, authorised=True))
    quarter = opened.executing_quarter(OPENS + timedelta(minutes=5))
    assert quarter is not None
    assert quarter.absorption_authorised() is True

    # Stage A republishes, now refusing, and Stage B is still inside the old row.
    replanned = plan_of(row_at(0, authorised=False), row_at(15, authorised=False))

    # The row Stage B is executing is the one it admitted, and it is unchanged.
    assert quarter.absorption_authorised() is True
    assert quarter.retention_gate == RETENTION_GATE_AUTHORISED
    # While the *next* row, which has not opened, takes the new publication.
    future = replanned.executing_quarter(OPENS + timedelta(minutes=20))
    assert future is not None
    assert future.absorption_authorised() is False


def test_a_later_publication_cannot_enlarge_the_open_row_verdict_either() -> None:
    """Symmetric, and it matters as much: authority is frozen, not merely capped.

    A row admitted refused stays refused for its own quarter even if the tariff
    turns favourable mid-quarter. Stage A gets the next row to act on, which is one
    quarter of latency and the same latency the objective already has.
    """
    opened = plan_of(row_at(0, authorised=False))
    quarter = opened.executing_quarter(OPENS + timedelta(minutes=5))
    assert quarter is not None

    assert quarter.absorption_authorised() is False
    assert quarter.retention_gate == RETENTION_GATE_EXPORT_SUPERIOR


def test_the_verdict_survives_a_row_boundary_inside_one_plan() -> None:
    """Each row carries its own, because the tariff moves inside a run.

    The three cheapest quarters of the 2026-09-03 window were priced 0.153, 0.159
    and 0.160 against export prices from 0.038 to 0.090 -- the comparison genuinely
    differs row to row, so a run-level verdict would be wrong for most of its rows.
    """
    plan = plan_of(row_at(0, authorised=True), row_at(15, authorised=False))

    first = plan.executing_quarter(OPENS + timedelta(minutes=1))
    second = plan.executing_quarter(OPENS + timedelta(minutes=16))
    assert first is not None and second is not None
    assert first.absorption_authorised() is True
    assert second.absorption_authorised() is False


# == 2. the purchase caps are not inherited ==============================


def test_the_run_level_frozen_remainder_does_not_bound_the_verdict() -> None:
    """**The refusal this gate is named for.**

    ``frozen_remaining_at_admission_kwh`` bounds the *battery objective*, and it
    still does. It must not touch the retention verdict: that authority is over free
    production, it cannot buy anything, and a run whose purchase budget is spent has
    every reason to keep storing production it does not have to pay for.

    *Mutation: add the frozen remainder to ``absorption_authorised`` and this
    fails.*
    """
    row = row_at(0, authorised=True)
    # A run whose purchase allowance is entirely spent.
    quarter = admit_quarter(
        row,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        run_id="run-1",
        plan_id="plan-1",
        revision=1,
        now=OPENS - timedelta(minutes=1),
        frozen_remaining_kwh=0.0,
    )

    # The objective is bounded to nothing, exactly as beta.27 requires.
    assert quarter.battery_allowance_kwh() == 0.0
    # And the free-production authority is untouched.
    assert quarter.absorption_authorised() is True


def test_a_spent_purchase_budget_still_permits_storing_production() -> None:
    """The beta.36 promise, one layer up: unspent authorisation is not a deficit.

    This is the same claim as the arithmetic gate's, asserted where the *authority*
    lives rather than where the setpoint does.
    """
    quarter = admit_quarter(
        row_at(0, authorised=True),
        intent=EXECUTION_INTENT_GRID_CHARGE,
        run_id="run-1",
        plan_id="plan-1",
        revision=1,
        now=OPENS - timedelta(minutes=1),
        frozen_remaining_kwh=0.0,
    )
    assert quarter.absorption_authorised() is True


# == 3. the contract round trips, and absence is a refusal ===============


def test_the_verdict_round_trips_through_the_persisted_claim() -> None:
    """``parse_target(target_as_published(t)) == t``, verdict included.

    The claim record holds the whole publication so a restart meeting a live
    dispatch can reconstruct the run rather than mint a competing one. A serialiser
    that dropped the verdict would restore a row that had lost its authority to keep
    free production -- which shows up as an exporting afternoon, not as an error.
    """
    published = {
        "plan_id": "plan-1",
        "revision": 1,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "purpose": EXECUTION_INTENT_GRID_CHARGE,
        "window_start": OPENS.isoformat(),
        "window_end": (OPENS + timedelta(minutes=30)).isoformat(),
        "issued_at": (OPENS - timedelta(minutes=15)).isoformat(),
        "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
        "battery_target_kwh": 0.56,
        "quarter_schedule": [
            row_at(0, authorised=True).as_dict(),
            row_at(15, authorised=False).as_dict(),
        ],
    }
    target = parse_target(published)
    assert target is not None
    assert [row.retention_authorised for row in target.quarter_schedule] == [
        True,
        False,
    ]

    assert parse_target(target_as_published(target)) == target


def test_a_publication_without_a_verdict_reads_back_as_a_refusal() -> None:
    """**Absent is unauthorised, which is beta.39 behaviour and the safe direction.**

    Every pre-beta.40 publication and every pre-beta.40 claim record lacks the key.
    Reading absence as a grant would change what a stored row authorises across an
    upgrade, on a row nobody re-decided.
    """
    legacy = row_at(0, authorised=True).as_dict()
    del legacy["retention_authorised"]
    del legacy["retention_gate"]

    target = parse_target(
        {
            "plan_id": "plan-1",
            "revision": 1,
            "intent": EXECUTION_INTENT_GRID_CHARGE,
            "window_start": OPENS.isoformat(),
            "window_end": (OPENS + timedelta(minutes=15)).isoformat(),
            "issued_at": OPENS.isoformat(),
            "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
            "battery_target_kwh": ROW_BATTERY_KWH,
            "quarter_schedule": [legacy],
        }
    )
    assert target is not None
    row = target.quarter_schedule[0]
    assert row.retention_authorised is False
    assert row.retention_gate is None
    # And nothing else about the row moved.
    assert row.battery_kwh == ROW_BATTERY_KWH
    assert row.grid_authorised_kwh == ROW_GRID_AUTHORISED_KWH


def test_a_non_boolean_verdict_is_a_refusal_rather_than_a_truthy_grant() -> None:
    """A malformed record must not authorise anything. Totality, not trust."""
    for value in ("yes", 1, 0.5, [], {}, None):
        raw = row_at(0, authorised=True).as_dict()
        raw["retention_authorised"] = value
        target = parse_target(
            {
                "plan_id": "plan-1",
                "revision": 1,
                "intent": EXECUTION_INTENT_GRID_CHARGE,
                "window_start": OPENS.isoformat(),
                "window_end": (OPENS + timedelta(minutes=15)).isoformat(),
                "issued_at": OPENS.isoformat(),
                "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
                "battery_target_kwh": ROW_BATTERY_KWH,
                "quarter_schedule": [raw],
            }
        )
        assert target is not None
        assert target.quarter_schedule[0].retention_authorised is False, value


def test_every_published_gate_word_is_in_the_vocabulary() -> None:
    """A reason a reader cannot look up is not a reason."""
    for authorised in (True, False):
        row = row_at(0, authorised=authorised)
        assert row.retention_gate in RETENTION_GATES


def test_the_carried_quarter_publishes_its_frozen_verdict() -> None:
    """Diagnostics carry the authority, so a reader can audit a live row."""
    quarter: CarriedQuarter = plan_of(row_at(0, authorised=True)).executing_quarter(
        OPENS + timedelta(minutes=1)
    )
    assert quarter is not None
    payload = quarter.as_dict()

    assert payload["retention_authorised"] is True
    assert payload["retention_gate"] == RETENTION_GATE_AUTHORISED


# == 4. the restart, end to end ===========================================


def test_the_verdict_and_its_ceiling_survive_a_restart() -> None:
    """**The persistence claim, walked rather than inferred.**

    A restart discards the carried run and keeps the causal record, so the run a
    live dispatch belongs to is rebuilt from ``record["target"]`` by
    ``carried_from_record``. That path is the one a beta.40 verdict has to survive:
    a restart that lost it would meet a charging battery with a row that no longer
    authorised keeping production, and start exporting again mid-afternoon.

    Walked here in the order the coordinator walks it -- publish, admit, freeze,
    persist, reload, re-derive -- rather than asserting the round trip alone.

    *Mutation: drop ``retention_authorised`` from ``target_as_published`` and this
    fails at the reloaded row.*
    """
    from custom_components.alpha_ems_manager.execution import (
        admit_plan,
        carried_from_record,
    )

    published = {
        "plan_id": "plan-1",
        "revision": 3,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "purpose": EXECUTION_INTENT_GRID_CHARGE,
        "window_start": OPENS.isoformat(),
        "window_end": (OPENS + timedelta(minutes=30)).isoformat(),
        "issued_at": (OPENS - timedelta(minutes=15)).isoformat(),
        "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
        "battery_target_kwh": 0.56,
        "quarter_schedule": [
            row_at(0, authorised=True, until=12.5).as_dict(),
            row_at(15, authorised=False).as_dict(),
        ],
    }
    live = parse_target(published)
    assert live is not None

    # The record the arm persists: the whole publication, round-tripped.
    record = {
        "run_id": "run-1",
        "plan_id": "plan-1",
        "revision": 3,
        "admitted_at": (OPENS - timedelta(minutes=15)).isoformat(),
        "affirmed_at": OPENS.isoformat(),
        "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
        "target": target_as_published(live),
    }

    # ... and the restart, which has nothing else to go on.
    adopted = carried_from_record(record)
    assert adopted is not None
    reloaded = admit_plan(adopted.target, run=adopted, now=OPENS)
    assert reloaded is not None

    opened = reloaded.executing_quarter(OPENS + timedelta(minutes=5))
    assert opened is not None
    assert opened.absorption_authorised() is True
    assert opened.retention_gate == RETENTION_GATE_AUTHORISED
    assert opened.retention_until_dc_kwh == pytest.approx(12.5)
    # And the row that had not opened is still refused, on the same reload.
    later = reloaded.executing_quarter(OPENS + timedelta(minutes=20))
    assert later is not None
    assert later.absorption_authorised() is False


def test_a_beta39_record_reloads_with_no_free_production_authority() -> None:
    """**The compatibility claim the unmoved schema version rests on.**

    A record written by beta.39 carries rows without either key. beta.40 must read
    it, adopt the run, and invent no authority: absent is a refusal, and a refused
    row cannot reach the absorption branch at all.
    """
    from custom_components.alpha_ems_manager.execution import (
        admit_plan,
        carried_from_record,
    )

    legacy_row = row_at(0, authorised=True, until=12.5).as_dict()
    for key in ("retention_authorised", "retention_gate", "retention_until_dc_kwh"):
        del legacy_row[key]

    record = {
        "run_id": "run-1",
        "plan_id": "plan-1",
        "revision": 1,
        "admitted_at": (OPENS - timedelta(minutes=15)).isoformat(),
        "affirmed_at": OPENS.isoformat(),
        "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
        "target": {
            "plan_id": "plan-1",
            "revision": 1,
            "intent": EXECUTION_INTENT_GRID_CHARGE,
            "window_start": OPENS.isoformat(),
            "window_end": (OPENS + timedelta(minutes=15)).isoformat(),
            "issued_at": OPENS.isoformat(),
            "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
            "battery_target_kwh": ROW_BATTERY_KWH,
            "quarter_schedule": [legacy_row],
        },
    }

    adopted = carried_from_record(record)
    assert adopted is not None
    plan = admit_plan(adopted.target, run=adopted, now=OPENS)
    assert plan is not None
    opened = plan.executing_quarter(OPENS + timedelta(minutes=5))
    assert opened is not None

    assert opened.absorption_authorised() is False
    assert opened.retention_gate is None
    assert opened.retention_until_dc_kwh is None
    # The objective and the ceiling it does carry are untouched.
    assert opened.battery_target_kwh == pytest.approx(ROW_BATTERY_KWH)


def test_a_malformed_persisted_ceiling_is_unbounded_and_never_zero() -> None:
    """Nonsense in the ceiling must not forbid what the verdict permitted.

    Absent and unreadable both mean "no economic bound", and the physical clamps
    still apply. Reading either as zero would silently disable the feature on a
    record nobody could see was broken.
    """
    for value in ("lots", None, float("inf"), float("nan"), [], {}):
        raw = row_at(0, authorised=True, until=12.5).as_dict()
        raw["retention_until_dc_kwh"] = value
        target = parse_target(
            {
                "plan_id": "plan-1",
                "revision": 1,
                "intent": EXECUTION_INTENT_GRID_CHARGE,
                "window_start": OPENS.isoformat(),
                "window_end": (OPENS + timedelta(minutes=15)).isoformat(),
                "issued_at": OPENS.isoformat(),
                "stale_after": (OPENS + timedelta(minutes=15)).isoformat(),
                "battery_target_kwh": ROW_BATTERY_KWH,
                "quarter_schedule": [raw],
            }
        )
        assert target is not None
        row = target.quarter_schedule[0]
        assert row.retention_until_dc_kwh is None, value
        # And the verdict itself is unaffected by a broken neighbour.
        assert row.retention_authorised is True, value
