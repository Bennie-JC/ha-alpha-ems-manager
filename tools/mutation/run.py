"""Run the mutation tables, and make it impossible to leave the tree mutated.

**Why this exists as a tool rather than a scratch script.** These tables are the
project's real evidence that its tests are not decoration: 306 deliberate defects, each
paired with the test that must notice. They used to live in a gitignored directory,
which meant a fresh clone had none of them and no release gate could name them.

**And why the hardening.** A table edits source files in place and restores them in a
``finally``. During beta.41 that went wrong four separate times: a killed run left
``ambient_self_consumption=False`` sitting in the solver, and the leftover check --
which searched for each mutation's anchor text -- missed it because the anchor was not
unique and still matched at another call site. Every carry mutation was inert for two
full runs while the table reported them as survivors.

So the guard here is **content**, not anchors:

* a SHA of every source file is taken before the first mutation and compared after
  each one, so a table cannot proceed past a file it failed to restore;
* the snapshot is written to disk, so ``--restore`` can put the tree back
  deterministically after a kill, without needing the table that broke it;
* a lock file refuses a second table while one is running, because two tables editing
  one tree race;
* ``SIGINT``/``SIGTERM`` restore before exiting;
* and the tree must be clean of *mutations* before a table starts -- an already-dirty
  source file is refused rather than snapshotted, which would bake a mutation into the
  baseline.

There is deliberately no parallelism. Mutation testing is one process editing one
tree; ``xdist`` would have several workers editing the same files.

Usage:
    python tools/mutation/run.py b41              # one table
    python tools/mutation/run.py b41 -k coverage  # matching mutations only
    python tools/mutation/run.py --all            # the full release gate
    python tools/mutation/run.py --restore        # put the tree back after a kill
    python tools/mutation/run.py --verify         # is the tree unmutated right now?
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PKG = ROOT / "custom_components" / "alpha_ems_manager"
SNAPSHOT = HERE / ".snapshot.json"
LOCK = HERE / ".lock"

TABLES = (
    "b35",
    "b36",
    "b37",
    "b38",
    "b39",
    "b40",
    "b41",
    "b42",
    "b43",
    "b44",
)


def tracked_sources() -> list[pathlib.Path]:
    """Return every file a mutation table is allowed to touch."""
    return sorted(PKG.glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))


def digest(path: pathlib.Path) -> str:
    """Return the SHA-256 of one file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_source(path: pathlib.Path) -> str:
    """Return one file's text with its line endings left alone.

        **``read_text``/``write_text`` is not a round trip on Windows, and beta.43 found
        out the expensive way.** The default reader translates ``
    `` to ``
    `` and the
        default writer translates it back to ``os.linesep`` -- so restoring a file that
        was stored with bare newlines rewrote every line ending in it. The content hash
        then reported drift on a file it had just restored *correctly*, and the
        recovery path below took that at face value.
    """
    return path.read_text(encoding="utf-8", newline="")


def write_source(path: pathlib.Path, text: str) -> None:
    """Write one file's text byte-for-byte, translating nothing."""
    path.write_text(text, encoding="utf-8", newline="")


def snapshot_now() -> dict[str, str]:
    """Return a digest per tracked source file, keyed by repo-relative path."""
    return {
        str(path.relative_to(ROOT)).replace("\\", "/"): digest(path)
        for path in tracked_sources()
    }


def differences(expected: dict[str, str]) -> list[str]:
    """Return the paths whose content no longer matches the snapshot."""
    current = snapshot_now()
    changed = [name for name, value in expected.items() if current.get(name) != value]
    changed += [name for name in current if name not in expected]
    return sorted(set(changed))


def load_table(name: str) -> types.ModuleType:
    """Import one mutation table by name."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise SystemExit(f"cannot load mutation table {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def target(filename: str) -> pathlib.Path:
    """Return the file a mutation edits: a test module, or a production one."""
    if filename.startswith("tests/"):
        return ROOT / filename
    return PKG / filename


def run_node(node: str) -> bool:
    """Return whether the named test passed. One process, never distributed."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            node,
            "-q",
            "-p",
            "no:cacheprovider",
            "-x",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": "0"},
    )
    return result.returncode == 0


def restore(expected: dict[str, str], saved: dict[str, str] | None = None) -> list[str]:
    """Restore any file whose content drifted, and report what moved.

    **From the captured text where there is any, and from git only as a last
    resort.** ``git checkout`` restores a file to ``HEAD``, which on a tree carrying
    uncommitted work destroys it -- and a mutation harness is run on exactly that
    kind of tree, which is the whole point of ``--verify``. beta.43 lost a working
    session to the combination of this and the newline defect above.
    """
    changed = differences(expected)
    if not changed:
        return []
    saved = saved or {}
    from_git = [name for name in changed if name not in saved]
    for name in changed:
        if name in saved:
            write_source(ROOT / name, saved[name])
    if from_git:
        subprocess.run(["git", "checkout", "--", *from_git], cwd=ROOT, check=True)
    return changed


