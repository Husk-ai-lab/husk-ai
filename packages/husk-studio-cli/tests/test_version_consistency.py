"""The version a package reports at runtime must match its pyproject.toml.

The 0.8.0 release bumped all seven manifests but left `__version__ = "0.7.0"`
hardcoded in three `__init__.py` files, so `husk-studio --version`, `husk-studio doctor`,
and the `husk_version` field of every export bundle reported the previous release.
Runtime versions are now read from installed metadata; this test is what fails if
anyone hardcodes one again, or if a manifest is bumped without a re-sync.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# (import name, path to the package's pyproject.toml relative to the repo root)
PACKAGES = [
    ("husk_studio", "packages/husk-studio-cli/pyproject.toml"),
    ("husk_studio_sandbox", "packages/husk-studio-sandbox/pyproject.toml"),
    ("husk_studio_backend", "packages/husk-studio-backend/pyproject.toml"),
]


def _declared(pyproject: str) -> str:
    data = tomllib.loads((REPO_ROOT / pyproject).read_text(encoding="utf-8"))
    version: str = data["project"]["version"]
    return version


@pytest.mark.parametrize(("module_name", "pyproject"), PACKAGES)
def test_runtime_version_matches_pyproject(module_name: str, pyproject: str) -> None:
    import importlib

    module = importlib.import_module(module_name)
    assert module.__version__ == _declared(pyproject), (
        f"{module_name}.__version__ is {module.__version__!r} but "
        f"{pyproject} declares {_declared(pyproject)!r}"
    )


def test_every_workspace_manifest_agrees_on_the_version() -> None:
    """The workspace root, its four members, and both package.json files ship as
    one release, so a partial bump is always a mistake."""
    import json

    versions = {p: _declared(p) for p in ["pyproject.toml", *[m for _, m in PACKAGES]]}
    versions["packages/husk-studio-shared/pyproject.toml"] = _declared(
        "packages/husk-studio-shared/pyproject.toml"
    )
    for manifest in ["package.json", "apps/studio/package.json"]:
        versions[manifest] = json.loads(
            (REPO_ROOT / manifest).read_text(encoding="utf-8")
        )["version"]

    assert len(set(versions.values())) == 1, f"versions disagree: {versions}"
