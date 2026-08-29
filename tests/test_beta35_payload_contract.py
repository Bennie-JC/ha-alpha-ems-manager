"""beta.35: no surface may read a key the execution payload does not publish.

**This defect class has now shipped three times.** The one that cost the most was
``sensor.py`` reading ``progress.get("grid_export_realized_kwh")`` -- a key written
nowhere in the package, one hit in the whole tree, and it was the reader. It is
reached exactly when the frozen target is missing, so the campaign that had just
moved 1.92 kWh published ``0.00 / 5.05`` and the logbook called it a cancellation.

Nothing about that is detectable by reading either side alone: the writer is
correct, the reader is syntactically fine, and ``dict.get`` turns the disagreement
into a plausible zero. So it is checked structurally, the way beta.33 checks that
no contract field is published null -- by walking the readers' source against a
payload the production path actually produced.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from .beta35_trace import step_clock
from .test_beta24_live_charge import LiveSurface, step_once
from .test_beta35_campaign_continuity import start_the_campaign

pytestmark = pytest.mark.usefixtures("control_surface")

PACKAGE = pathlib.Path("custom_components/alpha_ems_manager")

#: The reader modules, and the local names each binds a slice of the execution
#: payload to. Deliberately explicit: a guard that guessed which ``.get`` calls
#: were payload reads would either miss the one that mattered or fire on every
#: dictionary in the package.
#:
#: ``view`` is **not** in either set, and that is a real exclusion rather than a
#: convenience: ``_executing_view`` builds its own dictionary out of the payload,
#: so its keys are defined a few lines above where they are read and the compiler
#: of record is the reader itself. The keys this guard exists for are the ones
#: that cross a module boundary -- ``progress``, ``execution``, ``lifecycle`` --
#: where the writer and the reader can drift apart without either looking wrong.
READERS: dict[str, frozenset[str]] = {
    "sensor.py": frozenset({"execution", "progress", "boundary", "ownership"}),
    "activity.py": frozenset({"execution", "progress", "lifecycle", "boundary"}),
}


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def _keys_of(payload: Any, into: set[str]) -> None:
    """Collect every key name the payload publishes, at any depth."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            into.add(str(key))
            _keys_of(value, into)
    elif isinstance(payload, (list, tuple)):
        for entry in payload:
            _keys_of(entry, into)


def _reads(module: str, receivers: frozenset[str]) -> set[tuple[str, int]]:
    """Return every ``<receiver>.get("literal")` in ``module``, with its line."""
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            continue
        target = func.value
        # ``progress.get(...)`` and ``(execution.get("x") or {}).get("y")`` alike:
        # the receiver is either the bound name or an expression rooted in one.
        while isinstance(target, (ast.BoolOp, ast.Call, ast.Attribute)):
            if isinstance(target, ast.BoolOp) and target.values:
                target = target.values[0]
            elif isinstance(target, ast.Call):
                target = target.func
            else:
                target = target.value
        if not isinstance(target, ast.Name) or target.id not in receivers:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add((first.value, node.lineno))
    return found


async def test_every_key_a_surface_reads_is_a_key_the_payload_writes(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The guard that would have caught R7, run against a real campaign.**

    The payload is the one an owned, executing, multi-quarter export actually
    produced -- not a fixture of what it ought to contain -- so a key that exists
    only in a reader's imagination has nowhere to hide.

    *Mutation: restore the ``grid_export_realized_kwh`` read in ``sensor.py`` and
    this fails, naming the file and the line.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))

    published: set[str] = set()
    _keys_of(report, published)
    assert "objective_realized_kwh" in published, "the payload itself must be live"

    unwritten: list[str] = []
    for module, receivers in READERS.items():
        for key, line in sorted(_reads(module, receivers)):
            if key not in published:
                unwritten.append(f"{module}:{line} reads {key!r}")

    assert unwritten == [], (
        "a surface reads a key nothing publishes; dict.get turns that into a "
        "plausible zero rather than an error:\n  " + "\n  ".join(unwritten)
    )
