"""The shard split decides what CI runs, and nothing tested it.

``scripts/shard_plan.py`` is the only thing standing between "eight jobs run the
suite between them" and "a test file runs twice, or not at all". It had no tests: the
suite it shards was the thing being trusted to notice, and a file quietly assigned to
nobody is exactly the failure a green suite cannot report.

The specific defect these were written for: a file missing from the committed manifest
was assigned to **shard 1**, which is the shard the manifest already loads heaviest --
LPT puts the single largest file there and cannot split it. So every new test file was
appended to the worst shard and the imbalance grew with the suite. On the beta.57 run
that was three files stacked on a shard already at 2.07x the ideal.

These tests pin the two properties that actually matter -- **nothing is lost, and
nothing runs twice** -- plus the determinism the eight CI jobs depend on: they each
compute their own membership independently, so any disagreement between them is a test
executed twice or skipped entirely.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "shard_plan.py"


def _shard_plan():
    """Import the CI script by path. It is not a package, and CI runs it as a file."""
    spec = importlib.util.spec_from_file_location("shard_plan", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["shard_plan"] = module
    spec.loader.exec_module(module)
    return module


shard_plan = _shard_plan()


#: Eight shards, deliberately uneven, with shard 1 heaviest -- the shape the real
#: manifest has, because the largest single test file lives there and cannot be split.
UNEVEN_TOTALS = [1853.0, 400.0, 300.0, 250.0, 200.0, 150.0, 100.0, 50.0]


def _deal(
    known: set[str], planned: set[str], totals: list[float]
) -> dict[int, list[str]]:
    """Return what every shard would claim, keyed by zero-based shard index."""
    return {
        index: shard_plan.unplanned_for(known, planned, totals, index)
        for index in range(len(totals))
    }


# ===========================================================================
# 1. nothing is lost, and nothing runs twice
# ===========================================================================


def test_every_unplanned_file_is_claimed_by_exactly_one_shard() -> None:
    """**The property the whole script exists for.**

    A file the manifest has never seen still has to run. Losing one is silent: the
    suite stays green because the test that would have failed was never executed.

    *Mutation: drop the modulo, or return ``[]`` for some shard, and a file vanishes.*
    """
    planned = {"tests/test_old.py"}
    known = planned | {f"tests/test_new_{n}.py" for n in range(11)}

    dealt = _deal(known, planned, UNEVEN_TOTALS)
    claimed = [name for names in dealt.values() for name in names]

    assert sorted(claimed) == sorted(known - planned)
    assert len(claimed) == len(set(claimed)), "a file was claimed by two shards"


def test_a_manifest_that_already_covers_everything_adds_nothing() -> None:
    """The ordinary case: no drift, so every shard claims exactly its own list."""
    planned = {f"tests/test_{n}.py" for n in range(20)}

    assert _deal(planned, planned, UNEVEN_TOTALS) == {index: [] for index in range(8)}


def test_more_unplanned_files_than_shards_still_lose_none() -> None:
    """The wrap-around case. Twenty files over eight shards is not eight files."""
    planned: set[str] = set()
    known = {f"tests/test_{n:02d}.py" for n in range(20)}

    dealt = _deal(known, planned, UNEVEN_TOTALS)
    claimed = [name for names in dealt.values() for name in names]

    assert sorted(claimed) == sorted(known)
    assert len(claimed) == 20


# ===========================================================================
# 2. determinism -- eight processes must agree without talking to each other
# ===========================================================================


def test_the_deal_follows_sorted_order_and_not_set_order() -> None:
    """**Sets are unordered, and CI computes this eight times in eight processes.**

    Two shards disagreeing about who owns a file is a test running twice or not at
    all, and it would follow the hash seed rather than anything a reader can see.

    Asserted positionally rather than by comparing two calls, because comparing two
    calls in one process proves nothing -- the same set iterates the same way twice.
    For these ten names set order genuinely is not sorted order (checked below), so
    the *n*-th lightest shard receiving the *n*-th **alphabetical** file is a real
    statement about which ordering the deal follows.

    *Mutation: drop the ``sorted`` on ``extra`` and the assignment follows set
    iteration order, which for this input is q, v, t, w, ... rather than q, r, s, t.*
    """
    planned: set[str] = set()
    names = {f"tests/test_{letter}.py" for letter in "zyxwvutsrq"}
    assert list(names) != sorted(names), "pick names whose set order is not sorted"

    dealt = _deal(names, planned, UNEVEN_TOTALS)
    lightest_first = sorted(range(8), key=lambda shard: (UNEVEN_TOTALS[shard], shard))

    for position, name in enumerate(sorted(names)):
        owner = lightest_first[position % 8]
        assert name in dealt[owner], (name, owner, dealt)


def test_two_shards_computing_independently_never_overlap() -> None:
    """Each shard calls this alone, with no knowledge of what the others claimed."""
    planned: set[str] = set()
    known = {f"tests/test_{n:02d}.py" for n in range(17)}

    dealt = _deal(known, planned, UNEVEN_TOTALS)
    for left in range(8):
        for right in range(left + 1, 8):
            assert not set(dealt[left]) & set(dealt[right])


# ===========================================================================
# 3. the defect itself: the heaviest shard stops absorbing the drift
# ===========================================================================


def test_the_heaviest_shard_is_served_last() -> None:
    """**The beta.57 defect, pinned.**

    Shard 1 is heaviest, so it must be the *last* to receive an unplanned file, not
    the first. With seven files and eight shards it should receive none at all.

    *Mutation: restore ``extra = sorted(known - planned) if index == 0 else []`` and
    shard 1 takes all seven.*
    """
    planned: set[str] = set()
    known = {f"tests/test_{n}.py" for n in range(7)}

    dealt = _deal(known, planned, UNEVEN_TOTALS)

    assert dealt[0] == [], "the heaviest shard absorbed the drift again"
    assert sum(len(names) for names in dealt.values()) == 7


def test_the_lightest_shard_is_served_first() -> None:
    """One unplanned file goes to the shard with the most room, not to shard 1."""
    planned: set[str] = set()
    lightest = UNEVEN_TOTALS.index(min(UNEVEN_TOTALS))

    dealt = _deal(planned | {"tests/test_new.py"}, planned, UNEVEN_TOTALS)

    assert dealt[lightest] == ["tests/test_new.py"]
    assert all(dealt[index] == [] for index in range(8) if index != lightest)


def test_equal_shards_fall_back_to_shard_order() -> None:
    """A tie is broken by shard number, so the deal is total rather than arbitrary."""
    planned: set[str] = set()
    known = {"tests/test_a.py", "tests/test_b.py", "tests/test_c.py"}

    dealt = _deal(known, planned, [100.0] * 8)

    assert dealt[0] == ["tests/test_a.py"]
    assert dealt[1] == ["tests/test_b.py"]
    assert dealt[2] == ["tests/test_c.py"]


# ===========================================================================
# 4. the committed manifest, as CI will actually read it
# ===========================================================================


def test_the_committed_manifest_names_no_file_twice_and_none_that_is_gone() -> None:
    """The real artifact, not a fixture. **This is what CI reads.**

    A file in two shards is paid for twice; a file naming nothing on disk is a shard
    spending its budget on a path that no longer exists.

    **Deliberately not asserting that the manifest lists every file on disk.** It is
    allowed to lag -- that is the whole point of the drift rule, and requiring
    equality here would turn "somebody added a test" into a red suite until the
    manifest was regenerated. What may never lag is the *selection*, and the test
    below asserts exactly that.
    """
    root = _SOURCE.parents[1]
    stored = json.loads((root / "tools" / "shards.json").read_text(encoding="utf-8"))
    planned = [name for bucket in stored["files"] for name in bucket]
    on_disk = set(shard_plan.collected_files(root))

    assert len(planned) == len(set(planned)), "a file is planned into two shards"
    assert set(planned) <= on_disk, sorted(set(planned) - on_disk)
    assert len(stored["files"]) == len(stored["projected_seconds"]) == 8


def test_the_manifest_and_the_drift_rule_together_run_everything() -> None:
    """Whatever the manifest's age, the union of the eight shards is the suite.

    Simulated by hiding three files from the manifest and asking each shard what it
    would run -- the same question CI asks, eight times, in eight processes.
    """
    root = _SOURCE.parents[1]
    stored = json.loads((root / "tools" / "shards.json").read_text(encoding="utf-8"))
    buckets = [list(bucket) for bucket in stored["files"]]
    totals = stored["projected_seconds"]
    on_disk = set(shard_plan.collected_files(root))

    hidden = sorted(on_disk)[:3]
    for bucket in buckets:
        for name in hidden:
            if name in bucket:
                bucket.remove(name)
    planned = {name for bucket in buckets for name in bucket}

    selected: list[str] = []
    for index, bucket in enumerate(buckets):
        selected += bucket + shard_plan.unplanned_for(on_disk, planned, totals, index)

    assert sorted(selected) == sorted(on_disk)
    assert len(selected) == len(set(selected))


# ===========================================================================
# 5. the planner itself, and the multi-report reading CI needs
# ===========================================================================


def test_costs_are_summed_across_every_report() -> None:
    """CI uploads one JUnit per shard, so a whole run is eight files, never one."""
    root = pathlib.Path(pytest.__file__).parent  # any real directory
    assert root.exists()

    def _write(tmp: pathlib.Path, name: str, seconds: float) -> pathlib.Path:
        tmp.write_text(
            '<testsuites><testsuite name="pytest">'
            f'<testcase file="tests/test_x.py" name="{name}" time="{seconds}"/>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        return tmp

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        folder = pathlib.Path(raw)
        first = _write(folder / "a.xml", "one", 1.5)
        second = _write(folder / "b.xml", "two", 2.5)

        assert shard_plan.measured_costs(first) == {"tests/test_x.py": 1.5}
        assert shard_plan.measured_costs(first, second) == {"tests/test_x.py": 4.0}


def test_the_planner_cannot_beat_its_largest_file() -> None:
    """Stated because it is the reason a rebalance is not always the answer.

    LPT puts the heaviest file somewhere, and that shard can never be lighter than it.
    When the projected slowest shard equals the largest file, the split is already as
    good as a *per-file* split gets and the file itself is the thing to change.
    """
    costs = {"tests/test_huge.py": 600.0} | {
        f"tests/test_{n}.py": 10.0 for n in range(40)
    }
    buckets, totals = shard_plan.plan(costs, sorted(costs), 8)

    assert max(totals) == pytest.approx(600.0)
    assert sorted(name for bucket in buckets for name in bucket) == sorted(costs)


def test_an_unmeasured_file_is_costed_at_the_median_not_at_zero() -> None:
    """A new file with no timing must not look free, or it lands on a full shard."""
    costs = {f"tests/test_{n}.py": float(n + 1) for n in range(9)}
    files = [*sorted(costs), "tests/test_brand_new.py"]

    _buckets, totals = shard_plan.plan(costs, files, 2)

    assert sum(totals) == pytest.approx(sum(costs.values()) + 5.0)
