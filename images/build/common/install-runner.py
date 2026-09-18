#!/usr/bin/env python3
"""Install the locked GitHub runner and RunsOn bootstrap on plain Ubuntu."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit


def download_verified(spec, output):
    archive = output / Path(urlsplit(spec["url"]).path).name
    subprocess.run([
        "curl", "--fail", "--location", "--retry", "3", "--proto", "=https",
        "--tlsv1.2", "--output", str(archive), spec["url"],
    ], check=True)
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != spec["sha256"]:
        raise SystemExit(f"runner input checksum mismatch: {archive.name}")
    return archive


def main():
    lock = json.loads(Path(sys.argv[1]).read_text())["runner"]
    output = Path(sys.argv[2]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    home = Path("/home/runner")
    subprocess.run([
        "useradd", "--uid", "1001", "--user-group", "--create-home",
        "--home-dir", str(home), "--shell", "/bin/bash", "runner",
    ], check=True)
    sudoers = Path("/etc/sudoers.d/90-ami-example-runner")
    sudoers.write_text("runner ALL=(ALL) NOPASSWD: ALL\n")
    sudoers.chmod(0o440)

    github_archive = download_verified(lock["github"], output)
    with tarfile.open(github_archive) as archive:
        archive.extractall(home, filter="data")
    subprocess.run([
        "chown", "--recursive", "runner:runner", str(home),
    ], check=True)
    bootstrap_download = download_verified(lock["bootstrap"], output)
    bootstrap = Path(f"/usr/local/bin/runs-on-bootstrap-v{lock['bootstrap']['version']}")
    shutil.copyfile(bootstrap_download, bootstrap)
    bootstrap.chmod(0o755)

if __name__ == "__main__":
    main()
