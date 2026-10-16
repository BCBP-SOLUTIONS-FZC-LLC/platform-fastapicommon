# Versioning and releases

This repository is a **Python package** (`platform-fastapicommon` on PyPI, imported as `fastapicommon`) consumed by platform services. Versions are published with **Git tags**, released to **PyPI**, and described in [CHANGELOG.md](./CHANGELOG.md).

## Semantic versioning (SemVer)

We use [SemVer 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH` (e.g. `v0.2.0`).

| Bump | When you change | Examples |
|------|------------------|----------|
| **MAJOR** | Breaking change in the **public** API (each subpackage's `__all__`) | Removed export, renamed function or parameter, changed `RequestContext` / `CurrentUser` fields, changed problem types or metric names, incompatible default behaviour in `create_platform_app` |
| **MINOR** | New backward-compatible capability | New middleware or dependency, new exported symbol, new optional `PlatformSettings` field |
| **PATCH** | Backward-compatible fix | Bug fix, performance, docs-only correction in published API behaviour |

### What counts as public API

| In scope (SemVer applies) | Out of scope (may change without MAJOR) |
|---------------------------|----------------------------------------|
| Every symbol in a subpackage's `__all__` (`fastapicommon.auth`, `.metrics`, `.grpc`, ...; [ARCHITECTURE.md § Public API](./ARCHITECTURE.md#public-api)) | Names starting with `_` (`_errors`, `_scope`, `_context_from_metadata`, ...) and submodule paths (`fastapicommon.auth.dependencies`) when the re-export is unchanged |
| Documented `PlatformSettings` fields, env vars and their defaults | `examples/` (reference services) |
| Metric names and label sets ([registry](./docs/observability/metric-registry.md)), problem types and codes, log event names and fields | `tests/` |
| The `fastapicommon-metricslint` commands, flags and exit codes | The `add_middleware` call order (the documented behaviour is covered, not the implementation) |

Import **`fastapicommon.<subpackage>`** only. Python cannot enforce this the way Go's `internal/` does; `make api-compat` (griffe) reports breaks against the last tag.

### Guarantees

- **Pre-1.0 (now):** while the package is `0.y.z`, a **MINOR may break** (SemVer §4); every break is listed under *Changed (breaking)* / *Removed (breaking)* in CHANGELOG with migration steps. A PATCH never breaks.
- **MAJOR `v1` (once released):** we avoid breaking changes in the public API within `v1.x`; breaking changes ship as `v2.0.0` with migration in CHANGELOG.
- **MINOR:** after `1.0.0`, safe to upgrade with `pip install -U` without code changes unless you opt into new features.
- **PATCH:** drop-in replacement; upgrade recommended for security fixes.

### Recorded exceptions

- **`0.3.0` ships the platform-gincommon `v2.0.0` alignment as a MINOR.** It requires `OBSERVABILITY_DOMAIN` and a vocabulary `APP_ENV` while metrics are on, removes the legacy metric names, moves logs to stderr in Zap's layout, exports traces to `localhost:4317` by default, changes every error body to RFC 9457, and makes repeated identity headers unauthenticated. This is allowed by the pre-1.0 rule above and no consumer had been deployed to any environment. It is still recorded here because each item would be a MAJOR after `1.0.0`. Do **not** let an unpinned install pull `0.3.0` into a service without following [Upgrading to 0.3.0](./README.md#10-upgrading-to-030) and `docs/observability/migration.md`.

## Supported releases

| Version | Status | PyPI | Supported until |
|---------|--------|------|-----------------|
| `0.3.x` | **Next** (`[Unreleased]`) | — | Active once released |
| `0.2.x` | **Current** (from `v0.2.0`) | `platform-fastapicommon==0.2.0` | Security fixes until `0.3.0` ships |
| `0.1.x` | Superseded | `==0.1.1` | No further releases; upgrade to `0.2.x` |

Releases up to `v0.2.0` predate release signing and `SHA256SUMS`. When `1.0.0` ships, a new MAJOR line gives the previous major **security fixes only** for a period the platform team defines (typically 6 months).

## Consume a release

Pin in your service's `pyproject.toml` / requirements:

```bash
pip install "platform-fastapicommon==0.2.0"          # or "platform-fastapicommon[grpc]==0.2.0"
uv add "platform-fastapicommon[grpc]==0.2.0"
```

| Pin style | Use when |
|-----------|----------|
| `==0.2.0` | Production; exact reproducibility (recommended while `0.y.z`) |
| `~=0.2.0` | Production; accept PATCH updates on `0.2.x` |
| `>=0.2,<0.3` | The same, written as a range |
| unpinned / `>=0.2` | Experiments only; never in production, especially pre-1.0 |

pip resolves versions from **[PyPI](https://pypi.org/project/platform-fastapicommon/)**. The distribution is `platform-fastapicommon`; the import is `fastapicommon` (`fastapicommon` alone was rejected by PyPI as too similar to an unrelated package).

## Publishing to PyPI

The package publishes to public PyPI with **[Trusted Publishing](https://docs.pypi.org/trusted-publishers/)** (OIDC). There is no API token to create, store or rotate, and no GitHub secret is involved: `release.yml`'s `pypi-publish` job requests a short-lived token with `permissions: id-token: write`.

The GitHub repository is **private**, while the package on PyPI is intentionally **public**. Trusted Publishing checks only the token's claims (owner, repository, workflow, environment), not the repository's visibility. A published sdist / wheel contains the source, so together with the `Apache-2.0` license in `pyproject.toml` this library's code is effectively open source.

### One-time setup (human steps — cannot be done from this repo)

1. **PyPI account** at [pypi.org/account/register](https://pypi.org/account/register/), with 2FA (PyPI requires it). A [TestPyPI](https://test.pypi.org/account/register/) account is optional for a dry run.
2. **Trusted Publisher**, registered at [pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/) as a [pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) before the first upload:

   | Field | Value |
   |-------|-------|
   | PyPI project name | `platform-fastapicommon` |
   | Owner | `BCBP-SOLUTIONS-FZC-LLC` |
   | Repository name | `platform-fastapicommon` |
   | Workflow filename | `release.yml` |
   | Environment name | `pypi` |

3. **`pypi` GitHub Environment** (Settings → Environments), optionally with required reviewers so every publish waits for approval. Its deployment rules allow tag `v*` and branch `main`. The branch rule is needed because a `workflow_dispatch` re-run executes on the selected branch, not on the tag.

## Maintainer release process

The release workflow (`.github/workflows/release.yml`) automates validation, the PyPI publish, the image push and GitHub Release creation. Your job is to prepare the commit and push the tag.

1. **Bump the version** in `pyproject.toml` (`[project].version`), `src/fastapicommon/__init__.py` (`__version__`) and `tests/unit/test_package.py`.

2. **Update CHANGELOG.md:** move `[Unreleased]` entries into a new `## [X.Y.Z] - YYYY-MM-DD` section, and update the compare links at the bottom.
   The release workflow reads this section for the GitHub Release body — the release fails with a clear error if it is missing or undated.

3. **Run `make ci` and the release guard locally** to confirm everything is green before tagging:
   ```bash
   make ci                         # every CI gate, with the coverage gate
   make release-guard TAG=vX.Y.Z   # pyproject version == tag, dated CHANGELOG section
   ```

4. **Merge** to `main` — the tag must be reachable from `origin/main`.

5. **Create and push an annotated tag** — this triggers the release workflow automatically:
   ```bash
   git tag -a v0.3.0 -m "v0.3.0"
   git push origin v0.3.0
   ```

6. **The release workflow** (triggered by the tag) will:
   - **Verify** the tag, the package version, the dated CHANGELOG section and which floating image tags may move.
   - **Re-run the `ci.yml` gates** at the tagged commit (tests on the Python matrix and at the lowest dependencies, quality, Hadolint, Trivy, smoke, interop), plus a **blocking** `api-compat`.
   - **Build** the wheel and sdist once, reproducibly from the hash-locked `build-constraints.txt`, with `SHA256SUMS`.
   - **Push** the example image to GHCR (`X.Y.Z`, forward-only `X.Y` / `X`, no `latest`), but only if its content fingerprint matches the image Trivy and smoke judged. The image is Cosign-signed and carries an SBOM and SLSA provenance.
   - **Publish to PyPI**, then create the **[GitHub Release](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon/releases)** with the CHANGELOG section, the same files and a Cosign-signed `SHA256SUMS`. PyPI comes after the image push because a PyPI version can never be re-uploaded.

7. **Notify consumers** (Slack/ADR) with upgrade notes if MINOR or MAJOR.

### Pre-release tags (optional)

| Tag pattern | Meaning |
|-------------|---------|
| `v0.3.0-rc.1` | Release candidate; not for production unless approved |
| `v0.3.0-beta.1` | Early integration testing |

Pre-releases never move the floating `X.Y` / `X` image tags.

### Verifying a release

Needs [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) ≥ 2.4. A tag-triggered release is signed by `release.yml@refs/tags/<tag>`. A re-run dispatched from `main` is signed by `release.yml@refs/heads/main`, which the regular expression below also accepts.

```bash
TAG=vX.Y.Z   # the release to verify
REPO=BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon
IDENTITY="^https://github\.com/${REPO}/\.github/workflows/release\.yml@refs/(tags/${TAG}|heads/main)$"

# Wheel + sdist: verify the signed checksums, then the files against them
gh release download "$TAG" -R "$REPO" -p SHA256SUMS -p SHA256SUMS.sigstore.json -p '*.whl' -p '*.tar.gz'
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
sha256sum --check SHA256SUMS   # macOS: shasum -a 256 -c SHA256SUMS

# Example service image (tag without the v)
cosign verify --certificate-identity-regexp "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "ghcr.io/bcbp-solutions-fzc-llc/platform-fastapicommon:${TAG#v}"
```

The files on PyPI are byte-identical to the release's wheel and sdist (`SHA256SUMS` matches them, and PyPI carries PEP 740 attestations). Releases up to `v0.2.0` predate signing and `SHA256SUMS`.

## Compatibility matrix (library ↔ Python)

| platform-fastapicommon | Python (`requires-python`) | Tested in CI | Aligned with |
|------------------------|----------------------------|--------------|--------------|
| `0.1.x`–`0.2.x` | `>=3.11` | 3.11, 3.12, 3.13, 3.14 | platform-gincommon `v1.x` wire contract |
| `0.3.x` | `>=3.11` | 3.11, 3.12, 3.13, 3.14, plus every dependency at its floor (`validate-test.yml`) | platform-gincommon `v2.0.0` (registry, RFC 9457, logs, traces) |

Consumer services must run a Python version this package's `requires-python` allows. Raising the floor changes `requires-python` and the CI matrix together, and is a MAJOR-level change after `1.0.0`.

## Related files

| File | Purpose |
|------|---------|
| [CHANGELOG.md](./CHANGELOG.md) | User-facing history per version |
| [README.md](./README.md#versioning--releases) | Summary table and quick links |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Development guide and PR checklist |
| [SECURITY.md](./SECURITY.md) | Supported versions, trust model, vulnerability reporting |
| [.github/workflows/release.yml](./.github/workflows/release.yml) | Automated release pipeline |
| [pyproject.toml](./pyproject.toml) | Package metadata, version and minimum Python version |
