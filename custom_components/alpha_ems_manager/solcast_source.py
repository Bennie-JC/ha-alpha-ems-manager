"""Phase 5: the read-only Solcast boundary.

The impure half of the PV forecast. Everything here fetches values and hands them
to :mod:`pv_forecast`, which does the thinking. Nothing here decides anything.

Read-only, and structurally so
------------------------------

Alpha EMS calls exactly two Solcast actions -- ``query_forecast_data`` and
``diagnostic`` -- and both only read. Both are registered response-only, both
serve the integration's own cache, and **neither consumes the account's API
allowance**, which matters more than it looks: the live account this was built
against has ten calls a day, eight of them already spent when it was measured. If
reading a forecast cost quota, the per-site path would be unusable.

Four guards keep this a read-only boundary rather than a general-purpose way to
call services, and each one is asserted structurally:

* Only two modules in the package contain ``async_call`` at all.
* Every call site here passes **string literals** for the domain and the action.
  This is the mirror image of the Phase-4 adapter, whose single call site passes
  variables from a planned command; between them, neither module can reach an
  arbitrary domain.
* **No function here takes a domain or an action as an argument.** That is what
  stops a helpful ``_call(domain, service, data)`` appearing later and quietly
  becoming the escape hatch.
* Every mutating Solcast action is named once, in ``const``, so a test can prove
  none of them appears anywhere else in the package.

API keys
--------

The diagnostic response contains account configuration. Anything key-like is
dropped **here**, at the boundary, so no key material can reach provenance,
storage or a diagnostics download. Only the named fields below are read out of
the response at all, which makes that a property of the code rather than a
promise.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    PV_UNAVAILABLE_DIAGNOSTIC_MISSING,
    PV_UNAVAILABLE_ENTRY_NOT_FOUND,
    PV_UNAVAILABLE_NO_SOLCAST_ENTRY,
    PV_UNAVAILABLE_SERVICE_MISSING,
    RESPONSE_SHAPE_FLAT,
    RESPONSE_SHAPE_NESTED,
    RESPONSE_SHAPE_UNUSABLE,
    SOLCAST_DOMAIN,
    SOLCAST_SERVICE_DIAGNOSTIC,
    SOLCAST_SERVICE_QUERY_FORECAST,
)
from .pv_forecast import PvSite

_LOGGER = logging.getLogger(__name__)

#: Response keys this module reads. Nothing outside this set leaves the boundary,
#: which is how "no key material is exposed" becomes structural rather than
#: aspirational.
_SITE_FIELDS = (
    "resource_id",
    "name",
    "capacity",
    "capacity_dc",
    "azimuth",
    "tilt",
    "loss_factor",
)


# --- capability ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SolcastCapability:
    """What of the Solcast boundary was found, named even when absent.

    Every field here is a fact that can be demonstrated. There is deliberately
    **no config-entry state check**, and that is the beta.10 correction.

    beta.9 required the Solcast config entry to be in state ``LOADED``. That
    produced a live false negative on every Home Assistant restart. Solcast
    registers its actions at component level, so both are visible while its
    config entry is still setting up -- and Alpha EMS takes its first refresh
    during its own setup, which can win that race. The captured snapshot then
    reported both actions present *and* the entry not loaded, which is precisely
    the combination the live installation showed, and it stood until the next
    quarter-hour boundary.

    The state was never load-bearing. Calling a registered action is safe by
    definition, and a failure is caught and reported as a failed call rather than
    guessed at in advance. What matters is that an entry is selected, that it
    exists, and that the actions are there to call.
    """

    entry_selected: bool = False
    #: The selected id names a config entry that exists. Provable, and unlike the
    #: former state check it cannot be true one moment and false the next while
    #: the integration is perfectly usable.
    entry_found: bool = False
    query_service: bool = False
    diagnostic_service: bool = False

    @property
    def usable(self) -> bool:
        """Return whether a forecast can be requested at all."""
        return self.entry_selected and self.entry_found and self.query_service

    @property
    def discoverable(self) -> bool:
        """Return whether the site list can be read.

        Separate from :attr:`usable`, because an installation with a stored
        selection can still fetch a forecast without re-reading the site list.
        """
        return self.usable and self.diagnostic_service

    @property
    def unavailable_reason(self) -> str | None:
        """Return why the boundary is unusable, most specific cause first.

        Every branch names something that was actually checked. There is no
        "not loaded" any more, because that state could not be proven from
        anything the source reliably exposes.
        """
        if not self.entry_selected:
            return PV_UNAVAILABLE_NO_SOLCAST_ENTRY
        if not self.entry_found:
            return PV_UNAVAILABLE_ENTRY_NOT_FOUND
        if not self.query_service:
            return PV_UNAVAILABLE_SERVICE_MISSING
        if not self.diagnostic_service:
            return PV_UNAVAILABLE_DIAGNOSTIC_MISSING
        return None

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics form, naming what was looked for."""
        return {
            "entry_selected": self.entry_selected,
            "entry_found": self.entry_found,
            "query_forecast_data": self.query_service,
            "diagnostic": self.diagnostic_service,
            "usable": self.usable,
            "discoverable": self.discoverable,
            "unavailable_reason": self.unavailable_reason,
            "basis": (
                "two read-only actions, both response-only and both served from "
                "the Solcast integration's own cache; neither consumes the "
                "account's API allowance. capability is established from the "
                "entry existing and the actions being registered, never from the "
                "entry's setup state -- that state is not needed to call a "
                "registered action, and requiring it produced a false negative "
                "on every restart"
            ),
        }


