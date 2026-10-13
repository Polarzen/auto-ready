"""Source-tree launcher for the packaged simulator verification helper."""

from __future__ import annotations

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from auto_ready.verification import (  # noqa: E402
    RefusingInput,
    SimulatorBoundInput,
    SimulatorOnlyBackend,
    build_input_adapter,
    main,
    same_identity,
)

__all__ = [
    "RefusingInput",
    "SimulatorBoundInput",
    "SimulatorOnlyBackend",
    "build_input_adapter",
    "main",
    "same_identity",
]


if __name__ == "__main__":
    raise SystemExit(main())
