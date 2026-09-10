"""Balance test shards by measured runtime, not by filename.

**Why measured.** The suite's cost is wildly uneven: on beta.41, four files were
60 % of all processor time and one test alone was 11 % of it. A shard split by name,
or by test count, puts the expensive files wherever the alphabet happens to send them
and the slowest shard sets the wall-clock for everyone. So membership is decided here,
from a JUnit XML the suite already emits, and nowhere else -- there is no
hand-maintained list to drift.

**The algorithm is longest-processing-time-first**, which is the standard greedy
bound for this problem: sort files by measured cost descending, and put each into
whichever shard is currently lightest. It cannot beat the largest single file, so the
projected slowest shard is reported next to the ideal -- if those two are far apart,
the answer is to split a file, not to add shards.

A file with no timing (new, or renamed since the artifact was produced) is assigned
the median cost rather than zero, so an unmeasured file is never silently treated as
free. A file missing from a *committed manifest* is handled separately -- see
:func:`unplanned_for`.

Usage:
    python scripts/shard_plan.py timings.xml --shards 8
    python scripts/shard_plan.py timings.xml --shards 8 --json manifest.json
    python scripts/shard_plan.py timings.xml --shards 8 --shard 2   # that shard's files

**Regenerating the committed manifest from a real CI run.** The timings come from
the eight ``timings-shard-N`` artifacts CI uploads, and they are the only figures
that reflect the runner rather than a developer's machine. There is no bot: this is
run by hand, and it should be run whenever the split has drifted -- which the header
of a plan tells you, by printing the projected slowest shard next to the ideal.

    gh run download <run-id> --pattern 'timings-shard-*' --dir timings/
    python scripts/shard_plan.py timings/*/junit-*.xml --shards 8 \\
        --json tools/shards.json

Between regenerations nothing is lost: a file the manifest has never seen is still
run, distributed by :func:`unplanned_for` rather than dropped.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import xml.etree.ElementTree as ET


def measured_costs(*reports: pathlib.Path) -> dict[str, float]:
    """Return seconds per test file, summed over every case in every report.

    **Several reports, because CI produces several.** Each shard uploads its own
    JUnit XML, so the timings for a whole suite arrive as eight artifacts and never
    as one file. Summing across them here is what lets the manifest be regenerated
    from a real run rather than from a local approximation of one; a single report
    still works and is the same call with one argument.
    """
    costs: dict[str, float] = {}
    for report in reports:
        for case in ET.parse(report).iter("testcase"):
            name = case.get("file") or (case.get("classname") or "").replace(".", "/")
            if not name:
                continue
            if not name.endswith(".py"):
                name = f"{name}.py"
            costs[name] = costs.get(name, 0.0) + float(case.get("time") or 0.0)
    return costs


def collected_files(root: pathlib.Path) -> list[str]:
    """Return every test file on disk, so a new one is sharded rather than skipped."""
    return sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in (root / "tests").glob("test_*.py")
    )


def unplanned_for(
    known: set[str], planned: set[str], totals: list[float], index: int
) -> list[str]:
    """Return the un-manifested files this shard owns. **Never silently nobody's.**

    A file added since the manifest was written still has to run, and until beta.57
    every one of them went to shard 1 -- which is the shard the manifest already
    loads heaviest, because the largest single file lives there and LPT cannot split
    it. So each new test file was appended to the worst shard, and the imbalance grew
    with the suite until somebody regenerated the manifest by hand. On the beta.57
    run that was three files on top of a shard already at 2.07x the ideal.

    Dealt out **lightest projected shard first**, cycling. That reuses the figure the
    manifest already carries rather than inventing a second cost model, and it is a
    deal rather than a hash: position *i* of the sorted list goes to the *i*-th
    lightest shard, so every file lands in exactly one shard and no shard is skipped
    before another is used twice. Sorting the names, and breaking a tie on shard load
    by shard number, makes the answer identical on every worker -- which is the
    property that matters, since eight processes compute this independently and a
    disagreement means a test runs twice or not at all.

    This is a fallback, not a plan: it distributes rather than balances, because the
    cost of a brand-new file is exactly what nobody knows yet. Regenerating the
    manifest is still what makes a file's cost count.
    """
    extra = sorted(known - planned)
    if not extra:
        return []
    lightest_first = sorted(
        range(len(totals)), key=lambda shard: (totals[shard], shard)
    )
    return [
        name
        for position, name in enumerate(extra)
        if lightest_first[position % len(lightest_first)] == index
    ]


def plan(
    costs: dict[str, float], files: list[str], shards: int
) -> tuple[list[list[str]], list[float]]:
    """Return the files per shard and the projected seconds per shard."""
    if shards < 1:
        raise SystemExit("a plan needs at least one shard")
    known = [value for value in costs.values() if value > 0.0]
    fallback = statistics.median(known) if known else 1.0
    weighted = sorted(
        ((costs.get(name, fallback), name) for name in files), reverse=True
    )
    buckets: list[list[str]] = [[] for _ in range(shards)]
    totals = [0.0] * shards
    for cost, name in weighted:
        lightest = min(range(shards), key=lambda index: totals[index])
        buckets[lightest].append(name)
        totals[lightest] += cost
    return [sorted(bucket) for bucket in buckets], totals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "report",
        nargs="*",
        type=pathlib.Path,
        help="one or more JUnit XMLs from pytest; CI produces one per shard",
    )
    parser.add_argument(
        "--manifest",
        type=pathlib.Path,
        default=None,
        help="read a committed manifest instead of re-planning from a report",
    )
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument(
        "--shard", type=int, default=None, help="print only this shard's files, 1-based"
    )
    parser.add_argument("--json", type=pathlib.Path, default=None)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    args = parser.parse_args()

    if args.manifest:
        # **Consuming a committed plan, not re-deriving one.** CI must run the same
        # split every job sees, and re-planning per shard from a report that may not
        # be present would let two shards disagree about who owns a file -- which
        # shows up as a test running twice, or not at all.
        stored = json.loads(args.manifest.read_text(encoding="utf-8"))
        buckets = stored["files"]
        totals = stored.get("projected_seconds", [0.0] * len(buckets))
        if args.shard is None:
            raise SystemExit("--manifest needs --shard")
        index = args.shard - 1
        if not 0 <= index < len(buckets):
            raise SystemExit(f"shard {args.shard} is outside 1..{len(buckets)}")
        known = set(collected_files(args.root.resolve()))
        planned = {name for bucket in buckets for name in bucket}
        print(" ".join(buckets[index] + unplanned_for(known, planned, totals, index)))
        return 0

    if not args.report:
        raise SystemExit("give a JUnit report, or --manifest with --shard")
    costs = measured_costs(*args.report)
    files = collected_files(args.root.resolve())
    buckets, totals = plan(costs, files, args.shards)

    if args.shard is not None:
        index = args.shard - 1
        if not 0 <= index < args.shards:
            raise SystemExit(f"shard {args.shard} is outside 1..{args.shards}")
        print(" ".join(buckets[index]))
        return 0

    total = sum(totals)
    ideal = total / args.shards if args.shards else 0.0
    slowest = max(totals) if totals else 0.0
    heaviest = max(costs.values(), default=0.0)

    for number, (bucket, seconds) in enumerate(zip(buckets, totals, strict=True), 1):
        print(f"shard {number}: {seconds / 60:6.2f} min  {len(bucket):3d} files")
    print()
    print(f"measured total     {total / 60:7.2f} min across {len(costs)} timed files")
    print(f"ideal per shard    {ideal / 60:7.2f} min")
    print(f"projected slowest  {slowest / 60:7.2f} min  ({slowest / ideal:.2f}x ideal)")
    print(f"largest one file   {heaviest / 60:7.2f} min  -- the floor for any split")

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "shards": args.shards,
                    "files": buckets,
                    "projected_seconds": totals,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
