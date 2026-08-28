"""Five vocabularies, five known collisions, and a build that fails on a sixth.

**This is the test that would have caught R12.** Five string values are shared
across four published vocabularies -- ``quarter_expired`` three times,
``quarter_target_reached`` and ``target_reached`` twice each -- and the last one is
the dangerous one: ``SHORTFALL_TARGET_REACHED`` is published inside
``binding_clamps``, and ``"target_reached"`` was the only token the Activity
surface mapped to Success. A clamp reason and a completion reason wearing one
string, on a surface where one of them means the money was made.

**Renaming them was considered and overruled.** They are published values; a user
automation matching on one would break for the sake of tidiness. So beta.32 adds
disambiguation instead -- every published ending carries ``reason_vocabulary`` --
and adds this guard, which costs nothing and forbids the *next* collision.

The allow-list below is explicit and must be argued, entry by entry. Adding to it
is a decision a reviewer can see; a new collision that is not in it fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from custom_components.alpha_ems_manager import const

#: The vocabularies whose values are published and must stay distinguishable.
#:
#: Each is a *prefix* rather than a hand-listed tuple, so a constant added later
#: joins its vocabulary automatically. That matters: the whole failure mode here is
#: a constant added to one family that happens to collide with another, and a
#: hand-listed set would silently omit exactly the new arrival.
_FAMILIES: dict[str, str] = {
    "quarter_completion": "QUARTER_END_",
    "binding_clamp": "SHORTFALL_",
    "run_stop": "EXECUTION_STOP_",
    "tick": "TICK_",
}

#: The five collisions beta.32 inherited, each with the reason it is tolerated and
#: the field that tells the two apart.
#:
#: **Read this as a list of debts, not of decisions.** Every entry is a place where
#: two different questions share an answer string, and the only thing standing
#: between a reader and a wrong conclusion is a second field they have to consult.
_ALLOWED: dict[str, str] = {
    # A quarter that ran out of time, a clamp that bound because it did, and a run
    # stopped for the same reason. Three layers describing one event, and
    # ``reason_vocabulary`` says which layer is speaking.
    "quarter_expired": "quarter_completion | binding_clamp | run_stop",
    # The quarter met its objective; the run stopped because the quarter did.
    "quarter_target_reached": "quarter_completion | run_stop",
    # **The dangerous one.** A clamp that bound because the target was reached, and
    # a run stop meaning the same -- but the clamp is published inside
    # ``binding_clamps`` where the Activity surface used to look for its Success
    # token.
    "target_reached": "binding_clamp | run_stop",
    # A reserve bound that clamped a quarter, and a stop reason with the same
    # name. The stop reason was deleted in beta.32 as unreachable, so this entry
    # exists to be *removed* if the clamp is ever renamed.
    "reserve_limit": "binding_clamp",
    # A tick that stopped for a reason the run-stop vocabulary also names.
    "stopped_quarter_expired": "tick",
}


def _values(prefix: str) -> dict[str, str]:
    """Return every published value in one family, keyed by value."""
    found: dict[str, str] = {}
    for name in dir(const):
        if not name.startswith(prefix):
            continue
        value = getattr(const, name)
        if not isinstance(value, str):
            continue
        found[value] = name
    return found


def test_the_four_vocabularies_are_pairwise_disjoint_apart_from_the_known_five():
    """A value in two families must be on the allow-list, with a reason."""
    families = {name: _values(prefix) for name, prefix in _FAMILIES.items()}
    collisions: dict[str, list[str]] = {}
    names = sorted(families)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            for value in set(families[left]) & set(families[right]):
                collisions.setdefault(value, []).append(f"{left}/{right}")

    unexpected = {
        value: where for value, where in collisions.items() if value not in _ALLOWED
    }
    assert not unexpected, (
        "a new value collision across published vocabularies: "
        f"{unexpected}. Either give it a distinct value, or add it to _ALLOWED "
        "with the reason it is tolerable and the field that disambiguates it"
    )


def test_every_allowed_collision_is_still_a_real_collision():
    """The allow-list may not accumulate entries that no longer apply.

    An allow-list nobody prunes stops being a record of known debt and becomes a
    blanket. So an entry that has been fixed -- by a rename, or by the constant
    being deleted, which is what happened to ``reserve_limit``'s run-stop half in
    beta.32 -- fails here until it is removed.
    """
    families = {name: _values(prefix) for name, prefix in _FAMILIES.items()}
    for value in _ALLOWED:
        holders = [name for name, values in families.items() if value in values]
        assert holders, f"{value!r} is on the allow-list but appears in no family"


def test_the_reason_vocabulary_field_names_every_family_that_can_collide():
    """Every colliding family must have a ``reason_vocabulary`` value to name it.

    The allow-list is only defensible because a reader can tell the two apart, and
    that is only true if the disambiguating field can *say* which one it is.
    """
    for value, families in _ALLOWED.items():
        for family in families.split(" | "):
            assert family in {*const.REASON_VOCABULARIES, "tick"}, (
                f"{value!r} is disambiguated by {family!r}, which is not a "
                "published reason_vocabulary value"
            )


# ---------------------------------------------------------------------------
# and the other half of R10: a vocabulary entry nothing can produce
# ---------------------------------------------------------------------------

#: Reasons the Activity surface names that production is allowed not to produce.
#:
#: **Empty, and it must stay empty.** beta.31 shipped five entries that no
#: production assignment could reach -- and two of them had *green unit tests*,
#: constructed by hand from inputs the pipeline could not produce. That is the
#: shape of defect this list exists to make impossible: a reason a test can reach
#: and the code cannot.
_UNREACHABLE_BY_DESIGN: frozenset[str] = frozenset()


def _production_source() -> str:
    """Return every production module's source, concatenated."""
    root = Path(const.__file__).parent
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(root.glob("*.py"))
        if path.name not in {"const.py", "activity.py"}
    )


