# Release process

How Bourdon cuts a new version. The whole pipeline is `git tag vX.Y.Z && git push --tags` once the one-time PyPI configuration is in place.

## One-time setup (PyPI Trusted Publishing)

Bourdon uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) so the publish flow never needs a long-lived API token in GitHub Secrets. Configure it once on the PyPI side:

1. Log in to PyPI as a maintainer of the `bourdon` project.
2. Go to [pypi.org/manage/project/bourdon/settings/publishing/](https://pypi.org/manage/project/bourdon/settings/publishing/).
3. Add a "trusted publisher" under **GitHub** with these fields:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `bourdon` |
   | Owner | `getbourdon` |
   | Repository name | `bourdon` |
   | Workflow filename | `release.yml` |
   | Environment name | `pypi` |

4. Save. PyPI is now configured to accept OIDC-signed uploads from `getbourdon/bourdon`'s `release.yml` workflow when it runs under the `pypi` GitHub environment.

5. In the GitHub repo, ensure a `pypi` environment exists:
   - [github.com/getbourdon/bourdon/settings/environments/new](https://github.com/getbourdon/bourdon/settings/environments/new)
   - Name: `pypi`
   - Add deployment protections if desired (required reviewers, branch restrictions) — recommended at least restricting to tags matching `v*`.

After steps 1–5 are done once, every release becomes a one-line git operation.

## Cutting a release

Pre-flight checks:

```bash
# Working tree clean, on main, in sync
git checkout main && git pull origin main
git status -s    # must be empty

# Tests green
.venv/bin/python -m pytest -q

# Decide the version number per SemVer. Open RELEASE_NOTES_v<NEW>.md beside
# pyproject.toml and write the notes BEFORE tagging.
```

Bump + commit + tag + push:

```bash
# 1. Bump pyproject.toml: version = "X.Y.Z"
# 2. Write RELEASE_NOTES_vX.Y.Z.md
# 3. Open a PR (release: vX.Y.Z -- one-line summary)
# 4. After CI passes and the PR merges, on the merge commit:
git checkout main && git pull
git tag -a vX.Y.Z -m "vX.Y.Z -- one-line summary"
git push origin vX.Y.Z
```

The tag push fires `.github/workflows/release.yml` which:

1. Checks out the tag.
2. Builds sdist + wheel via `python -m build`.
3. Asserts the built package version matches the tag (defensive — catches bumps that didn't propagate).
4. Publishes to PyPI via OIDC. No tokens.
5. Creates the GitHub Release from `RELEASE_NOTES_vX.Y.Z.md` (or updates it if one already exists at that tag).
6. Opens a `chore(homebrew): bump Formula to vX.Y.Z` PR with the recomputed `url` + `sha256`. Review and merge it.

`RELEASE_NOTES_vX.Y.Z.md` must be present on the tagged commit — step 5 hard-fails if it's missing. That file lands in the release PR alongside the `pyproject.toml` bump.

## Homebrew formula bump

The release workflow's `homebrew_bump` job opens the Formula PR automatically. Review the diff, confirm `sha256` matches `curl -sL https://github.com/getbourdon/bourdon/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256`, and merge.

See `homebrew/README.md` for **resource-dep refresh recipes** (PyYAML etc.) — those are not automated and need a human bump when an underlying dep advances.

## Retroactive publish (one-off)

If a tag exists but never published to PyPI — e.g. v0.7.0 was tagged before this workflow was added — trigger the workflow manually:

```bash
gh workflow run release.yml -f tag=v0.7.0
```

The workflow's `workflow_dispatch` input checks out that tag and publishes from it. Use this once per existing-but-unpublished tag, then never again.

## Versioning policy

- Strict SemVer (MAJOR.MINOR.PATCH).
- 0.x means anything can change between minor versions.
- v1.0.0 is reserved for either:
  - A second unaffiliated user actively using Bourdon productively, or
  - A commercial wedge (Phase 1.7 team federation w/ ACLs, hosted federation service) shipping as code.
- The BSL 1.1 conversion clock is per-version: vX.Y.Z auto-converts to Apache 2.0 four years from its release date. v0.7.0 = 2030-05-26.

## Troubleshooting

- **"Trusted publishing not configured"** — the PyPI side hasn't been set up. Do the one-time setup above.
- **"Environment `pypi` not found"** — the GitHub repo doesn't have the environment yet. Create it under repo Settings > Environments.
- **Version mismatch error in CI** — the tag and `pyproject.toml`'s `version = "..."` line disagree. Make sure the bump landed on the commit you tagged.
- **OIDC token rejected** — usually the workflow filename or environment name in PyPI's trusted-publisher config doesn't match what the workflow actually uses. They must match exactly.
