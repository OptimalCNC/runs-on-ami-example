#!/usr/bin/env python3
"""Install the checksum-verified workflow linter for Linux x86-64."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/tools")
    args = parser.parse_args()
    destination = args.directory.resolve()
    spec = json.loads((ROOT / "tools.lock.json").read_text())["actionlint"]
    cache = destination / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / spec["url"].rsplit("/", 1)[-1]

    def checksum():
        with archive.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    if not archive.exists() or checksum() != spec["sha256"]:
        with urllib.request.urlopen(spec["url"], timeout=60) as source, archive.open("wb") as target:
            shutil.copyfileobj(source, target)
    if checksum() != spec["sha256"]:
        raise ValueError("download checksum mismatch: actionlint")
    unpacked = destination / "actionlint"
    unpacked.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        source.extractall(unpacked, filter="data")
    binary = unpacked / spec["binary"]
    binary.chmod(0o755)
    target = destination / "bin/actionlint"
    target.parent.mkdir(exist_ok=True)
    target.unlink(missing_ok=True)
    target.symlink_to(Path("..") / "actionlint" / spec["binary"])
    print(f"Installed actionlint {spec['version']}")


if __name__ == "__main__":
    main()
