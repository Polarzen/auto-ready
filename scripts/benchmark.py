"""Synthetic local matching benchmark; no desktop capture or input."""

import json
import platform
import statistics
import time
from pathlib import Path

import cv2
import numpy as np

from auto_ready.models import Frame, Geometry, Identity, Region, Settings, Template
from auto_ready.recognition import match


def main():
    cv2.setNumThreads(1)
    template = np.random.default_rng(8).integers(0, 256, (60, 160, 3), dtype=np.uint8)
    image = np.full((1080, 1920, 3), 50, np.uint8)
    image[700:760, 1100:1260] = template
    frame = Frame(
        image,
        Geometry(0, 0, 1920, 1080, 96),
        Identity(1, 1, 1, "synthetic"),
        time.monotonic(),
        "benchmark",
        1,
    )
    reference = Template(template, (1920, 1080), 96)
    output = {
        "python": platform.python_version(),
        "opencv": cv2.__version__,
        "numpy": np.__version__,
        "opencv_threads": cv2.getNumThreads(),
        "source": "synthetic 1920x1080 BGR image, 160x60 template; no game evidence",
        "cases": {},
    }
    for label, region in {
        "full_client": Region(),
        "search_quarter": Region(0.5, 0.5, 0.5, 0.5),
    }.items():
        samples = []
        cpu_start = time.process_time()
        for _ in range(20):
            started = time.perf_counter()
            result = match(frame, reference, region, Settings())
            samples.append((time.perf_counter() - started) * 1000)
            assert result.unique is not None
        output["cases"][label] = {
            "samples": len(samples),
            "median_ms": round(statistics.median(samples), 2),
            "max_ms": round(max(samples), 2),
            "cpu_seconds_total": round(time.process_time() - cpu_start, 3),
            "note": "Matching only; application adds capture, checks, and 150ms wait between ticks",
        }
    directory = Path("artifacts")
    directory.mkdir(exist_ok=True)
    (directory / "benchmark.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
