# Versioning and releases

This repository is a **Python package** (`fastapicommon`) consumed by platform services.
Versions are published with **Git tags** and described in [CHANGELOG.md](./CHANGELOG.md).

## Semantic versioning (SemVer)

We use [SemVer 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH` (e.g. `v0.2.0`).

| Bump | When you change | Examples |
|------|------------------|----------|
| **MAJOR** | Breaking change in the public API | Removed export, renamed a public function/class, changed `RequestContext`/`CurrentUser` field names, incompatible default behavior in `create_platform_app` |
| **MINOR** | New backward-compatible capability | New middleware, new exported symbol, new optional `PlatformSettings` field |
| **PATCH** | Backward-compatible fix | Bug fix, performance, docs-only correction in published API behavior |

**Pre-1.0 caveat (SemVer §4):** while the package is `0.y.z`, the public API is still
considered unstable — a `MINOR` bump (`0.1.0` → `0.2.0`) may include breaking changes, same
as any `0.y.z` package under the SemVer spec. Once `1.0.0` ships, the MAJOR/MINOR/PATCH
guarantees above apply strictly, matching `platform-gincommon`'s `v1.x` policy.

### What counts as public API

| In scope (SemVer applies) | Out of scope (may change without MAJOR) |
|----------------------------|------------------------------------------|
| Every symbol re-exported from a subpackage's `__all__` (see the tables in [`ARCHITECTURE.md`](./ARCHITECTURE.md#public-api)) | Any name prefixed with `_` (e.g. `_build_span_exporter`, `_context_from_metadata`, `_ClientCallDetails`, `_PropagationAuth`) |
| Documented `PlatformSettings` fields and their defaults | `examples/` (reference/demo services only) |
| Documented env vars consumed by `PlatformSettings` (`APP_NAME`, `OTLP_ENDPOINT`, etc.) | `tests/` |
| Prometheus metric names and label sets in the [metrics table](./ARCHITECTURE.md#registered-prometheus-metrics) | Internal middleware ordering *implementation* (the documented *behavior* — e.g. context available to the whole stack — is covered; the specific `add_middleware` call order is not) |

Import from the top-level `fastapicommon.<module>` packages only (e.g.
`fastapicommon.auth`, `fastapicommon.bootstrap`). Submodule paths like
`fastapicommon.auth.dependencies` are implementation detail and may be reorganized in a
MINOR release as long as the re-export from `fastapicommon.auth.__all__` is unchanged.

### Guarantees

- **MAJOR `v1` (once released):** We avoid breaking changes in the public API within
  `v1.x`. When breaking changes are required, we release **`v2.0.0`** and document
  migration in `CHANGELOG.md`.
- **MINOR:** Safe to upgrade with `pip install -U platform-fastapicommon` without code changes,
  unless you opt into new features — except during `0.y.z`, see the pre-1.0 caveat above.
- **PATCH:** Drop-in replacement; upgrade recommended for security fixes.

## Supported releases

| Version | Status | Supported until |
|---------|--------|------------------|
| `0.x` | **Current** | Active; patch releases as needed until `1.0.0` ships |

When `1.0.0` ships, this table will track the same policy as `platform-gincommon`: a new
MAJOR line gets the previous major security-fixes-only support for a period defined by the
platform team (typically 6 months after the new major's release).

## Consume a release

```bash
pip install platform-fastapicommon==0.1.0
# or, for gRPC support:
pip install "platform-fastapicommon[grpc]==0.1.0"
```

The PyPI distribution name is `platform-fastapicommon` — the importable package remains
`fastapicommon` (`fastapicommon` alone was rejected by PyPI as too similar to an existing
unrelated package). See the README's Install section.

| Pin style | Use when |
|-----------|----------|
| `==0.1.0` | Production; exact reproducibility |
| `~=0.1.0` | Production; accept PATCH updates on `0.1.x` only |
| `>=0.1,<0.2` | Accept PATCH updates on the current MINOR |
| unpinned / `>=0.1` | Experiments only; not recommended for prod, especially pre-1.0 |

pip resolves versions from **[PyPI](https://pypi.org/project/platform-fastapicommon/)** — see
[Publishing to PyPI](#publishing-to-pypi) for the one-time account setup, and
[Maintainer release process](#maintainer-release-process) for how a version actually gets
published there.

## Publishing to PyPI

This package publishes to public PyPI using **[Trusted Publishing](https://docs.pypi.org/trusted-publishers/)**
(OIDC) — there is no API token to create, store, or rotate. This is a **one-time setup**;
after it's done, every tagged release publishes automatically via
`.github/workflows/release.yml`'s `pypi-publish` job.

**This is a deliberate choice, confirmed with the team:** the `platform-fastapicommon`
GitHub repository stays **private** (source history, issues, PRs, CI logs), while the
published package on PyPI is intentionally **public**. Trusted Publishing works
identically regardless of repo visibility — PyPI only checks the OIDC token's claims
(owner/repo/workflow/environment), not whether the repo is public. The tradeoff to keep in
mind: a published sdist/wheel contains the actual source, so the *code* becomes publicly
downloadable via PyPI even though the repo itself isn't — combined with the
`Apache-2.0` license in `pyproject.toml`, this is effectively an open-source release of
this specific library, distinct from the private repos it's developed in.

### One-time setup (human steps — cannot be automated or done from this repo)

1. **Create a PyPI account** at [pypi.org/account/register](https://pypi.org/account/register/)
   if the team doesn't already have one for this purpose. PyPI requires 2FA (an
   authenticator app or security key) on every account — set that up during registration.
2. **(Recommended) Create a [TestPyPI](https://test.pypi.org/account/register/) account
   too** — a separate site/account from real PyPI, useful for a dry-run publish before the
   first real release. Not required for the automated flow below.
3. **Register a Trusted Publisher for this project — before it's ever been published:**
   PyPI supports a ["pending publisher"](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   flow that lets you claim the project name and wire up OIDC trust *before* the first
   upload, so there's no chicken-and-egg manual-token step even for the very first release.
   Go to **[pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/)**
   and add:
   | Field | Value |
   |-------|-------|
   | PyPI project name | `platform-fastapicommon` (not `fastapicommon` — rejected as too similar to an existing unrelated package, `fastapi-common`) |
   | Owner | `BCBP-SOLUTIONS-FZC-LLC` |
   | Repository name | `platform-fastapicommon` |
   | Workflow filename | `release.yml` |
   | Environment name | `pypi` |
4. **Create the `pypi` GitHub Environment** (Settings → Environments → New environment,
   name it `pypi`) if you want an approval gate before publishing — optional, but the
   `pypi-publish` job in `release.yml` already targets an `environment: pypi`, so if you
   add required reviewers to that environment, every publish will pause for manual
   approval. If you skip this, the environment is created implicitly with no protection
   rules and publishing proceeds automatically.

   **Deployment branches and tags rule (configured):** restricted to two rules — Tag
   `v*` (matches actual tag pushes, `github.ref` = `refs/tags/vX.Y.Z`) and Branch `main`
   (needed because `workflow_dispatch` runs execute against whichever branch is selected
   in the "Run workflow" UI — typically `main` — not against the `tag` input string, so
   without this second rule the manual-trigger fallback could never satisfy a tag-only
   restriction and `pypi-publish` would block indefinitely on it).

No GitHub Actions secret is created or referenced anywhere in this flow — the `pypi-publish`
job's `permissions: id-token: write` is what lets it request a short-lived OIDC token that
PyPI verifies against the Trusted Publisher config above.

## Maintainer release process

The release workflow (`.github/workflows/release.yml`) automates validation, the PyPI
publish, the Docker image push, and GitHub Release creation. Your job is to prepare the
commit and push the tag.

1. **Merge** all changes for the release to `main`.

2. **Bump the version** in `pyproject.toml` (`[project].version`) and
   `src/fastapicommon/__init__.py` (`__version__`) — both must match the tag.

3. **Update `CHANGELOG.md`:** move `[Unreleased]` entries into a new
   `## [X.Y.Z] - YYYY-MM-DD` section. The release workflow reads this section to
   populate the GitHub Release body — the tag push will fail with a clear error if the
   section is missing.

4. **Run `make ci` locally** to confirm everything is green before tagging:
   ```bash
   make ci   # format-check + lint (ruff + mypy) + test (coverage gate) + build
   ```

5. **Create and push an annotated tag** — this triggers the release workflow
   automatically:
   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

6. **The release workflow** (triggered by the tag) will:
   - Re-run lint, type check, and tests (with the coverage gate) at the exact tagged
     commit, across the supported Python versions.
   - Build the wheel/sdist, verify it with `twine check`, and **publish it to PyPI** via
     Trusted Publishing (requires the one-time setup above to already be done).
   - Build and push the example service's Docker image to GHCR with version tags
     (`v0.1.0`, `v0.1`, `v0` — no automatic `latest`, since it's a demo image).
   - Create a **[GitHub Release](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-fastapicommon/releases)**
     with the CHANGELOG section as release notes, once both the PyPI publish and the
     Docker push succeed.

7. **Notify consumers** (Slack/ADR) with upgrade notes if MINOR or MAJOR.

### Pre-release tags (optional)

| Tag pattern | Meaning |
|-------------|---------|
| `v0.2.0-rc.1` | Release candidate; not for production unless approved |
| `v0.2.0-beta.1` | Early integration testing |

## Compatibility matrix (library ↔ Python)

| fastapicommon | Python | Tested in CI |
|---------------|--------|--------------|
| `0.x` | `>=3.11` | 3.11, 3.12, 3.13, 3.14 (see `.github/workflows/ci.yml` coverage job matrix) |

The `requires-python` floor in `pyproject.toml` and the CI matrix must be updated together
if the minimum supported Python version changes — that is itself a MAJOR-level change once
the package reaches `1.0.0` (it may ship as MINOR pre-1.0, per the caveat above).

## Related files

| File | Purpose |
|------|---------|
| [CHANGELOG.md](./CHANGELOG.md) | User-facing history per version |
| [README.md](./README.md#versioning--releases) | Summary table and quick links |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Development guide and PR checklist |
| [SECURITY.md](./SECURITY.md) | Trust model, supported versions, vulnerability reporting |
| [.github/workflows/release.yml](.github/workflows/release.yml) | Automated release pipeline |
| [pyproject.toml](./pyproject.toml) | Package metadata and minimum Python version |
