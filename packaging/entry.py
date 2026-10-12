"""PyInstaller entry point for the Husk Studio binary.

Kept separate from `husk_studio/__main__.py` so the frozen app has a plain script to
launch, and so `multiprocessing.freeze_support()` runs before anything else: without
it a frozen app that ever spawns a worker would re-run the whole CLI in the child.
"""

from __future__ import annotations

import multiprocessing


def run() -> None:
    from husk_studio.cli import main

    main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
