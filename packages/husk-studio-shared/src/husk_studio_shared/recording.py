"""Recording format versioning.

A Husk *recording* — the SQLite trace DB or an exported fixture — is stamped with
a format version so a reader can refuse to silently misinterpret data written by
an incompatible writer. The SQLite DB carries it in ``PRAGMA user_version``;
exported fixtures carry it in ``meta.json["format_version"]``.

Bump :data:`RECORDING_FORMAT_VERSION` whenever the on-disk shape of
``runs`` / ``spans`` / ``branches`` (or the fixture layout) changes in a way a
reader must account for, and register a migration step in the backend's DB layer
keyed by the version it upgrades *from*.
"""

from __future__ import annotations

#: Current on-disk recording format. v1 = the original runs/spans/snapshots/
#: branches/http_cassettes/cursor_events schema with a ``user_version`` stamp.
#: v2 adds the nullable, indexed ``runs.project_id`` column (project scoping).
RECORDING_FORMAT_VERSION = 2


class RecordingFormatError(RuntimeError):
    """A recording's format version cannot be read by this build of Husk."""


def check_format_version(
    found: int,
    *,
    source: str = "recording",
    allow_migration: bool = True,
) -> int:
    """Validate ``found`` against this build's supported format version.

    Returns the version that should be in effect after loading. A version equal
    to the current one passes through. A *newer* version always fails loudly
    (this build cannot know its shape). An *older* version is accepted only when
    ``allow_migration`` is true — the caller (the backend DB layer) is then
    responsible for running the registered migration steps to bring it current.

    Raises :class:`RecordingFormatError` with an actionable message otherwise.
    """
    if found == RECORDING_FORMAT_VERSION:
        return found
    if found > RECORDING_FORMAT_VERSION:
        raise RecordingFormatError(
            f"{source} was written in recording format v{found}, but this build of "
            f"Husk understands only up to v{RECORDING_FORMAT_VERSION}. "
            f"Upgrade Husk to read this recording."
        )
    if not allow_migration:
        raise RecordingFormatError(
            f"{source} is recording format v{found}; this build is v"
            f"{RECORDING_FORMAT_VERSION} and migration was disabled for this read."
        )
    return found
