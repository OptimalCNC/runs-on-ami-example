#!/usr/bin/env python3
"""Install checksum-verified image build or publishing tools locally."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


def file_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def install(name, spec, destination):
    cache = destination / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / spec["url"].rsplit("/", 1)[-1]
    if not archive.exists() or file_sha(archive) != spec["sha256"]:
        with urllib.request.urlopen(spec["url"], timeout=60) as source, archive.open("wb") as target:
            shutil.copyfileobj(source, target)
    if file_sha(archive) != spec["sha256"]:
        raise ValueError(f"download checksum mismatch: {name}")
    unpacked = destination / name
    unpacked.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            source.extractall(unpacked)
            for member in source.infolist():
                path = unpacked / member.filename
                if path.is_file():
                    path.chmod((member.external_attr >> 16) & 0o777 or 0o644)
    else:
        raise ValueError(f"unsupported tool archive: {archive.name}")
    binary_dir = destination / "bin"
    binary_dir.mkdir(exist_ok=True)
    if name == "aws_cli":
        subprocess.run([str(unpacked / "aws/install"), "--install-dir", str(destination / "aws-installed"),
                        "--bin-dir", str(binary_dir), "--update"], check=True)
    elif name == "qemu_plugin":
        binary = next(unpacked.glob("packer-plugin-qemu_v*"))
        subprocess.run([str(binary_dir / "packer"), "plugins", "install", "--path", str(binary),
                        "github.com/hashicorp/qemu"], check=True,
                       env={**os.environ, "PACKER_PLUGIN_PATH": str(destination / "plugins")})
    else:
        source = unpacked / spec["binary"]
        source.chmod(0o755)
        target = binary_dir / name
        target.unlink(missing_ok=True)
        target.symlink_to(source)
    print(f"Installed {name} {spec['version']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/tools",
                        help="installation directory (default: images/.local/tools)")
    parser.add_argument("--group", choices=("build", "publish"), default="build",
                        help="build installs Packer and QEMU plugin; publish installs AWS CLI (default: build)")
    args = parser.parse_args()
    destination = Path(args.directory).resolve()
    if args.group == "build":
        pins = read_json(ROOT / "build/xenomai-cobalt/inputs.lock.json")["tools"]
        names = ("packer", "qemu_plugin")
    else:
        pins = read_json(ROOT / "publish/tools.lock.json")["tools"]
        names = ("aws_cli",)
    for name in names:
        install(name, pins[name], destination)


if __name__ == "__main__":
    main()
