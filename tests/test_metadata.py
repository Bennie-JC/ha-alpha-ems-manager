"""Repository metadata must not claim support we do not have.

`hacs.json` previously declared Home Assistant 2024.1.0 while the code used APIs
introduced well after it, so an installation on that core would have crashed
immediately. These tests keep the declared floor honest and consistent.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

COMPONENT = Path("custom_components/alpha_ems_manager")

#: The lowest Home Assistant release this project claims to support.
MINIMUM_HA = "2025.1.0"


def read_json(path: Path) -> dict:
    """Return a parsed JSON document."""
    return json.loads(path.read_text(encoding="utf-8"))


#: The single authoritative version. Home Assistant's manifest is the only place
#: the integration version is declared; every other reference is checked against
#: it rather than repeating it.
VERSION: str = read_json(COMPONENT / "manifest.json")["version"]


def test_hacs_declares_the_supported_minimum() -> None:
    """HACS blocks installation below this version."""
    assert read_json(Path("hacs.json"))["homeassistant"] == MINIMUM_HA


def test_the_readme_documents_the_same_minimum() -> None:
    """A user reading the README gets the same answer HACS enforces."""
    readme = Path("README.md").read_text(encoding="utf-8")

    assert f"Minimum Home Assistant version: {MINIMUM_HA}" in readme
    # The old, wrong floor must not linger anywhere in the document.
    assert "2024.1.0" not in readme


def test_the_architecture_notes_document_the_same_minimum() -> None:
    """The architecture reference agrees too."""
    notes = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

    assert MINIMUM_HA in notes
    assert "2024.1.0" not in notes


def test_the_minimum_is_at_least_as_new_as_the_apis_used() -> None:
    """The floor is justified by what the code actually calls.

    ``entry.runtime_data`` and generic ``ConfigEntry`` typing arrived in 2024.6
    and coordinator ``config_entry`` support in 2024.8, so anything below 2025.1
    would be optimistic at best.
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in COMPONENT.glob("*.py")
    )
    assert "runtime_data" in sources
    assert "config_entry=entry" in sources

    major, minor, _patch = MINIMUM_HA.split(".")
    assert (int(major), int(minor)) >= (2024, 8)


def test_the_manifest_is_internally_consistent() -> None:
    """Required keys are present and correctly ordered for hassfest."""
    manifest = read_json(COMPONENT / "manifest.json")

    required = {"domain", "name", "codeowners", "documentation", "iot_class"}
    assert required <= set(manifest)
    assert manifest["domain"] == "alpha_ems_manager"
    assert manifest["requirements"] == []

    after_first_two = [key for key in manifest if key not in ("domain", "name")]
    assert after_first_two == sorted(after_first_two)


@pytest.mark.parametrize("language", ["en", "nl"])
def test_the_translation_files_parse(language: str) -> None:
    """Both bundles are valid JSON with the expected top-level sections."""
    payload = read_json(COMPONENT / "translations" / f"{language}.json")

    assert {"config", "options", "selector", "exceptions"} <= set(payload)


def test_the_changelog_preserves_the_historical_release() -> None:
    """The 0.1.0 entry is history and must not be rewritten or dropped."""
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [0.1.0] - 2026-06-14" in changelog
    # Its original wording, describing the architecture that actually shipped.
    assert "1-minute interval" in changelog
    assert "Binary sensor: reserve satisfied." in changelog


def test_the_changelog_documents_the_current_release() -> None:
    """The version in the manifest has a matching changelog section."""
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [Unreleased]" in changelog
    assert f"## [{VERSION}]" in changelog


def test_no_stable_release_is_claimed_anywhere() -> None:
    """The guard that matters most: this must not read as a stable 1.0.0.

    Learning and forecast behaviour has not been validated across enough real
    days. A stable section in the changelog, or a stable manifest version, would
    be a promise the project cannot currently keep.
    """
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [1.0.0]" not in changelog
    assert "## [0.2.0]" not in changelog
    assert VERSION != "1.0.0"
    assert "-beta." in VERSION


