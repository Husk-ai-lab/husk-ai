"""Package version, read from installed metadata.

Hardcoding it here meant it drifted from pyproject.toml: the 0.8.0 release bumped
every manifest but left this at "0.7.0", so `husk-studio --version`, `doctor`, and the
`husk_version` field of every export bundle all reported the previous release.
Reading the installed distribution's metadata makes pyproject.toml the only place a
version is written.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("husk-studio")
except PackageNotFoundError:  # imported from a source tree with no install
    __version__ = "0.0.0+unknown"
