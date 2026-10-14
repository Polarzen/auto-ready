"""Archive Git-visible source bytes without changing the index or history."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


def create_snapshot(root: Path, destination: Path) -> dict:
    root = root.resolve()
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    ).decode("utf-8").split("\0")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "source-snapshot.zip"
    records = []
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in sorted(set(filter(None, names))):
            path = root / name
            path.resolve().relative_to(root)
            if not path.exists():
                records.append({"path": name, "deleted": True})
                continue
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"Source snapshot requires a regular file: {name}")
            contents = path.read_bytes()
            records.append({"path": name, "bytes": len(contents),
                            "sha256": hashlib.sha256(contents).hexdigest()})
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, contents)
    result = {"schema_version": 1, "archive": archive.name,
              "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": records}
    (destination / "source-snapshot.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    snapshot = create_snapshot(Path(__file__).resolve().parents[1], args.destination)
    print(snapshot["sha256"])
