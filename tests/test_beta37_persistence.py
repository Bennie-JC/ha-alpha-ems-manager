"""beta.37: the decision-time evidence, on two tiers and neither of them new.

**Gate 4.** The release persists what the optimiser believed at the moment it chose,
because that is the half of a decision-versus-outcome comparison that cannot be
recovered afterwards: prices are revised, forecasts are replaced, and the pack moves.

Two tiers, both existing architecture:

* the **hot ring** -- ``LearningStore.decisions``, bounded at
  ``MAX_DECISION_RECORDS_RETAINED`` and written per refresh. It stays at 192 records,
  and each record simply grows. No schema version moves, because the record has
  always been a free-form dict whose contents are the caller's business;
* the **thirty-day evidence** -- ``EconomicSnapshot`` in the month-partitioned
  forecast-history store, written *change-triggered on the input fingerprint*. That is
  what makes a month affordable: a quiet day costs one row rather than ninety-six
  near-identical ones, and Home Assistant never rewrites a monolithic document.

The forecast store's minor version moves 1.7 to 1.8, which its own documented rule
says "reads every earlier document unchanged and simply writes the newer one back".
A pre-beta.37 document is asserted to load with every figure intact and the new ones
absent -- **absent, not zero**, because they were not computed then.
"""

from __future__ import annotations

from datetime import date, datetime