def discover(hass: HomeAssistant, entry_id: str | None) -> SolcastCapability:
    """Return what of the Solcast boundary is present right now.

    Exception-free, exactly as the Phase-4 capability check is: a missing
    integration is a fact to report, not an error to raise.
    """
    if not entry_id:
        return SolcastCapability()

    return SolcastCapability(
        entry_selected=True,
        entry_found=hass.config_entries.async_get_entry(entry_id) is not None,
        query_service=hass.services.has_service(
            SOLCAST_DOMAIN, SOLCAST_SERVICE_QUERY_FORECAST
        ),
        diagnostic_service=hass.services.has_service(
            SOLCAST_DOMAIN, SOLCAST_SERVICE_DIAGNOSTIC
        ),
    )


# --- the diagnostic read ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SolcastFacts:
    """The source facts the diagnostic action reports.

    Read, never inferred. Where the response does not carry a field it stays
    ``None``, and ``None`` is recorded as "the source did not say" rather than
    being replaced with the value the field usually has.
    """

    sites: tuple[PvSite, ...] = ()
    excluded_sites: tuple[str, ...] = ()
    integration_version: str | None = None
    estimate_key: str | None = None
    dampening_enabled: bool | None = None
    auto_dampening: bool | None = None
    get_actuals: bool | None = None
    use_actuals: float | None = None
    hard_limit_raw: float | None = None
    api_limit: int | None = None
    api_used: int | None = None
    forecast_health: str | None = None
    #: Which shape the action's response was in. Reported so a future change of
    #: convention is visible in a diagnostics download rather than showing up as
    #: an unexplained absence of every field at once, which is how the beta.10
    #: defect presented.
    response_shape: str = RESPONSE_SHAPE_NESTED

    @property
    def site_ids(self) -> tuple[str, ...]:
        """Return every discovered identifier, sorted."""
        return tuple(sorted(site.resource_id for site in self.sites))

    def hard_limit_binds(self, dc_capacity_kw: float | None) -> bool | None:
        """Return whether the configured hard limit can actually clip this array.

        A raw value alone is misleading. The live account reports a hard limit of
        ``100.0`` against a six-kilowatt array, so a bare "configured: true" would
        have implied the source models clipping when it demonstrably cannot: under
        every reading the observed figures do not already disprove -- a hundred
        kilowatts, a hundred percent of DC, a hundred percent of AC -- the limit
        is a no-op. Recording the judgement separately from the value is what lets
        a later phase tell a real limit from a default.

        ``None`` when it cannot be decided, which is not the same as ``False``.
        """
        if self.hard_limit_raw is None or dc_capacity_kw is None:
            return None
        if dc_capacity_kw <= 0.0:
            return None
        return self.hard_limit_raw < dc_capacity_kw

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics form. Carries no key material by construction."""
        return {
            "integration_version": self.integration_version,
            "site_count": len(self.sites),
            "site_ids": list(self.site_ids),
            "excluded_sites": list(self.excluded_sites),
            "estimate_key": self.estimate_key,
            "dampening_enabled": self.dampening_enabled,
            "auto_dampening": self.auto_dampening,
            "get_actuals": self.get_actuals,
            "use_actuals": self.use_actuals,
            "hard_limit_raw": self.hard_limit_raw,
            "api_limit": self.api_limit,
            "api_used": self.api_used,
            "forecast_health": self.forecast_health,
            "response_shape": self.response_shape,
        }


def _as_bool(value: Any) -> bool | None:
    """Return a real boolean, or ``None`` when the field said nothing usable."""
    if isinstance(value, bool):
        return value
    return None


def _as_number(value: Any) -> float | None:
    """Return a numeric field, or ``None``. Strings are not coerced."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: Any) -> int | None:
    """Return an integer field, or ``None``."""
    number = _as_number(value)
    return None if number is None else int(number)


