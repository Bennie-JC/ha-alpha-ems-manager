"""Data update coordinator for Alpha EMS Manager.

The coordinator is responsible for:

* Reading the configured source sensors (house load, PV, prices, battery).
* Learning the household load profile per 15-minute interval, bucketed by
  season and by weekday/weekend.
* Persisting the learned profile so it survives restarts.
* Combining the learned load with PV forecasts and battery state to estimate
  the reserve energy required between the next sell window and the next buy
  window.

This is a scaffold: the learning and reserve maths are intentionally simple and
self-contained so they can be refined later. No AlphaESS write commands are
performed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY_KWH_ENTITY,
    CONF_BATTERY_CURRENT_KWH_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CUMULATIVE_HOUSE_LOAD_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR,
    CONF_FRANK_PRICES_TODAY_SENSOR,
    CONF_FRANK_PRICES_TOMORROW_SENSOR,
    CONF_PV_ACTUAL_TODAY_SENSOR,
    CONF_PV_EAST_SENSOR,
    CONF_PV_FORECAST_TODAY_SENSOR,
    CONF_PV_FORECAST_TOMORROW_SENSOR,
    CONF_PV_WEST_SENSOR,
    CONFIDENCE_DAYS_TARGET,
    CONFIDENCE_UPDATES_TARGET,
    DOMAIN,
    GLOBAL_PROFILE_KEY,
    INTERVAL_MINUTES,
    INTERVALS_PER_DAY,
    LEARNING_ALPHA,
    MAX_QUARTER_DELTA_KWH,
    MIN_CONFIDENT_SLOTS,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


def _season_for(moment: datetime) -> str:
    """Return a coarse meteorological season label for the given moment."""
    month = moment.month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _day_type_for(moment: datetime) -> str:
    """Return 'weekend' for Saturday/Sunday, otherwise 'weekday'."""
    return "weekend" if moment.weekday() >= 5 else "weekday"


def _interval_index(moment: datetime) -> int:
    """Return the 0-based 15-minute interval index within the day."""
    return (moment.hour * 60 + moment.minute) // INTERVAL_MINUTES


def _profile_key(season: str, day_type: str) -> str:
    """Return the storage key for a season/day-type profile."""
    return f"{season}_{day_type}"


@dataclass
class LearningModel:
    """Container for learned per-interval load profiles.

    ``profiles`` maps a profile key to a list of ``INTERVALS_PER_DAY`` floats,
    each representing the learned energy (kWh) consumed during that 15-minute
    interval. A ``"global"`` profile aggregates everything; season/day-type
    profiles are maintained alongside it for future, more granular planning.
    """

    profiles: dict[str, list[float]] = field(default_factory=dict)
    # Baseline tracking for cumulative-delta calculation.
    previous_house_load: float | None = None
    previous_slot: int | None = None
    previous_update_time: str | None = None
    # Diagnostics / debug bookkeeping for the most recent learning cycle.
    last_raw_delta: float | None = None
    last_delta_per_slot: float | None = None
    distributed_slots: list[int] = field(default_factory=list)
    last_learned_slots: list[int] = field(default_factory=list)
    last_update: str | None = None
    update_count: int = 0
    # Distinct calendar dates on which a valid sample was learned.
    learned_dates: list[str] = field(default_factory=list)

    @property
    def learned_days(self) -> int:
        """Return the number of distinct days with learned data."""
        return len(self.learned_dates)

    def profile(self, key: str) -> list[float]:
        """Return (creating if needed) the profile for a given key."""
        if key not in self.profiles:
            self.profiles[key] = [0.0] * INTERVALS_PER_DAY
        return self.profiles[key]

    def global_profile(self) -> list[float]:
        """Return the aggregate (global) learned profile."""
        return self.profile(GLOBAL_PROFILE_KEY)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the model for persistent storage."""
        return {
            "profiles": self.profiles,
            "previous_house_load": self.previous_house_load,
            "previous_slot": self.previous_slot,
            "previous_update_time": self.previous_update_time,
            "last_raw_delta": self.last_raw_delta,
            "last_delta_per_slot": self.last_delta_per_slot,
            "distributed_slots": self.distributed_slots,
            "last_learned_slots": self.last_learned_slots,
            "last_update": self.last_update,
            "update_count": self.update_count,
            "learned_dates": self.learned_dates,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LearningModel:
        """Restore a model from persistent storage.

        Falls back to the previous storage schema keys
        (``last_cumulative_value``/``last_interval_index``) so existing learned
        data is not lost on upgrade.
        """
        if not data:
            return cls()
        return cls(
            profiles=data.get("profiles", {}),
            previous_house_load=data.get(
                "previous_house_load", data.get("last_cumulative_value")
            ),
            previous_slot=data.get(
                "previous_slot", data.get("last_interval_index")
            ),
            previous_update_time=data.get("previous_update_time"),
            last_raw_delta=data.get("last_raw_delta", data.get("last_delta")),
            last_delta_per_slot=data.get("last_delta_per_slot"),
            distributed_slots=data.get("distributed_slots", []),
            last_learned_slots=data.get("last_learned_slots", []),
            last_update=data.get("last_update"),
            update_count=data.get("update_count", 0),
            learned_dates=data.get("learned_dates", []),
        )


class AlphaEmsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate state reading, learning and reserve calculation."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator.

        No fixed ``update_interval`` is used. Instead the coordinator is driven
        by a wall-clock listener (see :meth:`async_setup_quarter_hour_tracking`)
        so that learning runs exactly at minutes 0, 15, 30 and 45 and quarter
        deltas are attributed to the correct wall-clock quarter.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.model = LearningModel()
        self.storage_loaded = False
        self.storage_saved = False

    @callback
    def async_setup_quarter_hour_tracking(self) -> Callable[[], None]:
        """Schedule learning updates on wall-clock quarter-hour boundaries.

        Returns an unsubscribe callable that the config entry registers for
        teardown.
        """

        @callback
        def _handle_quarter_hour(now: datetime) -> None:
            _LOGGER.debug("Quarter-hour boundary reached at %s", now.isoformat())
            self.hass.async_create_task(self.async_request_refresh())

        return async_track_time_change(
            self.hass,
            _handle_quarter_hour,
            minute=[0, 15, 30, 45],
            second=0,
        )

    # -- Configuration helpers -------------------------------------------------

    def _config(self, key: str) -> str | None:
        """Return a configured entity id, preferring options over data."""
        return self.entry.options.get(key, self.entry.data.get(key))

    def _state(self, key: str) -> State | None:
        """Return the Home Assistant state object for a configured entity."""
        entity_id = self._config(key)
        if not entity_id:
            return None
        return self.hass.states.get(entity_id)

    def _float(self, key: str) -> float | None:
        """Return a configured entity's numeric state, or None if unavailable."""
        state = self._state(key)
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    # -- Persistent storage ----------------------------------------------------

    async def async_load_store(self) -> None:
        """Load the learned model from disk."""
        stored = await self._store.async_load()
        self.model = LearningModel.from_dict(stored)
        self.storage_loaded = True
        _LOGGER.debug(
            "Store loaded: %s profiles, %s learned days, %s updates",
            len(self.model.profiles),
            self.model.learned_days,
            self.model.update_count,
        )

    async def async_save_store(self) -> None:
        """Persist the learned model to disk."""
        await self._store.async_save(self.model.to_dict())
        self.storage_saved = True
        _LOGGER.debug(
            "Store saved: %s profiles, %s learned days, %s updates",
            len(self.model.profiles),
            self.model.learned_days,
            self.model.update_count,
        )

    # -- Learning --------------------------------------------------------------

    @staticmethod
    def _slots_between(previous_slot: int, current_slot: int) -> list[int]:
        """Return the slots to learn into, from ``previous_slot`` (exclusive).

        Distributes across every missed quarter up to and including
        ``current_slot``. Handles midnight rollover by wrapping from the end of
        the day back to the start.
        """
        if current_slot >= previous_slot:
            return list(range(previous_slot + 1, current_slot + 1))
        # Day rollover: previous_slot+1..95 then 0..current_slot.
        return list(range(previous_slot + 1, INTERVALS_PER_DAY)) + list(
            range(0, current_slot + 1)
        )

    def _rebase_baseline(self, cumulative: float, slot: int, now: datetime) -> None:
        """Advance the baseline without learning (reset/spike handling)."""
        self.model.previous_house_load = cumulative
        self.model.previous_slot = slot
        self.model.previous_update_time = now.isoformat()
        self.model.last_delta_per_slot = None
        self.model.distributed_slots = []
        self.model.last_learned_slots = []

    def _learn_house_load(self, now: datetime) -> bool:
        """Update the learned profile from the cumulative house-load sensor.

        The source sensor is cumulative daily kWh and resets at midnight. On
        each quarter-hour boundary we compute the delta since the previous
        reading and, when valid, distribute it across every missed quarter slot
        using an exponential moving average. Both the global profile and the
        current season/day-type profile are updated.

        Returns ``True`` when the model changed and should be persisted.
        """
        source_entity = self._config(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)
        cumulative = self._float(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)

        self.model.update_count += 1
        self.model.last_update = now.isoformat()

        slot = _interval_index(now)
        previous = self.model.previous_house_load

        _LOGGER.debug(
            "House-load read: source=%s current=%s previous=%s slot=%s "
            "(update #%s)",
            source_entity,
            cumulative,
            previous,
            slot,
            self.model.update_count,
        )

        if cumulative is None:
            _LOGGER.debug(
                "House-load source %s unavailable; skipping learning",
                source_entity,
            )
            return True

        # First reading after start/reset: only establish a baseline.
        if previous is None:
            self._rebase_baseline(cumulative, slot, now)
            _LOGGER.debug(
                "Established house-load baseline=%.3f kWh at slot %s",
                cumulative,
                slot,
            )
            return True

        raw_delta = cumulative - previous
        self.model.last_raw_delta = round(raw_delta, 3)

        _LOGGER.debug(
            "Raw delta calculated: %.3f - %.3f = %.3f kWh (slot %s)",
            cumulative,
            previous,
            raw_delta,
            slot,
        )

        # Negative delta => daily counter reset at midnight: rebase only.
        if raw_delta < 0:
            self._rebase_baseline(cumulative, slot, now)
            _LOGGER.debug(
                "Negative raw delta (%.3f kWh) treated as midnight reset; "
                "rebased baseline to %.3f kWh",
                raw_delta,
                cumulative,
            )
            return True

        # Implausibly large delta => spike: ignore learning, rebase safely.
        if raw_delta > MAX_QUARTER_DELTA_KWH:
            self._rebase_baseline(cumulative, slot, now)
            _LOGGER.debug(
                "Raw delta %.3f kWh exceeds spike limit %.1f kWh; ignoring "
                "sample and rebasing baseline",
                raw_delta,
                MAX_QUARTER_DELTA_KWH,
            )
            return True

        # Determine the slots to learn into, distributing across missed quarters.
        previous_slot = self.model.previous_slot
        if previous_slot is None:
            target_slots = [slot]
        else:
            target_slots = self._slots_between(previous_slot, slot)
        if not target_slots:
            target_slots = [slot]

        delta_per_slot = raw_delta / len(target_slots)
        self.model.last_delta_per_slot = round(delta_per_slot, 4)
        self.model.distributed_slots = list(target_slots)
        self.model.last_learned_slots = list(target_slots)

        season = _season_for(now)
        day_type = _day_type_for(now)
        profile_key = _profile_key(season, day_type)

        global_profile = self.model.global_profile()
        keyed_profile = self.model.profile(profile_key)

        for target in target_slots:
            for prof in (global_profile, keyed_profile):
                old_value = prof[target]
                if old_value == 0.0:
                    prof[target] = delta_per_slot
                else:
                    prof[target] = (
                        old_value * (1 - LEARNING_ALPHA)
                        + delta_per_slot * LEARNING_ALPHA
                    )

        # Record the learned day for the confidence model.
        date_str = now.date().isoformat()
        if date_str not in self.model.learned_dates:
            self.model.learned_dates.append(date_str)

        # Advance the baseline.
        self.model.previous_house_load = cumulative
        self.model.previous_slot = slot
        self.model.previous_update_time = now.isoformat()

        _LOGGER.debug(
            "Distributed raw_delta=%.3f kWh as %.4f kWh/slot over slots %s "
            "for profile %s (global + keyed)",
            raw_delta,
            delta_per_slot,
            target_slots,
            profile_key,
        )
        return True

    def _global_learned_slots_count(self) -> int:
        """Return the number of non-zero slots in the global profile."""
        return sum(1 for value in self.model.global_profile() if value > 0.0)

    def _learning_confidence(self) -> float:
        """Return a 0..100 confidence score for the learned profile."""
        learned_slots = self._global_learned_slots_count()
        learned_days = self.model.learned_days
        confidence = (
            (learned_slots / INTERVALS_PER_DAY) * 40
            + (learned_days / CONFIDENCE_DAYS_TARGET) * 40
            + (self.model.update_count / CONFIDENCE_UPDATES_TARGET) * 20
        )
        confidence = max(0.0, min(100.0, confidence))
        _LOGGER.debug(
            "Confidence: slots=%s days=%s updates=%s -> %.1f%%",
            learned_slots,
            learned_days,
            self.model.update_count,
            confidence,
        )
        return round(confidence, 1)

    def _predicted_daily_load(self) -> float:
        """Return the learned total load (kWh): sum of the 96 global slots."""
        return round(sum(self.model.global_profile()), 3)

    def _predicted_remaining_load(self, now: datetime) -> float:
        """Return learned load (kWh) from the current slot to end of day."""
        index = _interval_index(now)
        return round(sum(self.model.global_profile()[index:]), 3)

    # -- Reserve calculation ---------------------------------------------------

    def _required_reserve(self, now: datetime) -> float:
        """Estimate reserve energy (kWh) needed until the next buy window.

        Temporary logic until sell-to-next-buy reserve is implemented: the
        reserve equals the learned load still expected for the rest of today,
        clamped between 0 and the battery capacity.
        """
        remaining_load = self._predicted_remaining_load(now)

        capacity = self._float(CONF_BATTERY_CAPACITY_KWH_ENTITY)
        reserve = max(remaining_load, 0.0)
        if capacity is not None:
            reserve = min(reserve, capacity)
        return round(reserve, 3)

    # -- Coordinator update ----------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Read sources, learn, and compute derived values."""
        now = dt_util.now()

        changed = self._learn_house_load(now)

        # Persist learned data after each learning cycle.
        if changed:
            await self.async_save_store()

        battery_current = self._float(CONF_BATTERY_CURRENT_KWH_SENSOR)
        battery_capacity = self._float(CONF_BATTERY_CAPACITY_KWH_ENTITY)
        battery_soc = self._float(CONF_BATTERY_SOC_SENSOR)

        required_reserve = self._required_reserve(now)
        reserve_satisfied = (
            battery_current is not None and battery_current >= required_reserve
        )

        season = _season_for(now)
        day_type = _day_type_for(now)
        profile_key = _profile_key(season, day_type)
        current_slot = _interval_index(now)
        learned_slots_count = self._global_learned_slots_count()
        confidence = self._learning_confidence()
        current_house_load = self._float(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)

        # Profile status: a short human-readable lifecycle label.
        if learned_slots_count < MIN_CONFIDENT_SLOTS:
            profile_status = "learning"
        elif self.model.learned_days < 7:
            profile_status = "improving"
        else:
            profile_status = "ready"

        return {
            "season": season,
            "day_type": day_type,
            "profile_key": profile_key,
            "interval_index": current_slot,
            "intervals_per_day": INTERVALS_PER_DAY,
            "predicted_daily_load_kwh": self._predicted_daily_load(),
            "predicted_remaining_load_kwh": self._predicted_remaining_load(now),
            "pv_forecast_today_kwh": self._float(CONF_PV_FORECAST_TODAY_SENSOR),
            "pv_forecast_tomorrow_kwh": self._float(CONF_PV_FORECAST_TOMORROW_SENSOR),
            "pv_actual_today_kwh": self._float(CONF_PV_ACTUAL_TODAY_SENSOR),
            "pv_east_kwh": self._float(CONF_PV_EAST_SENSOR),
            "pv_west_kwh": self._float(CONF_PV_WEST_SENSOR),
            "frank_price_today": self._float(CONF_FRANK_PRICES_TODAY_SENSOR),
            "frank_price_tomorrow": self._float(CONF_FRANK_PRICES_TOMORROW_SENSOR),
            "frank_cheapest_time_today": self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR
            ),
            "frank_most_expensive_time_today": self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR
            ),
            "frank_cheapest_time_tomorrow": self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR
            ),
            "frank_most_expensive_time_tomorrow": self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR
            ),
            "battery_current_kwh": battery_current,
            "battery_capacity_kwh": battery_capacity,
            "battery_soc": battery_soc,
            "required_reserve_kwh": required_reserve,
            "reserve_satisfied": reserve_satisfied,
            "learned_profiles": len(self.model.profiles),
            # Learning metrics surfaced as dedicated sensors.
            "learning_confidence": confidence,
            "learning_days": self.model.learned_days,
            "learned_slots_count": learned_slots_count,
            "last_quarter_load_kwh": self.model.last_delta_per_slot,
            "profile_status": profile_status,
            # Diagnostic / debug fields.
            "source_entity": self._config(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR),
            "source_value": current_house_load,
            "current_house_load": current_house_load,
            "current_slot": current_slot,
            "previous_house_load": self.model.previous_house_load,
            "previous_slot": self.model.previous_slot,
            "previous_update_time": self.model.previous_update_time,
            "last_raw_delta": self.model.last_raw_delta,
            "last_delta_per_slot": self.model.last_delta_per_slot,
            "distributed_slots": self.model.distributed_slots,
            "update_count": self.model.update_count,
            "last_update": self.model.last_update,
            "storage_loaded": self.storage_loaded,
            "storage_saved": self.storage_saved,
            # Backward-compatible debug keys for the predicted-load sensor.
            "last_house_load": self.model.previous_house_load,
            "last_delta": self.model.last_raw_delta,
            "last_slot": self.model.previous_slot,
        }

    def _raw_state(self, key: str) -> str | None:
        """Return the raw string state of a configured entity."""
        state = self._state(key)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state
