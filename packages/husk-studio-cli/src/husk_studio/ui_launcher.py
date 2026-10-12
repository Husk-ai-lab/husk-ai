from __future__ import annotations

import logging
import os
import webbrowser

log = logging.getLogger(__name__)


def open_browser_at(url: str) -> None:
    """Open the default browser at the given URL, unless HUSK_NO_BROWSER is set."""
    if os.environ.get("HUSK_NO_BROWSER"):
        log.info("HUSK_NO_BROWSER set; would have opened %s", url)
        return
    try:
        webbrowser.open(url)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to open browser at %s: %s", url, e)
