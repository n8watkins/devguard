# Publishing devguard

How a release reaches each install channel, and the one-time setup each needs.
Everything ships from one tag: push `vX.Y.Z` on `main` and `.github/workflows/release.yml` does the rest.

| Channel | Built by | Turned on by |
| --- | --- | --- |
| GitHub Release (standalone builds for Linux and macOS, x64 and arm64; the wheel and sdist; `SHA256SUMS`; build provenance) | `build`, `python` and `publish` jobs | Always on |
| `install.sh` | Reads the latest release | Always on |
| PyPI (`agent-devguard`) | `pypi` job | Repository variable `PYPI_PUBLISH=true` and a trusted publisher on PyPI |
| npm (`agent-devguard` plus `agent-devguard-<os>-<arch>`) | `npm` job, from the release's builds | Repository variable `NPM_PUBLISH=true` |
| Homebrew (`n8watkins/tap/devguard`) | `homebrew` job, writes `Formula/devguard.rb` in `n8watkins/homebrew-tap` | Repository variable `HOMEBREW_TAP=true` and secret `HOMEBREW_TAP_DEPLOY_KEY` (both set) |

PyPI, npm and Homebrew run only after the GitHub Release is published, and use its files.
A failed one never undoes the release; fix the cause and re-run the job (each skips versions already published).

## Releasing

1. Set the version in `pyproject.toml` and `src/devguard/__init__.py`.
2. Merge that to `main`, then tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The workflow checks the tag is on `main` and both versions match, runs the tests and the end-to-end walk-through, builds on four runners (Linux builds on Ubuntu 22.04, so they run on glibc 2.35 and newer), runs the walk-through against each Linux build, publishes the release, then each channel that is on.

## One-time setup: PyPI

1. On pypi.org (account with two-factor authentication), add a pending trusted publisher for the project `agent-devguard`: owner `n8watkins`, repository `devguard`, workflow `release.yml`, environment `pypi`.
2. In the repository settings, create the environment `pypi` and set the repository variable `PYPI_PUBLISH` to `true`.

No token is stored anywhere; the workflow publishes with short-lived OIDC credentials.

## One-time setup: npm

1. An npm account with two-factor authentication for publishing.
2. First publish, with a token (npm only lets you set up trusted publishing for packages that exist): a granular access token that can publish new packages, as the repository secret `NPM_TOKEN`, and the repository variable `NPM_PUBLISH=true`.
3. After the first release reaches npm: for each of the five packages (`agent-devguard`, `agent-devguard-linux-x64`, `agent-devguard-linux-arm64`, `agent-devguard-darwin-x64`, `agent-devguard-darwin-arm64`), add a trusted publisher (GitHub Actions, `n8watkins/devguard`, `release.yml`) and require two-factor authentication with tokens disallowed.
4. Delete the `NPM_TOKEN` secret and revoke the token.

## Homebrew (set up)

The tap is `n8watkins/homebrew-tap`, shared by pass-env and devguard.
The release workflow pushes the formula with an SSH deploy key that can write to that one repository and nothing else: its public half is a deploy key on the tap ("release workflows: pass-env, devguard"), its private half the `HOMEBREW_TAP_DEPLOY_KEY` secret here and in the other repo.
To rotate it, generate a new key pair, replace the deploy key on the tap, and set the secret again in both repos (`gh secret set HOMEBREW_TAP_DEPLOY_KEY < key`), then delete the private key file.
The repository variable `HOMEBREW_TAP=true` turns the job on; set it to anything else to pause it.

## Checking changes to packaging

`.github/workflows/packaging.yml` runs on any pull request that changes the code or the packaging.
It builds devguard like a release, runs the walk-through against the build, runs the build on Ubuntu 22.04 with no Python installed, installs it from a throwaway npm registry with npm, pnpm, yarn, bun and npx, runs the installer, and installs and tests the Homebrew formula on macOS.
Locally: `packaging/build-binary.sh dist`, then `packaging/smoke-npm.sh dist` (after writing `dist/SHA256SUMS`).
