"""Cross-platform notification sound helper."""

from __future__ import annotations

import sys


def notify() -> None:
    """Play the operating system's notification sound without blocking the caller."""
    if sys.platform == "win32":
        try:
            import winsound

            winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
            return
        except (ImportError, RuntimeError):
            pass

    # Qt provides a native beep when available; the terminal bell keeps this
    # module useful in headless and non-Qt environments too.
    try:
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:
            QApplication.beep()
            return
    except ImportError:
        pass
    sys.stdout.write("\a")
    sys.stdout.flush()
