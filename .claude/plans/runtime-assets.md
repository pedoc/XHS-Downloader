# Runtime asset packaging completion

Date: 2026-10-17
Branch: `fix/local-security-hardening`

## PRD

Finish the existing, uncommitted Docker and cx_Freeze runtime-resource allowlists. Ship the files the application needs, in their existing relative locations, without bundling unrelated sample code, advertisements, screenshots, userscripts, translation sources, or translation-maintenance scripts.

Acceptance criteria:

- Docker and cx_Freeze carry the same application-resource manifest.
- Preserve `static/GUI`, the TUI stylesheet, the three platform icon formats, the two compiled gettext catalogs, and `LICENSE`.
- Preserve the existing CLI/Windows GUI executable definitions, package options, and OpenTelemetry distribution metadata.
- Keep unrelated repository files available in the checkout but out of runtime artifacts.
- Verify targeted regressions, the full offline suite, and coverage of changed Python production code. Attempt a real Windows build; distinguish static Docker checks from an actual image build.
- Do not commit, push, delete existing artifacts, change permissions/configuration, or alter unrelated work.

## Architecture

Keep resource selection in the existing `Dockerfile` COPY instructions and `setup.py` `include_files`; do not introduce a new runtime abstraction or dependency. Continue using directory inclusion for the dedicated GUI bundle and explicit file inclusion for other resources.

The application resolves its root from `sys.executable` when frozen, or from the source tree otherwise. Runtime resources must therefore retain their paths beneath the distribution root. Gettext consumes `.mo` files, not `.po`/`.pot` or helper scripts. User state remains separate under `Volume`.

## System Design

Application-resource manifest:

- `static/GUI/**` (bundled HTML, CSS, and JavaScript)
- `static/XHS-Downloader.tcss`
- `static/XHS-Downloader.ico`
- `static/XHS-Downloader.icns`
- `static/XHS-Downloader.png`
- `locale/en_US/LC_MESSAGES/xhs.mo`
- `locale/zh_CN/LC_MESSAGES/xhs.mo`
- `LICENSE`

cx_Freeze additionally retains installed `opentelemetry-api` metadata under `lib/<distribution-directory>`. Docker additionally copies application Python code and the installed dependency tree. These are distinct from the shared application-resource manifest.

## Tech Doc

Use the locked Python 3.12 environment and the declared `dev` and `build` dependency groups. cx_Freeze 8.7.2 accepts `(source, relative_destination)` include-file pairs and recursively copies directory sources; explicit file pairs preserve the intended layout. Official reference: https://cx-freeze.readthedocs.io/en/stable/setup_script.html . GitHub repository/code searches for existing `include_files`/`LC_MESSAGES` patterns were also performed; no external implementation was copied.

Tests capture the actual `setup.py` options with a fake cx_Freeze module on Windows/Linux/macOS, expand the final-stage Docker COPY instructions, check GUI local references and compiled catalogs, preserve metadata/options, and assert exclusion of unrelated resources. If a genuine missing behavior is found, add and demonstrate a failing regression before changing implementation.

Keep validation output outside the checkout at:

`C:/Users/pedoc/AppData/Local/Temp/xhs-runtime-validation.8MuJKb`

The copy-mode retry uses `copy-build/env`, `copy-build/metadata`, and a previously nonexistent `copy-build/freeze` output directory. The offline environment preparation lacked the locked `pywin32==312` wheel; retrying normal downloads retains `--locked` and does not upgrade dependencies. The initial build's generated checkout egg-info directory was inspected and moved into `initial-build-egg-info` under this validation directory, rather than deleted. Later builds explicitly direct egg metadata outside the checkout.

This avoids overwriting the pre-existing repository `.coverage` file or user application data.

Environment limitation: Docker CLI is present, but the daemon is not running (`docker_engine` named pipe is absent). Do not claim a container build/run passed without a successful real invocation.

## Task List

- [x] Inspect existing Dockerfile/setup.py changes and regression tests.
- [x] Check for a saved task plan; no relevant prior plan was found.
- [x] Trace runtime resource consumers and frozen/source root resolution.
- [x] Verify the targeted regression suite: 33 passed.
- [x] Install the declared locked build dependencies without removing existing environment extras.
- [x] Complete full offline regression: 932 passed in 182.61s. Whole-source branch coverage is approximately 58%, below the global 80% target; do not claim that target passed. The initial `--cov=setup` invocation did not capture `runpy` execution, so a separate absolute-file-filtered coverage run is in progress for the changed packaging script.
- [x] Measure `setup.py` using an absolute-file coverage filter: 33 tests passed in 42.59s, with 100% statement and branch coverage (14 statements, 4 branches).
- [ ] Complete real Windows cx_Freeze build and inspect its resource manifest. Initial attempt failed before resource copying: `[WinError 2]` for the delayed-linked `XHS-Downloader.exe` target. Diagnosis confirmed an existing `UV_LINK_MODE=symlink` environment interaction: cx_Freeze 8.7.2 defers creation of the executable link but immediately stats that target. This precedes include_files processing and is not caused by the resource allowlist. Rebuild using the same lock in a new copy-mode temporary environment and a new output directory; do not modify the existing environment, dependency versions, permissions, or security software. Do not retry the original command unchanged or reuse an existing output directory (cx_Freeze may delete it).
- [ ] Smoke-test the built artifact in its isolated output directory if the build succeeds.
- [ ] Fix any verified in-scope failures using a failing regression first.
- [x] Run Ruff checks for `setup.py` and `tests/test_runtime_assets.py`: all checks passed. `uv lock --check --offline` and `git diff --check` also passed; the local master branch is an ancestor of HEAD, with no merge conflicts.
- [x] Complete general/Python/security review after checks pass: all three reviewers found no blocking issue in the scoped changes; TDD review also found no current resource omission. These are static/configuration reviews, not claims that native builds succeeded.
- [ ] Record final verification results and any remaining environment blockers.