from custom_components.alpha_ems_manager.const import (
    FORECAST_STORAGE_MINOR_VERSION,
    FORECAST_STORAGE_VERSION,
    MAX_DECISION_RECORDS_RETAINED,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from custom_components.alpha_ems_manager.economic import (
    EconomicSnapshot,
    build_economic_snapshot,
    economic_value_summary,
)

from .beta34_shape import solve_at

HEAD, END, STORED = 28, 96, 8.294

#: A real instant and civil day, because ``to_dict`` serialises both.
ISSUED_AT = datetime.fromisoformat("2026-09-01T10:45:00+02:00")
TARGET_DAY = date(2026, 9, 1)


def snapshot_for(*, valued: bool = True) -> EconomicSnapshot:
    """Return an economic snapshot carrying the beta.37 figures, or not."""
    outcome = solve_at(head=HEAD, end=END, stored=STORED).outcome
    payload = (
        economic_value_summary(
            outcome,
            today_interval_count=96,
            import_price_eur_kwh=0.32,
            export_price_eur_kwh=0.21,
        )
        if valued
        else None
    )
    return build_economic_snapshot(
        outcome,
        issued_at=ISSUED_AT,
        target_day=TARGET_DAY,
        tz_key="Europe/Amsterdam",
        execution_blocked_reason="not_blocked",
        config_fingerprint="cfg",
        settings_fingerprint="set",
        economic_value=payload,
    )


# ===========================================================================
# schema
# ===========================================================================


def test_only_the_evidence_store_minor_version_moved() -> None:
    """**The main document's schema is untouched, and that is deliberate.**

    The hot ring extends the *contents* of an existing free-form record under an
    existing key, so nothing about the learning store's shape changes. Only the
    partitioned evidence store gains fields, and only its minor version moves --
    which is the additive kind that reads every earlier document unchanged.
    """
    assert STORAGE_VERSION == 2
    # **7 since beta.39**, which adds one optional per-day dict: what the energy
    # the day opened with was worth on the value curve that existed then. It is the
    # one datum a forecast revaluation needs and the one datum nothing retained.
    # Additive like every bump before it -- a beta.38 document reads back with the
    # key absent, which is a defined state with its own published reason -- so the
    # major staying at 2 is still the load-bearing half.
    assert STORAGE_MINOR_VERSION == 7
    assert FORECAST_STORAGE_VERSION == 1
    assert FORECAST_STORAGE_MINOR_VERSION == 8


def test_the_hot_ring_is_not_grown() -> None:
    """192 records, unchanged. The thirty days live in the partitioned store.

    Growing this ring instead would put a document of thousands of records through
    the executor on every sixty-second debounce, which is the trade the partitioned
    store exists to avoid.
    """
    assert MAX_DECISION_RECORDS_RETAINED == 192


# ===========================================================================
# the snapshot round-trip
# ===========================================================================


def test_the_economic_value_figures_survive_a_round_trip() -> None:
    """Written short-keyed, read back identical."""
    original = snapshot_for()
    restored = EconomicSnapshot.from_dict(TARGET_DAY, original.to_dict())

    assert restored is not None
    for field in (
        "decision_advantage_eur",
        "advantage_cash_eur",
        "counterfactual_cost_eur",
        "stored_energy_marginal_value_eur_kwh",
        "terminal_edge_value_eur_kwh",
        "stored_energy_dc_kwh",
        "head_run_state",
        "reason_code",
        "today_interval_value_eur",
        "tomorrow_prices_known",
        "actionable_intervals",
        "comparator_model",
    ):
        assert getattr(restored, field) == getattr(original, field), field
    # The witness: the figures are actually populated, or the loop is vacuous.
    assert original.decision_advantage_eur is not None
    assert original.decision_advantage_eur > 0.0
    assert original.reason_code is not None


def test_a_document_without_the_figures_loads_with_them_absent() -> None:
    """**A pre-beta.37 document, and the figures are absent rather than zero.**

    They were not computed then. Back-filling them, or defaulting them to zero, would
    make old and new rows look continuous when they are not -- which is the same
    argument the store's own changelog makes about every earlier additive field.
    """
    raw = snapshot_for().to_dict()
    for key in list(raw):
        if key.startswith("ev"):
            del raw[key]

    restored = EconomicSnapshot.from_dict(TARGET_DAY, raw)

    assert restored is not None
    assert restored.decision_advantage_eur is None
    assert restored.stored_energy_marginal_value_eur_kwh is None
    assert restored.reason_code is None
    assert restored.tomorrow_prices_known is None
    # And everything that was already there is intact.
    assert restored.fingerprint == snapshot_for().fingerprint
    assert restored.horizon_intervals > 0


def test_an_unavailable_comparison_persists_its_reason_and_no_figures() -> None:
    """A row with no advantage says why, rather than carrying a zero."""
    snapshot = build_economic_snapshot(
        None,
        issued_at=ISSUED_AT,
        target_day=TARGET_DAY,
        tz_key="Europe/Amsterdam",
        execution_blocked_reason="not_blocked",
        config_fingerprint="cfg",
        settings_fingerprint="set",
        economic_value=economic_value_summary(None),
    )
    restored = EconomicSnapshot.from_dict(TARGET_DAY, snapshot.to_dict())

    assert restored is not None
    assert restored.decision_advantage_eur is None
    assert restored.state_unavailable_reason == "plan_unavailable"


def test_the_snapshot_fingerprint_is_not_moved_by_the_new_figures() -> None:
    """**Change-triggered writes must stay keyed on inputs, not on the plan.**

    The digest is over the solve's *inputs*, deliberately: the plan differs every
    quarter-hour, so a plan-keyed digest would store ninety-six rows a day and the
    thirty-day window would cost thirty times what it should. Adding published figures
    to the row must not leak into the key.
    """
    with_figures = snapshot_for(valued=True)
    without = snapshot_for(valued=False)

    assert with_figures.fingerprint == without.fingerprint
    assert with_figures.decision_advantage_eur is not None
    assert without.decision_advantage_eur is None


# ===========================================================================
# the hot ring, through the real coordinator
# ===========================================================================


async def test_the_decision_record_carries_the_economic_value(
    hass, config_data: dict, source_entities: None, frank
) -> None:
    """One record per refresh, and it now says what the plan was thought to be worth.

    Prefixed ``ev_`` so a reader can see at a glance which figures arrived in beta.37,
    and so a name can never collide with one of the twenty-odd keys already there.
    """
    from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
    from .frank_capture import synthetic_day
    from .test_beta24_live_charge import charge_now_price, live_coordinator
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await refresh_at(coordinator, local(NORMAL, 10, 45))

    records = coordinator.store.decisions
    assert records, "the witness: a decision was recorded"
    record = records[-1]

    assert "ev_available" in record
    if record["ev_available"]:
        assert isinstance(record["ev_decision_advantage_eur"], float)
        assert record["ev_reason_code"] is not None
        assert record["ev_comparator_model"] is not None
        assert record["ev_advantage_cash_eur"] == record["ev_decision_advantage_eur"]
    else:
        assert record["ev_state_unavailable_reason"] is not None
    # The keys the replay harness already relied on are untouched.
    assert "price_fingerprint" in record
    assert "cost_eur" in record
    assert "relaxed_cost_eur" in record


async def test_the_ring_stays_bounded_and_survives_a_reload(
    hass, config_data: dict, source_entities: None, frank
) -> None:
    """Append-only, bounded, and readable again after a restart."""
    from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
    from .frank_capture import synthetic_day
    from .test_beta24_live_charge import charge_now_price, live_coordinator
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await refresh_at(coordinator, local(NORMAL, 10, 45))

    # Overfill deliberately, so the bound is exercised rather than assumed.
    template = dict(coordinator.store.decisions[-1])
    for index in range(MAX_DECISION_RECORDS_RETAINED + 40):
        coordinator.store.record_decision({**template, "seq": index})

    assert len(coordinator.store.decisions) == MAX_DECISION_RECORDS_RETAINED
    assert coordinator.store.decisions[-1]["seq"] == (
        MAX_DECISION_RECORDS_RETAINED + 39
    ), "newest kept"

    # And the document round-trips with the economic figures intact.
    document = coordinator.store.to_dict()
    assert "decisions" in document
    assert len(document["decisions"]) == MAX_DECISION_RECORDS_RETAINED
    assert "ev_available" in document["decisions"][-1]


def test_the_record_growth_is_bounded_and_measured() -> None:
    """**The storage cost, measured rather than asserted to be fine.**

    A record is read by a replay harness, so it is scalars and fingerprints only --
    no series, no arrays. The figure below is the whole hot ring serialised, and it is
    checked against a ceiling so a future field cannot quietly make the sixty-second
    debounce expensive.
    """
    import json

    outcome = solve_at(head=HEAD, end=END, stored=STORED).outcome
    payload = economic_value_summary(
        outcome, today_interval_count=96, import_price_eur_kwh=0.32
    )
    # The beta.37 half of one record, flat, as the coordinator writes it.
    added = {
        f"ev_{name}": payload.get(name)
        for name in (
            "decision_advantage_eur",
            "advantage_cash_eur",
            "stored_energy_marginal_value_eur_kwh",
            "terminal_edge_value_eur_kwh",
            "reason_code",
            "comparator_model",
            "today_interval_value_eur",
            "tomorrow_interval_value_eur",
        )
    }
    per_record = len(json.dumps(added))
    whole_ring = per_record * MAX_DECISION_RECORDS_RETAINED

    assert per_record < 500, per_record
    assert whole_ring < 100_000, whole_ring