def _as_text(value: Any) -> str | None:
    """Return a string field, or ``None``."""
    return value if isinstance(value, str) and value else None


def _read_site(raw: Any) -> PvSite | None:
    """Return one site, reading only the named fields.

    Everything not in :data:`_SITE_FIELDS` is ignored rather than copied, so a
    response that grows an API key field cannot leak it through here.
    """
    if not isinstance(raw, Mapping):
        return None
    resource_id = _as_text(raw.get("resource_id"))
    if resource_id is None:
        return None
    return PvSite(
        resource_id=resource_id,
        name=_as_text(raw.get("name")) or resource_id,
        capacity_kw=_as_number(raw.get("capacity")),
        capacity_dc_kw=_as_number(raw.get("capacity_dc")),
        azimuth=_as_number(raw.get("azimuth")),
        tilt=_as_number(raw.get("tilt")),
        loss_factor=_as_number(raw.get("loss_factor")),
    )


def unwrap_response(response: Any) -> tuple[Mapping[str, Any], str]:
    """Return the payload inside an action response, and which shape it was in.

    **This is the beta.11 correction, and it was a real defect.** Both Solcast
    actions wrap their result: ``query_forecast_data`` returns ``{"data": [...]}``
    and ``diagnostic`` returns ``{"data": {...}}``. beta.10 unwrapped the first and
    read the second at the top level, so every field came back absent -- no sites,
    no estimate key, no version -- and the PV layer reported
    ``no_solcast_sites_discovered`` on an account that plainly had two.

    The reason the tests did not catch it is worth recording: the fake was written
    from a human-readable transcription of a diagnostic download rather than from
    the raw action response, so it reproduced the same wrong assumption and could
    only ever confirm it.

    The flat shape is still accepted. It costs one branch, it is what a future
    release might return, and refusing it would turn a shape change into a silent
    total loss of PV rather than a degradation.
    """
    if not isinstance(response, Mapping):
        return {}, RESPONSE_SHAPE_UNUSABLE
    inner = response.get("data")
    if isinstance(inner, Mapping):
        return inner, RESPONSE_SHAPE_NESTED
    return response, RESPONSE_SHAPE_FLAT


