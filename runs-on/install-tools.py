#!/usr/bin/env python3
"""Install pinned Linux x86-64 Terraform and AWS CLI tools for RunsOn."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent


def file_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def install(name, spec, destination):
    cache = destination / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / spec["url"].rsplit("/", 1)[-1]
    if not archive.is_file() or file_sha(archive) != spec["sha256"]:
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as target:
            download = Path(target.name)
            try:
                with urllib.request.urlopen(spec["url"], timeout=60) as source:
                    shutil.copyfileobj(source, target)
                target.flush()
                if file_sha(download) != spec["sha256"]:
                    raise ValueError(f"download checksum mismatch: {name}")
                download.replace(archive)
            finally:
                download.unlink(missing_ok=True)

    unpacked = destination / name
    unpacked.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        source.extractall(unpacked)
        for member in source.infolist():
            path = unpacked / member.filename
            if path.is_file():
                path.chmod((member.external_attr >> 16) & 0o777 or 0o644)

    binary_dir = destination / "bin"
    binary_dir.mkdir(exist_ok=True)
    if name == "aws_cli":
        subprocess.run([str(unpacked / "aws/install"), "--install-dir", str(destination / "aws-installed"),
                        "--bin-dir", str(binary_dir), "--update"], check=True)
    else:
        binary = unpacked / spec["binary"]
        binary.chmod(0o755)
        link = binary_dir / name
        link.unlink(missing_ok=True)
        link.symlink_to(binary)
    print(f"Installed {name} {spec['version']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/tools")
    parser.add_argument("--terraform-only", action="store_true", help="install only Terraform for infrastructure checks")
    args = parser.parse_args()
    pins = json.loads((ROOT / "tools.lock.json").read_text())
    for name in ("terraform",) if args.terraform_only else ("terraform", "aws_cli"):
        install(name, pins[name], args.directory.resolve())


if __name__ == "__main__":
    main()
