"""One solved horizon, shared by every test that asks for the same one.

**Why this exists.** Measured on beta.41, four test files were 60 % of the whole
suite's CPU time — 79.5 of 131 minutes — and one test alone was 14.2 minutes. None of
them was slow because it asserted too much. They were slow because they solved the
*same* horizons over and over: two files sweep the identical 42 shape-and-state
combinations inside every one of their 24 tests, and a third sweeps a 250-point grid
in a single test. Roughly a thousand solves where forty-two would do.

A solve is a pure function of its inputs, and everything it returns is a frozen
dataclass, so the result can be shared. Nothing here weakens an assertion or drops a
scenario: the same combinations are solved, once each, and every test reads the same
figures it read before.

**The one real hazard is the cache key, and it is guarded rather than trusted.**
``solve_at`` takes callables — ``price_fn``, ``load_fn``, ``pv_fn`` — and a lambda
has no identity a key can use: every ``lambda i: 0.0`` in the suite shares the
qualname ``<lambda>``. Keying on that would silently serve one price curve's plan to a
test asking about a different one, which is worse than being slow. So an anonymous
function is **refused**, with a message saying what to do instead. That refusal is
what makes the sharing safe, and it is why the callers below pass named
module-level functions.
"""

from __future__ import annotations

from typing import Any

from .beta34_shape import Solved, solve_at

_CACHE: dict[tuple[Any, ...], Solved] = {}


def _identify(name: str, value: Any) -> tuple[Any, ...]:
    """Return a stable key fragment for one keyword argument."""
    if callable(value):
        qualname = getattr(value, "__qualname__", "")
        if not qualname or "<lambda>" in qualname or "<locals>" in qualname:
            raise AssertionError(
                f"solve_cache cannot key the callable passed as {name!r}: "
                f"{qualname or type(value).__name__!r} is anonymous or local, so two "
                "different curves would share one cache entry and one test would "
                "silently read another test's plan. Give it a module-level name."
            )
        return (name, "fn", qualname)
    return (name, "value", repr(value))


def cache_key(kwargs: dict[str, Any]) -> tuple[Any, ...]:
    """Return the key one set of ``solve_at`` arguments resolves to."""
    return tuple(_identify(name, kwargs[name]) for name in sorted(kwargs))


def cached_solve_at(**kwargs: Any) -> Solved:
    """Return ``solve_at(**kwargs)``, solving each distinct horizon exactly once.

    Read-only by contract. Everything reachable from a :class:`Solved` is a frozen
    dataclass, so a shared instance cannot be mutated by one test on behalf of
    another — but a test that wants to *modify* a plan must call ``solve_at``
    directly rather than this.
    """
    key = cache_key(kwargs)
    hit = _CACHE.get(key)
    if hit is None:
        hit = _CACHE[key] = solve_at(**kwargs)
    return hit


def cache_size() -> int:
    """Return how many distinct horizons have been solved. For the speed tests."""
    return len(_CACHE)
