# Contributing to Husk

Thanks for your interest in improving **Husk — the visual debugger for AI agents**.
This guide covers the local setup, how to run the checks, and what we expect in a
pull request.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — manages the Python toolchain and the
  workspace. It installs the pinned Python 3.11 for you on first sync (see
  `.python-version`).
- **Node.js 20+** with **pnpm** via
  [corepack](https://nodejs.org/api/corepack.html) — only needed if you touch the
  Studio UI (`apps/studio`).
- **git**.

## Setup

```bash
git clone https://github.com/husk-ai-lab/husk-ai.git
cd husk-ai
uv sync --all-packages --group examples   # Python workspace + example deps
corepack pnpm install                      # only if working on the Studio UI
```

Boot the backend and Studio:

```bash
uv run husk-studio start   # opens http://localhost:7654
```

## Checks

Run these before opening a pull request — CI runs the same tests and lint:

```bash
uv run pytest -q        # tests
uv run ruff check .     # lint
```

If you changed the Studio UI, make sure it builds:

```bash
corepack pnpm --filter studio build
```

## Commit style

We follow [Conventional Commits](https://www.conventionalcommits.org/) —
`type(scope): summary`. A few examples from the history:

```
feat(studio-backend): auto-build studio on first run
fix(cursor-bridge): document from-source install
docs(get-started): align substep numbering
```

Common types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`. Keep each PR
focused on one logical change, and describe the *why*, not just the *what*.

## Licensing of contributions

Husk is source-available under the **Business Source License 1.1 (BUSL 1.1)**.
By submitting a contribution you agree that it is licensed under the same terms
as the project (see the [`LICENSE`](LICENSE) file), and that you have the right
to contribute it.

## Questions

Open a [GitHub issue](https://github.com/husk-ai-lab/husk-ai/issues) for bugs
and feature requests, or email [info@huskai.dev](mailto:info@huskai.dev). For
security issues, follow [SECURITY.md](SECURITY.md) instead of filing a public
issue.
