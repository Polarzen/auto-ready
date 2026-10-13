"""Capture only the application's own QWidget for visual QA. Never enables a round."""

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from auto_ready.app import _SmokeBackend, _SmokeHotkey, _SmokeInput
from auto_ready.config import ConfigStore
from auto_ready.native import enable_dpi_awareness
from auto_ready.ui import MainWindow


def main():
    enable_dpi_awareness()
    app = QApplication(sys.argv[:1])
    with tempfile.TemporaryDirectory(prefix="auto-ready-ui-qa-") as directory:
        window = MainWindow(
            backend=_SmokeBackend(),
            input_adapter=_SmokeInput(),
            store=ConfigStore(Path(directory)),
            hotkey_factory=_SmokeHotkey,
            notifier=lambda: None,
        )
        window.show()

        def capture():
            output = Path("artifacts/main-window.png")
            output.parent.mkdir(exist_ok=True)
            assert window.grab().save(str(output))
            print(str(output.resolve()))
            window.close()

        QTimer.singleShot(500, capture)
        app.exec()


if __name__ == "__main__":
    main()
