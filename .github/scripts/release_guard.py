"""Release guard: fail before anything is published unless the tag and the package version
agree.

    release_guard.py TAG [--dist DIR]

- TAG must be `vX.Y.Z` or `vX.Y.Z-PRERELEASE` (the release workflow's tag filter);
- `pyproject.toml`'s `[project].version` must equal TAG's version (PEP 440 comparison, so
  `v0.3.0-rc.1` matches `0.3.0rc1`);
- with `--dist`, every wheel's and sdist's metadata version in DIR must match too (what is
  actually uploaded, not just what pyproject says).

This is the single owner of the *version* checks. The CHANGELOG section is checked by
`verify-changelog-entry.sh`, through the same resolver (`changelog-section.sh`) that
`extract-release-notes.sh` uses for the GitHub Release body, so the gate and the notes can
never disagree about which section documents a tag. The git checks (dispatch ref, tag at
HEAD, tag on main) live in `verify-release-tag.sh`, which calls this script last.

Run from the repository root with `packaging` available, e.g.
`uv run --no-project --with packaging python .github/scripts/release_guard.py "$TAG"`
(`make release-guard TAG=vX.Y.Z` also checks the CHANGELOG section).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tarfile
import tomllib
import zipfile
from email.parser import HeaderParser
from pathlib import Path

from packaging.version import InvalidVersion, Version

_TAG_RE = re.compile(r"v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")


def _metadata_version(raw: bytes) -> str:
    return HeaderParser().parsestr(raw.decode()).get("Version", "")


def _dist_versions(dist: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for whl in sorted(dist.glob("*.whl")):
        with zipfile.ZipFile(whl) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
            found.append((whl.name, _metadata_version(zf.read(name))))
    for sdist in sorted(dist.glob("*.tar.gz")):
        with tarfile.open(sdist) as tf:
            member = next(
                m
                for m in tf.getmembers()
                if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")
            )
            f = tf.extractfile(member)
            assert f is not None
            found.append((sdist.name, _metadata_version(f.read())))
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag")
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args(argv)

    errors: list[str] = []
    m = _TAG_RE.fullmatch(args.tag)
    if m is None:
        print(
            f"release guard: tag {args.tag!r} is not vX.Y.Z or vX.Y.Z-PRERELEASE", file=sys.stderr
        )
        return 1
    tag_version = m.group(1)
    try:
        want = Version(tag_version)
    except InvalidVersion:
        print(f"release guard: tag {args.tag!r} is not a valid PEP 440 version", file=sys.stderr)
        return 1

    project_version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    if Version(project_version) != want:
        errors.append(
            f"pyproject.toml version {project_version} does not match tag {args.tag} — bump "
            "[project].version (and run `uv lock`) before tagging"
        )

    if args.dist is not None:
        dists = _dist_versions(args.dist)
        names = ", ".join(name for name, _ in dists)
        if not dists:
            errors.append(f"no wheel or sdist found in {args.dist}")
        for name, version in dists:
            if not version or Version(version) != want:
                errors.append(f"{name} has metadata version {version!r}, not {want}")

    for e in errors:
        # A GitHub Actions error annotation in CI; a plain message elsewhere.
        prefix = "::error title=Release guard::" if os.environ.get("GITHUB_ACTIONS") else ""
        print(f"{prefix}release guard: {e}", file=sys.stderr)
    if errors:
        return 1
    if args.dist is not None:
        print(f"release guard: built artifacts carry version {want}: {names}")
    print(f"release guard: tag {args.tag} and pyproject.toml version {project_version} agree")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