@pytest.mark.parametrize("table", ["_CANCEL_REASONS", "_ERROR_REASONS"])
def test_every_activity_reason_is_reachable_from_production_code(table: str) -> None:
    """For every reason Activity can print, a production assignment that lands.

    **The hole that let R10 ship.** Unit tests on synthetic ``ExecutionView``
    objects prove the *rendering*; they say nothing about whether the pipeline can
    ever hand over that input. Five of fourteen mappings were dead code, four
    reasons the system genuinely produced fell through to the fallback and printed
    "Canceled -- Plan Replaced", and two hand-built tests were green on inputs
    that could not occur.

    Reachability is established by finding the constant's *name* in a production
    **assignment** -- ``stop_reason=X``, ``ended=X``, ``reason = X``, ``return X``
    or the ``else`` arm of a conditional. Naming it in a comparison is deliberately
    not enough: ``if reason == X`` proves a branch reads the value, not that
    anything ever produces it, and that distinction is exactly what beta.31 got
    wrong -- five mappings named values nothing wrote.
    """
    from custom_components.alpha_ems_manager import activity

    source = _production_source()
    mapping = getattr(activity, table)
    for value in mapping:
        if value in _UNREACHABLE_BY_DESIGN:
            continue
        name = next(
            (
                candidate
                for candidate in dir(const)
                if candidate.startswith("EXECUTION_STOP_")
                and getattr(const, candidate) == value
            ),
            None,
        )
        assert name is not None, f"{value!r} is not a declared stop reason"
        assigned = re.search(
            rf"(?<![=!<>])=\s*\(?\s*{name}\b"
            rf"|\belse\s+{name}\b"
            rf"|\breturn\s+{name}\b",
            source,
        )
        assert assigned is not None, (
            f"{activity.__name__} can print {mapping[value]!r} for {name}, but no "
            "production module assigns it to a stop_reason. Either wire it, or "
            "delete the mapping -- a reason a test can reach and the code cannot "
            "is exactly the defect beta.32 closed"
        )


def test_the_unreachable_allow_list_is_empty() -> None:
    """Stated as its own assertion, because it is the acceptance criterion."""
    assert not _UNREACHABLE_BY_DESIGN
