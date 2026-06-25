"""Smart Trade Prediction Engine for Alpha EMS Manager.

Calculates whether a profitable grid buy→sell trade is possible using the
learned house load and PV correction models combined with Frank dynamic prices.

This module is PREDICTION ONLY. It does not issue any write commands, does not
control the battery, and does not modify any existing learning models.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    INTERVALS_PER_DAY,
    INTERVAL_MINUTES,
    PV_DAYLIGHT_END_SLOT,
    PV_DAYLIGHT_START_SLOT,
    TRADE_CHARGE_EFFICIENCY,
    TRADE_CONFIDENCE_DAYS_TARGET,
    TRADE_DISCHARGE_EFFICIENCY,
    TRADE_MAX_CHARGE_POWER_KW,
    TRADE_MAX_DISCHARGE_POWER_KW,
    TRADE_ROUNDTRIP_EFFICIENCY,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slot / time helpers  (unchanged from v1)
# ---------------------------------------------------------------------------

def _slot_index(dt: datetime) -> int:
    """Return 0-based 15-minute slot index within the day."""
    return (dt.hour * 60 + dt.minute) // INTERVAL_MINUTES


def _parse_frank_time(time_str: str | None, ref_date: date) -> datetime | None:
    """Parse a Frank sensor time state into a timezone-aware datetime.

    Handles full ISO strings ("2024-06-25T14:00:00+02:00"), "HH:MM", and
    "HH:MM:SS" using ``ref_date`` to supply the date when only a time is given.
    """
    if not time_str:
        return None
    parsed = dt_util.parse_datetime(time_str)
    if parsed is not None:
        return parsed
    parts = time_str.split(":")
    if len(parts) >= 2:
        try:
            hour, minute = int(parts[0]), int(parts[1])
            naive = datetime(ref_date.year, ref_date.month, ref_date.day, hour, minute)
            return dt_util.as_local(naive)
        except (ValueError, TypeError):
            pass
    return None


def _extract_price_from_attrs(
    attrs: dict, target_dt: datetime | None = None
) -> float | None:
    """Try to extract an energy price (€/kWh) from Frank sensor attributes.

    Attempts multiple common attribute formats used by Frank Quarter Prices and
    similar dynamic-pricing integrations:

    1. Direct ``price``, ``tariff``, ``value``, ``current_price``, or ``rate``
       scalar attributes (used on cheapest/most-expensive time sensors).
    2. A list of period dicts under ``prices``, ``data``, ``price_data``,
       ``tariffs``, or ``forecast`` — matched by ``start``/``datetime``/``time``
       field within ±15 min of ``target_dt``.
    3. A dict keyed by "HH:MM" time strings.
    """
    if not attrs:
        return None

    for key in ("price", "tariff", "value", "current_price", "rate"):
        if key in attrs:
            try:
                return float(attrs[key])
            except (TypeError, ValueError):
                pass

    if target_dt is not None:
        target_utc = dt_util.as_utc(target_dt)
        for list_key in ("prices", "data", "price_data", "tariffs", "forecast"):
            prices_data = attrs.get(list_key)
            if isinstance(prices_data, list):
                for item in prices_data:
                    if not isinstance(item, dict):
                        continue
                    for dt_key in ("datetime", "start", "time", "from", "hour"):
                        dt_val = item.get(dt_key)
                        if not dt_val:
                            continue
                        try:
                            slot_dt = dt_util.parse_datetime(str(dt_val))
                            if slot_dt is None:
                                p = str(dt_val).split(":")
                                if len(p) >= 2:
                                    h, m = int(p[0]), int(p[1])
                                    slot_dt = dt_util.as_local(
                                        target_dt.replace(
                                            hour=h, minute=m,
                                            second=0, microsecond=0,
                                        )
                                    )
                            if slot_dt is not None:
                                diff = abs(
                                    (dt_util.as_utc(slot_dt) - target_utc).total_seconds()
                                )
                                if diff < 900:
                                    for pk in ("price", "value", "tariff", "rate"):
                                        if pk in item:
                                            return float(item[pk])
                        except (ValueError, TypeError, AttributeError, OverflowError):
                            continue
            elif isinstance(prices_data, dict):
                target_str = target_dt.strftime("%H:%M")
                for k, v in prices_data.items():
                    if target_str in str(k):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass

    return None


# ---------------------------------------------------------------------------
# Quarter-price extraction and chronological spread selection
# ---------------------------------------------------------------------------

def _extract_all_quarter_prices(
    attrs: dict,
    ref_date: date,
) -> list[tuple[datetime, float]]:
    """Extract all (slot_datetime, price) pairs from Frank price sensor attributes.

    Tries list-of-dicts format first (under 'prices', 'data', 'price_data',
    'tariffs', or 'forecast' key), then falls back to HH:MM-keyed dict.
    Returns timezone-aware local datetimes paired with their prices.
    """
    if not attrs:
        return []
    result: list[tuple[datetime, float]] = []

    for list_key in ("prices", "data", "price_data", "tariffs", "forecast"):
        prices_data = attrs.get(list_key)
        if not isinstance(prices_data, list):
            continue
        for item in prices_data:
            if not isinstance(item, dict):
                continue
            slot_dt: datetime | None = None
            for dt_key in ("datetime", "start", "time", "from", "hour"):
                dt_val = item.get(dt_key)
                if not dt_val:
                    continue
                try:
                    parsed = dt_util.parse_datetime(str(dt_val))
                    if parsed is not None:
                        slot_dt = dt_util.as_local(parsed)
                    else:
                        parts = str(dt_val).split(":")
                        if len(parts) >= 2:
                            h, m = int(parts[0]), int(parts[1])
                            naive = datetime(
                                ref_date.year, ref_date.month, ref_date.day, h, m
                            )
                            slot_dt = dt_util.as_local(naive)
                    if slot_dt is not None:
                        break
                except (ValueError, TypeError, AttributeError, OverflowError):
                    continue
            if slot_dt is None:
                continue
            for pk in ("price", "value", "tariff", "rate"):
                if pk in item:
                    try:
                        result.append((slot_dt, float(item[pk])))
                    except (TypeError, ValueError):
                        pass
                    break
        if result:
            return result

    # Fallback: HH:MM-keyed dict at the top level of attrs.
    for k, v in attrs.items():
        if not isinstance(k, str) or ":" not in k:
            continue
        parts = k.split(":")
        if len(parts) < 2:
            continue
        try:
            h, m = int(parts[0]), int(parts[1])
            naive = datetime(ref_date.year, ref_date.month, ref_date.day, h, m)
            slot_dt = dt_util.as_local(naive)
            result.append((slot_dt, float(v)))
        except (TypeError, ValueError):
            continue

    return result


def _select_best_spread_pair(
    all_prices: list[tuple[datetime, float]],
    today_date: date,
    tomorrow_date: date,
) -> tuple[
    datetime | None,
    float | None,
    datetime | None,
    float | None,
    float | None,
    dict,
]:
    """Find the chronological (buy, sell) quarter pair with the highest spread.

    Iterates every valid pair (buy_time < sell_time) in the pre-sorted price
    list and returns the pair that maximises sell_price − buy_price.

    With ≤ 192 quarter-hour slots (today remainder + tomorrow), the O(n²)
    pass is at most ~18 k iterations — negligible on any HA host.

    Returns:
        (buy_dt, buy_price, sell_dt, sell_price, best_spread, diag_dict)
    The diag_dict always contains the five spread-diagnostic keys.
    """
    empty_diag: dict = {
        "valid_spread_pairs_checked": 0,
        "rejected_non_chronological_pairs": 0,
        "best_today_trade_spread": None,
        "best_tomorrow_trade_spread": None,
        "best_cross_day_trade_spread": None,
    }
    n = len(all_prices)
    if n < 2:
        return None, None, None, None, None, empty_diag

    best_buy_dt: datetime | None = None
    best_buy_price: float | None = None
    best_sell_dt: datetime | None = None
    best_sell_price: float | None = None
    best_spread: float | None = None
    best_today: float | None = None
    best_tomorrow: float | None = None
    best_cross: float | None = None
    valid_pairs = 0

    for i in range(n):
        buy_dt_i, buy_price_i = all_prices[i]
        for j in range(i + 1, n):
            sell_dt_j, sell_price_j = all_prices[j]
            # all_prices is sorted so sell_dt_j >= buy_dt_i; strict >
            # guards against same-slot duplicates in combined list.
            if sell_dt_j <= buy_dt_i:
                continue
            valid_pairs += 1
            spread = sell_price_j - buy_price_i

            if best_spread is None or spread > best_spread:
                best_spread = spread
                best_buy_dt = buy_dt_i
                best_buy_price = buy_price_i
                best_sell_dt = sell_dt_j
                best_sell_price = sell_price_j

            bd = buy_dt_i.date()
            sd = sell_dt_j.date()
            if bd == today_date and sd == today_date:
                if best_today is None or spread > best_today:
                    best_today = spread
            elif bd == tomorrow_date and sd == tomorrow_date:
                if best_tomorrow is None or spread > best_tomorrow:
                    best_tomorrow = spread
            elif bd == today_date and sd == tomorrow_date:
                if best_cross is None or spread > best_cross:
                    best_cross = spread

    return (
        best_buy_dt,
        best_buy_price,
        best_sell_dt,
        best_sell_price,
        best_spread,
        {
            "valid_spread_pairs_checked": valid_pairs,
            "rejected_non_chronological_pairs": 0,
            "best_today_trade_spread": (
                round(best_today, 4) if best_today is not None else None
            ),
            "best_tomorrow_trade_spread": (
                round(best_tomorrow, 4) if best_tomorrow is not None else None
            ),
            "best_cross_day_trade_spread": (
                round(best_cross, 4) if best_cross is not None else None
            ),
        },
    )


# ---------------------------------------------------------------------------
# Load window helper  (unchanged from v1)
# ---------------------------------------------------------------------------

def _load_in_window(
    profile: list[float], from_slot: int, to_slot: int, spans_midnight: bool = False
) -> float:
    """Sum learned load kWh in the slot range [from_slot, to_slot).

    When ``spans_midnight`` the range wraps: from_slot..95 + 0..to_slot.
    The profile is the EV-corrected global load profile so EV charging is
    never included in predicted load.
    """
    if not spans_midnight and to_slot >= from_slot:
        return round(sum(profile[from_slot:to_slot]), 3)
    return round(sum(profile[from_slot:]) + sum(profile[:to_slot]), 3)


# ---------------------------------------------------------------------------
# Bell-curve PV distribution  (replaces uniform daylight distribution)
# ---------------------------------------------------------------------------

def _build_pv_slot_weights(
    sunrise_slot: int,
    sunset_slot: int,
    east_fraction: float | None,
) -> tuple[list[float], int]:
    """Build a 96-slot PV weight array (summing to 1.0) and return (weights, peak_slot).

    Uses a sin^1.6 bell curve centred on solar noon for a single-array roof, or
    separate east/west biased curves (sin^1.8 multiplied by an asymmetric ramp)
    when east_fraction is provided.

    East curve: weight = sin(π·pos)^1.8 · (1.2 − pos)   – peaks earlier
    West curve: weight = sin(π·pos)^1.8 · (0.2 + pos)   – peaks later
    Both are normalised individually and then combined:
        final = east_normalised · east_fraction + west_normalised · west_fraction
    """
    n = max(0, sunset_slot - sunrise_slot)
    weights = [0.0] * INTERVALS_PER_DAY

    if n == 0:
        mid = max(0, min(sunrise_slot, INTERVALS_PER_DAY - 1))
        weights[mid] = 1.0
        return weights, mid

    if east_fraction is not None and 0.0 <= east_fraction <= 1.0:
        west_fraction = 1.0 - east_fraction
        east_raw = [0.0] * n
        west_raw = [0.0] * n
        for i in range(n):
            pos = i / max(n - 1, 1)
            east_raw[i] = math.sin(math.pi * pos) ** 1.8 * (1.2 - pos)
            west_raw[i] = math.sin(math.pi * pos) ** 1.8 * (0.2 + pos)
        east_sum = sum(east_raw)
        west_sum = sum(west_raw)
        for i in range(n):
            slot = sunrise_slot + i
            if 0 <= slot < INTERVALS_PER_DAY:
                e_norm = east_raw[i] / east_sum if east_sum > 0 else 0.0
                w_norm = west_raw[i] / west_sum if west_sum > 0 else 0.0
                weights[slot] = e_norm * east_fraction + w_norm * west_fraction
    else:
        raw = [0.0] * n
        for i in range(n):
            pos = i / max(n - 1, 1)
            raw[i] = math.sin(math.pi * pos) ** 1.6
        raw_sum = sum(raw)
        for i in range(n):
            slot = sunrise_slot + i
            if 0 <= slot < INTERVALS_PER_DAY:
                weights[slot] = raw[i] / raw_sum if raw_sum > 0 else 0.0

    # Final normalisation guard (floating point safety).
    total = sum(weights)
    if total > 0 and abs(total - 1.0) > 1e-9:
        weights = [w / total for w in weights]

    peak_slot = max(range(INTERVALS_PER_DAY), key=lambda s: weights[s])
    return weights, peak_slot


def _pv_in_window_curve(
    weights: list[float],
    now_slot: int,
    from_slot: int,
    to_slot: int,
    spans_midnight: bool,
    remaining_pv_today: float,
    forecast_tomorrow: float,
) -> float:
    """Estimate PV (kWh) in [from_slot, to_slot) using the precomputed bell curve.

    ``weights`` is the 96-slot array returned by ``_build_pv_slot_weights``; it
    sums to 1.0 and represents the shape of the full day's production.

    For same-day windows the fraction of REMAINING day production that falls in
    the window is computed as::

        pv = remaining_pv_today × Σweights[max(from,now):to] / Σweights[now:]

    For windows that cross midnight, the today part is computed the same way and
    the tomorrow part uses the same curve shape applied to forecast_tomorrow.
    """
    remaining_weight = sum(weights[now_slot:])

    if not spans_midnight:
        effective_from = max(from_slot, now_slot)
        effective_to = max(to_slot, effective_from)
        window_weight = sum(weights[effective_from:effective_to])
        if remaining_weight <= 0:
            return 0.0
        return round(remaining_pv_today * min(1.0, window_weight / remaining_weight), 3)

    # Spans midnight: today portion (from_slot → end-of-day) + tomorrow (0 → to_slot).
    effective_from = max(from_slot, now_slot)
    today_weight = sum(weights[effective_from:])
    today_pv = (
        remaining_pv_today * min(1.0, today_weight / remaining_weight)
        if remaining_weight > 0
        else 0.0
    )
    tomorrow_weight = sum(weights[:to_slot])  # weights sum to 1.0 → direct fraction
    tomorrow_pv = forecast_tomorrow * tomorrow_weight

    return round(today_pv + tomorrow_pv, 3)


# ---------------------------------------------------------------------------
# Trade Prediction Learning Model  (unchanged from v1)
# ---------------------------------------------------------------------------

@dataclass
class TradePredictionLearningModel:
    """Tracks how many days the trade engine has been running and its confidence."""

    trade_prediction_days: int = 0
    trade_prediction_success_count: int = 0
    trade_prediction_miss_count: int = 0
    trade_prediction_confidence: float = 0.0
    trade_prediction_status: str = "learning"
    current_date: str | None = None
    last_update: str | None = None
    learned_dates: list[str] = field(default_factory=list)

    def _update_status(self) -> None:
        days = self.trade_prediction_days
        if days < 3:
            self.trade_prediction_status = "learning"
        elif days < 7:
            self.trade_prediction_status = "improving"
        elif days < TRADE_CONFIDENCE_DAYS_TARGET:
            self.trade_prediction_status = "good"
        else:
            self.trade_prediction_status = "excellent"
        self.trade_prediction_confidence = round(
            min(100.0, days / TRADE_CONFIDENCE_DAYS_TARGET * 100), 1
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_prediction_days": self.trade_prediction_days,
            "trade_prediction_success_count": self.trade_prediction_success_count,
            "trade_prediction_miss_count": self.trade_prediction_miss_count,
            "trade_prediction_confidence": self.trade_prediction_confidence,
            "trade_prediction_status": self.trade_prediction_status,
            "current_date": self.current_date,
            "last_update": self.last_update,
            "learned_dates": self.learned_dates,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TradePredictionLearningModel:
        if not data:
            return cls()
        return cls(
            trade_prediction_days=data.get("trade_prediction_days", 0),
            trade_prediction_success_count=data.get("trade_prediction_success_count", 0),
            trade_prediction_miss_count=data.get("trade_prediction_miss_count", 0),
            trade_prediction_confidence=data.get("trade_prediction_confidence", 0.0),
            trade_prediction_status=data.get("trade_prediction_status", "learning"),
            current_date=data.get("current_date"),
            last_update=data.get("last_update"),
            learned_dates=data.get("learned_dates", []),
        )


# ---------------------------------------------------------------------------
# Main computation entry point
# ---------------------------------------------------------------------------

def compute_trade_prediction(  # noqa: C901 (intentionally long for clarity)
    now: datetime,
    # Battery state.
    battery_current_kwh: float | None,
    battery_capacity_kwh: float,
    battery_capacity_source: str,
    # Load profile (EV-corrected by the learning model).
    global_load_profile: list[float],
    learning_confidence: float,
    # PV data.
    expected_remaining_pv_today: float,
    corrected_forecast_tomorrow: float | None,
    pv_east_kwh: float | None,
    pv_west_kwh: float | None,
    # Sun entity for accurate sunrise/sunset (both may be None → fallback).
    sunrise_slot: int | None,
    sunset_slot: int | None,
    # Reserve parameters.
    reserve_floor_kwh: float,
    reserve_correction_factor: float,
    # Frank price data.
    frank_cheapest_time_today: str | None,
    frank_cheapest_time_tomorrow: str | None,
    frank_most_expensive_time_today: str | None,
    frank_most_expensive_time_tomorrow: str | None,
    frank_cheapest_today_attrs: dict,
    frank_cheapest_tomorrow_attrs: dict,
    frank_expensive_today_attrs: dict,
    frank_expensive_tomorrow_attrs: dict,
    frank_prices_today_attrs: dict,
    frank_prices_tomorrow_attrs: dict,
    frank_price_today: float | None,
    frank_price_tomorrow: float | None,
    # Minimum spread threshold.
    minimum_spread: float | None,
    # Mutable learning model (day-rollover is tracked here).
    trade_model: TradePredictionLearningModel,
) -> tuple[dict[str, Any], bool]:
    """Compute trade predictions for ``now``.

    Returns ``(result_dict, model_changed)`` where ``result_dict`` contains all
    trade-related keys to merge into coordinator data and ``model_changed`` is
    ``True`` when ``trade_model`` was mutated (day rollover) and should be saved.

    The global_load_profile is the EV-corrected learned load — EV charging never
    inflates any predicted load or safety buy values.
    """
    model_changed = False
    tomorrow_date = now.date() + timedelta(days=1)
    forecast_tomorrow = corrected_forecast_tomorrow or 0.0

    # --- Build PV bell-curve -------------------------------------------------
    # Determine sunrise/sunset slots (sun entity preferred, fallback to constants).
    sr_slot = sunrise_slot if sunrise_slot is not None else PV_DAYLIGHT_START_SLOT
    ss_slot = sunset_slot if sunset_slot is not None else PV_DAYLIGHT_END_SLOT
    # Guard against inverted or degenerate values.
    if ss_slot <= sr_slot:
        sr_slot = PV_DAYLIGHT_START_SLOT
        ss_slot = PV_DAYLIGHT_END_SLOT

    # Determine east/west fraction from actual production sensors.
    east_fraction: float | None = None
    east_used = False
    west_used = False
    pv_mode: str

    east_val = pv_east_kwh or 0.0
    west_val = pv_west_kwh or 0.0
    ew_total = east_val + west_val

    if (
        pv_east_kwh is not None
        and pv_west_kwh is not None
        and ew_total > 0.5
    ):
        east_fraction = east_val / ew_total
        east_used = True
        west_used = True
        pv_mode = "east_west_curve"
    elif sunrise_slot is not None:
        pv_mode = "sun_curve"
    else:
        pv_mode = "fallback_fixed_daylight"

    pv_weights, pv_peak_slot = _build_pv_slot_weights(sr_slot, ss_slot, east_fraction)
    daylight_slots = ss_slot - sr_slot

    _LOGGER.debug(
        "PV curve: mode=%s sunrise_slot=%s sunset_slot=%s daylight_slots=%s "
        "peak_slot=%s east_fraction=%s",
        pv_mode, sr_slot, ss_slot, daylight_slots, pv_peak_slot, east_fraction,
    )

    # --- Default result dict -------------------------------------------------
    result: dict[str, Any] = {
        "trade_found": False,
        "trade_executable": False,
        "trade_block_reason": "insufficient_data",
        "trade_possible": False,
        "available_energy_after_reserve": None,
        "battery_at_sell_target": None,
        "expected_pv_between_buy_and_sell": None,
        "expected_load_between_buy_and_sell": None,
        "available_battery_space_kwh": None,
        "max_buy_possible_kwh": None,
        "buy_to_full_reason": None,
        "predicted_buy_kwh": None,
        "predicted_buy_time": None,
        "predicted_buy_price": None,
        "predicted_sell_kwh": None,
        "predicted_sell_time": None,
        "predicted_sell_price": None,
        "predicted_profit": None,
        "gross_profit": None,
        "efficiency_loss_kwh": None,
        "required_reserve_after_sell": None,
        "expected_battery_at_sell": None,
        "expected_battery_at_buy": battery_current_kwh,
        "battery_can_reach_full_before_sell": None,
        "predicted_missing_kwh_for_full": None,
        "buy_limited_by_charge_power": None,
        "sell_limited_by_discharge_power": None,
        "safety_buy_needed": False,
        "safety_buy_kwh": 0.0,
        "safety_buy_time": None,
        "safety_buy_reason": "insufficient_data",
        "next_buy_source": "today_fallback",
        "expected_pv_until_sell": None,
        "expected_load_until_sell": None,
        "expected_pv_until_buy": None,
        "expected_load_until_buy": None,
        "expected_pv_sell_to_next_buy": None,
        "expected_load_sell_to_next_buy": None,
        "buy_cost": None,
        "sell_income": None,
        # Battery capacity meta.
        "battery_capacity_source": battery_capacity_source,
        # Power / efficiency constants for diagnostics.
        "max_charge_power_kw": TRADE_MAX_CHARGE_POWER_KW,
        "max_discharge_power_kw": TRADE_MAX_DISCHARGE_POWER_KW,
        "max_buy_kwh_per_quarter": round(TRADE_MAX_CHARGE_POWER_KW * 0.25, 3),
        "max_sell_kwh_per_quarter": round(TRADE_MAX_DISCHARGE_POWER_KW * 0.25, 3),
        "charge_efficiency": TRADE_CHARGE_EFFICIENCY,
        "discharge_efficiency": TRADE_DISCHARGE_EFFICIENCY,
        "roundtrip_efficiency": TRADE_ROUNDTRIP_EFFICIENCY,
        # PV curve diagnostics.
        "pv_distribution_mode": pv_mode,
        "pv_sunrise_slot": sr_slot,
        "pv_sunset_slot": ss_slot,
        "pv_daylight_slots": daylight_slots,
        "pv_curve_peak_slot": pv_peak_slot,
        "pv_east_used": east_used,
        "pv_west_used": west_used,
        # EV exclusion confirmation.
        "ev_exclusion_used": True,
        # Trade learning model.
        "trade_prediction_days": trade_model.trade_prediction_days,
        "trade_prediction_confidence": trade_model.trade_prediction_confidence,
        "trade_prediction_status": trade_model.trade_prediction_status,
        # Spread selection diagnostics (populated after price extraction).
        "trade_spread": None,
        "spread_selection_mode": "chronological_quarter_pairs",
        "valid_spread_pairs_checked": 0,
        "rejected_non_chronological_pairs": 0,
        "best_today_trade_spread": None,
        "best_tomorrow_trade_spread": None,
        "best_cross_day_trade_spread": None,
    }

    # --- Day rollover --------------------------------------------------------
    today_str = now.date().isoformat()
    if trade_model.current_date is not None and trade_model.current_date != today_str:
        prev = trade_model.current_date
        if prev not in trade_model.learned_dates:
            trade_model.learned_dates.append(prev)
        trade_model.trade_prediction_days = len(trade_model.learned_dates)
        trade_model._update_status()
        model_changed = True
        _LOGGER.debug(
            "Trade model day rollover: days=%s status=%s",
            trade_model.trade_prediction_days,
            trade_model.trade_prediction_status,
        )
    trade_model.current_date = today_str
    trade_model.last_update = now.isoformat()
    result["trade_prediction_days"] = trade_model.trade_prediction_days
    result["trade_prediction_confidence"] = trade_model.trade_prediction_confidence
    result["trade_prediction_status"] = trade_model.trade_prediction_status

    # --- Input guard ---------------------------------------------------------
    if battery_current_kwh is None:
        result["safety_buy_reason"] = "insufficient_data"
        return result, model_changed

    now_slot = _slot_index(now)
    today_date = now.date()

    # --- Extract quarter-hour prices and select best chronological pair ------
    #
    # Primary: build a sorted list of all future quarter-hour (datetime, price)
    # slots from the Frank full-price sensors and find the (buy, sell) pair
    # that maximises spread while guaranteeing buy_time < sell_time.
    #
    # Fallback: when the full-price list is unavailable, revert to the two
    # Frank time-point sensors (cheapest / most-expensive), which gives a
    # degenerate 1-pair evaluation with the old chronological guard.
    # ---------------------------------------------------------------------------

    prices_today_raw = _extract_all_quarter_prices(frank_prices_today_attrs, today_date)
    prices_today = [(dt, p) for dt, p in prices_today_raw if dt > now]
    prices_tomorrow = _extract_all_quarter_prices(frank_prices_tomorrow_attrs, tomorrow_date)
    all_prices = sorted(prices_today + prices_tomorrow, key=lambda x: x[0])

    buy_dt: datetime | None = None
    buy_price: float | None = None
    sell_dt: datetime | None = None
    sell_price: float | None = None
    best_spread: float | None = None
    next_buy_source = "insufficient_data"
    safety_buy_dt: datetime | None = None
    spread_selection_mode: str

    if len(all_prices) >= 2:
        spread_selection_mode = "chronological_quarter_pairs"
        buy_dt, buy_price, sell_dt, sell_price, best_spread, spread_diag = (
            _select_best_spread_pair(all_prices, today_date, tomorrow_date)
        )
        if buy_dt is not None:
            next_buy_source = (
                "tomorrow_prices" if buy_dt.date() == tomorrow_date else "today_prices"
            )
        # Safety buy uses the globally cheapest future quarter for scheduling.
        safety_buy_dt = min(all_prices, key=lambda x: x[1])[0]
    else:
        # Fallback: only Frank time-point sensors are available — evaluate the
        # single (cheapest, most-expensive) pair with a chronological guard.
        spread_selection_mode = "fallback_two_point"
        spread_diag = {
            "valid_spread_pairs_checked": 0,
            "rejected_non_chronological_pairs": 0,
            "best_today_trade_spread": None,
            "best_tomorrow_trade_spread": None,
            "best_cross_day_trade_spread": None,
        }
        if frank_cheapest_time_tomorrow:
            buy_dt = _parse_frank_time(frank_cheapest_time_tomorrow, tomorrow_date)
            if buy_dt is not None:
                next_buy_source = "tomorrow_prices"
                buy_price = _extract_price_from_attrs(frank_cheapest_tomorrow_attrs, buy_dt)
                if buy_price is None:
                    buy_price = _extract_price_from_attrs(frank_prices_tomorrow_attrs, buy_dt)
                if buy_price is None:
                    buy_price = frank_price_tomorrow
        if buy_dt is None and frank_cheapest_time_today:
            cand = _parse_frank_time(frank_cheapest_time_today, today_date)
            if cand is not None and cand > now:
                buy_dt = cand
                next_buy_source = "today_fallback"
                buy_price = _extract_price_from_attrs(frank_cheapest_today_attrs, buy_dt)
                if buy_price is None:
                    buy_price = _extract_price_from_attrs(frank_prices_today_attrs, buy_dt)
                if buy_price is None:
                    buy_price = frank_price_today
        sell_candidates: list[tuple[datetime, float | None]] = []
        if frank_most_expensive_time_today:
            cand = _parse_frank_time(frank_most_expensive_time_today, today_date)
            if cand is not None and cand > now:
                p = _extract_price_from_attrs(frank_expensive_today_attrs, cand)
                if p is None:
                    p = _extract_price_from_attrs(frank_prices_today_attrs, cand)
                sell_candidates.append((cand, p))
        if frank_most_expensive_time_tomorrow:
            cand = _parse_frank_time(frank_most_expensive_time_tomorrow, tomorrow_date)
            if cand is not None:
                p = _extract_price_from_attrs(frank_expensive_tomorrow_attrs, cand)
                if p is None:
                    p = _extract_price_from_attrs(frank_prices_tomorrow_attrs, cand)
                sell_candidates.append((cand, p))
        for cand_dt, cand_price in sell_candidates:
            if buy_dt is not None and cand_dt <= buy_dt:
                continue
            if sell_dt is None:
                sell_dt, sell_price = cand_dt, cand_price
            elif (
                cand_price is not None
                and sell_price is not None
                and cand_price > sell_price
            ):
                sell_dt, sell_price = cand_dt, cand_price
        best_spread = (
            (sell_price - buy_price)
            if sell_price is not None and buy_price is not None
            else None
        )
        safety_buy_dt = buy_dt

    # Publish resolved pair + spread diagnostics into result.
    result["spread_selection_mode"] = spread_selection_mode
    result["next_buy_source"] = next_buy_source
    result.update(spread_diag)
    if buy_dt is not None:
        result["predicted_buy_time"] = buy_dt.isoformat()
        result["predicted_buy_price"] = (
            round(buy_price, 4) if buy_price is not None else None
        )
    if sell_dt is not None:
        result["predicted_sell_time"] = sell_dt.isoformat()
        result["predicted_sell_price"] = (
            round(sell_price, 4) if sell_price is not None else None
        )
    if best_spread is not None:
        result["trade_spread"] = round(best_spread, 4)

    _LOGGER.debug(
        "Spread selection: mode=%s pairs_checked=%d buy=%s@%.4f "
        "sell=%s@%.4f spread=%.4f",
        spread_selection_mode,
        spread_diag["valid_spread_pairs_checked"],
        buy_dt.isoformat() if buy_dt else "None",
        buy_price or 0.0,
        sell_dt.isoformat() if sell_dt else "None",
        sell_price or 0.0,
        best_spread or 0.0,
    )

    # --- Minimum spread check ------------------------------------------------
    if minimum_spread is None or buy_price is None or sell_price is None:
        result["trade_block_reason"] = "insufficient_data"
        result["safety_buy_reason"] = "insufficient_data"
        _compute_safety_buy(
            result, now_slot, safety_buy_dt, battery_current_kwh, battery_capacity_kwh,
            global_load_profile, pv_weights,
            expected_remaining_pv_today, forecast_tomorrow,
            reserve_floor_kwh, reserve_correction_factor, learning_confidence,
            tomorrow_date,
        )
        return result, model_changed

    # --- Trade found: depends only on spread, not on battery/reserve/PV -----
    trade_found = (
        best_spread is not None
        and best_spread >= minimum_spread
        and buy_dt is not None
        and sell_dt is not None
    )
    result["trade_found"] = trade_found

    if not trade_found:
        if best_spread is None or buy_dt is None or sell_dt is None:
            block_reason = "no_valid_spread"
        else:
            block_reason = "spread_below_minimum"
        result["trade_block_reason"] = block_reason
        result["trade_executable"] = False
        result["trade_possible"] = False
        _compute_safety_buy(
            result, now_slot, safety_buy_dt, battery_current_kwh, battery_capacity_kwh,
            global_load_profile, pv_weights,
            expected_remaining_pv_today, forecast_tomorrow,
            reserve_floor_kwh, reserve_correction_factor, learning_confidence,
            tomorrow_date,
        )
        return result, model_changed

    # --- Trade calculation (always runs when trade_found = True) -------------
    # buy_dt and sell_dt are guaranteed non-None here (trade_found assertion).
    sell_slot = _slot_index(sell_dt)
    spans_to_sell = sell_dt.date() > now.date()

    exp_load_until_sell = _load_in_window(
        global_load_profile, now_slot, sell_slot, spans_to_sell
    )
    exp_pv_until_sell = _pv_in_window_curve(
        pv_weights, now_slot, now_slot, sell_slot, spans_to_sell,
        expected_remaining_pv_today, forecast_tomorrow,
    )

    batt_at_sell_no_buy = (
        battery_current_kwh + exp_pv_until_sell - exp_load_until_sell
    )

    # ------------------------------------------------------------------
    # Grid vs battery energy distinction.
    #
    # required_buy_battery = battery energy needed to reach 100% at sell
    # required_buy_grid    = grid energy to purchase (÷ charge_efficiency)
    # max_buy_grid         = max grid energy purchasable before sell
    # predicted_buy_kwh    = GRID energy purchased (sensor value)
    # battery_energy_added = energy actually stored (× charge_efficiency)
    # ------------------------------------------------------------------
    required_buy_battery = max(
        0.0, battery_capacity_kwh - batt_at_sell_no_buy
    )
    required_buy_grid = required_buy_battery / TRADE_CHARGE_EFFICIENCY

    if not spans_to_sell:
        quarters_until_sell = max(0, sell_slot - now_slot)
    else:
        quarters_until_sell = max(0, (INTERVALS_PER_DAY - now_slot) + sell_slot)

    max_buy_grid = quarters_until_sell * TRADE_MAX_CHARGE_POWER_KW * 0.25
    predicted_buy_kwh = round(min(required_buy_grid, max_buy_grid), 3)
    battery_energy_added = predicted_buy_kwh * TRADE_CHARGE_EFFICIENCY

    can_reach_full = required_buy_grid <= max_buy_grid
    missing_for_full = (
        round(max(0.0, required_buy_grid - max_buy_grid), 3)
        if not can_reach_full
        else 0.0
    )
    buy_limited = not can_reach_full

    # Battery energy at sell (clamped to capacity).
    exp_batt_at_sell = round(
        max(0.0, min(batt_at_sell_no_buy + battery_energy_added, battery_capacity_kwh)),
        3,
    )

    # Grid energy available from selling (discharge efficiency applied).
    effective_sell_energy = exp_batt_at_sell * TRADE_DISCHARGE_EFFICIENCY

    # ------------------------------------------------------------------
    # Reserve from sell to next cheapest buy (which is buy_dt itself).
    # ------------------------------------------------------------------
    next_buy_slot = _slot_index(buy_dt)
    spans_sell_to_buy = buy_dt.date() > sell_dt.date()

    exp_load_sell_to_buy = _load_in_window(
        global_load_profile, sell_slot, next_buy_slot, spans_sell_to_buy
    )
    remaining_after_sell = max(
        0.0, expected_remaining_pv_today - exp_pv_until_sell
    )
    pv_sell_to_buy = _pv_in_window_curve(
        pv_weights, sell_slot, sell_slot, next_buy_slot, spans_sell_to_buy,
        remaining_after_sell, forecast_tomorrow,
    )

    safety_margin_sell = (
        exp_load_sell_to_buy * (1 - learning_confidence / 100) * 0.25
    )
    req_reserve_after_sell = max(
        reserve_floor_kwh,
        exp_load_sell_to_buy * reserve_correction_factor
        - pv_sell_to_buy
        + reserve_floor_kwh
        + safety_margin_sell,
    )

    predicted_sell_kwh = round(
        max(0.0, effective_sell_energy - req_reserve_after_sell), 3
    )

    # Check if the intended sell can be discharged in time.
    sell_slot_distance = 1  # sell happens AT the sell quarter
    max_sell_grid = sell_slot_distance * TRADE_MAX_DISCHARGE_POWER_KW * 0.25
    sell_limited = predicted_sell_kwh > max_sell_grid

    # ------------------------------------------------------------------
    # Profit accounting (grid energy × price; efficiency already baked in)
    # ------------------------------------------------------------------
    buy_cost = round(predicted_buy_kwh * buy_price, 4)
    sell_income = round(predicted_sell_kwh * sell_price, 4)
    predicted_profit = round(sell_income - buy_cost, 4)

    # Gross profit = what we'd earn if round-trip efficiency were 100%.
    gross_profit = round(predicted_buy_kwh * (sell_price - buy_price), 4)

    # Total efficiency loss in kWh.
    charge_loss = predicted_buy_kwh * (1.0 - TRADE_CHARGE_EFFICIENCY)
    discharge_loss = exp_batt_at_sell * (1.0 - TRADE_DISCHARGE_EFFICIENCY)
    efficiency_loss_kwh = round(charge_loss + discharge_loss, 3)

    # ------------------------------------------------------------------
    # Buy-to-full attributes (always computed when trade_found).
    # ------------------------------------------------------------------
    available_battery_space = max(0.0, battery_capacity_kwh - battery_current_kwh)

    # PV and load specifically between buy window and sell window.
    buy_slot_btw = _slot_index(buy_dt)
    spans_buy_to_sell = sell_dt.date() > buy_dt.date()
    exp_load_buy_to_sell = _load_in_window(
        global_load_profile, buy_slot_btw, sell_slot, spans_buy_to_sell
    )
    exp_pv_buy_to_sell = _pv_in_window_curve(
        pv_weights, now_slot, buy_slot_btw, sell_slot, spans_buy_to_sell,
        expected_remaining_pv_today, forecast_tomorrow,
    )

    if available_battery_space <= 0.05:
        buy_to_full_reason = "already_full_enough"
    elif exp_pv_buy_to_sell >= available_battery_space:
        buy_to_full_reason = "solar_will_fill_battery"
    elif not can_reach_full:
        buy_to_full_reason = "charge_power_limited"
    elif predicted_buy_kwh > 0:
        buy_to_full_reason = "buy_needed_to_reach_full"
    else:
        buy_to_full_reason = "already_full_enough"

    # ------------------------------------------------------------------
    # Trade executable: trade_found AND sell energy is available.
    # ------------------------------------------------------------------
    available_energy_after_reserve_raw = effective_sell_energy - req_reserve_after_sell

    if req_reserve_after_sell > battery_capacity_kwh:
        trade_block_reason = "reserve_after_sell_too_high"
        trade_executable = False
    elif predicted_sell_kwh <= 0:
        trade_block_reason = "predicted_sell_energy_zero"
        trade_executable = False
    else:
        trade_block_reason = "none"
        trade_executable = True

    result.update({
        "predicted_buy_kwh": predicted_buy_kwh,
        "predicted_sell_kwh": predicted_sell_kwh,
        "predicted_profit": predicted_profit,
        "gross_profit": gross_profit,
        "efficiency_loss_kwh": efficiency_loss_kwh,
        "required_reserve_after_sell": round(req_reserve_after_sell, 3),
        "expected_battery_at_sell": exp_batt_at_sell,
        "battery_can_reach_full_before_sell": can_reach_full,
        "predicted_missing_kwh_for_full": missing_for_full,
        "buy_limited_by_charge_power": buy_limited,
        "sell_limited_by_discharge_power": sell_limited,
        "expected_pv_until_sell": round(exp_pv_until_sell, 3),
        "expected_load_until_sell": round(exp_load_until_sell, 3),
        "expected_pv_sell_to_next_buy": round(pv_sell_to_buy, 3),
        "expected_load_sell_to_next_buy": round(exp_load_sell_to_buy, 3),
        "buy_cost": buy_cost,
        "sell_income": sell_income,
        # New trade status fields.
        "trade_executable": trade_executable,
        "trade_possible": trade_executable,
        "trade_block_reason": trade_block_reason,
        "available_energy_after_reserve": round(available_energy_after_reserve_raw, 3),
        # Buy-to-full diagnostics.
        "battery_at_sell_target": round(battery_capacity_kwh, 3),
        "available_battery_space_kwh": round(available_battery_space, 3),
        "max_buy_possible_kwh": round(max_buy_grid, 3),
        "expected_pv_between_buy_and_sell": round(exp_pv_buy_to_sell, 3),
        "expected_load_between_buy_and_sell": round(exp_load_buy_to_sell, 3),
        "buy_to_full_reason": buy_to_full_reason,
    })

    _LOGGER.debug(
        "Trade: buy_grid=%.3f kWh @ %.4f  sell=%.3f kWh @ %.4f  "
        "profit=%.4f  gross=%.4f  eff_loss=%.3f kWh  "
        "trade_found=%s trade_executable=%s block=%s",
        predicted_buy_kwh, buy_price,
        predicted_sell_kwh, sell_price,
        predicted_profit, gross_profit, efficiency_loss_kwh,
        trade_found, trade_executable, trade_block_reason,
    )

    # --- Safety buy (always evaluated, uses cheapest future quarter) --------
    _compute_safety_buy(
        result, now_slot, safety_buy_dt, battery_current_kwh, battery_capacity_kwh,
        global_load_profile, pv_weights,
        expected_remaining_pv_today, forecast_tomorrow,
        reserve_floor_kwh, reserve_correction_factor, learning_confidence,
        tomorrow_date,
    )

    return result, model_changed


def _compute_safety_buy(
    result: dict[str, Any],
    now_slot: int,
    buy_dt: datetime | None,
    battery_current_kwh: float,
    battery_capacity_kwh: float,
    global_load_profile: list[float],
    pv_weights: list[float],
    remaining_pv_today: float,
    forecast_tomorrow: float,
    reserve_floor_kwh: float,
    reserve_correction_factor: float,
    learning_confidence: float,
    tomorrow_date: date,
) -> None:
    """Compute safety buy requirements and update *result* in place."""
    if buy_dt is None:
        result["safety_buy_reason"] = (
            "waiting_for_tomorrow_prices"
            if result.get("next_buy_source") == "today_fallback"
            else "insufficient_data"
        )
        return

    buy_slot = _slot_index(buy_dt)
    spans = buy_dt.date() >= tomorrow_date

    exp_load_until_buy = _load_in_window(
        global_load_profile, now_slot, buy_slot, spans
    )
    exp_pv_until_buy = _pv_in_window_curve(
        pv_weights, now_slot, now_slot, buy_slot, spans,
        remaining_pv_today, forecast_tomorrow,
    )

    result["expected_pv_until_buy"] = round(exp_pv_until_buy, 3)
    result["expected_load_until_buy"] = round(exp_load_until_buy, 3)

    safety_margin = exp_load_until_buy * (1 - learning_confidence / 100) * 0.25
    required_energy = max(
        reserve_floor_kwh,
        exp_load_until_buy * reserve_correction_factor
        - exp_pv_until_buy
        + reserve_floor_kwh
        + safety_margin,
    )
    safety_buy_kwh = max(0.0, required_energy - battery_current_kwh)

    if exp_pv_until_buy >= exp_load_until_buy and safety_buy_kwh == 0.0:
        result["safety_buy_needed"] = False
        result["safety_buy_kwh"] = 0.0
        result["safety_buy_reason"] = "enough_solar_expected"
    elif battery_current_kwh >= required_energy:
        result["safety_buy_needed"] = False
        result["safety_buy_kwh"] = 0.0
        result["safety_buy_reason"] = "enough_battery"
    else:
        result["safety_buy_needed"] = True
        result["safety_buy_kwh"] = round(safety_buy_kwh, 3)
        result["safety_buy_time"] = buy_dt.isoformat()
        result["safety_buy_reason"] = "battery_below_required_reserve"

    _LOGGER.debug(
        "Safety buy: needed=%s kwh=%.3f reason=%s "
        "(load=%.3f pv=%.3f required=%.3f battery=%.3f)",
        result["safety_buy_needed"],
        result["safety_buy_kwh"],
        result["safety_buy_reason"],
        exp_load_until_buy,
        exp_pv_until_buy,
        required_energy,
        battery_current_kwh,
    )
