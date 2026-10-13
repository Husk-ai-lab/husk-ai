# Releasing the Husk Studio binary

How a commit becomes the archives the desktop shell downloads, and how the same steps run on one
machine without CI. The shell's side of the contract (asset names, layout, start-up) is summarised
in [README.md](README.md#contract-with-the-desktop-shell). The backend repo uses the same shape
(`docs/RELEASING.md` there); `packaging/release.py` is a port of its `scripts/release.py`.

**Status:** `packaging/release.py`, its tests and the whole local loop (build → package → smoke →
extract → 24 binary tests against the archive → report) have been run on macOS arm64. The GitHub
workflow has **not run yet**; the Intel, Windows and Linux jobs have never produced a build here.
Expect a fix iteration or two on those, Windows most likely.

## What runs

| Trigger | Result |
| --- | --- |
| push to `main` touching `packages/`, `apps/studio/`, `packaging/`, `husk-studio.spec`, `pyproject.toml`, `uv.lock`, `pnpm-lock.yaml` | verify, build, package, smoke, binary tests, workflow artefacts + report. **No release** |
| tag `v<version>[-suffix]` | the same, then a **draft** GitHub release (a prerelease when the tag has a suffix) |
| manual (`workflow_dispatch`) | same as main, label `<version>-dev.<sha>` |

There is deliberately no `pull_request` trigger: `ci.yml` owns PRs, and the release is a draft, so a
person reads the report and publishes. The shell's stable channel reads `/releases/latest`, so
publishing is what reaches users.

Jobs: `verify` (version gate, ruff, mypy, pytest, packaging tests, pnpm check/test/build, once on
Linux) → `build` matrix (`mac-arm64`, `mac-x64`, `win-x64`, `linux-x64`; each reuses the built UI,
runs `packaging/build.py` then `release.py package --smoke`, then the binary tests on the
**extracted** archive) → `report` → `release` (tags only, the only job with `contents: write`).

`REQUIRED_TRIPLES` at the top of the workflow decides which targets must build. It starts at
`aarch64-apple-darwin`; the other rows are `continue-on-error` until each has gone green once, then
add its triple there and drop `experimental` on its matrix row.

A target is only an asset when its fragment says `built`: archive layout and CPU checked, smoke
passed, **and** the binary tests passed. The `release` job uploads exactly the archives listed in
`release-report.json`, so an archive from a target that failed late never ships.

## Cutting a release

1. Set the same version in the root `pyproject.toml` and the four `packages/*/pyproject.toml`
   (the root `package.json` follows by convention), add a `## [<version>]` section to
   `CHANGELOG.md`, commit to `main`, wait for the green run. The version gate fails the run and
   prints all five values if they disagree.
2. `git tag v<version>[-suffix] && git push origin v<version>[-suffix]`. The tag must be
   `v<version>` or `v<version>-<suffix>`; anything else fails in `release.py`. An existing tag
   cannot be reused: bump the version.
3. Open the draft release, read the report in its notes (known gaps are listed), publish.
4. On a clean profile let the shell install it and check it reaches `/health`. This step is **not**
   automated and has never been run against a published release.

Version labels must be consistent: the tag, the app version, and the asset name carry the same
string. (The old `v0.9` release has `0.8.1` inside and `0.1.0-alpha.1` on its asset; that is what
the gate now makes impossible.) Pick a version the shell will see as newer than whatever it has
installed; `0.9.0` can compare equal to `0.9`.

## The report

Every run writes `release-report.json` (stable `schema`, uploaded as an artefact and attached to
releases), `RELEASE_REPORT.md` (the job summary and, after the CHANGELOG section, the release notes)
and `SHA256SUMS`.

| Field | Meaning |
| --- | --- |
| `component` (`husk-studio`), `version`, `channel`, `min_version`, `commit`, `tag`, `built_at` | identity; `channel` is `beta` when the version has a suffix |
| `builder` | `github-hosted`, `self-hosted` or `local`, OS/arch, run URL |
| `quality` | pytest and vitest counts, the verify job's result |
| `targets[]` | per triple: status, asset, size, sha256, entries, python/pyinstaller versions, arch check, smoke (`/api/health` version, seconds), `binary_tests`, signing, `verified_on_real_hardware`, problems |
| `assets[]` | `{triple, name, size, sha256, url}` |
| `known_gaps[]` | what the build does **not** prove: unsigned, emulated only, missing targets, start-up slower than the shell's 20 s wait, binary tests not recorded, Windows stop semantics, bundled-interpreter replay, shell install not exercised |

## The script

```bash
python3 packaging/release.py version [--ref-type tag --ref-name v0.8.2]   # version gate + label
python3 packaging/release.py package --version V [--triple T] [--smoke]   # check, archive, run it
python3 packaging/release.py extract --archive A --dest D                 # prints the executable path
python3 packaging/release.py mark-tests --triple T --outcome success      # record binary tests
python3 packaging/release.py report --require T1,T2 [--tag vV]            # report + SHA256SUMS
```

`package --smoke` runs the executable out of the extracted archive with the shell's command line
and environment, then requires `/api/health` to answer `{ok: true}` with the build's version and
`/health` to answer the same body as `application/json`. Windows cannot be built on a Mac
(PyInstaller does not cross-compile): use a Windows machine or the hosted runner.

## Not in place yet

- Code signing and notarisation (macOS Developer ID, Windows Authenticode). No certificates exist;
  every asset is unsigned and the report says so. When they do, signing goes **before** `package`
  in the `build` job, gated on the secrets being present, and `--signed developer-id --notarised`
  records it.
- A signed manifest over `SHA256SUMS`. The shell checks GitHub's own asset `digest`, so the file is
  informational.
- The Studio ↔ Husk token handshake (`HUSK_LAUNCH_SECRET`, `STUDIO_TOKEN_URL`). Out of scope; the
  variables are ignored.
- The Docker image workflow (`docker-publish.yml`) is separate and untouched.