def test_the_manifest_version_is_valid_semver_prerelease() -> None:
    """HACS and Home Assistant both parse this string; a typo breaks install."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-(?:alpha|beta|rc)\.(\d+)", VERSION)

    assert match is not None, f"{VERSION!r} is not a SemVer pre-release"


def test_the_readme_states_the_current_version_and_beta_status() -> None:
    """A user landing on the repository must not mistake this for stable."""
    readme = Path("README.md").read_text(encoding="utf-8")

    assert VERSION in readme
    assert "public beta" in readme.lower()


def test_no_hacs_default_inclusion_is_claimed() -> None:
    """The project is not in the HACS default repository, and must not imply it.

    Claiming default inclusion would be false, and would send users looking for
    an entry in HACS that does not exist.
    """
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "not in the HACS default repository" in readme
    assert "custom repositor" in readme.lower()


def test_the_private_development_notes_are_not_part_of_the_repository() -> None:
    """Local assistant/editor instruction files must never be committed.

    The public developer reference is ``docs/ARCHITECTURE.md``. Anything named
    below is a personal working aid, is listed in ``.gitignore``, and would leak
    local workflow into the published repository.
    """
    tracked = set(
        subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )

    for private in (
        "CLAUDE.md",
        "CLAUDE.local.md",
        "GEMINI.md",
        "AGENTS.md",
        ".cursorrules",
        "NOTES.md",
    ):
        assert private not in tracked, f"{private} must not be tracked"

    # Checked on disk rather than in the index, so this passes in a working tree
    # where the file is staged, committed or merely present.
    assert Path("docs/ARCHITECTURE.md").is_file()


#: Markers that would credit a tool rather than the author. Checked as
#: lower-case substrings against every tracked text file, because the risk is a
#: generated trailer or a stray "assisted by" line slipping into a commit
#: message template, a changelog entry or a docstring -- not deliberate prose.
_ATTRIBUTION_MARKERS = (
    "co-authored-by: claude",
    "co-authored-by: chatgpt",
    "co-authored-by: openai",
    "co-authored-by: gemini",
    "co-authored-by: copilot",
    "generated with claude",
    "generated by claude",
    "generated with chatgpt",
    "generated by chatgpt",
    "generated with ai",
    "generated by ai",
    "ai-generated",
    "ai generated",
    "ai-assisted",
    "ai assisted",
    "written by claude",
    "claude.ai/code",
    "anthropic",
    "openai",
)

#: Extensions worth scanning. Binary assets cannot carry a trailer.
_TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _tracked_files() -> list[Path]:
    """Return every file git is tracking, as paths."""
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [Path(name) for name in output.split("\0") if name]


def test_no_tracked_file_credits_an_assistant_instead_of_the_author() -> None:
    """Bennie is the sole author, and the repository must say only that.

    The failure this guards against is mechanical rather than dishonest: a
    commit-message trailer, a release-note footer or a scaffolded docstring
    carrying a tool's name into a published repository. Scanning every tracked
    text file catches it wherever it lands.
    """
    # This file is the one legitimate place the markers appear: it has to name
    # them to search for them. Excluded by path rather than by obfuscating the
    # list, which would make the list itself unreadable.
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    for path in _tracked_files():
        if path.suffix.lower() not in _TEXT_SUFFIXES or not path.is_file():
            continue
        if path.resolve() == self_path:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        offenders.extend(
            f"{path}: {marker}" for marker in _ATTRIBUTION_MARKERS if marker in text
        )
    assert offenders == [], offenders


def test_no_commit_message_credits_an_assistant_instead_of_the_author() -> None:
    """**The gap the tracked-file scan above could not see.**

    That test reads file *contents*. A ``Co-Authored-By`` trailer lives in the
    commit object, which no tracked file contains -- so a trailer could land in
    every commit of a release and the suite would stay green. It did: the first
    beta.39 commit carried one, and nothing failed.

    Scanned over the **whole published line** -- every commit reachable from the
    current branch or from any tag -- rather than the last few commits, because
    "we cleaned the ones we remembered" is how the next one gets missed. Author
    and committer identity are checked as well: the credit can be in the header as
    easily as in the body.

    ``--all`` is deliberately *not* used. This repository keeps local
    ``refs/backup/*``, ``refs/original/*`` and ``backup/pre-rewrite-*`` refs from
    an earlier history cleanup, whose entire purpose is to preserve the state
    before that cleanup. Six of those commits carry trailers; none is reachable
    from a tag or from ``main``, and none was ever pushed. Failing on them would
    only make the guard something a maintainer has to argue with, and the pressure
    would be to loosen the markers rather than to keep the published history
    clean.
    """
    log = subprocess.run(
        [
            "git",
            "log",
            "HEAD",
            "--tags",
            "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    offenders: list[str] = []
    for record in log.split("\x1e"):
        if not record.strip():
            continue
        sha, author, author_email, committer, committer_email, body = (
            record.strip().split("\x1f", 5)
        )
        haystack = " ".join(
            (author, author_email, committer, committer_email, body)
        ).lower()
        offenders.extend(
            f"{sha[:12]}: {marker}"
            for marker in _ATTRIBUTION_MARKERS
            if marker in haystack
        )
        # Any co-author trailer at all, whoever it names: this is a single-author
        # project, and a trailer here is a tool's footer rather than a person.
        if "co-authored-by:" in body.lower():
            offenders.append(f"{sha[:12]}: co-authored-by trailer")

    assert offenders == [], offenders


def test_the_declared_author_is_the_maintainer() -> None:
    """The manifest names a human code owner and nothing else."""
    manifest = read_json(COMPONENT / "manifest.json")

    assert manifest["codeowners"] == ["@Bennie-JC"]
    assert all(owner.startswith("@") for owner in manifest["codeowners"])