def parse_diagnostic(response: Any) -> SolcastFacts:
    """Return the facts carried by a diagnostic response.

    Split out from the call so every shape -- absent keys, wrong types, the
    payload nested one level down, a block that is a list where a mapping was
    expected -- is testable without an instance, and so the fields that are read
    are visible in one place.
    """
    payload, shape = unwrap_response(response)
    if shape == RESPONSE_SHAPE_UNUSABLE:
        return SolcastFacts(response_shape=shape)
    response = payload

    raw_sites = response.get("sites")
    sites = tuple(
        site
        for site in (_read_site(entry) for entry in _as_sequence(raw_sites))
        if site is not None
    )

    configuration = response.get("configuration")
    configuration = configuration if isinstance(configuration, Mapping) else {}
    dampening = response.get("dampening")
    dampening = dampening if isinstance(dampening, Mapping) else {}

    excluded = tuple(
        sorted(
            identifier
            for identifier in (
                _as_text(entry)
                for entry in _as_sequence(configuration.get("excluded_sites"))
            )
            if identifier is not None
        )
    )

    return SolcastFacts(
        response_shape=shape,
        sites=sites,
        excluded_sites=excluded,
        integration_version=_as_text(response.get("version")),
        estimate_key=_as_text(configuration.get("key_estimate")),
        dampening_enabled=_as_bool(dampening.get("enabled")),
        # Read from either block, because the diagnostic reports auto-dampening in
        # its configuration and its dampening section describes the same switch.
        auto_dampening=(
            _as_bool(configuration.get("auto_dampen"))
            if _as_bool(configuration.get("auto_dampen")) is not None
            else _as_bool(dampening.get("auto_dampening"))
        ),
        get_actuals=_as_bool(configuration.get("get_actuals")),
        use_actuals=_as_number(configuration.get("use_actuals")),
        hard_limit_raw=_as_number(configuration.get("hard_limit")),
        api_limit=_as_int(response.get("api_limit")),
        api_used=_as_int(response.get("api_used")),
        forecast_health=_as_text(response.get("forecast_health")),
    )


def _as_sequence(value: Any) -> Sequence[Any]:
    """Return a sequence, or an empty one. A string is not a sequence of sites."""
    if isinstance(value, (list, tuple)):
        return value
    return ()


async def read_facts(hass: HomeAssistant) -> SolcastFacts | None:
    """Call the diagnostic action and return what it said, or ``None``.

    The domain and the action are string literals. There is no parameter here by
    which a caller could name a different one.
    """
    try:
        response = await hass.services.async_call(
            SOLCAST_DOMAIN,
            SOLCAST_SERVICE_DIAGNOSTIC,
            {},
            blocking=True,
            return_response=True,
        )
    except Exception:
        # Any failure is a fact to report, never an exception to propagate: the
        # PV forecast degrades and every other layer refreshes normally.
        _LOGGER.debug("Solcast diagnostic could not be read", exc_info=True)
        return None
    return parse_diagnostic(response)


# --- the forecast read --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SolcastQuery:
    """One site's rows, or the aggregate's, as returned.

    Rows are handed on untouched apart from being confirmed to be mappings. The
    unit conversion and the interval mapping happen in the pure module, where they
    are testable against hand-written rows.
    """

    site_id: str
    rows: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    failed: bool = False


async def read_forecast(
    hass: HomeAssistant,
    *,
    start: datetime,
    end: datetime,
    site_id: str | None = None,
) -> SolcastQuery:
    """Return the forecast rows for a window, for one site or the aggregate.

    ``site_id`` of ``None`` requests the aggregate, which is what omitting the
    site key does -- verified against the live source. The requested range is
    half-open: a row beginning exactly at ``end`` is not returned, which is why
    the caller asks for midnight-to-midnight rather than midnight-to-last-quarter.

    The domain and the action are string literals, and there is no parameter by
    which a caller could name a different one. ``site_id`` names a *rooftop*, not
    a service.
    """
    data: dict[str, Any] = {
        "start_date_time": start.isoformat(),
        "end_date_time": end.isoformat(),
        # Asked for explicitly rather than left to the source's default, so a
        # change in that default cannot silently start returning a dampened
        # series while provenance still records an undampened one.
        "undampened": False,
    }
    if site_id is not None:
        data["site"] = site_id

    try:
        response = await hass.services.async_call(
            SOLCAST_DOMAIN,
            SOLCAST_SERVICE_QUERY_FORECAST,
            data,
            blocking=True,
            return_response=True,
        )
    except Exception:
        _LOGGER.debug(
            "Solcast forecast query failed for %s",
            site_id or "the aggregate",
            exc_info=True,
        )
        return SolcastQuery(site_id=site_id or "", failed=True)

    rows = (response or {}).get("data")
    return SolcastQuery(
        site_id=site_id or "",
        rows=tuple(row for row in _as_sequence(rows) if isinstance(row, Mapping)),
    )