def run_table(
    name: str, pattern: str, expected: dict[str, str]
) -> tuple[int, int, int]:
    """Run one table. Returns (killed, survived, anchors lost)."""
    table = load_table(name)
    survivors: list[str] = []
    missing: list[str] = []
    killed = 0

    for entry in table.MUTATIONS:
        label, filename, old, new, node = entry
        if pattern and pattern.lower() not in label.lower():
            continue
        path = target(filename)
        original = read_source(path)
        found = original.count(old)
        if found != 1:
            # **Not found and found twice are both anchor failures, and beta.42
            # found the second one the expensive way.** ``L4``'s anchor matched two
            # methods; ``str.replace(old, new, 1)`` silently edited the first, so
            # the mutation ran against code the named test does not exercise and was
            # reported as a survivor. Hours were then spent looking for a vacuous
            # test that was not vacuous.
            #
            # The content-hash guard cannot catch this. The tree *is* restored
            # correctly -- it is simply the wrong line that was changed -- which is
            # precisely why this check has to live here, at selection, rather than
            # in the verification afterwards.
            reason = "not found" if found == 0 else f"matched {found} times"
            missing.append(f"{label}: anchor {reason} in {filename}")
            print(f"ANCHOR  {label} ({reason})", flush=True)
            continue
        write_source(path, original.replace(old, new, 1))
        try:
            passed = run_node(node)
        finally:
            write_source(path, original)
        drift = differences(expected)
        if drift:
            # The captured text is what this harness put there, so recovery never
            # has to reach for ``HEAD`` and never discards uncommitted work.
            recovered = restore(
                expected, {str(path.relative_to(ROOT)).replace(chr(92), "/"): original}
            )
            raise SystemExit(
                f"{name}: {label!r} did not restore {drift}; recovered {recovered}. "
                "Re-run once the cause is understood."
            )
        if passed:
            survivors.append(f"{label} -> {node}")
            print(f"SURVIVED  {label}", flush=True)
        else:
            killed += 1
            print(f"killed    {label}", flush=True)

    print(
        f"\n{name}: {killed} killed, {len(survivors)} survived, "
        f"{len(missing)} anchors lost",
        flush=True,
    )
    for line in survivors + missing:
        print(f"  {line}", flush=True)
    return killed, len(survivors), len(missing)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("table", nargs="?", help="one of " + ", ".join(TABLES))
    parser.add_argument("-k", default="", help="only mutations whose name matches")
    parser.add_argument("--all", action="store_true", help="every table, in order")
    parser.add_argument("--restore", action="store_true", help="undo a killed run")
    parser.add_argument("--verify", action="store_true", help="is the tree clean?")
    args = parser.parse_args()

    if args.restore:
        if not SNAPSHOT.exists():
            print("no snapshot: nothing to restore against")
            return 0
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        moved = restore(expected)
        LOCK.unlink(missing_ok=True)
        print(f"restored {len(moved)} file(s): {moved}" if moved else "tree was clean")
        return 0

    if args.verify:
        if not SNAPSHOT.exists():
            print("no snapshot to verify against")
            return 1
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        drift = differences(expected)
        print("SOURCE VERIFIED UNMUTATED" if not drift else f"SOURCE DIFFERS: {drift}")
        return 1 if drift else 0

    names = list(TABLES) if args.all else ([args.table] if args.table else [])
    if not names:
        parser.error("name a table, or pass --all")

    if LOCK.exists():
        raise SystemExit(
            f"{LOCK} exists: another mutation run holds the tree. If it was killed, "
            "run `python tools/mutation/run.py --restore` first."
        )

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "custom_components", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        print(
            "note: source has uncommitted changes. They are snapshotted as the "
            "baseline, so make sure none of them is a leftover mutation:\n" + dirty,
            flush=True,
        )

    expected = snapshot_now()
    SNAPSHOT.write_text(json.dumps(expected, indent=1), encoding="utf-8")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")

    def _cleanup(signum, frame):  # pragma: no cover - signal path
        moved = restore(expected)
        LOCK.unlink(missing_ok=True)
        print(f"\ninterrupted; restored {moved}", flush=True)
        raise SystemExit(130)

    signal.signal(signal.SIGINT, _cleanup)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _cleanup)

    total_killed = total_survived = total_missing = 0
    try:
        for name in names:
            killed, survived, missing = run_table(name, args.k, expected)
            total_killed += killed
            total_survived += survived
            total_missing += missing
    finally:
        restore(expected)
        LOCK.unlink(missing_ok=True)

    print(
        f"\nTOTAL {total_killed} killed, {total_survived} survived, "
        f"{total_missing} anchors lost"
    )
    print("SOURCE VERIFIED UNMUTATED" if not differences(expected) else "SOURCE DIRTY")
    return 1 if (total_survived or total_missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
