"""beta.34: an arm that cannot be claimed is never sent.

**The control-safety defect of 2026-08-29.** At 13:30:07 the integration armed a
full Mode 2 dispatch -- marker on, mode 2, -10 kW, duration 20 -- and wrote no
causal claim. From 13:31 to 13:44 every sixty-second tick read
``ownership_not_owned`` and declined to write, correctly: an unproven dispatch
must never be touched. The pack charged 3.14 kWh nobody had authorised and the
vendor dead-man ended it at 13:50:28.

The cause was that the arm path and the claim path disagreed about what an
authority is. The command was built from the **admitted plan's open row** -- which
is exactly what beta.29 designed, because Stage A's head is ``elapsed + 1`` and
the publication made at 19:45 structurally cannot affirm the 19:45 run. The claim
path required ``self._carried``, which by then was gone.

So the fix is not "refuse to arm without a carried run": that would delete
beta.29 and reintroduce the skipped-quarter defect it was written to remove. The
fix is to make the claim follow the authority that actually produced the command,
and to fail closed only when there is genuinely no authority at all.

Nothing about ownership is weakened. A claim is still a claim: ownership requires
the later ``dispatch_start`` readback to match, so a dispatch somebody else began
still cannot be appropriated.
"""

from __future__ import annotations

import inspect
import textwrap

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    DISPATCH_ENABLE,
    DISPATCH_MODE_SOC_CONTROL,
    plan_dispatch_arm,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_REFUSE_NO_CLAIMABLE_RUN,
    EXECUTION_INTENT_NET_EXPORT,
)
from custom_components.alpha_ems_manager.execution import (
    admit_plan,
    parse_target,
    target_as_published,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge, step_once
from .test_beta27_quarter_execution import quarter_at
from .test_beta29_quarter_authority_lifecycle import (
    after_dark,
    at_rest,
    orphan_the_quarter,
    withdraw_publications,
)
from .test_beta33_campaign_wiring import multi_segment_targets

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


# ===========================================================================
# 1. the claim follows the authority
# ===========================================================================


async def test_a_quarter_authority_arm_writes_its_own_claim(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The 13:30 incident, and the line that stops it repeating.**

    The exact beta.29 condition: an open row, no carried run, no further
    publication. beta.29 requires the dispatch to start. beta.34 additionally
    requires that when it does, a causal record exists -- otherwise the very next
    tick cannot prove the dispatch is ours and will refuse to stop it.

    *Mutation: make ``_claim_authority`` return ``None`` when ``run is None`` and
    this fails -- the arm is refused and nothing is written at all.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await after_dark(hass)
    await at_rest(hass, coordinator)
    orphan_the_quarter(
        coordinator,
        quarter_at(
            10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=0.25, export=0.04
        ),
    )
    withdraw_publications(coordinator, monkeypatch)
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    # beta.29's guarantee, unchanged: the row is the authority and it starts.
    assert coordinator._carried is None
    assert (report.get("authorization") or {}).get("authorized") is True
    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written[0] == BOOLEAN_EXECUTION_OWNER, written
    assert written[-1] == DISPATCH_ENABLE, written
    assert hass.states.get(DISPATCH_ENABLE).state == "on"

    # beta.34's addition: and it is claimable afterwards.
    record = coordinator.store.execution_record
    assert record is not None, "an arm with no record is an unstoppable dispatch"
    assert record["run_id"] == coordinator._plan.run_id
    assert record["admitted_plan_id"] == coordinator._plan.plan_id
    assert record["quarter_start"] == coordinator._quarter.quarter_start.isoformat()
    # A claim, not a grant: the readback has not happened yet.
    assert record["dispatch_start"] is None


async def test_the_admitted_plan_keeps_the_publication_it_was_admitted_from(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The field that makes a quarter-authority claim a full round trip.

    ``carried_from_record`` rebuilds a run from ``record["target"]`` after a
    restart, and refuses to adopt without it. Production populates it because
    ``admit_plan`` now keeps the target; asserted here on the production
    function, because the older test helpers build ``AdmittedPlan`` by hand and
    would happily pass with the field absent.

    *Mutation: drop ``target=target`` from ``admit_plan`` and this fails.*
    """
    coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    del coordinator
    published = next(t for t in targets if t["quarter_schedule"])
    target = parse_target(published)
    assert target is not None

    plan = admit_plan(target, run=None, now=local(NORMAL, 10, 30))
    assert plan is not None
    assert plan.target is target
    # And the round trip holds, which is what a restart depends on.
    assert parse_target(target_as_published(plan.target)) == target


# ===========================================================================
# 2. fail closed when there is no authority at all
# ===========================================================================


async def test_an_arm_with_no_authority_writes_nothing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Nothing, not even stage one -- and the guard is stated as a guard.**

    No carried run and no admitted plan is not a beta.29 condition; it is a
    command that came from nowhere. As the pipeline stands today it cannot
    happen: the command is built either from the carried run or from a row of the
    admitted plan, and both of those are exactly what ``_claim_authority``
    accepts, so a built activation always has something to claim it.

    That is a property of the current wiring, not a law, and it is the property
    the 13:30 incident *broke* -- there the two paths disagreed and the disagreement
    was invisible until a real dispatch was left unowned on real hardware. So the
    guard is asserted directly at the send site rather than through a lifecycle
    that cannot currently reach it, and this test says plainly which it is doing.

    *Mutation: delete the ``claim is None`` refusal and this fails -- the staged
    write proceeds and the record stays absent, which is the 13:30 state exactly.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await at_rest(hass, coordinator)
    live_surface.calls.clear()

    # A report that has been authorised and a staged sequence that activates --
    # with neither authority behind it. Every one of these fields is set by the
    # controller itself on an ordinary arming refresh.
    report: dict = {
        "authorization": {"authorized": True},
        "commands_planned": 1,
        "execution": {"result": {}},
    }
    coordinator._carried = None
    coordinator._plan = None
    coordinator._quarter = None
    coordinator._pending_commands = plan_dispatch_arm(
        mode=DISPATCH_MODE_SOC_CONTROL,
        power_kw=-1.0,
        cutoff_soc_percent=21,
        duration_minutes=20,
        pv_enabled=True,
    )
    coordinator._pending_activates = True
    coordinator._pending_is_reset = False
    coordinator._pending_stage_one = ()
    coordinator._pending_stage_two = ()
    coordinator._pending_verify = None
    coordinator.store.execution_record = None

    await coordinator._async_dispatch(report, dt_util.utcnow())

    assert live_surface.calls == [], [c.data for c in live_surface.calls]
    assert coordinator.store.execution_record is None
    result = report["execution"]["result"]
    assert result.get("arm_refused_reason") == CONTROL_REFUSE_NO_CLAIMABLE_RUN


def test_the_claim_authority_accepts_exactly_the_two_things_that_build_commands(
    hass: HomeAssistant,
) -> None:
    """The invariant behind the guard, stated so a later change has to break it.

    A command is built from a carried run or from a row of the admitted plan.
    Those are the two authorities, and ``_claim_authority`` accepts those two and
    nothing else -- which is what makes "a built activation always has something
    to claim it" true rather than lucky.
    """
    from custom_components.alpha_ems_manager import (
        coordinator as module,
    )

    source = textwrap.dedent(
        inspect.getsource(module.AlphaEmsCoordinator._claim_authority)
    )

    # The carried run first, unchanged and still the common case.
    assert "if run is not None:\n        return run" in source
    # Then the admitted plan, and only with an open row to be an authority for.
    assert "plan = self._plan" in source
    assert "quarter = self._quarter" in source
    assert source.count("return None") == 2
